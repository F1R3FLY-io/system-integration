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
}


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
