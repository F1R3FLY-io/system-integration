"""Polling helpers — wait for conditions with timeout.

Core polling logic lives in ``f1r3fly.polling``. This module provides
Node-aware wrappers and test-specific helpers (e.g. waiting for node
startup logs).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Dict, Optional, TypeVar

from f1r3fly.polling import (
    DeployError,
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

T = TypeVar("T")

# Always give a predicate this many tries, however slow each one is.
MIN_POLL_ATTEMPTS = 3


def poll_until(
    predicate: Callable[[], Optional[T]],
    timeout: float,
    interval: float = 3.0,
    description: str = "",
    min_attempts: int = MIN_POLL_ATTEMPTS,
) -> T:
    """Poll ``predicate`` until it returns truthy, with a floor on attempts.

    Wraps ``f1r3fly.polling.poll_until``, which checks its deadline only at the
    top of the loop and then sleeps unconditionally. One probe slower than the
    whole budget therefore yields exactly one try:

        TimeoutError: deploy ... inclusion on ...validator1:
        timed out after 10s (1 attempts)

    That is a poll loop that never polled. It is not specific to any one
    timeout — any budget on the same order as a single probe's latency
    degrades the same way, so a caller cannot tell from the message whether the
    condition was really absent or merely never re-checked. Under
    ``-n 16 --dist=loadgroup`` on one runner, a gRPC round trip past ten
    seconds is unremarkable, which is how `deploy_inclusion` (10s) reached it
    first. Diagnosed by claude-session-9f68c6fa from f1r3node-rust PR #178,
    run 30672935310.

    The floor makes "timed out" mean the condition did not hold across
    ``min_attempts`` independent observations. Worst-case wall time becomes
    roughly ``min_attempts * (probe_latency + interval)`` rather than
    ``timeout``: deliberate, since a run that is slow enough to hit the floor
    is one where failing fast costs a re-run and a diagnosis.

    ``time.monotonic`` rather than ``time.time`` so a clock adjustment mid-poll
    cannot end the loop early.
    """
    deadline = time.monotonic() + timeout
    last_err: Optional[Exception] = None
    attempts = 0

    while True:
        attempts += 1
        try:
            result = predicate()
            if result:
                return result
        except Exception as e:  # noqa: BLE001 — same contract as the client
            last_err = e

        if attempts >= min_attempts and time.monotonic() >= deadline:
            break
        time.sleep(interval)

    err_detail = f" (last error: {last_err})" if last_err else ""
    raise TimeoutError(
        f"{description or 'poll_until'}: timed out after {timeout}s "
        f"({attempts} attempts){err_detail}"
    )


# The node's vabn-expiration rejection, verbatim shape (captured from soak
# preflight 31919610258):
#   "Deploy validAfterBlockNumber 157 has expired at block 207 with deploy
#    lifespan 50."
# Retrying a submission is safe ONLY on this rejection — it is the node's
# guarantee that the deploy was NOT accepted, so a resend cannot
# double-submit. The regex is the retry GATE, not just a height extractor:
# anything that does not match this exact shape must be recorded as a
# failure, never retried. unit-tests/test_vabn_expiration_matcher.py pins
# the shape against the captured node wording, so silent drift on either
# side fails a sub-second test instead of degrading retry behavior.
VABN_EXPIRED_PATTERN = re.compile(
    r"Deploy validAfterBlockNumber \d+ has expired at block (\d+) with deploy lifespan \d+"
)


def parse_vabn_expiration(message: str) -> Optional[int]:
    """The node's CURRENT height from a vabn-expiration rejection, else None.

    A non-None return simultaneously authorizes a retry (the node rejected,
    nothing was accepted) and supplies the freshest possible height for the
    replacement validAfterBlockNumber.
    """
    match = VABN_EXPIRED_PATTERN.search(message)
    return int(match.group(1)) if match else None


# Owned by pyf1r3fly (f1r3fly/polling.py); the raise site there is the only
# writer of this wording. unit-tests/test_empty_par_translation.py fails if
# the upstream wording drifts, so the translation below cannot silently die.
_EMPTY_PAR_MARKER = "empty par list"


class EmptyParListError(DeployError):
    """The deploy finalized but its deployId channel read back empty.

    pyf1r3fly reports this only via the message text of a generic
    ``DeployError``, which callers were matching with a substring check —
    brittle across upstream rewording (PR #88 review, major finding).
    ``deploy_and_read`` translates that one message into this type at the
    wrapper boundary so callers can catch it structurally.
    """


__all__ = [
    "poll_until",
    "wait_for_node_running",
    "wait_for_deploy_included",
    "wait_for_finalized",
    "wait_for_deploy_finalized",
    "wait_for_lfb_with_ft",
    "deploy_and_read",
    "deploy_with_fallback",
    "propose_until_included",
    "wait_for_block_visible",
    "wait_for_block_visible_on_all_nodes",
    "wait_for_block_justified",
    "lfb_number",
    "wait_for_lfb_at_least",
    "wait_for_lfb_converged",
    "wait_for_node_quiet",
    "get_blocks_if_enough",
    "try_find_deploy",
    "all_blocks_visible",
    "DeployError",
    "EmptyParListError",
]

_RUNNING_MARKER = "Making a transition to Running state"


def wait_for_node_running(
    get_logs: Callable[[], str],
    is_running: Callable[[], bool],
    node_name: str,
    timeout: float,
    interval: float = 2.0,
    status_url: str = "",
) -> None:
    """Wait for a node to reach Running state.

    Primary signal: ``/api/status`` ``isReady == true`` (Rust nodes).
    Fallback signal: log marker — used when ``status_url`` is unset OR
    when the status response is missing ``isReady`` (older nodes, whose
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
                f"Node {node_name} exited before reaching Running state. Last logs:\n{tail}"
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
                        # doesn't expose the readiness flag.
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
        f"Node {node_name} did not reach Running state within {timeout}s. Last logs:\n{tail}"
    )


def wait_for_deploy_included(node, deploy_id: str, timeout: float):
    """Poll ``find_deploy`` until the deploy is included in a block.

    Node-aware wrapper around ``f1r3fly.polling.wait_for_deploy_included``.
    Returns the ``LightBlockInfo`` for the block containing the deploy.
    """
    return _client_wait_for_deploy_included(node._external_client(), deploy_id, timeout)


def wait_for_finalized(node, block_number: int, timeout: float) -> None:
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
    timeout: float,
    interval: float = 2.0,
) -> int:
    """Poll until ``node``'s LFB.blockNumber >= ``height``. Returns the
    observed LFB number on success. Causal: exits the moment the
    condition fires, not after a fixed wait.
    """
    return poll_until(
        predicate=lambda: lfb_number(node) if lfb_number(node) >= height else None,
        timeout=timeout,
        interval=interval,
        description=f"{node.name} LFB >= #{height}",
    )


