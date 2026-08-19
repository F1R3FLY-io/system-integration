"""Structured log event parsing for Rust node container logs.

Provides two capabilities:

1. **Event queries** — ``find_event(logs, event="foo")`` finds the first
   structured log event matching the given fields. Used by token metadata
   tests to verify startup/mismatch/verification events.

2. **Forbidden-pattern scanning** — ``scan_for_forbidden(logs, node_name,
   allowed)`` flags any line matching ``FORBIDDEN_PATTERNS`` whose key is
   not in ``allowed``. Used as a post-test health check via the autouse
   ``check_node_logs_after_test`` fixture in ``conftest.py``.

Both work on raw log strings (from ``node.logs()``), not Docker handles
directly, keeping this module provider-agnostic.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, Generator, Iterable, List, Optional

# ── Structured event queries ───────────────────────────────────────────


def iter_json_events(logs: str) -> Generator[dict, None, None]:
    """Yield each structured log event from log output.

    The Rust node emits one JSON object per line via tracing-subscriber's
    JSON layer. Lines that aren't parseable JSON are skipped.
    """
    for line in logs.splitlines():
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def find_event(logs: str, **fields: object) -> Optional[dict]:
    """Return the first JSON log event whose fields all match.

    Example::

        event = find_event(node.logs(), event="native_token_metadata_mismatch")
        assert event is not None
        assert "native-token-name" in event["mismatched_fields"]
    """
    for event in iter_json_events(logs):
        if all(event.get(k) == v for k, v in fields.items()):
            return event
    return None


def find_events(logs: str, **fields: object) -> List[dict]:
    """Return all matching JSON log events."""
    return [
        event
        for event in iter_json_events(logs)
        if all(event.get(k) == v for k, v in fields.items())
    ]


# ── Expected progress markers ─────────────────────────────────────────
#
# Substrings tests wait FOR, as opposed to FORBIDDEN_PATTERNS below, which they
# must never see. Centralized for the same reason: each one couples a test to a
# node log line, so when the node rewords one there is a single place to fix
# rather than a grep across suites. Every entry names the node behaviour it
# marks, not the test that waits on it.
SYNC_MARKERS: Dict[str, str] = {
    # Node has begun requesting the approved state from its peers — the start of
    # last-finalized-state sync on a fresh observer or joiner.
    "ApprovedStateRequestStarted": "request_approved_state: start",
    # LFS block requester's stream is up, so block requests are being issued and
    # their responses tracked. Emitted after the approved state is settled.
    "LfsBlockRequesterStarted": "LFS Block Requester stream initialized",
    # Requested blocks went unanswered within the requester's window and are
    # being re-requested. The retry path, not an error.
    "BlockRequestResend": "No responses for",
    # The floor-cache request loop re-asked after a response arrived for a
    # request it had already moved past — evidence the loop kept asking
    # through peer silence rather than wedging.
    "FloorCacheReAsked": "floor-cache channel full or closed",
    # The floor-cache request went unanswered through its retry budget and
    # the restore degraded to local derivation — loud, never a wedge.
    "FloorCacheDegraded": "Proceeding without the shipped floor cache",
    # Engine transitioned to Running — the restore pipeline completed.
    "TransitionedToRunning": "Making a transition to Running",
    # Heartbeat proposer created a block. Fires for every heartbeat-created
    # block, whether or not it carried user deploys.
    "HeartbeatBlockCreated": "Heartbeat: Successfully created block",
    # A peer was dropped from the connections table, after its heartbeat failure
    # streak reached the configured threshold.
    "PeerRemoved": "Removing peer",
}


def marker(key: str) -> str:
    """Return the node log substring registered under ``key``.

    Raises ``KeyError`` naming the available keys, so a typo fails at once rather
    than as a poll that can never succeed.
    """
    try:
        return SYNC_MARKERS[key]
    except KeyError:
        raise KeyError(
            f"unknown sync marker {key!r}; registered markers: {sorted(SYNC_MARKERS)}"
        ) from None


# ── Forbidden-pattern scanning ────────────────────────────────────────


@dataclass
class LogError:
    """A single forbidden-pattern log entry from a node."""

    node: str
    level: str
    message: str


# Patterns that indicate a node is in a broken state. Each entry causes
# any test in which a node emits a matching log line to fail with the
# matched line. Tests that legitimately exercise a known bug class can
# opt out via ``@pytest.mark.allow_forbidden_patterns(<key>, ...)``.
#
# Adding a pattern is a hard tightening — run the full suite to confirm
# no untagged test trips it. New entries should describe a class of
# consensus or runtime bug, not transient conditions handled gracefully
# (heartbeat fallback, peer disconnect, finalization-in-progress retry,
# etc.).
#
# Each entry's per-pattern comment names the bug class it catches; the
# few entries with active opt-outs name the opting-out test.
FORBIDDEN_PATTERNS: Dict[str, re.Pattern] = {
    # ── Always-deny by default; opt-outs rare ──
    # Panic in the node process. No legitimate test should produce this.
    "Panic": re.compile(r"panicked at"),
    # Generic "FATAL" keyword from tracing layer.
    "FatalKeyword": re.compile(r"\bFATAL\b"),
    # System contract or replay engine flagged a structural bug.
    "BugFound": re.compile(r"BUG FOUND"),
    # RootRepository state divergence (replay/play mismatch on rspace
    # roots). Catches the post-state hash divergence that surfaces when
    # rspace mutations don't replay deterministically.
    "RootRepositoryDivergence": re.compile(r"validateAndSetCurrentRoot FAILED.*not in roots store"),
    # Self-validation of a self-created block failed structurally — the
    # proposer built a block its own validator can't verify.
    "SelfCreatedBlockStructuralError": re.compile(
        r"Self-created block validation failed with structural error"
    ),
    # Replay rig divergence — a consume that succeeded during play
    # failed during replay.
    "ConsumeFailedReplayDivergence": re.compile(r"SystemRuntimeError\(ConsumeFailed\)"),
    # KvStore-level failure. Catches parent-child races on
    # mergeable-channels entries AND the broader "DAG storage is missing
    # hash" case (which is also keyed below as DAGStorageMissingHash).
    "KvStoreError": re.compile(r"KvStoreError|KvStore error"),
    # More-specific: a missing mergeable-store entry, raised by
    # `runtime_manager.rs:1035` when the per-block (post_state, creator,
    # seq_num) tuple has no persisted DeployMergeableData. This is the
    # cache/persistence gap tracked as Issue C in active-issues.md
    # (cache hits skip save_mergeable_channels) — surfaces as
    # InvalidTransaction on the proposer and breaks multi-parent merge
    # reconstruction. Kept distinct from KvStoreError so the bug class
    # is named in the failure message.
    "MissingMergeableEntry": re.compile(r"Missing mergeable entry"),
    # RSpace requested a root not in the local history store.
    "UnknownRootError": re.compile(r"UnknownRootError"),
    # System-deploy result consume never matched: the system runtime
    # could not consume the return value of an internal system deploy
    # (e.g. CloseBlock, slash, precharge/refund). Distinct from the
    # replay-divergence variant below: this fires from
    # `system_runtime.rs` when the consume itself times out after
    # retries, regardless of play-vs-replay context. Symptomatic of
    # state-channel writes that didn't materialize as expected
    # (multi-Datum, missing produce, etc.).
    "UnableToConsumeSystemDeploy": re.compile(r"Unable to consume results of system deploy"),
    # Tripwire: BlockException reached validate_with_effects despite the
    # dependency-gate fix.
    "UnexpectedBlockException": re.compile(r"UNEXPECTED.*BlockException"),
    # ── Bond-block bonds_cache mismatch — proposer ↔ replay divergence ──
    "InvalidBondsCache": re.compile(r"InvalidBondsCache"),
    "BondsCacheMismatch": re.compile(r"do not match block's bond cache"),
    # ── DagMerger single-value invariant on Number channels ──
    # An RSpace single-write Number channel (counter / balance) ended up
    # with multiple pre-state values when the proposer tried to merge
    # sibling branches at the same height. Surfaces under sustained
    # multi-validator load (e.g. test_shard_degradation BATCH 4+) when
    # 3 validators propose siblings touching the same shared channel.
    # Hard consensus bug — propose fails with BugError, shard wedges,
    # LFB freezes deterministically. See real-flakes-tracker #1.
    #
    # Two distinct error strings come from this same invariant — both
    # check ``data.len() > 1`` against a Number channel at different
    # points in the merge pipeline. Caught here in one entry because
    # both indicate the same root cause:
    #   - rholang_merging_logic.rs:225 "has N pre-state values; ..."
    #   - runtime_manager.rs:1109     "Expected at most one value for
    #     number channel ..., found N"
    "SingleValueInvariantViolated": re.compile(
        r"(has \d+ pre-state values; single-value invariant violated"
        r"|Expected at most one value for number channel)"
    ),
    # ── Reducer single-value violation (eval_single_expr / eval_to_bool) ──
    # reduce.rs raises this when a Par reaching a single-expression context
    # holds != 1 expressions: n_exprs=0 (empty Par) or >1 (multi-Datum). The
    # n_exprs=0 case is the orphan-#3 signature — a coupled state-channel
    # sub-map decoupled during merge (e.g. pendingWithdrawer[X] with no
    # allBonds[X] → Nil + reward), surfacing as a ReduceError in the PoS
    # payout. Dissolved by eager advance-only construction; a recurrence is a
    # merge/coupling regression. Distinct layer from SingleValueInvariantViolated
    # (merge pipeline) — this fires inside the reducer. Checked manually every
    # run; now enforced.
    "ReducerMultipleExpressions": re.compile(r"Multiple expressions given\. eval_"),
    # ── Seal / merge stale-consume backstop ──
    # state_change_merger.rs `make_trie_action` fail-closed tripwire: a
    # chain whose committed diff was rebased onto a divergent base reached
    # the fold — a single-value-cell race that should have been rejected
    # upstream in DagMerger or skipped by the seal. This is the integration
    # surface of the seal `item 2` regression (a non-foldable concurrent
    # write the seal double-folds); the skip-rejected fix removes the
    # in-seal source, so a recurrence under load is a merge/seal regression.
    "StaleConsume": re.compile(r"stale-consume on channel"),
    # ── Multi-parent pre-state divergence ──
    # interpreter_util.rs: a validator recomputed a multi-parent pre-state
    # that differs from the one the proposer signed — the divergent-FS /
    # divergent-merge symptom and head of the #71 InvalidTransaction cascade.
    "ComputedPreStateMismatch": re.compile(
        r"Computed pre-state hash .* does not equal block's pre-state hash"
    ),
    # ── Propose-path internal assertion ──
    # rnode's propose path raised an internal "this should never happen"
    # error tagged with the offending sequence number. Always indicates
    # a bug — either the DagMerger invariant above or a related state-
    # consistency assertion. Same family across surfaces: tracker #7
    # (synchrony_constraint test, seqNum -1) and tracker #1
    # (test_shard_degradation, seqNum N≥0). Distinct entry from
    # SingleValueInvariantViolated so each surface stays attributable
    # even when only one of the two messages reaches the captured tail.
    "ProposeBugError": re.compile(r"BugError \(seqNum -?\d+\)"),
    # ── Bug classes with known opt-outs ──
    # Any block recorded as invalid. Opt-outs:
    #   tests/custom/test_consensus_safety.py::test_validator_failure_recovery
    #   tests/custom/test_consensus_safety.py::test_validator_failure_halts_finalization
    "RecordingInvalidBlock": re.compile(r"Recording invalid block"),
    # More-specific InvalidBlock classes — diagnostic when surfaced
    # individually. RecordingInvalidBlock catches the generic case; the
    # specific entries below let the failure message name the bug class
    # directly. A single line may match both — the scanner reports the
    # first non-allowed key (dict iteration order); tests opting out of
    # the specific class should also opt out of RecordingInvalidBlock.
    #
    # InvalidRepeatDeploy: validator rejected a block whose body.deploys
    # contains a sig already applied in pre-state. Indicates a recovery
    # / dedup gap — either the proposer wrongly included a recovered
    # sig (proposer-side filter gap) or the applied_sigs computation is
    # wrong (merge-integration bug).
    "InvalidRepeatDeploy": re.compile(r"InvalidRepeatDeploy"),
    # InvalidTransaction: block_processor's catch-all for BlockException
    # during validation (see block_processor.rs:316-330 — converts
    # KvStoreError + others to InvalidTransaction "to prevent dependent-
    # block stall"). When this fires, look upstream for the original
    # exception (often MissingMergeableEntry or ConsumeFailed).
    "InvalidTransaction": re.compile(r"InvalidTransaction"),
    # Out-of-phlo execution that broke replay (Issue A — f1r3node-rust#47
    # / legacy f1r3node#506). When this appears in REPLAY (after also
    # appearing in PLAY), it indicates the play/replay paths handle the
    # phlo-exhaustion abort point differently and the system-deploy
    # precharge/refund consume can't reconcile. Caught here so it can't
    # silently appear during recovery / cross-validator replay.
    "ComputationOutOfPhlogistons": re.compile(r"Computation ran out of phlogistons"),
    # Observer / readonly node's reporting layer panicked with an
    # "Unused COMM event" — the reporter's event-replay diverged from
    # the recorded event log. Causes the gRPC stream serving observer
    # queries to be CANCELLED (RST_STREAM error code 8), timing out any
    # observer-LFB-sync check. Distinct from the general Panic key so
    # the failure message points at the observer-reporter bug class.
    "ObserverReporterUnusedCOMM": re.compile(r"Unused COMM event"),
    # DAG storage missing a referenced hash. Opt-outs:
    #   tests/shared/test_convergence.py::test_network_recovers_from_validator_pause
    "DAGStorageMissingHash": re.compile(r"DAG storage is missing hash"),
    # ── Finalized-read fork ──
    # derive_floor refused to advance because two finalized candidates hold
    # mutually non-contained state: the node's finalized read surface has
    # forked from its peers'. The guard is working (nothing is erased), but
    # the shard is split and every propose from the frozen base is refused.
    "IncompatibleFinalizedFork": re.compile(r"incompatible finalized fork"),
    # A floor advanced to a block that is not on the adopted LFB lineage, or
    # two floors certified incompatible chains.
    "FinalizedFloorSafetyViolation": re.compile(r"finalized-floor safety violation"),
    # ── Merge base / applied-diff incoherence ──
    # state_change_merger.rs `make_trie_action`: a retained chain removes a
    # datum the base does not hold, i.e. the applied diffs were computed
    # against a different base than the one being extended. Deterministic —
    # every propose over the same scope re-hits it and the shard wedges with
    # a frozen LFB.
    "MergeBaseIncoherent": re.compile(r"applied diffs are incoherent with the base state"),
}


def classify_deploy_losses(nodes, sig_prefixes: Iterable[str]) -> Dict[str, str]:
    """Explain WHY each deploy failed to finalize, not just that it did.

    A deploy can be lost several structurally different ways that all surface
    as the same ``DeployError``: the phlo-refund quarantine removing it from
    the rejected-deploy buffer, losing every merge it reaches, or the validity
    window closing before it was ever (re-)included. Reporting only the
    exception type forces a hand log-dive to tell them apart.

    Evidence lines matched, all keyed by the first 16 hex chars of the sig:

    - ``deploy lifecycle terminal verdict written`` (INFO,
      ``f1r3fly.casper.lifecycle``): flattened-JSON fields ``sig`` (exactly
      16 hex chars), ``state`` (Finalized/Expired/Failed), ``rejection_count``.
    - ``DagMerger rejected N user deploys: <sigs>`` (INFO): comma-joined
      16-hex-char sigs, one line per losing merge.
    - ``quarantined_toxic_rejected_buffer=true`` (WARN): the full sig hex is
      embedded in the ``error=`` message the quarantine parsed it from.

    Retry-gate deferrals are count-only INFO lines with no sigs, so per-sig
    gate counts are NOT log-recoverable; the per-decision basis needs
    ``f1r3fly.casper.recovery=debug`` and a hand-read.

    Takes ALL nodes and merges their evidence, because the evidence is split
    across them: quarantine fires only on the deploy's OWNER, merge rejections
    on whichever node performed the merge, and each node's lifecycle register
    writes its own terminal verdict.

    One streaming pass per node, on the failure path only.
    """
    prefixes = {p[:16] for p in sig_prefixes if p}
    if not prefixes:
        return {}
    facts: Dict[str, dict] = {
        p: {"quarantined": False, "merge_rejected": 0, "state": None, "rejection_count": None}
        for p in prefixes
    }
    for node in nodes:
        try:
            lines = node.iter_log_lines()
        except Exception:  # noqa: BLE001 - a node with no readable log adds nothing
            continue
        for line in lines:
            for prefix in prefixes:
                if prefix not in line:
                    continue
                fact = facts[prefix]
                if "quarantined_toxic_rejected_buffer=true" in line:
                    fact["quarantined"] = True
                if "DagMerger rejected" in line:
                    fact["merge_rejected"] += 1
                if "terminal verdict written" in line:
                    try:
                        record = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    fact["state"] = record.get("state")
                    fact["rejection_count"] = record.get("rejection_count")
    return {prefix: _describe_deploy_loss(fact) for prefix, fact in facts.items()}


def _describe_deploy_loss(fact: dict) -> str:
    """Turn one deploy's log evidence into a cause description."""
    state = fact.get("state") or "unknown"
    rejection_count = fact.get("rejection_count")
    rej = str(rejection_count) if rejection_count is not None else "?"

    if fact["quarantined"]:
        return (
            f"refund-quarantined (state={state}): a failed phlo refund removed the "
            "deploy from the rejected-deploy buffer, the only re-proposable copy"
        )
    # Ordering matters: a deploy that RECOVERED, or that has not yet reached a
    # terminal verdict, must never be described as lost. Reporting a slow
    # recovery as destroyed work is the most misleading direction this
    # diagnostic can fail in.
    if state == "Finalized":
        return (
            f"recovered, not lost: finalized after {rej} rejection(s) — the test's "
            "timeout expired before recovery completed, so this is a test-patience "
            "failure"
        )
    if state == "unknown":
        if fact["merge_rejected"]:
            return (
                f"still pending: lost {fact['merge_rejected']} merge(s), no terminal "
                "verdict written — the test's timeout expired while the deploy was "
                "still contestable, which is NOT proof it was lost (per-sig gate "
                "decisions are visible only at f1r3fly.casper.recovery=debug)"
            )
        return (
            "no per-sig log evidence and no terminal verdict — either genuinely "
            "pending untouched by any merge, or the scanned logs don't cover the "
            "run (the terminal-verdict line is INFO; check the logging filter)"
        )
    if state == "Expired":
        if fact["merge_rejected"] or (rejection_count or 0) > 0:
            return (
                f"merge-starved (state=Expired): rejected {rej}x "
                f"({fact['merge_rejected']} losing merge(s) in this shard's logs) and "
                "never re-landed before the validity window closed"
            )
        return (
            "expired without a recorded rejection: never included, or every inclusion "
            "was voided by finalization elsewhere — the validity window closed first; "
            "the count-only 'deferred by the retry gate' INFO lines say whether the "
            "gate was shut"
        )
    if state == "Failed":
        return f"terminal Failed: the deploy executed and failed ({rej} rejection(s))"
    return f"terminal verdict {state}: {rej} rejection(s)"


