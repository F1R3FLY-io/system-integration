"""WebSocket event streaming integration tests.

Tests the /ws/events WebSocket endpoint on F1R3FLY nodes.

The F1r3flyEvent enum defines 10 event types:

  Block lifecycle (fired continuously by heartbeat):
    - block-created      proposer built a block (before validation)
    - block-added        block validated and added to DAG
    - block-finalised    block finalized by the finalizer

  Genesis ceremony (fired once during startup):
    - sent-unapproved-block     boot broadcasts candidate to validators
    - block-approval-received   boot receives approval from a validator
    - sent-approved-block       boot broadcasts approved genesis block
    - approved-block-received   validator receives the approved block
    - entered-running-state     engine transitions to Running

  Node lifecycle:
    - node-started              HTTP server is ready

Genesis ceremony and node lifecycle events fire before any WebSocket
client can connect. The node buffers these startup events and replays
them to new WebSocket subscribers on connect, so all 9 event types are
receivable.

Both tests share a single module-scoped shard with WebSocket clients
connected during startup.
"""

import logging
import time
from typing import List

import pytest
from f1r3fly.websocket import (
    BLOCK_LIFECYCLE_EVENTS,
    EXPECTED_BOOT_EVENTS,
    EXPECTED_VALIDATOR_EVENTS,
    VALIDATOR_STARTUP_EVENTS,
    connect_ws,
    log_event_counts,
    validate_block_event,
    wait_for_events,
)

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID
from ...infra.node import Node
from ...infra.polling import wait_for_deploy_included
from ...infra.shard import Shard
from ...infra.types import NodeRole

pytestmark = pytest.mark.xdist_group("custom")


# ── Module-scoped fixture ──


class WsShardResult:
    def __init__(self):
        self.boot_events: List[dict] = []
        self.boot_errors: List[str] = []
        self.v1_events: List[dict] = []
        self.v1_errors: List[str] = []
        self.ro_events: List[dict] = []
        self.ro_errors: List[str] = []


@pytest.fixture(scope="module")
def ws_shard(provider, timeouts):
    """Start a shard and connect WebSocket clients BEFORE nodes reach Running.

    The shard containers are started with wait_running=False, then WS
    clients connect to the HTTP ports as soon as they're listening.
    This allows the clients to receive genesis ceremony and startup
    events live (not just replayed from buffer).

    After WS connects, we wait for Running state and expected events.
    """
    from ...infra.polling import wait_for_node_running

    config = ShardConfig(
        bonds=[(VALIDATOR1_ID, 60), (VALIDATOR2_ID, 40)],
        heartbeat=True,
        include_readonly=True,
    )

    # Start containers WITHOUT waiting for Running state
    handles = provider.create_shard(config, wait_running=False)
    shard = Shard(
        provider=provider,
        handles=handles,
        config=config,
        timeouts=timeouts,
    )

    result = WsShardResult()
    boot_ws = None
    boot_ws_thread = None
    v1_ws = None
    v1_ws_thread = None
    ro_ws = None
    ro_ws_thread = None

    try:
        boot = shard.boot
        v1 = shard.node("validator1")
        ro = shard.readonly

        # Connect WebSocket clients early — before Running state.
        # connect_ws retries until the HTTP port is listening, so it
        # handles the startup delay automatically.
        logging.info("Connecting WebSocket to boot at %s (pre-Running)...", boot.ws_url)
        boot_ws, boot_ws_thread = connect_ws(
            boot.ws_url,
            result.boot_events,
            result.boot_errors,
            timeout=timeouts.node_startup,
        )

        logging.info("Connecting WebSocket to validator1 at %s (pre-Running)...", v1.ws_url)
        v1_ws, v1_ws_thread = connect_ws(
            v1.ws_url,
            result.v1_events,
            result.v1_errors,
            timeout=timeouts.node_startup,
        )

        logging.info("Connecting WebSocket to readonly at %s (pre-Running)...", ro.ws_url)
        ro_ws, ro_ws_thread = connect_ws(
            ro.ws_url,
            result.ro_events,
            result.ro_errors,
            timeout=timeouts.node_startup,
        )

        # Now wait for all nodes to reach Running state
        for handle in handles:
            wait_for_node_running(
                get_logs=handle.logs,
                is_running=handle.is_running,
                node_name=handle.name,
                timeout=timeouts.node_startup,
                status_url=f"http://{handle.grpc_host}:{handle.ports.http}/api/status",
            )

        # Wait for expected events on all connections
        wait_for_events(result.v1_events, EXPECTED_VALIDATOR_EVENTS, timeout=timeouts.finalization)
        wait_for_events(result.boot_events, EXPECTED_BOOT_EVENTS, timeout=timeouts.command)
        wait_for_events(result.ro_events, EXPECTED_VALIDATOR_EVENTS, timeout=timeouts.finalization)

        # Clear transient errors from connection retries
        result.boot_errors.clear()
        result.v1_errors.clear()
        result.ro_errors.clear()

        yield result

    finally:
        if boot_ws:
            boot_ws.close()
        if boot_ws_thread:
            boot_ws_thread.join(timeout=timeouts.custom(5))
        if v1_ws:
            v1_ws.close()
        if v1_ws_thread:
            v1_ws_thread.join(timeout=timeouts.custom(5))
        if ro_ws:
            ro_ws.close()
        if ro_ws_thread:
            ro_ws_thread.join(timeout=timeouts.custom(5))
        shard.destroy()