def wait_for_lfb_converged(
    nodes,
    timeout: float,
    min_height: Optional[int] = None,
    max_spread: int = 3,
    interval: float = 3.0,
    description: str = "",
) -> Dict[str, int]:
    """Poll until every node's LFB is within ``max_spread`` of the highest —
    and, when ``min_height`` is given, at or above it — with both conditions
    satisfied by the **same** sample. Returns that ``{name: lfb}`` mapping.

    Use this instead of a per-node ``wait_for_lfb_at_least`` loop followed by
    a spread assertion. That shape waits on a *lower bound*, which exits the
    instant each node crosses it, so nothing bounds how far a fast node runs
    ahead while the remaining nodes are still being polled — the loop's own
    serialization manufactures the spread that is then asserted against, and
    the result measures scheduling luck rather than consensus.

    The deeper reason is that a lower bound **cannot distinguish "still
    catching up" from "permanently diverged"** — a shard that never converges
    reads identically to one mid-convergence. Requiring both conditions in one
    sample makes the timeout the signal for genuine non-convergence, which is
    the property these tests exist to assert. For the same reason, a spread
    failure here should be fixed by investigating convergence, not by raising
    ``max_spread``.

    Raises ``AssertionError`` (not ``TimeoutError``) on timeout, carrying the
    last polled sample plus its min/max/spread — without them a reviewer
    cannot tell a slow shard from a diverged one, nor which of the two
    conditions actually failed.

    Known property: the sample is N sequential RPCs, so on a live chain the
    measured spread includes the time to walk ``nodes``. If this proves too
    tight on loaded runners, require the predicate to hold on two consecutive
    passes rather than loosening ``max_spread``.
    """
    if not nodes:
        raise ValueError("wait_for_lfb_converged requires at least one node")

    what = description or (
        f"LFB spread <= {max_spread}"
        + (f" with all nodes >= #{min_height}" if min_height is not None else "")
    )

    last_seen: Dict[str, int] = {}

    def _sample() -> Optional[Dict[str, int]]:
        lfbs = {n.name: lfb_number(n) for n in nodes}
        last_seen.update(lfbs)
        if min_height is not None and min(lfbs.values()) < min_height:
            return None
        if max(lfbs.values()) - min(lfbs.values()) > max_spread:
            return None
        return lfbs

    try:
        return poll_until(
            predicate=_sample,
            timeout=timeout,
            interval=interval,
            description=what,
        )
    except TimeoutError:
        # Report the last sample actually polled, not a fresh read. Re-reading
        # here would describe the shard *after* the deadline — and if it caught
        # up in the interim the message would look converged while the test
        # fails, which is precisely the confusion this error exists to prevent.
        if not last_seen:
            raise AssertionError(f"{what}: no LFB sample was taken within {timeout}s")
        low, high = min(last_seen.values()), max(last_seen.values())
        raise AssertionError(
            f"{what}: not satisfied within {timeout}s — last sample min #{low}, "
            f"max #{high}, spread {high - low}: {last_seen}"
        )


