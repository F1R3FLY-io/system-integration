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

Both tests share a single module-scoped custom shard with WebSocket
clients connected to boot and validator1 during startup.
"""
import dataclasses
import json
import logging
import os
import shutil
import threading
import time
from typing import Dict, Generator, List, Set, Tuple

import pytest
import websocket

from .conftest import (
    CommandLineOptions,
    VALIDATOR1_ID,
    VALIDATOR2_ID,
    _custom_port_bases,
    _force_cleanup_custom_containers,
    _custom_compose_down,
    _custom_compose_up,
    _generate_custom_compose,
    _generate_custom_genesis,
    _get_tests_dir,
    _wait_for_custom_ports_free,
    _wait_for_port_listening,
    _wait_for_port_range_free,
    _wait_for_running_state,
    CUSTOM_BOOT_CONTAINER,
    CUSTOM_PROJECT_NAME,
)

from docker import DockerClient

pytestmark = pytest.mark.xdist_group("custom")

# ── Constants ──

_WS_TEST_PORT_BASE = 40600

BLOCK_LIFECYCLE_EVENTS = {"block-created", "block-added", "block-finalised"}

BLOCK_PAYLOAD_FIELDS = {
    "block-hash", "parent-hashes", "justification-hashes",
    "deploys", "creator", "seq-num",
}

# Events receivable on a validator node. node-started is replayed from
# the startup buffer; approved-block-received and entered-running-state
# arrive live or via replay depending on connection timing.
VALIDATOR_STARTUP_EVENTS = {
    "node-started",
    "approved-block-received",
    "entered-running-state",
}

# Boot genesis ceremony events. These fire during engine_init and are
# delivered live or replayed from the startup buffer.
BOOT_GENESIS_EVENTS = {
    "sent-unapproved-block",
    "block-approval-received",
    "sent-approved-block",
}

# All boot startup events: genesis ceremony + node lifecycle + running state.
# Boot does NOT get block-created — only bonded validators propose blocks.
BOOT_STARTUP_EVENTS = BOOT_GENESIS_EVENTS | {
    "node-started",
    "entered-running-state",
}

EXPECTED_VALIDATOR_EVENTS = VALIDATOR_STARTUP_EVENTS | BLOCK_LIFECYCLE_EVENTS
EXPECTED_BOOT_EVENTS = BOOT_STARTUP_EVENTS | {"block-added", "block-finalised"}


# ── Helpers ──

def _connect_ws(ws_url: str, events: List[dict], errors: List[str],
                timeout: float = 30) -> Tuple:
    """Connect a WebSocket client with retry. Returns (ws_app, ws_thread)."""
    connected = threading.Event()

    # Disconnection errors at teardown are expected and not failures.
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
            logging.debug("WebSocket expected disconnect: %s", msg)
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
        logging.info("WebSocket not ready at %s, retrying...", ws_url)
        time.sleep(1)

    raise AssertionError(
        f"WebSocket failed to connect to {ws_url} within {timeout}s. "
        f"Errors: {errors}"
    )


def _wait_for_events(events: List[dict], required: Set[str],
                     timeout: float = 60) -> None:
    """Poll until all required event types have been seen."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        seen = {e.get("event") for e in events}
        if required <= seen:
            return
        time.sleep(1)


def _validate_block_event(event: dict) -> None:
    """Assert correct envelope and payload structure for a block event."""
    event_type = event["event"]
    assert event.get("schema-version") == 1, (
        f"{event_type} missing or wrong schema-version: {event}"
    )
    assert "payload" in event, f"{event_type} missing payload: {event}"
    payload = event["payload"]
    missing = BLOCK_PAYLOAD_FIELDS - set(payload.keys())
    assert not missing, (
        f"{event_type} payload missing fields: {sorted(missing)}. "
        f"Got: {sorted(payload.keys())}"
    )
    assert isinstance(payload["block-hash"], str) and len(payload["block-hash"]) > 0, (
        f"{event_type} has empty block-hash"
    )
    assert isinstance(payload["parent-hashes"], list), (
        f"{event_type} parent-hashes should be a list"
    )
    assert isinstance(payload["deploys"], list), (
        f"{event_type} deploys should be a list"
    )
    assert isinstance(payload["seq-num"], int), (
        f"{event_type} seq-num should be an int"
    )


