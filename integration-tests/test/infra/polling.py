"""Polling helpers — wait for conditions with timeout.

Core polling logic lives in ``f1r3fly.polling``. This module provides
Node-aware wrappers and test-specific helpers (e.g. waiting for node
startup logs).
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable, Dict, Optional, Pattern, Union

from f1r3fly.polling import (
    DeployError,
    poll_until,
)
from f1r3fly.polling import (
    deploy_and_read as _client_deploy_and_read,
)
from f1r3fly.polling import (
    deploy_with_fallback as _client_deploy_with_fallback,
)
from f1r3fly.polling import (
    wait_for_deploy_finalized as _client_wait_for_deploy_finalized,
)
from f1r3fly.polling import (
    wait_for_deploy_included as _client_wait_for_deploy_included,
)
from f1r3fly.polling import (
    wait_for_finalized as _client_wait_for_finalized,
)

logger = logging.getLogger(__name__)

# Re-export poll_until directly — it's generic, no wrapping needed
__all__ = [
    "poll_until",
    "wait_for_node_running",
    "wait_for_deploy_included",
    "wait_for_finalized",
    "wait_for_deploy_finalized",
    "wait_for_lfb_with_ft",
    "deploy_and_read",
    "deploy_with_fallback",
    "wait_for_block_visible",
    "wait_for_block_visible_on_all_nodes",
    "wait_for_log_match",
    "lfb_number",
    "wait_for_lfb_at_least",
    "wait_for_lfb_stable",
    "DeployError",
]

_RUNNING_MARKER = "Making a transition to Running state"


def wait_for_node_running(
    get_logs: Callable[[], str],
    is_running: Callable[[], bool],
    node_name: str,
    timeout: int,
    interval: float = 2.0,
    status_url: str = "",
) -> None:
    """Wait for a node to reach Running state.

    Primary signal: ``/api/status`` ``isReady == true`` (Rust nodes).
    Fallback signal: log marker — used when ``status_url`` is unset OR
    when the status response is missing ``isReady`` (Scala nodes, whose
    ``/api/status`` schema predates that field).

    Also checks if the container/pod has exited — if so, raises
    immediately with the last log lines instead of waiting the full
    timeout.
    """
    import requests

    deadline = time.time() + timeout

    while time.time() < deadline:
        if not is_running():
            logs = get_logs()
            tail = "\n".join(logs.splitlines()[-20:])
            raise RuntimeError(
                f"Node {node_name} exited before reaching Running state. " f"Last logs:\n{tail}"
            )

        use_log_fallback = not status_url
        if status_url:
            try:
                resp = requests.get(status_url, timeout=3)
                if resp.status_code == 200:
                    status = resp.json()
                    if "isReady" in status:
                        if status["isReady"] is True:
                            logger.info("Node %s is ready (isReady=true)", node_name)
                            return
                    else:
                        # Status returned but no isReady field — this node
                        # doesn't expose the readiness flag (e.g. Scala).
                        # Use the log marker for the remainder of this poll.
                        use_log_fallback = True
            except (requests.ConnectionError, requests.Timeout, Exception):
                pass  # HTTP not up yet, keep waiting

        if use_log_fallback and _RUNNING_MARKER in get_logs():
            logger.info("Node %s reached Running state (log marker)", node_name)
            return

        time.sleep(interval)

    logs = get_logs()
    tail = "\n".join(logs.splitlines()[-20:])
    raise TimeoutError(
        f"Node {node_name} did not reach Running state within {timeout}s. " f"Last logs:\n{tail}"
    )


def wait_for_deploy_included(node, deploy_id: str, timeout: int):
    """Poll ``find_deploy`` until the deploy is included in a block.

    Node-aware wrapper around ``f1r3fly.polling.wait_for_deploy_included``.
    Returns the ``LightBlockInfo`` for the block containing the deploy.
    """
    return _client_wait_for_deploy_included(node._external_client(), deploy_id, timeout)


def wait_for_finalized(node, block_number: int, timeout: int) -> None:
    """Poll until the last finalized block reaches or exceeds ``block_number``.

    Node-aware wrapper around ``f1r3fly.polling.wait_for_finalized``.
    """
    _client_wait_for_finalized(node._external_client(), block_number, timeout)


def lfb_number(node) -> int:
    """Return ``node``'s LFB block number, or ``0`` if no LFB exists yet.

    Useful during shard bring-up before the first finalization, where
    ``last_finalized_block()`` raises. Tests that previously kept their
    own ``_get_lfb_number`` wrapper should import this instead.
    """
    try:
        return node.last_finalized_block().blockInfo.blockNumber
    except Exception:
        return 0


def wait_for_lfb_at_least(
    node,
    height: int,
    timeout: int,
    interval: float = 2.0,
) -> int:
    """Poll until ``node``'s LFB.blockNumber >= ``height``. Returns the
    observed LFB number on success. Causal: exits the moment the
    condition fires, not after a fixed wait.
    """
    return poll_until(
        predicate=lambda: (lfb_number(node) if lfb_number(node) >= height else None),
        timeout=timeout,
        interval=interval,
        description=f"{node.name} LFB >= #{height}",
    )


def wait_for_lfb_stable(
    node,
    timeout: int,
    interval: float = 5.0,
) -> int:
    """Poll ``node``'s LFB until two consecutive reads agree and return
    the stable height. Causal detector for "finalization pipeline has
    drained" — exits as soon as the LFB has not changed for one full
    ``interval``.

    Use after a perturbation (validator pause/kill, network partition)
    where existing in-flight finalization should be allowed to settle
    before asserting on the steady-state behavior.
    """
    deadline = time.time() + timeout
    last = lfb_number(node)
    while time.time() < deadline:
        time.sleep(interval)
        current = lfb_number(node)
        if current == last:
            return current
        last = current
    raise TimeoutError(
        f"{node.name} LFB did not stabilize within {timeout}s " f"(last observed: #{last})"
    )


def wait_for_deploy_finalized(
    node,
    deploy_id: str,
    timeout: int,
    interval: float = 3.0,
):
    """Poll ``deploy_finalization_status`` until the deploy reaches Finalized.

    Node-aware wrapper around ``f1r3fly.polling.wait_for_deploy_finalized``.
    Use this for deploy tracking instead of block-hash finalization — it
    reports the deploy's actual canonical-state inclusion, correctly
    handling the case where a block finalizes while the deploy's effects
    were rejected by merge and later re-included.

    Returns the ``DeployFinalizationStatusInfo`` on success.
    Raises ``DeployError`` on terminal Failed/Expired, ``TimeoutError``
    if Pending past ``timeout``.
    """
    return _client_wait_for_deploy_finalized(node._external_client(), deploy_id, timeout, interval)


def wait_for_lfb_with_ft(
    node,
    target_number: int,
    ftt: float,
    timeout: int,
    interval: float = 2.0,
):
    """Poll until ``node``'s LFB satisfies BOTH invariants:

    - ``blockNumber >= target_number``
    - ``faultTolerance >= ftt``

    Single ``last_finalized_block()`` call per iteration — no torn reads
    between the two fields. Returns the final ``BlockInfo`` once both
    hold. Use when a test must verify cross-node propagation of the
    cached per-block FT field, not just the LFB pointer.

    The Rust node updates the LFB pointer (via local clique oracle) and
    the per-block ``faultTolerance`` field (via
    ``propagate_ft_to_finalized_blocks``) on separate paths. They can be
    out of sync — especially on observer/readonly nodes. Asserting on
    the cached field is a stronger invariant than ``isFinalized`` alone.
    """

    def _check():
        lfb_info = node.last_finalized_block().blockInfo
        if lfb_info.blockNumber >= target_number and float(lfb_info.faultTolerance) >= ftt:
            return lfb_info
        return None

    return poll_until(
        predicate=_check,
        timeout=timeout,
        interval=interval,
        description=f"{node.name} LFB >= #{target_number} AND FT >= {ftt}",
    )


def deploy_and_read(
    node,
    term: str,
    private_key,
    inclusion_timeout: int,
    finalization_timeout: int,
    *,
    rho_file: str = None,
    substitutions: Optional[Dict[str, str]] = None,
    phlo_limit: int = 100_000,
    phlo_price: int = 1,
    shard_id: str = "root",
) -> tuple:
    """Deploy code (or .rho file), wait for finalization, read deployId data.

    Node-aware wrapper around ``f1r3fly.polling.deploy_and_read`` that
    adds .rho file resolution and string substitution.

    Args:
        node: Node instance.
        term: Rholang code (ignored if rho_file is set).
        private_key: PrivateKey for signing.
        inclusion_timeout: Seconds to wait for block inclusion.
        finalization_timeout: Seconds to wait for finalization.
        rho_file: If set, read code from this .rho file path.
        substitutions: String replacements to apply to the code.
        phlo_limit: Maximum phlo to spend.
        phlo_price: Phlo price per unit.
        shard_id: Target shard identifier.

    Returns:
        Tuple of (par_list, block_hash, block_number) where par_list is
        the list of Par values from the deployId channel.

    Raises:
        TimeoutError: If inclusion or finalization times out.
        DeployError: If the deploy is errored or returns no data.
    """
    import os

    if rho_file:
        resolved = rho_file
        if not os.path.isabs(rho_file) and not os.path.exists(rho_file):
            integration_tests_dir = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            resolved = os.path.join(integration_tests_dir, rho_file)
        with open(resolved) as f:
            term = f.read()

    if substitutions:
        for key, value in substitutions.items():
            term = term.replace(key, value)

    return _client_deploy_and_read(
        client=node._external_client(),
        term=term,
        private_key=private_key,
        inclusion_timeout=inclusion_timeout,
        finalization_timeout=finalization_timeout,
        phlo_limit=phlo_limit,
        phlo_price=phlo_price,
        shard_id=shard_id,
    )


def deploy_with_fallback(
    nodes,
    term: str,
    private_key,
    timeout_per_node: int,
    phlo_limit: int = 100_000,
    phlo_price: int = 1,
    valid_after_block_no: int = None,
    shard_id: str = "root",
    rho_file: str = None,
):
    """Submit a deploy, falling back to other validators if inclusion times out.

    Node-aware wrapper around ``f1r3fly.polling.deploy_with_fallback``
    that adds .rho file resolution.

    Returns ``(deploy_id, block_info)`` on success.
    Raises ``TimeoutError`` if no validator includes the deploy.
    """
    import os

    if rho_file:
        resolved = rho_file
        if not os.path.isabs(rho_file) and not os.path.exists(rho_file):
            integration_tests_dir = os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            )
            resolved = os.path.join(integration_tests_dir, rho_file)
        with open(resolved) as f:
            term = f.read()

    clients = [n._external_client() for n in nodes]

    return _client_deploy_with_fallback(
        clients=clients,
        term=term,
        private_key=private_key,
        timeout_per_client=timeout_per_node,
        phlo_limit=phlo_limit,
        phlo_price=phlo_price,
        valid_after_block_no=valid_after_block_no,
        shard_id=shard_id,
    )


def wait_for_block_visible(node, block_hash: str, timeout: int):
    """Poll ``get_block`` until the block is visible on the node."""

    def _check():
        try:
            node.get_block(block_hash)
            return True
        except Exception:
            return None

    poll_until(
        predicate=_check,
        timeout=timeout,
        interval=3.0,
        description=f"block {block_hash[:16]}... visible on {node.name}",
    )


def wait_for_block_visible_on_all_nodes(nodes, block_hash: str, timeout: int):
    """Poll until every node can return the block via ``get_block``.

    Synchronization barrier for assertions on freshly-proposed blocks.
    There is a brief window between gossip-receipt and block-store add
    on a peer where ``getBlock`` returns ``"received but not added yet"``
    (see ``casper/src/rust/api/block_api.rs:1288``). Tests that propose
    a block and immediately query it on every peer race that window.

    Use this before ``assert_block_finalized_on_all_nodes`` (or any other
    cross-node block assertion on a recently-proposed block).
    """
    pending = {n.name for n in nodes}
    deadline = time.time() + timeout
    while time.time() < deadline:
        still_pending = set()
        for node in nodes:
            if node.name not in pending:
                continue
            try:
                node.get_block(block_hash)
            except Exception:
                still_pending.add(node.name)
        pending = still_pending
        if not pending:
            return
        time.sleep(2.0)
    raise TimeoutError(
        f"Block {block_hash[:16]}... not visible on {len(pending)} node(s) "
        f"after {timeout}s: {sorted(pending)}"
    )


def wait_for_block_justified(node, validator_pubkey: str, block_hash: str, timeout: int):
    """Poll until a validator's block appears in the node's justifications.

    Stronger than ``wait_for_block_visible`` — checks that the block has
    been processed into the DAG and appears in the latest block's
    justification set. Required for tests that depend on synchrony
    constraint satisfaction, where mere storage visibility is insufficient.

    Args:
        node: Node to check.
        validator_pubkey: Public key hex of the validator whose block we expect.
        block_hash: Block hash to look for in justifications.
        timeout: Maximum seconds to wait.
    """

    def _check():
        try:
            blocks = node.get_blocks(1)
            if not blocks:
                return None
            for j in blocks[0].justifications:
                if j.validator == validator_pubkey and j.latestBlockHash == block_hash:
                    return True
            return None
        except Exception:
            return None

    poll_until(
        predicate=_check,
        timeout=timeout,
        interval=2.0,
        description=f"block {block_hash[:16]}... justified by {validator_pubkey[:16]}... on {node.name}",
    )


def wait_for_log_match(
    node,
    pattern: Union[str, Pattern[str]],
    timeout: int,
    interval: float = 1.0,
):
    """Poll the node's log for a regex match. Compiles ``pattern`` if str.

    Used by the slashing E2E tests to wait for the receiving validator's
    ``"Recording invalid block ... for <Variant>."`` log line before
    triggering the slash proposal. Returns the ``re.Match`` on success;
    raises ``TimeoutError`` if the pattern doesn't appear within
    ``timeout`` seconds.
    """
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern

    def _check():
        match = compiled.search(node.logs())
        return match if match else None

    return poll_until(
        predicate=_check,
        timeout=timeout,
        interval=interval,
        description=f"log on {node.name} matches {compiled.pattern!r}",
    )


# ── Polling predicates ─────────────────────────────────────────────


def get_blocks_if_enough(node, min_count: int):
    """Return blocks if the node has at least ``min_count``, else None."""
    blocks = node.get_blocks(50)
    return blocks if len(blocks) >= min_count else None


def try_find_deploy(node, deploy_id: str):
    """Return deploy block info if found, else None (no exception)."""
    try:
        return node.find_deploy(deploy_id)
    except Exception:
        return None


def all_blocks_visible(nodes, block_hashes: list) -> bool:
    """Return True if every block hash is visible on every node."""
    for bh in block_hashes:
        for node in nodes:
            try:
                node.get_block(bh)
            except Exception:
                return False
    return True
