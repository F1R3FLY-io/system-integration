"""Structured log event parsing for Rust node container logs.

Provides two capabilities:

1. **Event queries** — ``find_event(logs, event="foo")`` finds the first
   structured log event matching the given fields. Used by token metadata
   tests to verify startup/mismatch/verification events.

2. **Log scanning** — ``scan_for_errors(logs, node_name)`` flags any
   ERROR, WARN, or raw panic not matching the acceptable-patterns
   whitelist. Used as a post-test health check.

Both work on raw log strings (from ``node.logs()``), not Docker handles
directly, keeping this module provider-agnostic.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Generator, List, Optional


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


# ── Log error scanning ────────────────────────────────────────────────


@dataclass
class LogError:
    """A single unexpected error found in a node's logs."""
    node: str
    level: str  # "ERROR", "WARN", or "PANIC"
    message: str


# Known-okay WARN/ERROR/panic lines during normal operation.
# Each entry MUST have a comment explaining WHY it's acceptable.
# Build this list incrementally by running tests and triaging.
ACCEPTABLE_PATTERNS: List[re.Pattern] = [
]


def _parse_log_level(line: str) -> Optional[str]:
    """Extract the log level from a line.

    Returns "ERROR", "WARN", "PANIC", or None.
    """
    if "panicked at" in line:
        return "PANIC"

    json_start = line.find("{")
    if json_start == -1:
        return None

    try:
        obj = json.loads(line[json_start:])
        level = obj.get("level", "")
        if level in ("ERROR", "WARN"):
            return level
    except (json.JSONDecodeError, ValueError):
        pass

    return None


def scan_for_errors(logs: str, node_name: str) -> List[LogError]:
    """Scan log output for unexpected errors, warnings, and panics.

    Filters out lines matching ACCEPTABLE_PATTERNS.
    """
    unexpected = []
    for line in logs.splitlines():
        level = _parse_log_level(line)
        if level is None:
            continue
        if any(p.search(line) for p in ACCEPTABLE_PATTERNS):
            continue
        short = line[:250] + "..." if len(line) > 250 else line
        unexpected.append(LogError(node=node_name, level=level, message=short))
    return unexpected


def format_errors(errors: List[LogError], max_display: int = 30) -> str:
    """Format a list of LogErrors into a readable assertion message."""
    node_names = sorted(set(e.node for e in errors))
    by_level: dict[str, list] = {}
    for e in errors:
        by_level.setdefault(e.level, []).append(e)

    lines = [
        f"Unexpected log entries on {len(node_names)} node(s) "
        f"({', '.join(node_names)}): "
        f"{len(by_level.get('PANIC', []))} panics, "
        f"{len(by_level.get('ERROR', []))} errors, "
        f"{len(by_level.get('WARN', []))} warnings"
    ]
    for e in errors[:max_display]:
        lines.append(f"  [{e.node}] [{e.level}] {e.message}")
    if len(errors) > max_display:
        lines.append(f"  ... and {len(errors) - max_display} more")
    return "\n".join(lines)