def _log_event_counts(events: List[dict], label: str) -> None:
    """Log a summary of event type counts."""
    counts: Dict[str, int] = {}
    for e in events:
        t = e.get("event", "unknown")
        counts[t] = counts.get(t, 0) + 1
    logging.info(
        "%s: %d events (%s)", label, len(events),
        ", ".join(f"{t}:{counts[t]}" for t in sorted(counts)),
    )


# ── Module-scoped fixture: custom shard with early WebSocket ──

@dataclasses.dataclass
class WsShardResult:
    """Collected WebSocket events from boot and validator1."""
    boot_events: List[dict]
    boot_errors: List[str]
    v1_events: List[dict]
    v1_errors: List[str]


@pytest.fixture(scope="module")
def ws_shard(
    docker_client: DockerClient,
    command_line_options: CommandLineOptions,
) -> Generator[WsShardResult, None, None]:
    """Start a custom shard with WebSocket connected to boot and validator1.

    Connects to boot as soon as its HTTP port is listening (before
    validators start), and to validator1 as soon as its HTTP port is
    listening (before genesis completes). This captures genesis ceremony
    events on both nodes.

    Yields a WsShardResult with events from both connections.
    """
    port_bases = _custom_port_bases(_WS_TEST_PORT_BASE)
    project_name = f"{CUSTOM_PROJECT_NAME}-{_WS_TEST_PORT_BASE}"
    boot_http_port = port_bases['boot'] + 3
    v1_http_port = port_bases['validator1'] + 3
    boot_ws_url = f"ws://localhost:{boot_http_port}/ws/events"
    v1_ws_url = f"ws://localhost:{v1_http_port}/ws/events"
    timeout = command_line_options.node_startup_timeout

    bonds = [(VALIDATOR1_ID, 60), (VALIDATOR2_ID, 40)]

    boot_events: List[dict] = []
    boot_errors: List[str] = []
    v1_events: List[dict] = []
    v1_errors: List[str] = []
    genesis_dir = None
    compose_file = None
    boot_ws = None
    boot_ws_thread = None
    v1_ws = None
    v1_ws_thread = None

    try:
        genesis_dir = _generate_custom_genesis(bonds)
        compose_file = _generate_custom_compose(
            _get_tests_dir(), bonds, genesis_dir,
            ftt=0.99,
            required_signatures=1,
            global_cli_options={},
            per_node_cli_options=None,
            port_bases=port_bases,
        )

        _force_cleanup_custom_containers()
        _custom_compose_down(compose_file, project_name=project_name)
        _wait_for_custom_ports_free(len(bonds), port_bases=port_bases)
        _wait_for_port_range_free(port_bases['joiner'])

        # Phase 1: Start boot, wait for HTTP, connect WebSocket
        _custom_compose_up(compose_file, 'boot', project_name=project_name)
        logging.info("Waiting for boot HTTP port %d...", boot_http_port)
        _wait_for_port_listening('localhost', boot_http_port, timeout=120)
        logging.info("Boot HTTP port listening, connecting WebSocket...")
        boot_ws, boot_ws_thread = _connect_ws(
            boot_ws_url, boot_events, boot_errors, timeout=30,
        )
        logging.info("WebSocket connected to boot.")

        # Phase 2: Start validators
        logging.info("Starting validators...")
        _custom_compose_up(compose_file, project_name=project_name)

        # Phase 3: Connect WebSocket to validator1 as early as possible
        logging.info("Waiting for validator1 HTTP port %d...", v1_http_port)
        _wait_for_port_listening('localhost', v1_http_port, timeout=120)
        logging.info("Validator1 HTTP port listening, connecting WebSocket...")
        v1_ws, v1_ws_thread = _connect_ws(
            v1_ws_url, v1_events, v1_errors, timeout=30,
        )
        logging.info("WebSocket connected to validator1.")

        # Phase 4: Wait for all nodes to reach Running state
        container_names = [CUSTOM_BOOT_CONTAINER]
        for idx in range(len(bonds)):
            container_names.append(f"rnode.custom.validator{idx + 1}")

        for i, name in enumerate(container_names):
            volume_name = 'boot-data' if i == 0 else f'validator{i}-data'
            _wait_for_running_state(docker_client, name, timeout,
                                    data_volume=volume_name)
        logging.info("All nodes in Running state.")

        # Phase 5: Wait for expected events on both connections
        _wait_for_events(v1_events, EXPECTED_VALIDATOR_EVENTS, timeout=60)
        _wait_for_events(boot_events, EXPECTED_BOOT_EVENTS, timeout=30)

        # Clear transient errors from connection retries during startup.
        boot_errors.clear()
        v1_errors.clear()

        yield WsShardResult(
            boot_events=boot_events,
            boot_errors=boot_errors,
            v1_events=v1_events,
            v1_errors=v1_errors,
        )

    finally:
        if boot_ws:
            boot_ws.close()
        if boot_ws_thread:
            boot_ws_thread.join(timeout=5)
        if v1_ws:
            v1_ws.close()
        if v1_ws_thread:
            v1_ws_thread.join(timeout=5)
        if compose_file:
            _custom_compose_down(compose_file, project_name=project_name)
        _wait_for_custom_ports_free(len(bonds), port_bases=port_bases)
        _wait_for_port_range_free(port_bases['joiner'])
        if genesis_dir and os.path.exists(genesis_dir):
            shutil.rmtree(genesis_dir, ignore_errors=True)
        if compose_file and os.path.exists(compose_file):
            os.unlink(compose_file)


