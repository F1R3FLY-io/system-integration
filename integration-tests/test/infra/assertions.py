"""Assertion helpers for integration tests.

Par extraction and deploy checking: re-exported from pyf1r3fly.
Shard assertions: test-specific helpers for multi-node agreement checks.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

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

    # Classify HOW each deploy was lost. Refund-quarantine, retry-gate
    # starvation and merge starvation are structurally different failures that
    # all arrive here as DeployError; without this the cause is only
    # recoverable by hand-scanning shard logs. One streaming pass per affected
    # node, on the failure path only.
    causes: dict = {}
    if not_finalized:
        from .log_events import classify_deploy_losses

        try:
            causes = classify_deploy_losses(nodes, {sig16 for sig16, _, _ in not_finalized})
        except Exception:  # noqa: BLE001 - diagnostics must never mask the failure
            causes = {}
    detail = "; ".join(
        f"{sig16}@{node_name} {exc_name} -> {causes.get(sig16, 'unclassified')}"
        for sig16, node_name, exc_name in not_finalized[:3]
    )
    assert not not_finalized, (
        f"[{label}] {len(not_finalized)} of {len(deploy_ids)} deploys did not "
        f"finalize on all nodes (deploy-status, re-homing-aware). "
        f"first(3)={detail}"
    )


def collect_forensics(nodes, *, channel: Optional[str] = None, label: str = "") -> str:
    """Failure-time facts that are cheap over the node API, gathered in one
    place so a red test explains itself instead of sending someone to the
    shard logs.

    Answers, without any node-side debug stream:

    - is the finalized chain forked or merely lagging (per-node LFB number and
      hash, and whether the hashes agree);
    - is it advancing at all (two samples a second apart);
    - what does each node actually hold on the channel under test, read at
      that node's own finalized state;
    - did any node emit a forbidden-log signature (fork, merge incoherence,
      propose BugError) during the run.

    Diagnostics must never mask the failure that triggered them: every probe
    is individually guarded and reports its own error inline.
    """
    lines: list[str] = [f"--- forensics [{label}] ---"]

    def probe(what: str, fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - a failed probe is data, not a crash
            lines.append(f"  {what}: UNAVAILABLE ({type(exc).__name__}: {exc})")
            return None

    first: dict = {}
    for node in nodes:
        info = probe(f"lfb({node.name})", lambda n=node: n.last_finalized_block().blockInfo)
        if info is not None:
            first[node.name] = (info.blockNumber, info.blockHash[:10])
    unreachable = [n.name for n in nodes if n.name not in first]
    if unreachable:
        # A node that cannot answer is an INFRASTRUCTURE failure, not a
        # consensus one, and it silently poisons every all-node assertion:
        # a dead container reads as "this deploy has no verdict" forever.
        # Name it first and loudly.
        lines.append(f"  *** NODE(S) NOT ANSWERING: {unreachable} — treat as infra, not consensus")
    if first:
        hashes = {h for _, h in first.values()}
        lines.append(
            f"  LFB per node ({len(first)}/{len(nodes)} answering): {first}"
            f"  -> {'AGREED' if len(hashes) == 1 else 'FORKED/LAGGING'}"
        )

    # Sample over several block intervals. The heartbeat proposes on a
    # multi-second cadence, so a sub-interval window shows zero movement on a
    # perfectly healthy chain — a false FROZEN verdict sends the next reader
    # hunting a wedge that does not exist.
    time.sleep(_ADVANCE_SAMPLE_SECONDS)
    second: dict = {}
    for node in nodes:
        info = probe(f"lfb2({node.name})", lambda n=node: n.last_finalized_block().blockInfo)
        if info is not None:
            second[node.name] = info.blockNumber
    if first and second:
        advanced = {n for n, num in second.items() if num > first.get(n, (-1, ""))[0]}
        lines.append(
            f"  chain advancing on {len(advanced)}/{len(second)} nodes"
            f" over {_ADVANCE_SAMPLE_SECONDS:.0f}s"
            + ("" if advanced else "  <-- FROZEN")
        )

    if channel:
        values: dict = {}
        for node in nodes:
            values[node.name] = probe(
                f"read_channel({node.name})", lambda n=node: n.read_channel(channel)
            )
        lines.append(f'  finalized @"{channel}" per node: {values}')

    from .log_events import FORBIDDEN_PATTERNS

    hits: dict = {}
    for node in nodes:
        text = probe(f"logs({node.name})", lambda n=node: n.logs(tail=20000)) or ""
        node_hits = {key: len(pat.findall(text)) for key, pat in FORBIDDEN_PATTERNS.items()}
        node_hits = {k: v for k, v in node_hits.items() if v}
        if node_hits:
            hits[node.name] = node_hits
    lines.append(f"  forbidden-log signatures: {hits or 'none in scanned tail'}")

    return "\n".join(lines)


def assert_chain_advances(nodes, since_number: int, timeout: int, *, label: str) -> int:
    """Assert the finalized chain moved past ``since_number`` and all nodes
    agree on where it is. Returns the new LFB number.

    A shard that stops finalizing looks identical, from a deploy's point of
    view, to a shard that is merely slow — both leave deploys Pending. This
    separates them: a frozen LFB is a shard-liveness failure regardless of
    what any individual deploy did, and it names the freeze directly instead
    of surfacing minutes later as a deploy-status timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        numbers = {}
        for node in nodes:
            lfb = node.last_finalized_block().blockInfo
            numbers[node.name] = lfb.blockNumber
        current = min(numbers.values())
        if current > since_number:
            assert_all_nodes_agree_on_lfb(nodes, timeout=timeout)
            return current
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"[{label}] finalized chain did not advance past #{since_number} in "
                f"{timeout}s — the shard stopped finalizing. per-node LFB={numbers}"
            )
        time.sleep(2.0)


