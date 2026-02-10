"""
Consensus Health Integration Test

Session-wide log scan that runs after all other tests to verify no consensus
errors occurred during the entire test suite. This catches issues that may
not cause individual test failures but indicate underlying consensus problems.

This file should be listed LAST in pyproject.toml's python_files so it
executes after all other tests have run and accumulated their log output.
"""

import logging
import re
from typing import List, Tuple

import pytest
from docker.client import DockerClient

from .conftest import (
    assert_containers_running,
    ALL_CONTAINERS,
)
from .rnode import Node

pytestmark = pytest.mark.xdist_group("shard")


# Patterns that indicate consensus bugs. Each tuple is (pattern, description).
# These should NEVER appear in a healthy node's logs.
FATAL_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (
        re.compile(r"InvalidBondsCache"),
        "Non-deterministic post-state hash computation (InvalidBondsCache)",
    ),
    (
        re.compile(r"Self-created block validation failed with structural error"),
        "Structural block creation bug detected by self-validation",
    ),
    (
        re.compile(r"java\.lang\.Exception: Finalization in progress"),
        "Untyped finalization exception (should use FinalizationInProgressException)",
    ),
    (
        re.compile(r"\bFATAL\b"),
        "Fatal error causing node crash",
    ),
    (
        re.compile(r"\bpanic\b", re.IGNORECASE),
        "Panic in node process",
    ),
]

# Patterns that are acceptable (transient conditions handled gracefully).
# Logged here for informational purposes -- their presence is expected and
# not an error.
INFO_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (
        re.compile(r"Self-created block validation failed with transient reason"),
        "Transient validation failure (handled gracefully, will retry)",
    ),
    (
        re.compile(r"Propose failed: InternalDeployError"),
        "Propose discarded due to transient validation failure (will retry on next heartbeat)",
    ),
    (
        re.compile(r"Snapshot unavailable: finalization in progress"),
        "Finalization-in-progress handled gracefully (skipped propose cycle)",
    ),
    (
        re.compile(r"Block .+ validation deferred: finalization in progress"),
        "Block validation deferred during finalization (re-queued)",
    ),
]


def test_no_consensus_errors_in_logs(
    docker_client: DockerClient,
    bootstrap_node: Node,
    validator1_node: Node,
    validator2_node: Node,
    validator3_node: Node,
    readonly_node: Node,
) -> None:
    """Scan all node logs for consensus errors accumulated across the test suite.

    This test runs after all other tests and checks that no consensus-layer
    errors occurred during the entire session. It catches problems that may
    be silently recovered from (e.g., transient errors that shouldn't happen
    at all) or that cause intermittent failures in other tests.

    Fatal patterns cause test failure. Info patterns are logged but accepted.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    nodes = [
        bootstrap_node,
        validator1_node,
        validator2_node,
        validator3_node,
        readonly_node,
    ]

    errors: List[str] = []

    for node in nodes:
        node_logs = node.logs()

        # Check for fatal patterns
        for pattern, description in FATAL_PATTERNS:
            matches = pattern.findall(node_logs)
            if matches:
                errors.append(
                    f"[{node.name}] {description}: {len(matches)} occurrence(s)"
                )

        # Log info patterns (not errors)
        for pattern, description in INFO_PATTERNS:
            matches = pattern.findall(node_logs)
            if matches:
                logging.info(
                    "[%s] %s: %d occurrence(s)", node.name, description, len(matches),
                )

    assert len(errors) == 0, (
        "Consensus errors detected in node logs:\n"
        + "\n".join(f"  - {e}" for e in errors)
    )
