"""
Heartbeat Integration Tests

Tests for the heartbeat proposer functionality that automatically creates blocks
to maintain blockchain liveness when the Last Finalized Block (LFB) becomes stale.

Both Rust (f1r3node-rust-3) and Scala (f1r3node-scala) share the same heartbeat
proposer implementation (node/src/rust/instances/heartbeat_proposer.rs) with
identical log messages:
  - "Heartbeat: Starting with random initial delay of Xs ..."
  - "Heartbeat: Successfully created block"
  - "Heartbeat: Proposing block - reason: ..."
  - "CONFIGURATION ERROR: Heartbeat incompatible with max-number-of-parents=1"

Concurrent propose handling (both repos):
  - Uses Semaphore(1) non-blocking lock in ProposerInstance
  - If lock is held, returns "Failure: another propose is in progress" immediately
  - Heartbeat logs: "Heartbeat: Propose already in progress, will retry next check"

Standalone tests start a fresh node per test via docker-compose.
Shard tests use the session-scoped shard fixture (shared across tests).
"""

import time
from typing import Generator
from contextlib import contextmanager

import pytest
from f1r3fly.client import RClientException
from f1r3fly.crypto import PrivateKey
from docker.client import DockerClient

from .common import (
    CommandLineOptions,
    TestingContext,
)
from .conftest import (
    assert_containers_running,
    start_standalone_node,
    STANDALONE_PRIVATE_KEY,
    VALIDATOR1_KEY,
    ALL_CONTAINERS,
)
from .rnode import Node


USER_KEY = PrivateKey.from_hex(STANDALONE_PRIVATE_KEY)


# ---------------------------------------------------------------------------
# Standalone helper
# ---------------------------------------------------------------------------

@contextmanager
def start_node_with_heartbeat(
    command_line_options: CommandLineOptions,
    docker_client: DockerClient,
    heartbeat_enabled: bool = True,
    heartbeat_check_interval: int = 5,
    heartbeat_max_lfb_age: int = 3,
    max_number_of_parents: int = 10,
) -> Generator[Node, None, None]:
    """Start a standalone node with heartbeat configuration.

    Wraps start_standalone_node with heartbeat-specific CLI options.
    """
    cli_options = {
        "--heartbeat-check-interval": f"{heartbeat_check_interval}seconds",
        "--heartbeat-max-lfb-age": f"{heartbeat_max_lfb_age}seconds",
        "--max-number-of-parents": str(max_number_of_parents),
    }
    cli_flags = set()
    if heartbeat_enabled:
        cli_flags.add("--heartbeat-enabled")
    else:
        cli_options["--heartbeat-enabled"] = "false"

    with start_standalone_node(
        docker_client=docker_client,
        command_line_options=command_line_options,
        cli_flags=cli_flags,
        cli_options=cli_options,
    ) as node:
        yield node


# ===========================================================================
# STANDALONE TESTS
# ===========================================================================


@pytest.mark.xdist_group("standalone")
def test_heartbeat_creates_blocks_when_idle(
    command_line_options: CommandLineOptions,
    docker_client: DockerClient,
) -> None:
    """
    Test that heartbeat automatically creates blocks and emits expected logs.

    Verifies in standalone mode:
    1. Heartbeat starts and logs initialization message
    2. Without any deploys, heartbeat creates multiple blocks beyond genesis
    3. Log contains successful block creation messages
    4. No 'has not made progress' error (regression guard)
    """
    with start_node_with_heartbeat(
        command_line_options,
        docker_client,
        heartbeat_enabled=True,
        heartbeat_check_interval=5,
        heartbeat_max_lfb_age=3,
        max_number_of_parents=10,
    ) as node:
        # Poll until the node has created enough heartbeat blocks.
        # JVM startup inside Docker can take 20-30s, so a fixed sleep is
        # unreliable. Instead we poll with a generous deadline.
        heartbeat_timeout = int(90 * command_line_options.timeout_scale)
        deadline = time.time() + heartbeat_timeout
        final_count = 0
        success_count = 0
        while time.time() < deadline:
            final_count = node.get_blocks_count(10)
            logs = node.logs()
            success_count = logs.count("Heartbeat: Successfully created block")
            if final_count >= 4 and success_count >= 3:
                break
            time.sleep(5)

        logs = node.logs()

        # Verify heartbeat initialization log
        assert "Heartbeat: Starting with random initial delay" in logs, (
            "Should log heartbeat startup message"
        )

        assert final_count >= 4, (
            f"Heartbeat should create multiple blocks in standalone mode. "
            f"Got {final_count} blocks (expected >= 4: genesis + 3 heartbeat). "
            f"Successful heartbeat creations in logs: {success_count}"
        )

        assert success_count >= 3, (
            f"Should see at least 3 successful heartbeat block creations in logs, "
            f"got {success_count}"
        )

        # Regression guard: standalone validator should not hit this error
        assert "has not made progress" not in logs, (
            "Should NOT see 'has not made progress' error in standalone mode"
        )


