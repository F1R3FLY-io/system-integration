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
from typing import Dict, FrozenSet, Generator, List, Optional


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
        event for event in iter_json_events(logs)
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
# Each entry's per-pattern comment names the bug class it catches AND
# the tests that legitimately need to opt out.
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
    "RootRepositoryDivergence": re.compile(
        r"validateAndSetCurrentRoot FAILED.*not in roots store"
    ),

    # Self-validation of a self-created block failed structurally — the
    # proposer built a block its own validator can't verify.
    "SelfCreatedBlockStructuralError": re.compile(
        r"Self-created block validation failed with structural error"
    ),

    # Replay rig divergence — a consume that succeeded during play
    # failed during replay.
    "ConsumeFailedReplayDivergence": re.compile(
        r"SystemRuntimeError\(ConsumeFailed\)"
    ),

    # KvStore-level failure. Catches parent-child races on
    # mergeable-channels entries AND the broader "DAG storage is missing
    # hash" case (which is also keyed below as DAGStorageMissingHash —
    # tests opting out of the latter should also opt out of this).
    # Opt-outs:
    #   tests/shared/test_bonding_validators.py::test_bonding_validators
    #     (paired with DAGStorageMissingHash; sibling gap to the rspace
    #     forward-horizon work, see docs/TODO.md §2.14)
    "KvStoreError": re.compile(r"KvStoreError|KvStore error"),

    # RSpace requested a root not in the local history store.
    "UnknownRootError": re.compile(r"UnknownRootError"),

    # Tripwire: BlockException reached validate_with_effects despite the
    # dependency-gate fix.
    "UnexpectedBlockException": re.compile(r"UNEXPECTED.*BlockException"),

    # ── Bond-block bonds_cache mismatch — proposer ↔ replay divergence ──

    "InvalidBondsCache": re.compile(r"InvalidBondsCache"),
    "BondsCacheMismatch": re.compile(r"do not match block's bond cache"),

    # ── Bug classes with known opt-outs ──

    # Any block recorded as invalid. Opt-outs:
    #   tests/custom/test_consensus_safety.py::test_validator_failure_recovery
    #   tests/custom/test_consensus_safety.py::test_validator_failure_halts_finalization
    "RecordingInvalidBlock": re.compile(r"Recording invalid block"),

    # DAG storage missing a referenced hash. Opt-outs:
    #   tests/shared/test_convergence.py::test_network_recovers_from_validator_pause
    #   tests/shared/test_bonding_validators.py::test_bonding_validators
    #     (Phase C exercises a fresh observer against a multi-bond shard;
    #     see docs/TODO.md §2.14 — sibling gap to this session's rspace
    #     forward-horizon fix; node-side fix not yet started)
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
    matches: List[LogError] = []
    for line in logs.splitlines():
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