def lowest_lfb_number(nodes) -> int:
    """The slowest node's LFB number — the baseline for ``assert_chain_advances``."""
    return min(node.last_finalized_block().blockInfo.blockNumber for node in nodes)


# ── Verdict-aware deploy resolution ────────────────────────────────────
#
# Separates shard INTEGRITY from per-deploy FAIRNESS. A deploy the shard
# terminally judged Expired is a decision the shard made and reported: the
# window closed and the effect never landed. That is recorded, not failed.
# A deploy with NO verdict inside the budget, a Failed one, or one whose
# verdict disagrees across nodes means the shard did not decide or decided
# inconsistently — those stay hard failures.

_TERMINAL_STATE_RE = re.compile(r"terminal state (Finalized|Failed|Expired)")

# Window for the chain-advance probe. Must exceed the heartbeat's block cadence
# (casper.heartbeat.check-interval) or a healthy chain reads as frozen.
_ADVANCE_SAMPLE_SECONDS = 12.0


@dataclass
class DeployVerdicts:
    """Per-deploy terminal verdicts across all nodes.

    ``finalized`` is the subset callers must use to build expected state:
    an expired deploy's effect must NOT appear, and asserting the full
    submitted set as the expectation would fail for a shard that behaved
    correctly under contention.
    """

    finalized: List[str] = field(default_factory=list)
    expired: Dict[str, str] = field(default_factory=dict)  # sig16 -> detail
    per_sig_states: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def finalized_set(self) -> set:
        return set(self.finalized)

    def summary(self) -> str:
        return (
            f"{len(self.finalized)} finalized, {len(self.expired)} expired"
            + (f" ({', '.join(sorted(self.expired))})" if self.expired else "")
        )


def _terminal_state_from_error(exc: Exception) -> Optional[str]:
    """Extract the terminal state named in a pyf1r3fly ``DeployError``.

    The message is built by ``f1r3fly.polling.wait_for_deploy_finalized`` as
    "... reached terminal state <State> (rejection_count=N)". An
    unrecognised message yields None, which the caller treats as an
    integrity failure rather than guessing.
    """
    match = _TERMINAL_STATE_RE.search(str(exc))
    return match.group(1) if match else None


