"""Structured log event parsing for Rust node container logs.

Provides two capabilities:

1. **Event queries** — ``find_event(logs, event="foo")`` finds the first
   structured log event matching the given fields. Used by token metadata
   tests to verify startup/mismatch/verification events.

2. **Fatal log scanning** — ``scan_logs(logs, node_name)`` flags any line
   matching ``FATAL_PATTERNS``. Used as a post-test health check via the
   autouse ``check_node_logs_after_test`` fixture in ``conftest.py``.

Both work on raw log strings (from ``node.logs()``), not Docker handles
directly, keeping this module provider-agnostic.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, Generator, List, Optional, Tuple


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


# ── Fatal log scanning ────────────────────────────────────────────────


@dataclass
class LogError:
    """A single fatal log entry from a node."""
    node: str
    level: str
    message: str


# Patterns that indicate a node is in a broken state. Each (pattern,
# description) entry causes any test in which a node emits a matching
# log line to fail with the description prepended to the matched line.
#
# Carried forward from the pre-v2 ``test_consensus_health`` regression
# guard. New entries should describe a class of consensus or runtime
# bug — not transient conditions handled gracefully (heartbeat fallback,
# peer disconnect, finalization-in-progress retry, etc.).
FATAL_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (
        re.compile(r"panicked at"),
        "Panic in node process",
    ),
    (
        re.compile(r"validateAndSetCurrentRoot FAILED.*not in roots store"),
        "RootRepository state divergence (replay/play mismatch)",
    ),
    (
        re.compile(r"Self-created block validation failed with structural error"),
        "Structural block creation bug detected by self-validation",
    ),
    (
        re.compile(r"\bFATAL\b"),
        "Fatal error causing node crash",
    ),
]


def scan_logs(logs: str, node_name: str) -> List[LogError]:
    """Return one ``LogError`` per line that matches any ``FATAL_PATTERNS`` entry."""
    out: List[LogError] = []
    for line in logs.splitlines():
        for pattern, description in FATAL_PATTERNS:
            if pattern.search(line):
                short = line[:250] + "..." if len(line) > 250 else line
                out.append(LogError(
                    node=node_name,
                    level="FATAL",
                    message=f"{description}: {short}",
                ))
                break
    return out


# ── Forbidden patterns (autouse, opt-out via marker) ──────────────────
#
# Patterns that must NEVER appear in any test's logs unless that test
# explicitly opts out via @pytest.mark.allow_forbidden_patterns(...).
#
# Each entry has a comment naming the bug class it catches AND the tests
# that legitimately need to opt out. Adding a pattern requires running the
# full suite to confirm it doesn't fire on tests that aren't already
# opting out.
FORBIDDEN_PATTERNS: Dict[str, re.Pattern] = {
    # Bond-block bonds_cache mismatch — proposer ↔ replay divergence.
    # No legitimate test should produce this.
    "InvalidBondsCache": re.compile(r"InvalidBondsCache"),
    "BondsCacheMismatch": re.compile(r"do not match block's bond cache"),

    # Any block recorded as invalid. Opt-outs:
    #   tests/custom/test_consensus_safety.py::test_validator_failure_recovery
    #   tests/custom/test_consensus_safety.py::test_validator_failure_halts_finalization
    "RecordingInvalidBlock": re.compile(r"Recording invalid block"),

    # DAG storage missing a referenced hash. Opt-outs:
    #   tests/shared/test_convergence.py::test_network_recovers_from_validator_pause
    "DAGStorageMissingHash": re.compile(r"DAG storage is missing hash"),
}


def scan_for_forbidden(
    logs: str,
    node_name: str,
    allowed: FrozenSet[str] = frozenset(),
) -> List[LogError]:
    """Scan log output for forbidden-pattern matches not in `allowed`.

    `allowed` is a set of pattern keys (from FORBIDDEN_PATTERNS) that the
    caller expects to see. Lines matching allowed patterns are skipped;
    lines matching non-allowed patterns produce LogError entries with
    level="FORBIDDEN".

    Complementary to ``scan_logs`` (FATAL_PATTERNS) — that path catches
    always-fail signatures with no opt-out; this one catches patterns that
    have legitimate test-level opt-outs via marker.
    """
    matches: List[LogError] = []
    for line in logs.splitlines():
        for key, pattern in FORBIDDEN_PATTERNS.items():
            if key in allowed:
                continue
            if pattern.search(line):
                short = line[:250] + "..." if len(line) > 250 else line
                matches.append(
                    LogError(node=node_name, level="FORBIDDEN", message=short)
                )
                break
    return matches


def format_errors(errors: List[LogError], max_display: int = 30) -> str:
    """Format a list of ``LogError`` entries into a readable assertion message."""
    node_names = sorted(set(e.node for e in errors))
    lines = [
        f"Fatal log entries on {len(node_names)} node(s) "
        f"({', '.join(node_names)}): {len(errors)} total"
    ]
    for e in errors[:max_display]:
        lines.append(f"  [{e.node}] [{e.level}] {e.message}")
    if len(errors) > max_display:
        lines.append(f"  ... and {len(errors) - max_display} more")
    return "\n".join(lines)