# ── Tests ──


def test_block_events(ws_shard: WsShardResult) -> None:
    """All 3 block lifecycle events are received with correct structure."""
    events = ws_shard.v1_events
    errors = ws_shard.v1_errors

    assert not errors, f"WebSocket errors: {errors}"

    seen = {e.get("event") for e in events}
    assert "started" in seen, f"Missing 'started' handshake. Received: {sorted(seen)}"

    missing = BLOCK_LIFECYCLE_EVENTS - seen
    assert not missing, (
        f"Missing block event types: {sorted(missing)}. "
        f"Received: {sorted(seen)} ({len(events)} total)"
    )

    for event in events:
        event_type = event.get("event")
        if event_type == "started":
            assert event.get("schema-version") == 1
        elif event_type in BLOCK_LIFECYCLE_EVENTS:
            validate_block_event(event)

    log_event_counts(events, "Block events test passed (validator1)")


def test_startup_events_validator(ws_shard: WsShardResult) -> None:
    """Validator receives all startup events (live + replayed from buffer)."""
    events = ws_shard.v1_events
    errors = ws_shard.v1_errors

    assert not errors, f"WebSocket errors: {errors}"

    seen = {e.get("event") for e in events}
    missing = VALIDATOR_STARTUP_EVENTS - seen
    assert not missing, (
        f"Missing validator startup events: {sorted(missing)}. "
        f"Received: {sorted(seen)} ({len(events)} total)"
    )

    for event in events:
        event_type = event.get("event")
        if event_type in ("approved-block-received", "entered-running-state"):
            assert event.get("schema-version") == 1
            assert "payload" in event
            assert "block-hash" in event["payload"]
            assert isinstance(event["payload"]["block-hash"], str)
            assert len(event["payload"]["block-hash"]) > 0
        elif event_type == "node-started":
            assert event.get("schema-version") == 1
            assert "payload" in event
            assert "address" in event["payload"]

    log_event_counts(events, "Startup events test passed (validator1)")