# ── Tests ──

def test_block_events(ws_shard: WsShardResult) -> None:
    """All 3 block lifecycle events are received with correct structure.

    Uses the validator1 WebSocket connection. Verifies:
    1. The 'started' handshake event was received
    2. At least one of each: block-created, block-added, block-finalised
    3. Correct envelope (schema-version=1, payload) and payload fields
       for every block event
    """
    events = ws_shard.v1_events
    errors = ws_shard.v1_errors

    assert not errors, f"WebSocket errors: {errors}"

    seen = {e.get("event") for e in events}

    assert "started" in seen, (
        f"Missing 'started' handshake. Received: {sorted(seen)}"
    )

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
    """Validator receives all startup events (live + replayed from buffer).

    Verifies validator1 receives:
    1. node-started — replayed from startup buffer (fires before WS ready)
    2. approved-block-received — the approved genesis block from boot
    3. entered-running-state — engine transitions to Running
    """
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
            assert event.get("schema-version") == 1, (
                f"{event_type} wrong schema-version: {event}"
            )
            assert "payload" in event, (
                f"{event_type} missing payload: {event}"
            )
            assert "block-hash" in event["payload"], (
                f"{event_type} missing block-hash: {event['payload']}"
            )
            assert isinstance(event["payload"]["block-hash"], str), (
                f"{event_type} block-hash should be string"
            )
            assert len(event["payload"]["block-hash"]) > 0, (
                f"{event_type} has empty block-hash"
            )

        elif event_type == "node-started":
            assert event.get("schema-version") == 1, (
                f"node-started wrong schema-version: {event}"
            )
            assert "payload" in event, (
                f"node-started missing payload: {event}"
            )
            assert "address" in event["payload"], (
                f"node-started missing address: {event['payload']}"
            )

    _log_event_counts(events, "Startup events test passed (validator1)")


def test_startup_events_boot(ws_shard: WsShardResult) -> None:
    """Boot node receives all genesis ceremony events via startup replay.

    The node buffers events during startup and replays them to WebSocket
    clients on connect. Boot's WebSocket connects before validators start,
    so it receives:
    1. node-started — replayed from buffer
    2. sent-unapproved-block — boot broadcasts candidate to validators
    3. block-approval-received — boot receives approval (has sender field)
    4. sent-approved-block — boot broadcasts the approved genesis block
    5. entered-running-state — engine transitions to Running
    6. block-added, block-finalised — from heartbeat after Running
    """
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
            assert event.get("schema-version") == 1, (
                f"{event_type} wrong schema-version: {event}"
            )
            assert "payload" in event, (
                f"{event_type} missing payload: {event}"
            )
            assert "block-hash" in event["payload"], (
                f"{event_type} missing block-hash: {event['payload']}"
            )

        elif event_type == "block-approval-received":
            assert event.get("schema-version") == 1, (
                f"{event_type} wrong schema-version: {event}"
            )
            assert "payload" in event, (
                f"{event_type} missing payload: {event}"
            )
            payload = event["payload"]
            assert "block-hash" in payload, (
                f"{event_type} missing block-hash: {payload}"
            )
            assert "sender" in payload, (
                f"{event_type} missing sender: {payload}"
            )

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
