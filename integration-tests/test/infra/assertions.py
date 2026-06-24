"""Assertion helpers for integration tests.

Par extraction and deploy checking: re-exported from pyf1r3fly.
Shard assertions: test-specific helpers for multi-node agreement checks.
"""
from __future__ import annotations

import time
from typing import Optional

# Re-export deploy checking from pyf1r3fly
from f1r3fly.deploy import (
    DeployError,
    check_deploy_errored,
    check_deploy_succeeded,
)

from .types import NodeRole

# Re-export Par extraction from pyf1r3fly

# ── Test assertion wrappers ────────────────────────────────────────────
#
# These wrap the pyf1r3fly check_* functions with pytest-style assert
# messages. Tests can use either style depending on preference.


def assert_deploy_succeeded(block_info, deploy_id: str) -> None:
    """Assert the deploy is in the block, not errored, and has cost > 0."""
    try:
        check_deploy_succeeded(block_info, deploy_id)
    except DeployError as e:
        raise AssertionError(str(e)) from None


def assert_deploy_errored(
    block_info,
    deploy_id: str,
    error_contains: Optional[str] = None,
) -> None:
    """Assert the deploy is in the block and marked as errored."""
    try:
        check_deploy_errored(block_info, deploy_id, error_contains)
    except DeployError as e:
        raise AssertionError(str(e)) from None


# ── Shard assertions (test-specific, needs multiple nodes) ─────────────


def _get_block_with_retry(node, block_hash: str, timeout: int):
    """Retrieve a block from one node, polling through transient lookup races.

    A peer can have the block hash in its reception buffer but not yet
    added to its DAG: ``get_block`` then raises "received but not added
    yet" / "Failure to find block". The race is the same one documented
    in :py:func:`wait_for_block_visible_on_all_nodes`; this helper
    absorbs it locally so cross-node block assertions don't need a
    separate sync-barrier call ahead of them.

    Disagreement on a block that *all* nodes return is still the actual
    property the caller asserts — it surfaces immediately. Only the
    retrieval race is retried.

    ``timeout=0`` (the default) gives one-shot behaviour: a transient
    "not added yet" surfaces immediately as the property failure shape.
    Callers that know the race is in scope opt into polling by passing a
    value from the ``timeouts`` fixture (e.g. ``timeouts.finalization``).
    """
    deadline = time.monotonic() + timeout
    last_err: Optional[Exception] = None
    while True:
        try:
            return node.get_block(block_hash)
        except Exception as e:
            last_err = e
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"{node.name} could not retrieve block "
                    f"{block_hash[:16]} within {timeout}s: {last_err}"
                ) from last_err
            time.sleep(1.0)


def assert_all_nodes_agree_on_block(nodes, block_hash: str, timeout: int = 0) -> None:
    """Assert every node can retrieve the block and has the same post-state.

    Retrieval may be polled per-node through transient "not added yet"
    races via :py:func:`_get_block_with_retry` — ``timeout=0`` (default)
    is one-shot; callers in scope of the race opt into polling by passing
    a value from the ``timeouts`` fixture (typically
    ``timeouts.finalization``). Once every node returns the block,
    post-states are compared — disagreement IS the property failure this
    asserts and surfaces without further retry.
    """
    post_states = {}
    for node in nodes:
        block = _get_block_with_retry(node, block_hash, timeout)
        post_states[node.name] = block.blockInfo.postStateHash
    unique = set(post_states.values())
    assert len(unique) == 1, (
        f"Nodes disagree on post-state for block {block_hash[:16]}. " f"States: {post_states}"
    )