def test_startup_events_boot(ws_shard: WsShardResult) -> None:
    """Boot node receives all genesis ceremony events via startup replay."""
    events = ws_shard.boot_events
    errors = ws_shard.boot_errors

    assert not errors, f"WebSocket errors: {errors}"

    seen = {e.get("event") for e in events}
    missing = EXPECTED_BOOT_EVENTS - seen
    assert not missing, (
        f"Missing boot events: {sorted(missing)}. Received: {sorted(seen)} ({len(events)} total)"
    )

    for event in events:
        event_type = event.get("event")
        if event_type in ("sent-unapproved-block", "sent-approved-block"):
            assert event.get("schema-version") == 1
            assert "payload" in event
            assert "block-hash" in event["payload"]
        elif event_type == "block-approval-received":
            assert event.get("schema-version") == 1
            assert "payload" in event
            payload = event["payload"]
            assert "block-hash" in payload
            assert "sender" in payload
        elif event_type == "entered-running-state":
            assert event.get("schema-version") == 1
            assert "payload" in event
            assert "block-hash" in event["payload"]
        elif event_type == "node-started":
            assert event.get("schema-version") == 1
            assert "payload" in event
            assert "address" in event["payload"]
        elif event_type in BLOCK_LIFECYCLE_EVENTS:
            validate_block_event(event)

    log_event_counts(events, "Startup events test passed (boot)")


def test_startup_events_readonly(ws_shard: WsShardResult) -> None:
    """Readonly node receives block lifecycle events (except block-created).

    Readonly doesn't propose blocks, so it doesn't emit block-created.
    It receives block-added and block-finalised from validators.
    """
    events = ws_shard.ro_events
    errors = ws_shard.ro_errors

    assert not errors, f"WebSocket errors: {errors}"

    # Readonly receives block-added/block-finalised but NOT block-created
    readonly_expected = {
        "node-started",
        "entered-running-state",
        "approved-block-received",
        "block-added",
        "block-finalised",
    }

    seen = {e.get("event") for e in events}
    missing = readonly_expected - seen
    assert not missing, (
        f"Missing readonly events: {sorted(missing)}. "
        f"Received: {sorted(seen)} ({len(events)} total)"
    )

    # block-created should NOT be in readonly events
    assert "block-created" not in seen, (
        "Readonly should not receive block-created events (it doesn't propose)"
    )

    for event in events:
        event_type = event.get("event")
        if event_type in BLOCK_LIFECYCLE_EVENTS:
            validate_block_event(event)

    log_event_counts(events, "Startup events test passed (readonly)")


def test_deploy_appears_in_block_event(ws_shard: WsShardResult, provider, timeouts) -> None:
    """A deploy submitted after WS connect appears in a block-created event's deploys list."""
    events = ws_shard.v1_events
    errors = ws_shard.v1_errors

    events_before = len(events)

    v1_handle = None
    for handle in provider.active_handles:
        if "validator1" in handle.name:
            v1_handle = handle
            break
    assert v1_handle is not None, "Could not find validator1 in active handles"

    v1 = Node(handle=v1_handle, role=NodeRole.VALIDATOR)

    deploy_id = v1.deploy_string(
        '@"ws-deploy-test"!(42)',
        VALIDATOR1_ID.private_key(),
    )
    logging.info("Deployed for WS test, deploy_id=%s", deploy_id[:24])

    block_info = wait_for_deploy_included(v1, deploy_id, timeouts.deploy_inclusion)
    logging.info("Deploy included in block #%d", block_info.blockNumber)

    v1_pubkey = VALIDATOR1_ID.private_key().get_public_key().to_hex()

    deadline = time.time() + timeouts.finalization
    found_deploy_info = None
    found_event_type = None
    found_block_hash = None
    while time.time() < deadline:
        for event in events[events_before:]:
            event_type = event.get("event")
            if event_type in ("block-created", "block-added"):
                payload = event.get("payload", {})
                deploys = payload.get("deploys", [])
                # deploys is a list of dicts with "id", "cost", "deployer", "errored"
                for d in deploys:
                    if isinstance(d, dict) and d.get("id") == deploy_id:
                        found_deploy_info = d
                        found_event_type = event_type
                        found_block_hash = payload.get("block-hash", "?")
                        break
                if found_deploy_info:
                    break
        if found_deploy_info:
            break
        time.sleep(1)

    assert found_deploy_info is not None, (
        f"Deploy {deploy_id[:24]} not found in any block-created/block-added "
        f"event within {timeouts.finalization}s. "
        f"New events received: {len(events) - events_before}"
    )

    logging.info(
        "Deploy %s found in %s event (block %s)",
        deploy_id[:24],
        found_event_type,
        found_block_hash[:16],
    )

    # Verify deploy fields
    assert found_deploy_info["id"] == deploy_id, (
        f"Deploy ID mismatch: {found_deploy_info['id'][:24]} != {deploy_id[:24]}"
    )
    assert isinstance(found_deploy_info["cost"], int) and found_deploy_info["cost"] >= 0, (
        f"Deploy cost should be non-negative int, got {found_deploy_info.get('cost')}"
    )
    assert found_deploy_info["deployer"] == v1_pubkey, (
        f"Deploy deployer '{found_deploy_info.get('deployer', '')[:24]}' != "
        f"expected '{v1_pubkey[:24]}'"
    )
    assert found_deploy_info["errored"] is False, (
        f"Deploy should not be errored, got errored={found_deploy_info.get('errored')}"
    )

    # Transfers should be omitted on block-created/block-added events
    # (transfer extraction hasn't happened yet)
    if found_event_type in ("block-created", "block-added"):
        assert "transfers" not in found_deploy_info, (
            f"Deploy in {found_event_type} should not have transfers field"
        )

    logging.info(
        "Deploy fields verified: cost=%d, deployer=%s, errored=%s",
        found_deploy_info["cost"],
        found_deploy_info["deployer"][:16],
        found_deploy_info["errored"],
    )

    assert not errors, f"WebSocket errors during deploy test: {errors}"
    v1.close()