def scan_for_forbidden(
    logs: str,
    node_name: str,
    allowed: FrozenSet[str] = frozenset(),
) -> List[LogError]:
    """Scan log output for forbidden-pattern matches not in ``allowed``.

    ``allowed`` is a set of pattern keys (from ``FORBIDDEN_PATTERNS``)
    that the caller expects to see. Lines matching allowed patterns are
    skipped; lines matching non-allowed patterns produce ``LogError``
    entries with ``level="FORBIDDEN"``.

    A line that matches multiple patterns fires on the first
    non-opted-out match (dict iteration order). Tests that produce log
    lines matching several patterns must opt out of every applicable
    key — this is intentional: it forces the test author to acknowledge
    each known bug class the line represents.
    """
    return scan_lines_for_forbidden(logs.splitlines(), node_name, allowed)


def scan_lines_for_forbidden(
    lines: Iterable[str],
    node_name: str,
    allowed: FrozenSet[str] = frozenset(),
) -> List[LogError]:
    """Streaming form of :func:`scan_for_forbidden`.

    Consumes ``lines`` lazily, so a multi-hundred-MB node log can be scanned
    by iterating the file line-by-line instead of materializing the whole log
    as one string plus a ``splitlines()`` list (which previously doubled peak
    memory per node during the post-test scan).
    """
    matches: List[LogError] = []
    for line in lines:
        for key, pattern in FORBIDDEN_PATTERNS.items():
            if key in allowed:
                continue
            if pattern.search(line):
                short = line[:250] + "..." if len(line) > 250 else line
                matches.append(
                    LogError(
                        node=node_name,
                        level="FORBIDDEN",
                        message=f"[{key}] {short}",
                    )
                )
                break
    return matches