def assert_all_nodes_agree_on_lfb(nodes, timeout: int = 0) -> str:
    """Assert all nodes report the same LFB hash. Returns the common hash.

    Default (timeout=0) is one-shot: a snapshot disagreement raises
    immediately. Callers in scope of normal propagation lag — where one
    validator's finalizer runs a beat ahead of the others — opt into
    polling by passing a value from the ``timeouts`` fixture (typically
    ``timeouts.finalization``). On opt-in, polls until all nodes return
    the same LFB hash or the budget elapses. A persistent fork still
    surfaces as a loud AssertionError with the per-node state, just
    after the timeout instead of immediately.
    """
    deadline = time.monotonic() + timeout
    while True:
        lfb_info: dict = {}
        for node in nodes:
            lfb = node.last_finalized_block().blockInfo
            lfb_info[node.name] = (lfb.blockHash, lfb.blockNumber)
        hashes = {h for h, _ in lfb_info.values()}
        if len(hashes) == 1:
            return next(iter(hashes))
        if time.monotonic() >= deadline:
            raise AssertionError(f"Nodes disagree on LFB after {timeout}s: {lfb_info}")
        time.sleep(2.0)


def assert_contracts_consistent_across_nodes(
    readonly_node,
    contract_queries,
    block_hash: str = "",
) -> dict[str, list]:
    """Query each contract on readonly via exploratory deploy, return results.

    Args:
        readonly_node: Node with exploratory deploy support.
        contract_queries: Iterable of either ``(name, uri, method)`` for
            3-arg bridge-style contracts or ``(name, uri, method, param)``
            where ``param`` is a Rholang expression string or ``None``
            for the 2-arg ``(@method, ret)`` pattern.
        block_hash: Block hash to query against. Empty for latest.

    Returns:
        Dict mapping contract name to Par results list.

    Raises:
        AssertionError: If any query returns no results.
    """
    results = {}
    for entry in contract_queries:
        if len(entry) == 3:
            name, uri, method = entry
            param = "Nil"
        elif len(entry) == 4:
            name, uri, method, param = entry
        else:
            raise ValueError(f"contract_queries entry must be 3- or 4-tuple, got {entry!r}")
        pars = readonly_node.registry_query(uri, method, param=param, block_hash=block_hash)
        assert pars, (
            f"Contract {name} query {method} returned no results "
            f"on {readonly_node.name} at block {block_hash[:16]}"
        )
        results[name] = pars
    return results


def assert_bonds_map_consistent_across_nodes(
    nodes,
    block_hash: str,
    expected_bonds: dict,
    timeout: int = 0,
) -> None:
    """Assert every node's view of ``block_hash`` carries the same bonds map.

    The bonds map is part of the block payload — every node that accepted
    the block at validation time must compute the same map. A divergence
    means at least one node's local replay produced a different result
    (the failure mode of the original ``InvalidBondsCache`` bug, where a
    bond block validated on the proposer but the bonds map computed
    differently elsewhere).

    Retrieval may be polled per-node through transient "not added yet"
    races via :py:func:`_get_block_with_retry` — ``timeout=0`` (default)
    is one-shot; callers in scope of the race opt into polling by passing
    a value from the ``timeouts`` fixture. Map divergence — the property
    this asserts — surfaces immediately without further retry once every
    node returns the block.

    Args:
        nodes: Iterable of Node wrappers to query.
        block_hash: Finalized block hash to check.
        expected_bonds: ``{public_hex: stake}`` the bonds map must match
            exactly on every node. Stake values must match — not just
            key membership.
        timeout: Per-node retrieval budget in seconds. ``0`` = one-shot.

    Raises:
        AssertionError: with a per-node diff if any node disagrees, or
        with a "could not retrieve block" message if any node fails to
        return the block within ``timeout``.
    """
    per_node: dict = {}
    for node in nodes:
        block = _get_block_with_retry(node, block_hash, timeout)
        per_node[node.name] = {b.validator: b.stake for b in block.blockInfo.bonds}
    mismatches = {name: bonds for name, bonds in per_node.items() if bonds != expected_bonds}
    assert not mismatches, (
        f"Bonds map divergence at block {block_hash[:16]}... "
        f"expected {expected_bonds}; mismatches: {mismatches}"
    )