def resolve_deploy_verdicts(
    nodes,
    deploy_ids: list[str],
    timeout: int,
    *,
    label: str = "deploys",
) -> DeployVerdicts:
    """Resolve every deploy to a terminal verdict on EVERY node.

    Hard-fails (AssertionError) when the shard failed to decide or decided
    inconsistently:

    - no verdict within ``timeout`` (still Pending) — the shard never
      concluded; this is the frozen-chain / propose-wedge signature;
    - terminal ``Failed``;
    - a verdict that differs across nodes (e.g. Finalized on one node,
      Expired on another) — a forked read surface;
    - an unparseable terminal state.

    Records, without failing, deploys that every node judged ``Expired``.
    """
    from .polling import wait_for_deploy_finalized

    verdicts = DeployVerdicts()
    if not deploy_ids:
        return verdicts

    # Establish which nodes can answer BEFORE polling deploys. A dead container
    # cannot report a verdict, so polling it spends the full per-sig budget and
    # then reports "no terminal verdict" — indistinguishable from a shard that
    # failed to decide. That misread a node the kernel OOM-killed as a
    # consensus failure and cost ~29 minutes per occurrence. Fail immediately
    # and name the node instead.
    dead = []
    for node in nodes:
        try:
            node.last_finalized_block()
        except Exception as exc:  # noqa: BLE001
            dead.append(f"{node.name} ({type(exc).__name__})")
    if dead:
        raise AssertionError(
            f"[{label}] INFRASTRUCTURE: {len(dead)} of {len(nodes)} nodes are not answering "
            f"({', '.join(dead)}) — deploy verdicts cannot be resolved on a shard with a dead "
            f"node; this is not a consensus failure.\n" + collect_forensics(nodes, label=label)
        )

    integrity: list[tuple[str, str, str]] = []  # (sig16, node, reason)
    for sig in deploy_ids:
        sig16 = sig[:16]
        states: Dict[str, str] = {}
        for node in nodes:
            try:
                wait_for_deploy_finalized(node, sig, timeout)
                states[node.name] = "Finalized"
            except Exception as exc:  # noqa: BLE001 - DeployError is not re-exported here
                if isinstance(exc, TimeoutError):
                    states[node.name] = "NoVerdict"
                    continue
                state = _terminal_state_from_error(exc)
                states[node.name] = state or f"Unknown({type(exc).__name__})"
        verdicts.per_sig_states[sig16] = states

        distinct = set(states.values())
        if distinct == {"Finalized"}:
            verdicts.finalized.append(sig)
        elif distinct == {"Expired"}:
            verdicts.expired[sig16] = "Expired on all nodes"
        elif "NoVerdict" in distinct:
            integrity.append((sig16, _nodes_in_state(states, "NoVerdict"), "no terminal verdict"))
        elif "Failed" in distinct:
            integrity.append((sig16, _nodes_in_state(states, "Failed"), "terminal Failed"))
        else:
            integrity.append(
                (sig16, "all", f"verdict differs across nodes: {sorted(distinct)}")
            )

    if integrity:
        causes = _classify(nodes, {sig16 for sig16, _, _ in integrity})
        detail = "; ".join(
            f"{sig16}@{where} {reason} -> {causes.get(sig16, 'unclassified')}"
            for sig16, where, reason in integrity[:3]
        )
        raise AssertionError(
            f"[{label}] {len(integrity)} of {len(deploy_ids)} deploys have no coherent "
            f"terminal verdict (deploy-status, re-homing-aware). first(3)={detail}\n"
            + collect_forensics(nodes, label=label)
        )

    if verdicts.expired:
        causes = _classify(nodes, set(verdicts.expired))
        for sig16 in sorted(verdicts.expired):
            verdicts.expired[sig16] = causes.get(sig16, "unclassified")
        logging.warning(
            "STARVATION-RECORD [%s]: %d of %d deploys expired without landing — %s",
            label,
            len(verdicts.expired),
            len(deploy_ids),
            "; ".join(f"{s}: {c}" for s, c in sorted(verdicts.expired.items())),
        )
    logging.info("[%s] deploy verdicts: %s", label, verdicts.summary())
    return verdicts


def _nodes_in_state(states: Dict[str, str], state: str) -> str:
    return ",".join(sorted(name for name, value in states.items() if value == state)) or "?"


def _classify(nodes, sig16s: set) -> dict:
    """Attach the loss mechanism to each sig; diagnostics never mask a failure."""
    from .log_events import classify_deploy_losses

    try:
        return classify_deploy_losses(nodes, sig16s)
    except Exception:  # noqa: BLE001
        return {}


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
    last_lfb_view = None  # per-node (number, hash-prefix) at the final poll
    while time.time() < deadline:
        try:
            infos = {n.name: n.last_finalized_block().blockInfo for n in nodes}
            lfbs = {name: info.blockHash for name, info in infos.items()}
            last_lfb_view = {
                name: f"#{info.blockNumber}:{info.blockHash[:10]}" for name, info in infos.items()
            }
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

    channel_name = what[2:-1] if what.startswith('@"') and what.endswith('"') else None
    raise AssertionError(
        f"[{label}] finalized {what} did not converge to {expected} on all nodes "
        f"within {timeout:.0f}s; last={last}, water={water}, "
        f"final per-node LFBs={last_lfb_view}\n"
        + collect_forensics(nodes, channel=channel_name, label=label)
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