@pytest.mark.xdist_group("standalone")
def test_heartbeat_disabled_when_max_parents_is_one(
    command_line_options: CommandLineOptions,
    docker_client: DockerClient,
) -> None:
    """
    Test that heartbeat is disabled with warning when max-number-of-parents=1.

    Both Rust and Scala heartbeat proposers guard against this configuration.
    Heartbeat blocks would fail InvalidParents validation with max-number-of-parents=1
    because empty blocks can't include all required parents.
    """
    with start_node_with_heartbeat(
        command_line_options,
        docker_client,
        heartbeat_enabled=True,
        heartbeat_check_interval=5,
        heartbeat_max_lfb_age=3,
        max_number_of_parents=1,
    ) as node:
        logs = node.logs()
        assert "Heartbeat incompatible with max-number-of-parents=1" in logs or \
               "CONFIGURATION ERROR" in logs, (
            "Should log warning about max-number-of-parents=1"
        )

        initial_count = node.get_blocks_count(10)

        # Poll for 15s, failing early if any unexpected blocks appear.
        # This is a negative assertion: heartbeat should NOT create blocks.
        deadline = time.time() + 15
        while time.time() < deadline:
            time.sleep(3)
            final_count = node.get_blocks_count(10)
            assert final_count == initial_count, (
                f"Heartbeat should be disabled: initial={initial_count}, "
                f"final={final_count} (block appeared unexpectedly)"
            )


# ===========================================================================
# SHARD TESTS
# ===========================================================================


@pytest.mark.xdist_group("shard")
def test_heartbeat_creates_blocks_when_idle_shard(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
    validator2_node: Node,
    validator3_node: Node,
) -> None:
    """
    Test that heartbeat creates blocks on all shard validators and emits expected logs.

    Validates heartbeat under multi-validator coordination and multi-parent DAG.
    The shard conf (shared-rnode.conf) has heartbeat.enabled=true by default.
    """
    # Pre-check: all shard containers must be running
    assert_containers_running(docker_client, ALL_CONTAINERS)

    validators = [validator1_node, validator2_node, validator3_node]

    # Verify heartbeat initialization log on at least one validator
    logs = validator1_node.logs()
    assert "Heartbeat: Starting with random initial delay" in logs, (
        "Shard validator should log heartbeat startup message"
    )

    # Record the highest block number seen by each validator.
    # Using blockNumber instead of get_blocks_count(depth) because the
    # depth parameter returns a sliding window -- as the chain advances,
    # older blocks scroll out and the count can decrease even though new
    # blocks are being created.
    initial_block_numbers = [
        max(b.blockNumber for b in v.get_blocks(5)) for v in validators
    ]

    # Poll until all validators have advanced by at least 2 blocks.
    shard_timeout = int(90 * testing_context.timeout_scale)
    deadline = time.time() + shard_timeout
    all_advanced = False
    while time.time() < deadline:
        final_block_numbers = [
            max(b.blockNumber for b in v.get_blocks(5)) for v in validators
        ]
        if all(
            final_block_numbers[i] >= initial_block_numbers[i] + 2
            for i in range(len(validators))
        ):
            all_advanced = True
            break
        time.sleep(5)

    # Post-check: all containers still running after wait
    assert_containers_running(docker_client, ALL_CONTAINERS)

    assert all_advanced, (
        f"All validators should advance by at least 2 blocks from heartbeat. "
        + ", ".join(
            f"{validators[i].name}: initial={initial_block_numbers[i]}, "
            f"final={final_block_numbers[i]}"
            for i in range(len(validators))
        )
    )


@pytest.mark.xdist_group("shard")
def test_manual_propose_during_heartbeat_shard(
    docker_client: DockerClient,
    validator1_node: Node,
    testing_context,
) -> None:
    """
    Regression test: manual propose during heartbeat in shard mode.

    When heartbeat is active and auto-proposing, a concurrent manual propose
    call should not crash the node. Both Rust and Scala use a Semaphore(1)
    non-blocking lock -- if the lock is held, the call returns immediately
    with "Failure: another propose is in progress".

    The race condition is most likely in shard mode where multiple validators
    are heartbeating concurrently.
    """
    # Pre-check: all shard containers must be running
    assert_containers_running(docker_client, ALL_CONTAINERS)

    # Use highest block number instead of block count. In a multi-parent DAG
    # with multiple validators, show_blocks(depth=N) returns a sliding window
    # whose count can plateau even as new blocks arrive (old blocks fall out of
    # the window). Block number is monotonically increasing and reliable.
    initial_block_number = max(
        b.blockNumber for b in validator1_node.get_blocks(5)
    )

    # Deploy a contract on validator1
    validator1_node.deploy_string(
        '@"shard-heartbeat-test"!(1)',
        VALIDATOR1_KEY,
    )

    # Attempt manual propose while heartbeat is active on all validators.
    # Expected outcomes:
    #   - Success: manual propose won the lock race
    #   - "another propose is in progress": heartbeat holds the lock (acceptable)
    try:
        validator1_node.propose()
    except RClientException as e:
        message = str(e)
        assert "another propose is in progress" in message or "Failure" in message, (
            f"Expected 'another propose is in progress' but got: {message}"
        )

    # Poll until the node has advanced beyond the initial block number.
    propose_timeout = int(60 * testing_context.timeout_scale)
    deadline = time.time() + propose_timeout
    final_block_number = initial_block_number
    while time.time() < deadline:
        final_block_number = max(
            b.blockNumber for b in validator1_node.get_blocks(5)
        )
        if final_block_number > initial_block_number:
            break
        time.sleep(5)

    assert final_block_number > initial_block_number, (
        f"Validator1 should still be creating blocks after manual propose attempt. "
        f"initial_block={initial_block_number}, final_block={final_block_number}"
    )

    # Post-check: all containers still running
    assert_containers_running(docker_client, ALL_CONTAINERS)

    # Verify no crash indicators
    logs = validator1_node.logs()
    assert "FATAL" not in logs, "Validator should not have crashed"
    assert "panic" not in logs.lower(), "Validator should not have panicked"