def assert_block_finalized_on_all_nodes(
    nodes,
    block_hash: str,
    timeout: int = 0,
    interval: float = 2.0,
) -> None:
    """Assert every node has the block AND reports `isFinalized=True`.

    Stricter than `wait_for_block_visible`, which passes for any block in
    the metadata store regardless of validity. A peer that flagged the
    block invalid still returns it from `get_block` but never finalizes it.

    Catches the case where a peer accepted the block at the protocol level
    (it's in their store) but rejected it at validation time (e.g.
    `Invalid(InvalidBondsCache)`). The proposer's block is finalized
    locally; if any peer's view is not, that's the bug.

    By default does NOT poll (timeout=0) — caller is responsible for waiting
    for finalization first via `wait_for_finalized` or `poll_until`. Set
    ``timeout > 0`` to opt into polling for the per-block ``isFinalized``
    field, which can lag the LFB advance by a few seconds in high-contention
    multi-validator scenarios.
    """
    from f1r3fly.client import F1r3flyClientException

    deadline = time.time() + timeout
    not_finalized: dict = {}
    while True:
        not_finalized = {}
        for node in nodes:
            try:
                block = node.get_block(block_hash)
                if not block.blockInfo.isFinalized:
                    not_finalized[node.name] = {
                        "block_number": block.blockInfo.blockNumber,
                        "fault_tolerance": float(block.blockInfo.faultTolerance),
                    }
            except F1r3flyClientException as e:
                # Transient race during high-contention multi-validator scenarios:
                # a node has received the block hash via the propagation layer but
                # hasn't fully indexed it for state-query gRPC calls yet. Treat as
                # "not finalized yet on this node" and let the polling loop retry.
                # Without this catch, a transient indexing race fails the assertion
                # with a confusing error message that masks the real consensus state.
                msg = str(e)
                if "received but not added yet" in msg or "not added yet" in msg:
                    not_finalized[node.name] = {
                        "block_number": None,
                        "fault_tolerance": None,
                        "transient_error": "received but not added yet",
                    }
                else:
                    raise
        if not not_finalized or time.time() >= deadline:
            break
        time.sleep(interval)
    assert not not_finalized, (
        f"Block {block_hash[:16]}... is not finalized on "
        f"{len(not_finalized)} node(s) after {timeout}s: {not_finalized}"
    )


def assert_all_deploys_finalized_on_all_nodes(
    nodes,
    deploy_ids: list[str],
    timeout: int,
    *,
    label: str = "deploys",
) -> None:
    """Assert every deploy in ``deploy_ids`` reaches Finalized on EVERY node.

    Deploy-centric, not block-centric. Polls each node's
    ``deploy_finalization_status`` (via ``wait_for_deploy_finalized``), which
    follows a deploy across multi-parent **re-homing**: a deploy whose first
    block loses a merge and is re-included into a finalized descendant is
    correctly reported Finalized. The older pattern -- resolve ``find_deploy``
    once, then poll that fixed block hash for ``isFinalized`` -- FALSELY fails
    that case, because the losing-fork block never finalizes even though the
    deploy does (its work moved to a different, finalized block).

    Zero tolerance for genuinely dropped work: a deploy that never finalizes on
    some node within ``timeout`` (Pending -> TimeoutError) or terminally fails
    (Failed/Expired -> DeployError) is collected and reported with a diagnostic.

    Use this for bg-load / deploy-orphaning regression checks instead of
    locating the block with ``find_deploy`` and asserting that block's hash
    finalizes.
    """
    # Local import keeps the assertions -> polling edge lazy (no import cycle).
    from .polling import wait_for_deploy_finalized

    if not deploy_ids:
        return
    not_finalized: list[tuple[str, str, str]] = []  # (sig[:16], node.name, reason)
    for sig in deploy_ids:
        for node in nodes:
            try:
                wait_for_deploy_finalized(node, sig, timeout)
            except Exception as exc:  # noqa: BLE001
                # TimeoutError (Pending past timeout) and DeployError (terminal
                # Failed/Expired) both mean "did not finalize here". Caught broad
                # because the deploy-status DeployError is f1r3fly.polling's, not
                # the f1r3fly.deploy class re-exported at module scope.
                not_finalized.append((sig[:16], node.name, type(exc).__name__))
                break  # one un-finalizing node is enough; move to the next deploy
    assert not not_finalized, (
        f"[{label}] {len(not_finalized)} of {len(deploy_ids)} deploys did not "
        f"finalize on all nodes (deploy-status, re-homing-aware). "
        f"first(3)={not_finalized[:3]}"
    )