def wait_for_node_quiet(node, timeout: float, interval: float = 1.0) -> None:
    """Block until ``node``'s HTTP API stops responding.

    Used after ``node.pause()`` to confirm SIGSTOP has actually landed
    on the rnode process — empirically that delivery is not instant
    (observed >10s gap between pause() returning and the process
    actually halting under load). Until the process is suspended its
    block-creation thread continues to produce blocks that influence
    finalization.

    Detection: ``/api/status`` raises (timeout / connection refused)
    for one consecutive successful poll. The probe uses a short HTTP
    timeout (2s) so this returns promptly once the process is stopped.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            node.api_get("/status", timeout=2)
        except Exception:
            return
        time.sleep(interval)
    raise TimeoutError(
        f"{node.name} still responding to /api/status after {timeout}s — "
        f"pause() may not have taken effect"
    )


def wait_for_deploy_finalized(
    node,
    deploy_id: str,
    timeout: float,
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
    timeout: float,
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
    inclusion_timeout: float,
    finalization_timeout: float,
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
        EmptyParListError: If the deploy finalized but the deployId
            channel read back empty.
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

    try:
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
    except EmptyParListError:
        raise
    except DeployError as err:
        if _EMPTY_PAR_MARKER in str(err):
            raise EmptyParListError(str(err)) from err
        raise


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


def propose_until_included(node, deploy_id: str, timeout: float, interval: float = 0.5) -> str:
    """Drive ``node`` to propose until ``deploy_id`` lands in a block; return its hash.

    For building deterministic history on a shard with ``heartbeat=False``, where
    nothing proposes unless a test asks it to. Each attempt re-checks inclusion
    before and after proposing, because the deploy may have been picked up by a
    propose already in flight.

    ``No new deploys`` and ``another propose is in progress`` are expected while
    racing an in-flight propose and are retried; any other propose failure is a
    real error and propagates.
    """
    from f1r3fly.client import F1r3flyClientException

    retryable = ("No new deploys", "another propose is in progress")

    def _attempt():
        try:
            return node.find_deploy(deploy_id).blockHash
        except F1r3flyClientException:
            pass

        try:
            node.propose()
        except F1r3flyClientException as exc:
            if not any(text in str(exc) for text in retryable):
                raise

        try:
            return node.find_deploy(deploy_id).blockHash
        except F1r3flyClientException:
            return None

    return poll_until(
        _attempt,
        timeout=timeout,
        interval=interval,
        description=f"deploy {deploy_id[:24]} becomes available and is proposed",
    )


def wait_for_block_visible(node, block_hash: str, timeout: float):
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


def wait_for_block_visible_on_all_nodes(nodes, block_hash: str, timeout: float):
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


def wait_for_block_justified(node, validator_pubkey: str, block_hash: str, timeout: float):
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