def record_scanned(
    name: str,
    offset: int,
    scanned: int,
    offsets: Dict[str, int],
    owners: Dict[str, FrozenSet[str]],
    allowed: FrozenSet[str],
) -> None:
    """Record a SUCCESSFUL per-test scan for later retirement judging.

    ``offsets[name]`` becomes the judged-through line count and
    ``owners[name]`` the allowance set the lines were judged under. Call
    only after a completed scan — a failed scan must fail closed by
    recording nothing (the window was not judged, and accumulating
    allowances across failed scans could hide a forbidden event produced
    under a stricter test).
    """
    offsets[name] = offset + scanned
    owners[name] = allowed


def scan_retired_snapshot(
    name: str,
    log_text: str,
    offsets: Dict[str, int],
    owners: Dict[str, FrozenSet[str]],
    current_allowed: FrozenSet[str],
) -> List[LogError]:
    """Judge a retired node's unjudged log tail under its OWNER's allowances.

    The snapshot holds the node's full cumulative log; the prefix through
    ``offsets[name]`` was already judged by per-test scans under their own
    tests' allowances and is skipped (re-judging it would mis-attribute
    lines to the consuming test). The remaining teardown-window lines
    belong to the node's last owning test and are judged under THAT
    test's recorded allowances only — the consuming test's allowances
    never apply to another test's lines, so they cannot hide a forbidden
    teardown event. ``current_allowed`` is used only when no owner is
    recorded: a transient node attached and detached within the current
    test, whose whole log belongs to the current test.

    Pops the bookkeeping entries, so a new node reusing the retired name
    starts fresh. Callers must invoke this BEFORE recording the current
    test's scans (see conftest ordering comment).
    """
    offset = offsets.pop(name, 0)
    owner_allowances = owners.pop(name, None)
    effective = current_allowed if owner_allowances is None else owner_allowances
    tail = log_text.splitlines()[offset:]
    return scan_lines_for_forbidden(tail, name, effective)


def format_errors(errors: List[LogError], max_display: int = 30) -> str:
    """Format a list of ``LogError`` entries into a readable assertion message."""
    node_names = sorted(set(e.node for e in errors))
    lines = [
        f"Forbidden log entries on {len(node_names)} node(s) "
        f"({', '.join(node_names)}): {len(errors)} total"
    ]
    for e in errors[:max_display]:
        lines.append(f"  [{e.node}] [{e.level}] {e.message}")
    if len(errors) > max_display:
        lines.append(f"  ... and {len(errors) - max_display} more")
    return "\n".join(lines)