# ── Cross-node channel-value agreement (FS node-identity) ──────────────


def _values_agree(values) -> bool:
    """True iff every value equals the first. Works for dicts (which are
    unhashable and so cannot go through a ``set``)."""
    items = list(values)
    return all(v == items[0] for v in items[1:])


def _channel_reader(channel: str):
    return lambda node, bh: node.read_channel(channel, bh)


def _balance_reader(vault_addr: str):
    return lambda node, bh: node.get_vault().get_balance(vault_addr, bh)


def _pick_reader(nodes):
    """The exploratory-capable node. Channel peeks and vault-balance reads are
    served ONLY by a read-only RNode — validators reject them ("Exploratory
    deploy can only be executed on read-only RNode") — so a finalized VALUE is
    read there. Falls back to ``nodes[0]`` if no read-only node is present.
    """
    for node in nodes:
        if getattr(node, "role", None) == NodeRole.READONLY:
            return node
    return nodes[0]


def assert_value_consistent_across_nodes(
    nodes, read_fn, block_hash: str, what: str, *, block_timeout: float = 5.0
):
    """Read a finalized value (via ``read_fn``) on the read-only node at
    ``block_hash`` and assert every node agrees on that block's POST-STATE HASH.

    The value is read once on the read-only observer because validators do not
    serve exploratory reads. All-node FS node-identity is asserted on the
    block's post-state hash instead — every node can serve a block query, and a
    shared post-state hash subsumes channel/balance agreement (same post-state
    ⇒ same value). A divergent post-state is the #71 cascade shape that
    ``assert_block_finalized_on_all_nodes`` alone can miss when the divergent
    cell does not itself gate finalization.
    """
    assert_all_nodes_agree_on_block(nodes, block_hash, timeout=int(block_timeout))
    return read_fn(_pick_reader(nodes), block_hash)


def assert_channel_consistent_across_nodes(nodes, channel: str, block_hash: str):
    """All-node FS node-identity for a named channel at ``block_hash``."""
    return assert_value_consistent_across_nodes(
        nodes, _channel_reader(channel), block_hash, f'@"{channel}"'
    )


def assert_balance_consistent_across_nodes(nodes, vault_addr: str, block_hash: str):
    """All-node FS node-identity for a vault balance at ``block_hash``."""
    return assert_value_consistent_across_nodes(
        nodes, _balance_reader(vault_addr), block_hash, f"balance({vault_addr[:12]})"
    )


