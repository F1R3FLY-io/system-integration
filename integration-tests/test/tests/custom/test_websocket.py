"""WebSocket event streaming integration tests.

Tests the /ws/events WebSocket endpoint on F1R3FLY nodes.

The F1r3flyEvent enum defines 9 event types:

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

import json
import logging
import threading
import time
from typing import Dict, List

import pytest
import websocket

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")


# ── Constants ──

BLOCK_LIFECYCLE_EVENTS = {"block-created", "block-added", "block-finalised"}

BLOCK_PAYLOAD_FIELDS = {
    "block-hash", "parent-hashes", "justification-hashes",
    "deploys", "creator", "seq-num",
}

VALIDATOR_STARTUP_EVENTS = {
    "node-started",
    "approved-block-received",
    "entered-running-state",
}

BOOT_GENESIS_EVENTS = {
    "sent-unapproved-block",
    "block-approval-received",
    "sent-approved-block",
}

BOOT_STARTUP_EVENTS = BOOT_GENESIS_EVENTS | {
    "node-started",
    "entered-running-state",
}

EXPECTED_VALIDATOR_EVENTS = VALIDATOR_STARTUP_EVENTS | BLOCK_LIFECYCLE_EVENTS
EXPECTED_BOOT_EVENTS = BOOT_STARTUP_EVENTS | {"block-added", "block-finalised"}


# ── Helpers ──

def _connect_ws(ws_url, events, errors, timeout=30):
    """Connect a WebSocket client with retry. Returns (ws_app, ws_thread)."""
    connected = threading.Event()

    _EXPECTED_DISCONNECT_ERRORS = (
        "Connection to remote host was lost",
        "Connection reset by peer",
        "Connection refused",
    )

    def on_message(ws, message):
        try:
            event = json.loads(message)
            events.append(event)
            logging.info("WS event: %s", event.get("event", "unknown"))
        except json.JSONDecodeError as e:
            errors.append(f"Bad JSON: {e}")

    def on_error(ws, error):
        msg = str(error)
        if any(expected in msg for expected in _EXPECTED_DISCONNECT_ERRORS):
            return
        errors.append(msg)

    def on_open(ws):
        logging.info("WebSocket connected to %s", ws_url)
        connected.set()

    deadline = time.time() + timeout
    ws_app = None
    ws_thread = None

    while time.time() < deadline:
        errors.clear()
        connected.clear()
        ws_app = websocket.WebSocketApp(
            ws_url,
            on_message=on_message,
            on_error=on_error,
            on_open=on_open,
        )
        ws_thread = threading.Thread(target=ws_app.run_forever, daemon=True)
        ws_thread.start()

        if connected.wait(timeout=5):
            return ws_app, ws_thread

        ws_app.close()
        ws_thread.join(timeout=3)
        ws_app = None
        ws_thread = None
        time.sleep(1)

    raise AssertionError(
        f"WebSocket failed to connect to {ws_url} within {timeout}s. "
        f"Errors: {errors}"
    )


def _wait_for_events(events, required, timeout=60):
    """Poll until all required event types have been seen."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        seen = {e.get("event") for e in events}
        if required <= seen:
            return
        time.sleep(1)


def _validate_block_event(event):
    event_type = event["event"]
    assert event.get("schema-version") == 1, (
        f"{event_type} missing or wrong schema-version: {event}"
    )
    assert "payload" in event, f"{event_type} missing payload: {event}"
    payload = event["payload"]
    missing = BLOCK_PAYLOAD_FIELDS - set(payload.keys())
    assert not missing, (
        f"{event_type} payload missing fields: {sorted(missing)}"
    )
    assert isinstance(payload["block-hash"], str) and len(payload["block-hash"]) > 0
    assert isinstance(payload["parent-hashes"], list)
    assert isinstance(payload["deploys"], list)
    assert isinstance(payload["seq-num"], int)


def _log_event_counts(events, label):
    counts: Dict[str, int] = {}
    for e in events:
        t = e.get("event", "unknown")
        counts[t] = counts.get(t, 0) + 1
    logging.info(
        "%s: %d events (%s)", label, len(events),
        ", ".join(f"{t}:{counts[t]}" for t in sorted(counts)),
    )


# ── Module-scoped fixture ──

class WsShardResult:
    def __init__(self):
        self.boot_events: List[dict] = []
        self.boot_errors: List[str] = []
        self.v1_events: List[dict] = []
        self.v1_errors: List[str] = []


@pytest.fixture(scope="module")
def ws_shard(provider, timeouts):
    """Start a shard and connect WebSocket clients to boot and validator1.

    The shard is created normally via Shard.create(). After all nodes
    reach Running state, WebSocket clients connect and receive buffered
    startup events via the node's replay mechanism.

    Yields a WsShardResult with events from both connections.
    """
    config = ShardConfig(
        bonds=[(VALIDATOR1_ID, 60), (VALIDATOR2_ID, 40)],
        heartbeat=True,
    )
    shard = Shard.create(provider, config, timeouts)
    result = WsShardResult()
    boot_ws = None
    boot_ws_thread = None
    v1_ws = None
    v1_ws_thread = None

    try:
        boot = shard.boot
        v1 = shard.node("validator1")

        boot_ws_url = f"ws://{boot.grpc_host}:{boot.http_port}/ws/events"
        v1_ws_url = f"ws://{v1.grpc_host}:{v1.http_port}/ws/events"

        # Connect WebSocket to boot (receives buffered genesis ceremony events)
        logging.info("Connecting WebSocket to boot at %s...", boot_ws_url)
        boot_ws, boot_ws_thread = _connect_ws(
            boot_ws_url, result.boot_events, result.boot_errors, timeout=timeouts.command,
        )

        # Connect WebSocket to validator1
        logging.info("Connecting WebSocket to validator1 at %s...", v1_ws_url)
        v1_ws, v1_ws_thread = _connect_ws(
            v1_ws_url, result.v1_events, result.v1_errors, timeout=timeouts.command,
        )

        # Wait for expected events on both connections
        _wait_for_events(result.v1_events, EXPECTED_VALIDATOR_EVENTS, timeout=timeouts.finalization)
        _wait_for_events(result.boot_events, EXPECTED_BOOT_EVENTS, timeout=timeouts.command)

        # Clear transient errors from connection retries
        result.boot_errors.clear()
        result.v1_errors.clear()

        yield result

    finally:
        if boot_ws:
            boot_ws.close()
        if boot_ws_thread:
            boot_ws_thread.join(timeout=5)
        if v1_ws:
            v1_ws.close()
        if v1_ws_thread:
            v1_ws_thread.join(timeout=5)
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
            _validate_block_event(event)

    _log_event_counts(events, "Block events test passed (validator1)")


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

    _log_event_counts(events, "Startup events test passed (validator1)")


def test_startup_events_boot(ws_shard: WsShardResult) -> None:
    """Boot node receives all genesis ceremony events via startup replay."""
    events = ws_shard.boot_events
    errors = ws_shard.boot_errors

    assert not errors, f"WebSocket errors: {errors}"

    seen = {e.get("event") for e in events}
    missing = EXPECTED_BOOT_EVENTS - seen
    assert not missing, (
        f"Missing boot events: {sorted(missing)}. "
        f"Received: {sorted(seen)} ({len(events)} total)"
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
            _validate_block_event(event)

    _log_event_counts(events, "Startup events test passed (boot)")