def test_transfers_available_event(ws_shard: WsShardResult, provider, timeouts) -> None:
    """TransfersAvailable event fires on readonly after transfer deploy finalization."""
    ro_events = ws_shard.ro_events
    events_before = len(ro_events)

    # Get a validator Node for the transfer
    v1_handle = None
    for handle in provider.active_handles:
        if "validator1" in handle.name:
            v1_handle = handle
            break
    assert v1_handle is not None, "Could not find validator1 in active handles"

    v1 = Node(handle=v1_handle, role=NodeRole.VALIDATOR)
    v1_key = VALIDATOR1_ID.private_key()
    v1_vault = v1_key.get_public_key().get_vault_address()
    v2_vault = VALIDATOR2_ID.private_key().get_public_key().get_vault_address()

    # Submit a transfer deploy
    deploy_id = v1.vault.transfer_ensure(
        v1_vault,
        v2_vault,
        1_000_000,
        v1_key,
    )
    logging.info("Transfer deploy submitted: %s", deploy_id[:24])

    block_info = wait_for_deploy_included(v1, deploy_id, timeouts.deploy_inclusion)
    logging.info(
        "Transfer included in block #%d (%s)", block_info.blockNumber, block_info.blockHash[:16]
    )

    # Wait for transfers-available event on readonly
    deadline = time.time() + timeouts.finalization * 2
    found_event = None
    while time.time() < deadline:
        for event in ro_events[events_before:]:
            if event.get("event") == "transfers-available":
                payload = event.get("payload", {})
                if payload.get("block-hash") == block_info.blockHash:
                    found_event = event
                    break
        if found_event:
            break
        time.sleep(1)

    assert found_event is not None, (
        f"transfers-available event not received on readonly within "
        f"{timeouts.finalization * 2}s for block {block_info.blockHash[:16]}. "
        f"New events: {len(ro_events) - events_before}"
    )

    payload = found_event["payload"]
    assert payload["block-hash"] == block_info.blockHash
    assert isinstance(payload["block-number"], int) and payload["block-number"] >= 0
    assert isinstance(payload["deploys"], list) and len(payload["deploys"]) > 0, (
        "transfers-available should have at least 1 deploy with transfers"
    )

    # Verify deploy transfer structure
    for deploy_transfers in payload["deploys"]:
        assert "deploy-id" in deploy_transfers, "missing deploy-id"
        assert "transfers" in deploy_transfers, "missing transfers"
        assert isinstance(deploy_transfers["transfers"], list)

    logging.info(
        "transfers-available event verified: block %s, %d deploys with transfers",
        payload["block-hash"][:16],
        len(payload["deploys"]),
    )
    v1.close()
