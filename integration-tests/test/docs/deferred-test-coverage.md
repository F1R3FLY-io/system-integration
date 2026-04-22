# Deferred Test Coverage

Tests not implemented in the current pass. Low priority or requires additional infrastructure.

## HTTP Endpoints

| Endpoint | Reason deferred |
|---|---|
| `POST /api/explore-deploy-by-block-hash` | gRPC equivalent tested widely. HTTP is a thin wrapper |
| `POST /api/data-at-name-by-block-hash` | gRPC equivalent tested (`test_get_data_at_name_empty_payload`). HTTP requires Par JSON construction |
| `POST /api/propose` (admin port 40405) | gRPC `propose` tested widely via `node.propose()`. Admin port adds test infrastructure complexity |

## gRPC Methods

| Method | Reason deferred |
|---|---|
| `showMainChain` | Niche streaming method. Block list endpoints cover the same data |
| `listenForContinuationAtName` | Niche. No HTTP equivalent. Used for debugging RSpace continuations |
| `previewPrivateNames` | Niche. Used by clients computing signatures before deploying |
| `getEventByHash` | Debugging/auditing. Returns raw COMM/produce/consume events |
| `visualizeDag` | Visualization. Returns DOT format. No functional impact |
| `machineVerifiableDag` | Machine-parseable DAG. No functional impact |
| `proposeResult` | Tested implicitly via propose flow. Explicit test low value |
| `getBlocksByHeights` (gRPC) | HTTP equivalent tested. No `node.py` wrapper exists for gRPC path |

## WebSocket Events

All 10 event types are now tested. No deferred WebSocket coverage.

## Known Flaky Tests

### WebSocket startup event tests (test_websocket.py)

`test_startup_events_validator`, `test_startup_events_boot`, `test_startup_events_readonly` intermittently fail when run alongside other test suites (shared shard tests). Genesis ceremony events (`approved-block-received`, `sent-unapproved-block`, `sent-approved-block`) are not consistently captured by the WebSocket client even though the startup buffer should replay them.

**Root cause:** The startup buffer replay mechanism in `events_info.rs` replays buffered events on WebSocket connect, but some genesis ceremony events are intermittently missing from the buffer or lost during replay. Needs investigation into buffer seal timing vs event publish timing in the genesis ceremony path.

**Workaround:** These tests pass reliably when run in isolation (`pytest test_websocket.py`).

### Validator startup timeout

Validators occasionally fail to reach Running state within 90s when Docker is under resource pressure (multiple shards starting concurrently or sequentially). The genesis ceremony requires P2P discovery + approval exchange across all validators, which can take 2-3 approval cycles (10s each) plus genesis block creation (20-30s). Now mitigated by `isReady` API polling (replaces log parsing) which is more reliable, but the underlying genesis ceremony duration under load remains.
