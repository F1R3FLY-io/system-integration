"""Node capability validation for opt-in integration regressions."""

from __future__ import annotations

import re
from collections.abc import Iterable

_CAPABILITY_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def validate_node_capabilities(capabilities: Iterable[object], *, source: str) -> frozenset[str]:
    """Return a validated capability set, rejecting malformed or duplicate names."""
    validated: set[str] = set()
    for capability in capabilities:
        if not isinstance(capability, str) or not _CAPABILITY_PATTERN.fullmatch(capability):
            raise ValueError(f"invalid node capability from {source}: {capability!r}")
        if capability in validated:
            raise ValueError(f"duplicate node capability from {source}: {capability}")
        validated.add(capability)
    return frozenset(validated)


def missing_node_capabilities(
    required: frozenset[str], available: frozenset[str]
) -> tuple[str, ...]:
    """Return required capabilities absent from the available set."""
    return tuple(sorted(required - available))