def await_value_converges_on_all_nodes(
    nodes,
    read_fn,
    expected,
    timeout: float,
    label: str,
    *,
    what: str = "value",
    non_regression: Optional[str] = None,  # "map" | "up" | "down" | None
    upper_bound: Optional[int] = None,
    lower_bound: Optional[int] = None,
    volatile=frozenset(),
    interval: float = 1.0,
):
    """Poll the FINALIZED value until it equals ``expected``, enforcing:

      * **node-identity** — whenever all nodes have finalized to the same cut,
        every node must agree on that block's POST-STATE HASH (the value itself
        is read on the read-only node, since validators reject exploratory
        reads; post-state agreement subsumes value agreement). Convergence is
        only accepted at such an aligned cut, so a converged result is
        all-nodes-agreed by construction;
      * **non-regression** (``non_regression``):
          - ``"map"``  — an add-only key (not in ``volatile``) once finalized
            never vanishes or changes (the dropped-entry mode);
          - ``"up"``   — an integer never decreases (a finalized credit/count
            not undone);
          - ``"down"`` — an integer never increases (a finalized debit /
            guarded-decrement not reverted);
      * **anti-double-apply bounds** — ``upper_bound`` (value never exceeds —
        catches a double-applied credit/count) and ``lower_bound`` (value never
        below — catches a double-applied decrement, the item-1 ``FS=-20`` mode)
        fail immediately rather than as an opaque convergence timeout.

    Between finalization rounds, nodes' LFBs can momentarily differ; those
    iterations still advance convergence/non-regression against the read-only
    node's finalized read but defer the cross-node identity check until aligned.
    """
    reader = _pick_reader(nodes)
    deadline = time.time() + timeout
    water = None  # high/low-water: dict for "map", int for "up"/"down"
    last = None
    while time.time() < deadline:
        try:
            lfbs = {n.name: n.last_finalized_block().blockInfo.blockHash for n in nodes}
        except Exception:  # noqa: BLE001 — transient query race during contention
            time.sleep(interval)
            continue
        aligned = _values_agree(lfbs.values())
        ref_block = lfbs[reader.name]
        if aligned:
            # All-node FS node-identity at the shared finalized cut. Retrieval
            # races ("not added yet") are absorbed; a post-state divergence is
            # the bug and is surfaced.
            try:
                assert_all_nodes_agree_on_block(nodes, ref_block, timeout=int(interval) + 1)
            except AssertionError as e:
                if "disagree on post-state" in str(e):
                    raise
                time.sleep(interval)
                continue
        try:
            cur = read_fn(reader, ref_block)
        except Exception:  # noqa: BLE001 — transient read during contention
            time.sleep(interval)
            continue
        if cur is None:
            time.sleep(interval)
            continue

        # Non-regression vs the running water-mark.
        if non_regression == "map" and isinstance(water, dict):
            for k, v in water.items():
                if k in volatile:
                    continue
                assert cur.get(k) == v, (
                    f"[{label}] finalized-state REGRESSION: {k}={v} was finalized then "
                    f"vanished/changed (now {cur.get(k)!r}) at LFB {ref_block[:16]}; "
                    f"full finalized map {cur}"
                )
        elif non_regression == "up" and water is not None:
            assert (
                cur >= water
            ), f"[{label}] finalized-value REGRESSION: dropped from {water} to {cur}"
        elif non_regression == "down" and water is not None:
            assert cur <= water, f"[{label}] finalized-value REGRESSION: rose from {water} to {cur}"

        # Anti-double-apply bounds.
        if upper_bound is not None:
            assert cur <= upper_bound, (
                f"[{label}] OVER-apply: finalized value {cur} exceeds upper bound "
                f"{upper_bound} — a write was double-applied"
            )
        if lower_bound is not None:
            assert cur >= lower_bound, (
                f"[{label}] OVER-apply: finalized value {cur} below lower bound "
                f"{lower_bound} — a decrement was double-applied (item-1 mode)"
            )

        # Advance the water-mark.
        if non_regression == "map":
            water = dict(water) if isinstance(water, dict) else {}
            for k, v in cur.items():
                if k not in volatile:
                    water[k] = v
        elif non_regression == "up":
            water = cur if water is None else max(water, cur)
        elif non_regression == "down":
            water = cur if water is None else min(water, cur)

        last = cur
        if aligned and cur == expected:
            return cur
        time.sleep(interval)

    raise AssertionError(
        f"[{label}] finalized {what} did not converge to {expected} on all nodes "
        f"within {timeout:.0f}s; last={last}, water={water}"
    )


def await_channel_converges_on_all_nodes(nodes, channel: str, expected, timeout, label, **kw):
    """All-node finalized convergence for a named channel. See
    :func:`await_value_converges_on_all_nodes`."""
    return await_value_converges_on_all_nodes(
        nodes,
        _channel_reader(channel),
        expected,
        timeout,
        label,
        what=f'@"{channel}"',
        **kw,
    )


def await_balance_converges_on_all_nodes(nodes, vault_addr: str, expected, timeout, label, **kw):
    """All-node finalized convergence for a vault balance. See
    :func:`await_value_converges_on_all_nodes`."""
    return await_value_converges_on_all_nodes(
        nodes,
        _balance_reader(vault_addr),
        expected,
        timeout,
        label,
        what=f"balance({vault_addr[:12]})",
        **kw,
    )
