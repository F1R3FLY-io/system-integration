---
description: Autonomous and robust workflow for large file upload tests and AI debugging
---

# 1. Pre-Flight Health Check
Verify that all nodes (Bootstrap, Validators, Observer) are fully responsive and forming a healthy network before running the long 10+ hour test.

// turbo
```bash
cd services/rust-client && cargo run -- network-health
```

# 2. Clean Environment
Make sure no docker-compose or previous python test process is running. Stop the shard if it's running, and clean all leftover volumes to start from a pristine state.

// turbo-all
```bash
docker-compose down -v || true
docker rm -f $(docker ps -aq) 2>/dev/null || true
docker volume rm $(docker volume ls -q) 2>/dev/null || true
pkill -f pytest || true
```

# 3. Run Test & Route Logs
Execute the streaming test and route its output directly to `tests.logs` to keep the terminal clean and parsing simple. 
*(Optional AI Agent Step)*: Run `cargo run -- watch-blocks` in a separate background loop to monitor for finalized and errored blocks in real-time, allowing you to abort early if `errored` deploys appear.

// turbo
```bash
poetry run pytest integration-tests/test/test_file_upload.py::TestFileUploadE2E::test_large_file_upload_streaming -s -v --timeout=40000 > tests.logs 2>&1
```

# 4. Immediate Check & AI Variable Extraction
Extract the Deploy ID automatically from the pytest log for the subsequent node checks. This allows AI agents to fully automate the Node State verification pipeline.

// turbo-all
```bash
echo "Looking for [LARGE-FILE-TEST] Deploy ID in tests.logs..."
grep "\[LARGE-FILE-TEST\] 6GB file streaming upload finished. Deploy ID:" tests.logs
export DEPLOY_ID=$(grep "\[LARGE-FILE-TEST\] 6GB file streaming upload finished. Deploy ID:" tests.logs | awk '{print $NF}' | tail -n 1)
echo "Extracted DEPLOY_ID: $DEPLOY_ID"
```

# 5. AI Debugging Playbook: Node State Verification
If the test fails, timeouts, or hits a roadblock, the AI agent must run the following checks across the shard. Using the extracted `$DEPLOY_ID`, see exactly which nodes received the deploy and generated a `<BLOCK_HASH>`. Replace `<BLOCK_HASH>` in the lines below when checking finalization.

```bash
cd services/rust-client

# --- BOOTSTRAP NODE (Port 40400/40403) ---
cargo run -- get-deploy -d $DEPLOY_ID -H localhost --http-port 40403
cargo run -- blocks --block-hash <BLOCK_HASH> -H localhost -p 40403
cargo run -- is-finalized -b <BLOCK_HASH> -H localhost -p 40402

# --- VALIDATOR 1 (Port 40410/40413) ---
cargo run -- get-deploy -d $DEPLOY_ID -H localhost --http-port 40413
cargo run -- blocks --block-hash <BLOCK_HASH> -H localhost -p 40413
cargo run -- is-finalized -b <BLOCK_HASH> -H localhost -p 40412

# --- VALIDATOR 2 (Port 40420/40423) ---
cargo run -- get-deploy -d $DEPLOY_ID -H localhost --http-port 40423
cargo run -- blocks --block-hash <BLOCK_HASH> -H localhost -p 40423
cargo run -- is-finalized -b <BLOCK_HASH> -H localhost -p 40422

# --- VALIDATOR 3 (Port 40430/40433) ---
cargo run -- get-deploy -d $DEPLOY_ID -H localhost --http-port 40433
cargo run -- blocks --block-hash <BLOCK_HASH> -H localhost -p 40433
cargo run -- is-finalized -b <BLOCK_HASH> -H localhost -p 40432

# --- OBSERVER (Port 40450/40453) ---
cargo run -- get-deploy -d $DEPLOY_ID -H localhost --http-port 40453
cargo run -- blocks --block-hash <BLOCK_HASH> -H localhost -p 40453
cargo run -- is-finalized -b <BLOCK_HASH> -H localhost -p 40452
```

# 6. Deep Dive: Parsing Core Scala Node Logs
If the `rust-client` indicates a replication inconsistency (e.g., Deploy reached `Validator 1` but not the `Observer`), grep the lagging node's log bundle (or `tests.logs`/docker compose output).

**Fast trace:**
```bash
grep "[LARGE-FILE-TEST]" tests.logs
```

**Stale Issue / Deep Trace Heuristics:**
If this is a stale issue, or you need to see exactly where the file processing halted within the node's core subsystems, parse the specific node's output using this comprehensive grep:
// turbo
```bash
grep -E "FileRequester|FileReplicationSetup|TransportLayer|FileUploadAPI|DeployGrpcServiceV1|\[LARGE-FILE-TEST\]" node.log 
```
*(Replace `node.log` with `tests.logs` if running combined docker-compose logging.)*

# 7. Expanding Context via Scala Node Modification
If the above heuristics don't identify the cause:
1. Document which subsystem failed and explain why.
2. Edit `./services/f1r3node` Scala code to print more `log.debug` info. **IMPORTANT**: force developers to use the specific prefix `[LARGE-FILE-TEST]` so you can easily grep it later.
3. Build the updated node code from the root repo folder:
// turbo
```bash
sc build f1r3node
```
4. Repeat the entire workflow to capture the new trace context.
