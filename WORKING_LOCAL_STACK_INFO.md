# Working Local Stack Information

> Generated from live system state on 2026-01-22. All data sourced from `docker ps`, `docker inspect`, `ps aux`, `lsof`, and the dev-env introspect server.

## Architecture Overview

```
                    F1R3Sky Frontend (Expo Web)
                         :19006
                           |
                    connects to embers API
                           |
                           v
              Embers API (Rust) [::1]:8080
              /                          \
             v                            v
   Mainnet Shard (Docker)         Testnet Shard (Docker)
   Validator :14401/02/03         Validator :15401/02/03
   Read-only :14413               Read-only :15413
                           |
              F1R3Sky Backend Dev-Env (Node.js in-process)
              PLC :2582 | PDS :2583 | BSKY :2584 | Ozone :2587
              Introspect :2581 | DataPlane + BSYNC (dynamic)
                           |
              PostgreSQL :5433 (Docker)
              Redis :6380 (Docker)
```

## Execution Paths

Visual representation of how each service starts, from the initial user command through every intermediate step to the final running process.

### Shard (F1R3Node — 4 containers)

```
Terminal 1:
┌─────────────────────────────────────────────────────────────────────────────┐
│  cd services/embers/docker                                                  │
│  docker compose up                                                          │
└─────────────┬───────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Docker Compose reads: docker-compose.yaml                                  │
│  Project name: "docker"                                                     │
│  Creates network: docker_default (bridge)                                   │
└─────────────┬───────────────────────────────────────────────────────────────┘
              │
              ├──── Pulls image: f1r3flyindustries/f1r3fly-scala-node:latest
              │     (if not cached locally — 2.46 GB, arm64)
              │
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Creates 4 containers in parallel:                                          │
│                                                                             │
│  ┌─── docker-firefly-1 (mainnet validator) ────────────────────────────┐    │
│  │  Mounts: mainnet/genesis, certs/node.key.pem, certs/node.cert.pem  │    │
│  │  Ports: 14401→40401, 14402→40402, 14403→40403                      │    │
│  │  Entrypoint: /opt/docker/bin/rnode --profile=docker                 │    │
│  │  Command: run -s --validator-private-key=6a786ec3...                 │    │
│  │           --host=firefly --synchrony-constraint-threshold=0.0       │    │
│  │                                                                     │    │
│  │  ┌─ Startup sequence (inside container): ──────────────────────┐    │    │
│  │  │  1. JVM starts (GraalVM 22 CE, Java 17)                    │    │    │
│  │  │  2. Loads RChain Node 1.0.0-SNAPSHOT                       │    │    │
│  │  │  3. Builds in-memory blockMetadataStore                    │    │    │
│  │  │  4. Checks Ollama (AI) service → disabled                  │    │    │
│  │  │  5. No approved block found → starts bootstrap ceremony    │    │    │
│  │  │  6. Parses genesis/wallets.txt (3 wallets, 50Q each)       │    │    │
│  │  │  7. Parses genesis/bonds.txt (1 validator, stake=4)        │    │    │
│  │  │  8. Starts stand-alone node using rspace                   │    │    │
│  │  │  9. HTTP API server → 0.0.0.0:40403                       │    │    │
│  │  │  10. Admin HTTP API → 0.0.0.0:40405                       │    │    │
│  │  │  11. Self-approves genesis block                           │    │    │
│  │  │  12. External gRPC API → 0.0.0.0:40401                    │    │    │
│  │  │  13. Kademlia RPC → firefly:40404                          │    │    │
│  │  │  14. Internal gRPC API → firefly:40402                     │    │    │
│  │  │  15. ✓ Healthcheck passes (grpcurl + curl)                 │    │    │
│  │  └────────────────────────────────────────────────────────────┘    │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│  ┌─── docker-firefly-read-1 (mainnet read-only) ──────────────────────┐    │
│  │  No mounts (no genesis, no certs needed)                           │    │
│  │  Port: 14413→40403                                                 │    │
│  │  Command: run --bootstrap=rnode://ebffd419...@firefly               │    │
│  │                ?protocol=40400&discovery=40404                      │    │
│  │                                                                     │    │
│  │  ┌─ Startup: ─────────────────────────────────────────────────┐    │    │
│  │  │  1. Connects to firefly:40404 (Kademlia discovery)         │    │    │
│  │  │  2. Downloads approved block from validator                 │    │    │
│  │  │  3. Syncs chain state                                      │    │    │
│  │  │  4. HTTP API → 0.0.0.0:40403 (read-only)                  │    │    │
│  │  │  5. ✓ Healthcheck passes                                   │    │    │
│  │  └────────────────────────────────────────────────────────────┘    │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│  ┌─── docker-firefly-testnet-1 (testnet validator) ───────────────────┐    │
│  │  Same as mainnet validator but:                                    │    │
│  │  - Mounts: testnet/genesis (separate chain)                        │    │
│  │  - Ports: 15401→40401, 15402→40402, 15403→40403                   │    │
│  │  - --host=firefly-testnet                                          │    │
│  │  - Independent genesis ceremony, independent blockchain            │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│  ┌─── docker-firefly-read-testnet-1 (testnet read-only) ──────────────┐    │
│  │  Same as mainnet read-only but:                                    │    │
│  │  - Port: 15413→40403                                              │    │
│  │  - Bootstraps from firefly-testnet instead of firefly              │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────────┘
```

### F1R3Sky Backend Dev-Env (7 services from 1 command)

```
Terminal 2:
┌─────────────────────────────────────────────────────────────────────────────┐
│  cd services/f1r3sky-backend                                                │
│  make run-dev-env                                                           │
└─────────────┬───────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Makefile target:                                                            │
│  cd packages/dev-env; NODE_ENV=development pnpm run start                   │
└─────────────┬───────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  pnpm start script:                                                         │
│  ../dev-infra/with-test-redis-and-db.sh node --enable-source-maps dist/bin.js│
└─────────────┬───────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Shell script: with-test-redis-and-db.sh + _common.sh                       │
│                                                                             │
│  1. SERVICES="db_test redis_test"                                           │
│  2. Checks: docker ps → Docker available? ─── Yes ──┐                      │
│                                                       │                     │
│  3. docker compose --file docker-compose.yaml \       ▼                     │
│        up --wait --force-recreate db_test redis_test                        │
│                                                                             │
│     ┌─── dev-infra-db_test-1 ────────────────────────────────────────┐      │
│     │  Image: postgres:14.4-alpine                                   │      │
│     │  Port: 5433→5432                                               │      │
│     │  User: pg / Password: password                                 │      │
│     │  Healthcheck: pg_isready -U pg (500ms interval, 20 retries)    │      │
│     │  Storage: Ephemeral (no volume)                                │      │
│     └────────────────────────────────────────────────────────────────┘      │
│                                                                             │
│     ┌─── dev-infra-redis_test-1 ─────────────────────────────────────┐      │
│     │  Image: redis:7.0-alpine                                       │      │
│     │  Port: 6380→6379                                               │      │
│     │  Healthcheck: redis-cli ping (500ms interval, 20 retries)      │      │
│     │  Storage: Ephemeral (no volume)                                │      │
│     └────────────────────────────────────────────────────────────────┘      │
│                                                                             │
│  4. Waits for both healthchecks to pass ✓                                   │
│                                                                             │
│  5. Exports environment variables:                                          │
│     DB_POSTGRES_URL=postgresql://pg:password@127.0.0.1:5433/postgres        │
│     REDIS_HOST=127.0.0.1:6380                                              │
│                                                                             │
│  6. Sets up SIGINT trap (cleanup on Ctrl+C):                                │
│     → docker compose rm --force --stop --volumes db_test redis_test         │
│                                                                             │
│  7. Executes: node --enable-source-maps dist/bin.js                         │
└─────────────┬───────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Node.js: dist/bin.js (compiled from packages/dev-env/src/bin.ts)           │
│  PID: 13874 | Single event loop, all services in-process                    │
│                                                                             │
│  Calls: TestNetwork.create({...})                                           │
│                                                                             │
│  ┌─ Creation order: ───────────────────────────────────────────────────┐    │
│  │                                                                     │    │
│  │  Step 1: Read env vars                                              │    │
│  │    DB_POSTGRES_URL → postgresql://pg:password@127.0.0.1:5433/postgres│    │
│  │    REDIS_HOST → 127.0.0.1:6380                                     │    │
│  │                                                                     │    │
│  │  Step 2: PLC Server ─────────────────────────────────── :2582       │    │
│  │    └─ In-memory DID registry                                        │    │
│  │    └─ No database (in-memory storage)                               │    │
│  │                                                                     │    │
│  │  Step 3: thirdPartyPds ─────────────────────────── :dynamic (temp)  │    │
│  │    └─ Temporary PDS for bootstrapping service identities            │    │
│  │    └─ Creates OzoneServiceProfile (DID + signing key)               │    │
│  │    └─ Creates LexiconAuthorityProfile (DID for lexicon authority)   │    │
│  │    └─ Will be shut down after step 9                                │    │
│  │                                                                     │    │
│  │  Step 4: BSKY AppView ──────────────────────────────── :2584        │    │
│  │    └─ Connects to PostgreSQL (schema: appview_bsky)                 │    │
│  │    └─ Connects to Redis (127.0.0.1:6380)                           │    │
│  │    └─ Static private key → DID: did:plc:dw4kbjf5mn7nhenabiqpkyh3   │    │
│  │    └─ Spawns sub-services:                                          │    │
│  │         ├─ DataPlane (gRPC) ─────────────────── :dynamic (~53677)   │    │
│  │         │   └─ Subscribes to PDS firehose (ws://localhost:2583)     │    │
│  │         │   └─ Indexes repo events → PostgreSQL                    │    │
│  │         └─ BSYNC (HTTP) ─────────────────────── :dynamic (~53678)   │    │
│  │             └─ PostgreSQL schema: bsync                             │    │
│  │             └─ Handles mutes, blocks, notifications                 │    │
│  │                                                                     │    │
│  │  Step 5: PDS (Personal Data Server) ───────────────── :2583         │    │
│  │    └─ DID: did:web:localhost                                        │    │
│  │    └─ Connects to PLC (http://localhost:2582)                       │    │
│  │    └─ Connects to BSKY (http://localhost:2584)                      │    │
│  │    └─ SQLite + /tmp for data (ephemeral)                            │    │
│  │    └─ WebSocket firehose: ws://localhost:2583/xrpc/...subscribeRepos│    │
│  │                                                                     │    │
│  │  Step 6: Mock network utilities                                     │    │
│  │    └─ Handle resolution bypasses DNS (localhost dev)                 │    │
│  │                                                                     │    │
│  │  Step 7: Ozone (Moderation) ───────────────────────── :2587         │    │
│  │    └─ DID: did:plc:l6bvhs5s6sy6fj64otlc54ec                       │    │
│  │    └─ PostgreSQL schema: ozone_db                                   │    │
│  │    └─ Connects to PLC, BSKY, PDS                                   │    │
│  │                                                                     │    │
│  │  Step 8: Migrate service profiles                                   │    │
│  │    └─ OzoneServiceProfile: thirdPartyPds → main PDS                │    │
│  │    └─ LexiconAuthorityProfile: thirdPartyPds → main PDS            │    │
│  │                                                                     │    │
│  │  Step 9: Close thirdPartyPds                                        │    │
│  │    └─ No longer running                                             │    │
│  │                                                                     │    │
│  │  Step 10: Process all pending events                                │    │
│  │    └─ Ensures BSKY has indexed all setup records                    │    │
│  │    └─ Ensures Ozone has processed all events                        │    │
│  │                                                                     │    │
│  │  Step 11: Create Ozone admin policies                               │    │
│  │                                                                     │    │
│  │  Step 12: Introspect Server ───────────────────────── :2581         │    │
│  │    └─ JSON endpoint: {plc, pds, bsky, ozone, db} URLs + DIDs       │    │
│  │                                                                     │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│  ✓ All services running. Process stays alive until Ctrl+C.                  │
│                                                                             │
│  On Ctrl+C:                                                                 │
│    Node.js exits → shell trap fires                                         │
│    → docker compose rm --force --stop --volumes db_test redis_test          │
│    → PostgreSQL + Redis containers destroyed (all data lost)                │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Embers API (Rust)

```
Terminal 3:
┌─────────────────────────────────────────────────────────────────────────────┐
│  cd services/embers                                                         │
│  cargo make embers run                                                      │
└─────────────┬───────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Root Makefile.toml — task "embers":                                         │
│  script = '''                                                               │
│    cd packages/embers                                                       │
│    cargo make ${1}        ← ${1} = "run"                                    │
│  '''                                                                        │
└─────────────┬───────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  packages/embers/Makefile.toml — task "run":                                 │
│                                                                             │
│  Sets 21 environment variables:                                             │
│    EMBERS__ADDRESS=::1                                                      │
│    EMBERS__PORT=8080                                                        │
│    EMBERS__LOG_LEVEL=info,embers=trace                                      │
│    EMBERS__AES_ENCRYPTION_KEY=48E37E0E...                                   │
│    EMBERS__MAINNET__DEPLOY_SERVICE_URL=http://localhost:14401                │
│    EMBERS__MAINNET__PROPOSE_SERVICE_URL=http://localhost:14402               │
│    EMBERS__MAINNET__VALIDATOR_WS_API_URL=ws://localhost:14403                │
│    EMBERS__MAINNET__OBSERVER_URL=http://localhost:14413                      │
│    EMBERS__MAINNET__OBSERVER_WS_API_URL=ws://localhost:14413                 │
│    EMBERS__MAINNET__SERVICE_KEY=232DADA5...                                  │
│    EMBERS__MAINNET__WALLETS_ENV_KEY=8BDC54B5...                              │
│    EMBERS__MAINNET__AGENTS_ENV_KEY=69D4BC8E...                               │
│    EMBERS__MAINNET__AGENTS_TEAMS_ENV_KEY=85348C6D...                         │
│    EMBERS__MAINNET__OSLFS_ENV_KEY=E6441631...                                │
│    EMBERS__TESTNET__DEPLOY_SERVICE_URL=http://localhost:15401                │
│    EMBERS__TESTNET__PROPOSE_SERVICE_URL=http://localhost:15402               │
│    EMBERS__TESTNET__VALIDATOR_WS_API_URL=ws://localhost:15403                │
│    EMBERS__TESTNET__OBSERVER_URL=http://localhost:15413                      │
│    EMBERS__TESTNET__OBSERVER_WS_API_URL=ws://localhost:15413                 │
│    EMBERS__TESTNET__SERVICE_KEY=732240A4...                                  │
│    EMBERS__TESTNET__ENV_KEY=D1BD29C2...                                      │
│    RUST_BACKTRACE=full                                                      │
│                                                                             │
│  Runs: cargo run --bin embers                                               │
└─────────────┬───────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Cargo: compile + run                                                       │
│                                                                             │
│  1. Checks if source changed since last build                               │
│  2. If changed: compiles packages/embers/src/main.rs                        │
│     - Resolves dependencies (Poem, Figment, prost, secp256k1, etc.)         │
│     - Links firefly-client library (local workspace dep)                    │
│     - Output: target/debug/embers                                           │
│  3. Executes: target/debug/embers                                           │
└─────────────┬───────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Binary: embers (PID 91153)                                                 │
│                                                                             │
│  ┌─ Startup sequence (src/main.rs): ──────────────────────────────────┐     │
│  │                                                                     │    │
│  │  1. Figment reads EMBERS__* env vars → Config struct                │    │
│  │     - Splits on "__" to create nested config                        │    │
│  │     - Validates all required keys present                           │    │
│  │                                                                     │    │
│  │  2. Initialize tracing subscriber (logging)                         │    │
│  │     - Level: info globally, trace for embers crate                  │    │
│  │                                                                     │    │
│  │  3. Create blockchain clients:                                      │    │
│  │     ├─ Mainnet read client → http://localhost:14413 (observer)      │    │
│  │     ├─ Mainnet write client → http://localhost:14401 (deploy)       │    │
│  │     │                       → http://localhost:14402 (propose)      │    │
│  │     ├─ Mainnet WS clients → ws://localhost:14403, ws://14413       │    │
│  │     ├─ Testnet read client → http://localhost:15413                 │    │
│  │     └─ Testnet write client → http://localhost:15401, 15402        │    │
│  │                                                                     │    │
│  │  4. Bootstrap internal services:                                    │    │
│  │     ├─ WalletsService (mainnet + testnet wallet ops)                │    │
│  │     ├─ AgentsService (agent CRUD via blockchain)                    │    │
│  │     ├─ AgentsTeamsService (agent group management)                  │    │
│  │     ├─ OslfsService (on-chain file storage)                         │    │
│  │     └─ TestnetService (testnet wallet/agent ops)                    │    │
│  │                                                                     │    │
│  │  5. Build Poem router:                                              │    │
│  │     ├─ Mount API routes (OpenAPI-documented)                        │    │
│  │     ├─ Add middleware: CORS (allow all origins)                     │    │
│  │     ├─ Add middleware: RequestId tracking                           │    │
│  │     ├─ Add middleware: Compression                                  │    │
│  │     └─ Add middleware: NormalizePath (trim trailing slash)          │    │
│  │                                                                     │    │
│  │  6. Bind TcpListener to [::1]:8080                                  │    │
│  │     └─ ✓ Server listening on IPv6 localhost port 8080               │    │
│  │                                                                     │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│  ✓ API ready. Verify: curl http://[::1]:8080/api/service/ready              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### F1R3Sky Frontend (Expo Web)

```
Terminal 4:
┌─────────────────────────────────────────────────────────────────────────────┐
│  cd services/f1r3sky                                                        │
│  yarn web                                                                   │
└─────────────┬───────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Yarn resolves script from package.json:                                    │
│  "web": "expo start --web"                                                  │
└─────────────┬───────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Expo CLI (PID 18811)                                                       │
│                                                                             │
│  1. Reads .env file:                                                        │
│     EXPO_PUBLIC_ENV=development                                             │
│     EXPO_PUBLIC_EMBERS_URL=http://localhost:5173                             │
│     EXPO_PUBLIC_EMBERS_API_URL=http://[::1]:8080                            │
│                                                                             │
│  2. Reads app.config.js (Expo configuration)                                │
│     - Platform: web                                                         │
│     - Bundle identifier, permissions, etc.                                  │
│                                                                             │
│  3. Starts Metro Bundler ────────────────────────── :8081                   │
│     └─ JavaScript/TypeScript bundler                                        │
│     └─ Watches source files for changes                                     │
│     └─ Transforms JSX/TSX → browser-compatible JS                          │
│     └─ Resolves imports and bundles dependencies                            │
│     └─ Spawns worker processes (jest-worker) for parallel transforms        │
│                                                                             │
│  4. Starts Webpack Dev Server ───────────────────── :19006                  │
│     └─ Serves the web application                                           │
│     └─ Hot Module Replacement (live code updates)                           │
│     └─ Proxies API requests as configured                                   │
│                                                                             │
│  ┌─ Runtime connections: ─────────────────────────────────────────────┐     │
│  │  @atproto/api ──────→ PDS at localhost:2583 (auth, write records)  │     │
│  │  @atproto/api ──────→ BSKY at localhost:2584 (read feeds/profiles) │     │
│  │  @f1r3fly-io/embers-client-sdk ──→ Embers at [::1]:8080           │     │
│  │                                    (wallets, agents, blockchain)    │     │
│  └────────────────────────────────────────────────────────────────────┘     │
│                                                                             │
│  ✓ App available at http://localhost:19006                                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Complete Service Dependency Graph

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         USER (Browser)                                      │
│                              │                                              │
│                    http://localhost:19006                                    │
│                              │                                              │
│                              ▼                                              │
│                    ┌─── F1R3Sky Frontend ───┐                               │
│                    │   (Expo Web :19006)    │                               │
│                    └───┬──────────────┬────┘                               │
│                        │              │                                     │
│          ┌─────────────┘              └──────────────────┐                  │
│          │ @f1r3fly-io/                   @atproto/api   │                  │
│          │ embers-client-sdk                             │                  │
│          ▼                                               ▼                  │
│  ┌─── Embers API ───┐                    ┌──── PDS ────────────┐           │
│  │ (Rust :8080)      │                    │ (Node.js :2583)     │           │
│  │                   │                    │                     │           │
│  │ Wallets           │                    │ Auth/Sessions       │           │
│  │ Agents            │                    │ User Repos          │           │
│  │ Teams             │                    │ Blob Storage        │           │
│  │ OSLFS             │                    │                     │           │
│  └───────┬───────────┘                    └──┬──────────────┬──┘           │
│          │                                   │              │               │
│          │ gRPC (deploy/propose)             │ DID          │ WebSocket     │
│          │ HTTP/WS (observe)                 │ resolve      │ firehose      │
│          │                                   ▼              ▼               │
│          │                            ┌─── PLC ───┐  ┌── BSKY AppView ──┐  │
│          │                            │ (:2582)   │  │ (Node.js :2584)  │  │
│          │                            │           │  │                   │  │
│          │                            │ DID ←─────┼──┤ Indexes feeds     │  │
│          │                            │ Registry  │  │ DataPlane (gRPC)  │  │
│          │                            └───────────┘  │ BSYNC (HTTP)     │  │
│          │                                 ▲         └────────┬──────────┘  │
│          │                                 │                  │             │
│          │                                 │ DID resolve      │ labels      │
│          │                                 │                  ▼             │
│          │                            ┌─── Ozone ────────────────────┐      │
│          │                            │ (Node.js :2587)              │      │
│          │                            │ Content moderation/labeling  │      │
│          │                            └──────────────────────────────┘      │
│          │                                                                  │
│          │                            ┌─── PostgreSQL ───────────────┐      │
│          │                            │ (Docker :5433)               │      │
│          │                            │ Schemas: appview_bsky,       │      │
│          │                            │   ozone_db, bsync            │      │
│          │                            └──────────────────────────────┘      │
│          │                                                                  │
│          │                            ┌─── Redis ────────────────────┐      │
│          │                            │ (Docker :6380)               │      │
│          │                            │ Cache for BSKY AppView       │      │
│          │                            └──────────────────────────────┘      │
│          │                                                                  │
│          ▼                                                                  │
│  ┌─────────────────────── F1R3FLY Shard ─────────────────────────────┐      │
│  │                                                                    │     │
│  │  MAINNET                          TESTNET                          │     │
│  │  ┌─── Validator ───┐             ┌─── Validator ───┐              │     │
│  │  │ :14401 deploy   │             │ :15401 deploy   │              │     │
│  │  │ :14402 propose  │             │ :15402 propose  │              │     │
│  │  │ :14403 API/WS   │             │ :15403 API/WS   │              │     │
│  │  └────────┬────────┘             └────────┬────────┘              │     │
│  │           │ bootstrap                      │ bootstrap             │     │
│  │           ▼                                ▼                       │     │
│  │  ┌─── Read-only ───┐             ┌─── Read-only ───┐              │     │
│  │  │ :14413 API/WS   │             │ :15413 API/WS   │              │     │
│  │  └─────────────────┘             └─────────────────┘              │     │
│  │                                                                    │     │
│  └────────────────────────────────────────────────────────────────────┘     │
│                                                                             │
│  ┌─── Introspect (:2581) ────────────────────────────────────────────┐      │
│  │  Development utility — lists all service URLs + DIDs as JSON      │      │
│  │  No service depends on it                                          │     │
│  └────────────────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Port Map

| Port | Protocol | Service | Description |
|------|----------|---------|-------------|
| 2581 | HTTP | F1R3Sky Dev-Env Introspect | JSON endpoint showing all service URLs/DIDs |
| 2582 | HTTP | F1R3Sky Dev-Env PLC | DID Placeholder Registry |
| 2583 | HTTP/WS | F1R3Sky Dev-Env PDS | Personal Data Server |
| 2584 | HTTP | F1R3Sky Dev-Env BSKY AppView | AT Protocol AppView |
| 2587 | HTTP | F1R3Sky Dev-Env Ozone | Moderation Service |
| 5433 | TCP | PostgreSQL (Docker) | Database for AT Protocol services |
| 6380 | TCP | Redis (Docker) | Cache for AT Protocol services |
| 8080 | HTTP | Embers API (local) | Blockchain bridge API (binds [::1]) |
| 8081 | HTTP | Metro Bundler | Expo/React Native dev server |
| 14401 | gRPC | Mainnet Validator Deploy | Transaction deployment service |
| 14402 | gRPC | Mainnet Validator Propose | Block proposal service |
| 14403 | HTTP/WS | Mainnet Validator API | REST API + WebSocket |
| 14413 | HTTP/WS | Mainnet Read-only API | Read-only REST API + WebSocket |
| 15401 | gRPC | Testnet Validator Deploy | Transaction deployment service |
| 15402 | gRPC | Testnet Validator Propose | Block proposal service |
| 15403 | HTTP/WS | Testnet Validator API | REST API + WebSocket |
| 15413 | HTTP/WS | Testnet Read-only API | Read-only REST API + WebSocket |
| 19006 | HTTP | F1R3Sky Frontend (Expo Web) | React Native web dev server |
| 53677 | HTTP | F1R3Sky Dev-Env DataPlane | gRPC dataplane (dynamic port) |
| 53678 | HTTP | F1R3Sky Dev-Env BSYNC | Background sync (dynamic port) |

## Build Sources

### Pulled Externally (Docker Hub)

| Service | Image | Version | Size | Pulled Date |
|---------|-------|---------|------|-------------|
| Mainnet Validator | `f1r3flyindustries/f1r3fly-scala-node:latest` | 1.0.0-SNAPSHOT | 2.46 GB | 2026-01-21 |
| Mainnet Read-only | `f1r3flyindustries/f1r3fly-scala-node:latest` | 1.0.0-SNAPSHOT | 2.46 GB | 2026-01-21 |
| Testnet Validator | `f1r3flyindustries/f1r3fly-scala-node:latest` | 1.0.0-SNAPSHOT | 2.46 GB | 2026-01-21 |
| Testnet Read-only | `f1r3flyindustries/f1r3fly-scala-node:latest` | 1.0.0-SNAPSHOT | 2.46 GB | 2026-01-21 |
| PostgreSQL | `postgres:14.4-alpine` | 14.4 | 301 MB | 2022-08-09 |
| Redis | `redis:7.0-alpine` | 7.0.15 | 50.1 MB | 2024-05-22 |

### Built Locally

| Service | Source Path | Language | Build Command | Output |
|---------|-------------|----------|---------------|--------|
| Embers API | `services/embers/packages/embers/` | Rust | `cargo make embers run` | `target/debug/embers` binary |
| PLC Server | `services/f1r3sky-backend/packages/plc/` | TypeScript | `pnpm build` (workspace) | `dist/` (runs in-process) |
| PDS | `services/f1r3sky-backend/packages/pds/` | TypeScript | `pnpm build` (workspace) | `dist/` (runs in-process) |
| BSKY AppView | `services/f1r3sky-backend/packages/bsky/` | TypeScript | `pnpm build` (workspace) | `dist/` (runs in-process) |
| Ozone | `services/f1r3sky-backend/packages/ozone/` | TypeScript | `pnpm build` (workspace) | `dist/` (runs in-process) |
| DataPlane | `services/f1r3sky-backend/packages/bsky/` | TypeScript | Created internally by BSKY | In-process sub-service |
| BSYNC | `services/f1r3sky-backend/packages/bsync/` | TypeScript | `pnpm build` (workspace) | `dist/` (runs in-process) |
| Introspect | `services/f1r3sky-backend/packages/dev-env/src/introspect.ts` | TypeScript | `pnpm build` (workspace) | `dist/` (runs in-process) |
| Dev-Env Orchestrator | `services/f1r3sky-backend/packages/dev-env/` | TypeScript | `pnpm build` (workspace) | `dist/bin.js` |
| F1R3Sky Frontend | `services/f1r3sky/` | React Native/TS | No build (Expo dev server) | Served live from source |

---

## 1. Shard (F1R3Node Blockchain Nodes)

### What This Is

F1R3Node is a blockchain node implementation forked from RChain. RChain is a proof-of-stake blockchain that uses the Rholang smart contract language and a concurrent execution model based on the rho-calculus. F1R3FLY's fork extends it with additional storage and data management capabilities.

A **shard** is a self-contained blockchain network consisting of one or more validator nodes (which produce blocks and reach consensus) and optional read-only observer nodes (which follow the chain but don't participate in consensus). In this local dev setup, the shard contains two independent networks (mainnet and testnet), each with one validator and one read-only observer.

The nodes communicate using **gRPC** (Google Remote Procedure Call), a high-performance RPC framework that uses Protocol Buffers for serialization. External clients (like Embers) also connect to the nodes via gRPC for deploying smart contracts and proposing blocks, and via HTTP/WebSocket for querying chain state.

The node is written in **Scala** and runs on the **GraalVM JDK 17** (a high-performance Java runtime). It uses the **Casper** consensus protocol for finality.

### How Other Services Interact With It

- **Embers API** connects to the shard's gRPC ports (deploy, propose) to submit Rholang smart contract deploys and trigger block creation. It connects to the HTTP/WS ports to query blockchain state and subscribe to events.
- **F1R3Sky PDS** (in the Docker compose production config, but NOT in the local dev-env) connects to the shard to store AT Protocol data on-chain.
- Read-only nodes bootstrap from validators and replicate chain state without participating in consensus.

### Additional Internal Ports (not mapped to host)

These ports exist inside each container but are not published to the host:

| Internal Port | Protocol | Purpose |
|---------------|----------|---------|
| 40400 | gRPC | Protocol server (node-to-node communication, block propagation) |
| 40404 | UDP | Kademlia peer discovery (DHT-based node discovery) |
| 40405 | HTTP | Admin API (node management, diagnostics) |

### Genesis Configuration

The genesis block is the first block in the chain, created during the "bootstrap ceremony." It defines initial wallet balances and validator bonds.

**Mainnet genesis wallets** (`services/embers/docker/mainnet/genesis/wallets.txt`):

| Address | Balance |
|---------|---------|
| `1111jyBBTGUTqnvcd7ggu8YdaZjhi4yTkFksgnMAKWmZ1HoTzDzk2` | 50,000,000,000,000,000 |
| `1111EjdAxnKb5zKUc8ikuxfdi3kwSGH7BJCHKWjnVzfAF3SjCBvjh` | 50,000,000,000,000,000 |
| `11117Jv1oQo1qkxrKrHXumDZu183yoPRhRXJgqy2D3Gh53bUUZYqY` | 50,000,000,000,000,000 |

**Mainnet genesis bonds** (`services/embers/docker/mainnet/genesis/bonds.txt`):

| Validator Public Key | Stake |
|---------------------|-------|
| `04b103b9a8225589ce98d8...` | 4 |

The validator's private key (`6a786ec3...`) corresponds to this bond, making it the sole block producer. With `synchrony-constraint-threshold=0.0`, the validator can finalize blocks immediately without waiting for other validators.

---

**Started via:** `docker compose up` from `/services/embers/docker/`
**Compose file:** `/services/embers/docker/docker-compose.yaml`
**Compose project name:** `docker`
**Docker network:** `docker_default` (bridge, subnet 172.19.0.0/16)

All four nodes use the same image pulled from Docker Hub.

### 1.1 Mainnet Validator

The validator node is the block producer for the mainnet shard. It runs as a "ceremony master" — meaning it bootstraps the genesis block on first startup, self-approves it (since it's the only bonded validator), and then begins accepting deploys and proposing blocks. The `-s` flag enables standalone mode (single-validator network). Embers connects to this node's gRPC ports to deploy smart contracts and trigger block proposals.

| Field | Value |
|-------|-------|
| Container name | `docker-firefly-1` |
| Image | `f1r3flyindustries/f1r3fly-scala-node:latest` |
| Image source | Pulled from Docker Hub |
| Image size | 2.46 GB |
| Image arch | arm64 / linux |
| Image built | 2026-01-21 |
| Network alias | `firefly` |
| IP address | 172.19.0.4 |
| Status | healthy |
| RChain version | 1.0.0-SNAPSHOT (commit 09646d5e) |
| RChain network ID | `testnet` (internal protocol identifier, not related to mainnet/testnet compose naming) |
| JVM | GraalVM 22 CE, Java 17 |
| User | root |
| Working directory | `/opt/docker` |
| Data directory | `/var/lib/rnode` |
| Ollama (AI) service | Disabled |

**Ports:**

| Host | Container | Protocol | Purpose |
|------|-----------|----------|---------|
| 14401 | 40401 | gRPC | Deploy service (casper.v1.DeployService) |
| 14402 | 40402 | gRPC | Propose service |
| 14403 | 40403 | HTTP/WS | REST API + WebSocket |

**Entrypoint & Command:**
```
Entrypoint: /opt/docker/bin/rnode --profile=docker -XX:ErrorFile=/var/lib/rnode/hs_err_pid%p.log
Command: run -s \
  --validator-private-key=6a786ec387aff99fcce1bd6faa35916bfad3686d5c98e90a89f77670f535607c \
  --host=firefly \
  --no-upnp \
  --allow-private-addresses \
  --synchrony-constraint-threshold=0.0 \
  --protocol-port=40400 \
  --discovery-port=40404 \
  --tls-key-path=/var/lib/rnode/node.key.pem \
  --tls-certificate-path=/var/lib/rnode/node.certificate.pem
```

**Volumes (bind mounts):**

| Host path | Container path | Mode |
|-----------|---------------|------|
| `services/embers/docker/mainnet/genesis` | `/var/lib/rnode/genesis` | rw |
| `services/embers/docker/certs/node.key.pem` | `/var/lib/rnode/node.key.pem` | ro |
| `services/embers/docker/certs/node.certificate.pem` | `/var/lib/rnode/node.certificate.pem` | ro |

**Environment Variables:**

| Variable | Value |
|----------|-------|
| `JAVA_HOME` | `/usr/lib64/graalvm/graalvm22-ce-java17` |
| `LANG` | `en_US.UTF-8` |

**Healthcheck:**
```
CMD-SHELL: grpcurl -plaintext 127.0.0.1:40401 casper.v1.DeployService.status | jq -e && curl -s 127.0.0.1:40403/status | jq -e
```

---

### 1.2 Mainnet Read-only

The read-only observer node follows the mainnet chain by bootstrapping from the validator node via the RChain protocol. It replicates all blocks and state but does not participate in consensus or produce blocks. It provides a read-only HTTP/WebSocket API for querying chain state without putting load on the validator. Embers connects to this node's HTTP/WS port (14413) for all read operations (querying deploys, listening to events), keeping the validator free for write operations.

| Field | Value |
|-------|-------|
| Container name | `docker-firefly-read-1` |
| Image | `f1r3flyindustries/f1r3fly-scala-node:latest` |
| Image source | Pulled from Docker Hub (same as validator) |
| Network alias | `firefly-read` |
| IP address | 172.19.0.3 |
| Status | healthy |

**Ports:**

| Host | Container | Protocol | Purpose |
|------|-----------|----------|---------|
| 14413 | 40403 | HTTP/WS | REST API + WebSocket (read-only) |

**Entrypoint & Command:**
```
Entrypoint: /opt/docker/bin/rnode --profile=docker -XX:ErrorFile=/var/lib/rnode/hs_err_pid%p.log
Command: run \
  --bootstrap=rnode://ebffd419dea60220734ccea8875e86d87bac10a7@firefly?protocol=40400&discovery=40404 \
  --host=firefly-read \
  --no-upnp \
  --allow-private-addresses
```

**Volumes:** None (no genesis data or TLS certs needed for read-only observer)

**Environment Variables:** Same as validator (JAVA_HOME, LANG)

**Healthcheck:** Same as validator

---

### 1.3 Testnet Validator

A separate validator node running an independent blockchain network with its own genesis block. It uses the same image and private key as the mainnet validator but operates on a different Docker hostname (`firefly-testnet`) with separate port mappings (15401-15403). This provides an isolated environment for testing deploys without affecting mainnet state. Embers connects to this node's ports for testnet operations.

| Field | Value |
|-------|-------|
| Container name | `docker-firefly-testnet-1` |
| Image | `f1r3flyindustries/f1r3fly-scala-node:latest` |
| Image source | Pulled from Docker Hub (same image) |
| Network alias | `firefly-testnet` |
| IP address | 172.19.0.5 |
| Status | healthy |

**Ports:**

| Host | Container | Protocol | Purpose |
|------|-----------|----------|---------|
| 15401 | 40401 | gRPC | Deploy service |
| 15402 | 40402 | gRPC | Propose service |
| 15403 | 40403 | HTTP/WS | REST API + WebSocket |

**Entrypoint & Command:**
```
Entrypoint: /opt/docker/bin/rnode --profile=docker -XX:ErrorFile=/var/lib/rnode/hs_err_pid%p.log
Command: run -s \
  --validator-private-key=6a786ec387aff99fcce1bd6faa35916bfad3686d5c98e90a89f77670f535607c \
  --host=firefly-testnet \
  --no-upnp \
  --allow-private-addresses \
  --synchrony-constraint-threshold=0.0 \
  --protocol-port=40400 \
  --discovery-port=40404 \
  --tls-key-path=/var/lib/rnode/node.key.pem \
  --tls-certificate-path=/var/lib/rnode/node.certificate.pem
```

**Volumes (bind mounts):**

| Host path | Container path | Mode |
|-----------|---------------|------|
| `services/embers/docker/testnet/genesis` | `/var/lib/rnode/genesis` | rw |
| `services/embers/docker/certs/node.key.pem` | `/var/lib/rnode/node.key.pem` | ro |
| `services/embers/docker/certs/node.certificate.pem` | `/var/lib/rnode/node.certificate.pem` | ro |

**Environment Variables:** Same as mainnet validator

**Healthcheck:** Same as mainnet validator

---

### 1.4 Testnet Read-only

The read-only observer for the testnet network. Same role as the mainnet read-only node (1.2) but bootstraps from the testnet validator. Embers uses this node's port (15413) for testnet read queries.

| Field | Value |
|-------|-------|
| Container name | `docker-firefly-read-testnet-1` |
| Image | `f1r3flyindustries/f1r3fly-scala-node:latest` |
| Image source | Pulled from Docker Hub (same image) |
| Network alias | `firefly-read-testnet` |
| IP address | 172.19.0.2 |
| Status | healthy |

**Ports:**

| Host | Container | Protocol | Purpose |
|------|-----------|----------|---------|
| 15413 | 40403 | HTTP/WS | REST API + WebSocket (read-only) |

**Entrypoint & Command:**
```
Entrypoint: /opt/docker/bin/rnode --profile=docker -XX:ErrorFile=/var/lib/rnode/hs_err_pid%p.log
Command: run \
  --bootstrap=rnode://ebffd419dea60220734ccea8875e86d87bac10a7@firefly-testnet?protocol=40400&discovery=40404 \
  --host=firefly-read-testnet \
  --no-upnp \
  --allow-private-addresses
```

**Volumes:** None

**Environment Variables:** Same as validator

**Healthcheck:** Same as validator

---

## 2. Embers API Server

### What This Is

Embers is a **REST API bridge** between the F1R3FLY blockchain and frontend applications. It translates HTTP requests into blockchain operations (smart contract deploys via gRPC) and provides a wallet/agent management layer with encrypted data storage on-chain.

It is written in **Rust** using the **Poem** web framework (a lightweight async HTTP framework similar to Actix or Axum). Configuration is handled by **Figment**, a layered configuration library that reads environment variables prefixed with `EMBERS__` and splits them on double underscores to create a nested config structure (e.g., `EMBERS__MAINNET__PORT` becomes `config.mainnet.port`).

The API provides:
- **Wallet management**: Create, store, and use blockchain wallets. Wallet data is encrypted with AES-GCM (256-bit) before being stored on-chain.
- **Agent management**: CRUD operations for AI agents whose state is persisted to the blockchain.
- **Agent teams**: Group management for collections of agents.
- **OSLFS (On-chain Storage Layer File System)**: Object storage using the blockchain as a persistence layer.
- **Testnet operations**: Mirror of mainnet services pointing to the testnet shard.

Data encryption uses **AES-GCM** (Galois/Counter Mode), a symmetric authenticated encryption algorithm. Each data domain (wallets, agents, teams, OSLFS) has its own encryption key for isolation.

### How Other Services Interact With It

- **F1R3Sky Frontend** calls the Embers REST API at `http://[::1]:8080` for wallet and agent operations (via the `@f1r3fly-io/embers-client-sdk` npm package).
- **Embers** calls the **Shard** validator nodes via gRPC (deploy + propose) and HTTP/WebSocket (observe) to read/write blockchain state.
- The API exposes **OpenAPI/Swagger** documentation for endpoint discovery.

### Key Technologies

| Technology | Purpose |
|------------|---------|
| Rust | Systems language providing memory safety and high performance |
| Poem 3.1 | Async HTTP web framework (handles routing, middleware, OpenAPI) |
| Figment | Configuration library (reads env vars into typed structs) |
| AES-GCM | Symmetric authenticated encryption for on-chain data |
| gRPC (via prost) | Protocol Buffers-based RPC for blockchain communication |
| secp256k1 | Elliptic curve cryptography for wallet key management |
| tokio | Async runtime (event loop, task scheduling) |

| Field | Value |
|-------|-------|
| Process | `target/debug/embers` (PID 91153) |
| Binary path | `/services/embers/target/debug/embers` |
| Built locally | Yes (debug build via cargo) |
| Build command | `cargo make embers run` (from `/services/embers/`) |
| Language | Rust |
| Web framework | Poem 3.1 |
| Config source | Environment variables (Figment, prefixed `EMBERS__`, split on `__`) |
| Listening on | `[::1]:8080` (IPv6 localhost only) |

**How to start:**
```bash
cd services/embers
cargo make embers run
```

**How to verify:**
```bash
curl http://[::1]:8080/api/service/ready
```

### How `cargo make embers run` Works

The command is a multi-step delegation chain:

**Step 1:** `cargo make embers run` (from `/services/embers/`)

The root `Makefile.toml` task `embers` is a delegator:
```toml
[tasks.embers]
script = '''
cd packages/embers
cargo make ${1}
'''
```
It `cd`s into `packages/embers` and forwards the argument (`run`) to `cargo make`.

**Step 2:** `cargo make run` (now in `/services/embers/packages/embers/`)

The `packages/embers/Makefile.toml` defines the `run` task:
```toml
[tasks.run]
command = "cargo"
args = ["run", "--bin", "embers"]
env.EMBERS__ADDRESS = "::1"
env.EMBERS__PORT = 8080
# ... (all environment variables set inline)
```
This sets all environment variables, then runs `cargo run --bin embers`.

**Step 3:** `cargo run --bin embers`

Cargo compiles `packages/embers/src/main.rs` (debug build → `target/debug/embers`) if source has changed, then executes the binary.

**Step 4:** Binary starts Poem HTTP server

The binary reads configuration via Figment (env vars prefixed `EMBERS__`, split on `__`), bootstraps internal services (Wallets, Agents, AgentsTeams, OSLFS, Testnet), and binds a TCP listener to `[::1]:8080`.

**Full chain:**
```
cargo make embers run
  → cd packages/embers && cargo make run
    → cargo run --bin embers (with env vars set by Makefile.toml)
      → compiles src/main.rs → target/debug/embers
        → Poem server listens on [::1]:8080
```

**Port:**

| Host | Protocol | Purpose |
|------|----------|---------|
| 8080 | HTTP | REST API (binds to [::1] / IPv6 localhost) |

**Environment Variables (set by Makefile.toml):**

| Variable | Value | Purpose |
|----------|-------|---------|
| `EMBERS__ADDRESS` | `::1` | Bind address (IPv6 localhost) |
| `EMBERS__PORT` | `8080` | Listen port |
| `EMBERS__LOG_LEVEL` | `info,embers=trace` | Log level |
| `EMBERS__AES_ENCRYPTION_KEY` | `48E37E0E448C482ADEAE83CD15FE91AA4E2459ED67D707BB40EF17BB18E60EE4` | AES-GCM encryption key (32 bytes hex) |
| `RUST_BACKTRACE` | `full` | Full panic backtraces |

**Mainnet Connection Variables:**

| Variable | Value | Purpose |
|----------|-------|---------|
| `EMBERS__MAINNET__DEPLOY_SERVICE_URL` | `http://localhost:14401` | gRPC deploy to mainnet validator |
| `EMBERS__MAINNET__PROPOSE_SERVICE_URL` | `http://localhost:14402` | gRPC propose to mainnet validator |
| `EMBERS__MAINNET__VALIDATOR_WS_API_URL` | `ws://localhost:14403` | WebSocket to mainnet validator |
| `EMBERS__MAINNET__OBSERVER_URL` | `http://localhost:14413` | HTTP to mainnet read-only node |
| `EMBERS__MAINNET__OBSERVER_WS_API_URL` | `ws://localhost:14413` | WebSocket to mainnet read-only node |
| `EMBERS__MAINNET__SERVICE_KEY` | `232DADA5BBAFC0799D5F370DA04AF70CE438F69F954512B26D6FB5B560B81DFE` | Wallet private key for mainnet txns |
| `EMBERS__MAINNET__WALLETS_ENV_KEY` | `8BDC54B5551812C43428EB172A2079ABBEF13B5370BB7535F78807CDEBA3E7B3` | Wallet data encryption key |
| `EMBERS__MAINNET__AGENTS_ENV_KEY` | `69D4BC8ED86915383E68FAF1E4F9D8E22E02CDD3702730C61FE3B45FBBDF0097` | Agent data encryption key |
| `EMBERS__MAINNET__AGENTS_TEAMS_ENV_KEY` | `85348C6D6AEF0B4761F8B8047111B3A2F7C9DF8CB24F91B66B77893DDE21DEE5` | Agent teams encryption key |
| `EMBERS__MAINNET__OSLFS_ENV_KEY` | `E6441631C4E164BF13A0532BF6775606965089CE3750E5ED39AAA9EC0DF81E67` | OSLFS encryption key |

**Testnet Connection Variables:**

| Variable | Value | Purpose |
|----------|-------|---------|
| `EMBERS__TESTNET__DEPLOY_SERVICE_URL` | `http://localhost:15401` | gRPC deploy to testnet validator |
| `EMBERS__TESTNET__PROPOSE_SERVICE_URL` | `http://localhost:15402` | gRPC propose to testnet validator |
| `EMBERS__TESTNET__VALIDATOR_WS_API_URL` | `ws://localhost:15403` | WebSocket to testnet validator |
| `EMBERS__TESTNET__OBSERVER_URL` | `http://localhost:15413` | HTTP to testnet read-only node |
| `EMBERS__TESTNET__OBSERVER_WS_API_URL` | `ws://localhost:15413` | WebSocket to testnet read-only node |
| `EMBERS__TESTNET__SERVICE_KEY` | `732240A471E12931D858F147165BA1B52C011B92B9E8CD7959AADF06D7ACE622` | Wallet private key for testnet txns |
| `EMBERS__TESTNET__ENV_KEY` | `D1BD29C232D11142E852EEE23482B239AF5494DFA10D64E82A72A8CDF82D5127` | Testnet general encryption key |

**Internal Services Bootstrapped:**
- AgentsService (agent CRUD, uses mainnet read/write clients)
- AgentsTeamsService (agent group management)
- OslfsService (object storage/filesystem)
- WalletsService (wallet management, listens to blockchain events)
- TestnetService (mirror of mainnet services for testnet)

**API Features:**
- OpenAPI/Swagger documentation
- CORS enabled (`allow_origin_regex("*")`)
- Request ID tracking
- Response compression
- Trailing slash normalization

---

## 3. F1R3Sky Backend (Dev-Env)

### What This Is

F1R3Sky Backend is a fork of Bluesky's **AT Protocol** (Authenticated Transfer Protocol) implementation. AT Protocol is a decentralized social networking protocol that separates identity, data storage, and application views into independent layers. It was designed by Bluesky (the company behind the Bluesky social network) as an open protocol for building federated social applications.

The F1R3FLY fork extends AT Protocol with blockchain integration — in production, user data can be stored on the F1R3FLY blockchain via the shard nodes, providing decentralized persistence beyond traditional database storage.

**Key AT Protocol concepts:**
- **DID (Decentralized Identifier)**: A self-sovereign identity standard (W3C spec). Each user and service has a DID that uniquely identifies them regardless of where their data is hosted. Example: `did:plc:abc123` or `did:web:example.com`.
- **Lexicon**: A schema language for defining API endpoints and data types. Lexicons are namespaced (e.g., `app.bsky.feed.post`) and define the structure of records.
- **Repository**: A user's data store — a Merkle tree of signed records. Each user has one repo containing all their posts, likes, follows, etc.
- **XRPC**: The RPC protocol AT Protocol uses for API calls. Endpoints are named by their lexicon (e.g., `com.atproto.sync.subscribeRepos`).

In the local dev-env, all AT Protocol services run **in-process** within a single Node.js event loop (no separate containers). This is different from the Docker compose production configuration where each service runs in its own container.

**Important note:** In this local dev-env, the PDS does NOT have blockchain integration enabled. The blockchain connection (DEPLOY_SERVICE_URL, etc.) only exists in the Docker compose production configuration (`docker-compose.f1r3sky.yml`). Locally, the PDS stores data in temporary SQLite databases and `/tmp` directories.

### How Other Services Interact With It

- **F1R3Sky Frontend** connects to the PDS (port 2583) for user authentication and account management, and to the BSKY AppView (port 2584) for reading social feed data.
- **BSKY AppView** subscribes to the PDS WebSocket firehose (`ws://localhost:2583`) to index all repository changes in real-time.
- **Ozone** connects to both PDS and BSKY for content moderation actions.
- All services use the **PLC** registry for DID resolution (looking up where a user's data lives).

---

**Started via:** `make run-dev-env` from `/services/f1r3sky-backend/`

**Process:** `node --enable-source-maps dist/bin.js` (PID 13874)
**Built locally:** Yes (TypeScript compiled to `dist/`)
**Source:** `/services/f1r3sky-backend/packages/dev-env/src/bin.ts`
**Node version:** v20.19.5 (via NVM)

### Dev-Env Orchestrator: How It Works

The orchestrator is a multi-stage startup chain that provisions infrastructure, then boots AT Protocol services in-process in a specific dependency order.

**Stage 1: Makefile entry point**
```makefile
# services/f1r3sky-backend/Makefile
run-dev-env:
    cd packages/dev-env; NODE_ENV=development pnpm run start
```

**Stage 2: pnpm start script**
Runs: `../dev-infra/with-test-redis-and-db.sh node --enable-source-maps dist/bin.js`

**Stage 3: Shell script (`packages/dev-infra/with-test-redis-and-db.sh` + `_common.sh`)**

1. Sets `SERVICES="db_test redis_test"`
2. Checks if Docker daemon is available
3. Runs `docker compose --file docker-compose.yaml up --wait --force-recreate db_test redis_test`
4. Waits for healthchecks to pass (PostgreSQL: `pg_isready`, Redis: `redis-cli ping`)
5. Exports environment variables:
   - `DB_POSTGRES_URL=postgresql://pg:password@127.0.0.1:5433/postgres`
   - `REDIS_HOST=127.0.0.1:6380`
6. Executes the passed command (`node --enable-source-maps dist/bin.js`) with those env vars
7. Sets up a SIGINT trap — on Ctrl+C, runs `docker compose rm --force --stop --volumes db_test redis_test` to tear down containers

**Stage 4: Node.js bin.ts — `TestNetwork.create()` (`packages/dev-env/src/network.ts`)**

All services are created in-process in the same Node.js event loop (no child processes spawned). They share the same PostgreSQL database but use separate schemas. Creation order:

1. Reads `DB_POSTGRES_URL` and `REDIS_HOST` from environment (set by shell script)
2. Creates **PLC** server (port 2582) — DID Placeholder Registry
3. Creates a temporary **thirdPartyPds** on a dynamic port — used only for bootstrapping service identity profiles
4. Creates **OzoneServiceProfile** on thirdPartyPds — establishes Ozone's DID identity
5. Creates **LexiconAuthorityProfile** on thirdPartyPds — establishes lexicon authority DID
6. Creates **BSKY AppView** (port 2584) with:
   - Database schema: `appview_bsky`
   - Static private key: `3f916c70...` (for consistent DID across restarts)
   - Subscribes to PDS firehose at `ws://localhost:2583`
   - Internally spawns **DataPlane** (gRPC, dynamic port) and **BSYNC** (HTTP, dynamic port)
7. Creates **PDS** (port 2583) configured with PLC, BSKY, and Ozone URLs/DIDs
8. Mocks network utilities (handle resolution bypasses DNS for localhost)
9. Creates **Ozone** (port 2587) with:
   - Database schema: `ozone_db`
   - Connected to BSKY, PDS, and PLC
10. Migrates service profiles from thirdPartyPds → main PDS (account migration)
11. **Closes thirdPartyPds** (no longer running after setup)
12. Processes all pending events across services (ensures consistent state)
13. Creates admin policies in Ozone
14. Starts **IntrospectServer** (port 2581) — JSON endpoint exposing all service URLs and DIDs

**Environment variables (set on the Node.js process):**

| Variable | Value | Set By |
|----------|-------|--------|
| `NODE_ENV` | `development` | Makefile (`NODE_ENV=development pnpm run start`) |
| `DB_POSTGRES_URL` | `postgresql://pg:password@127.0.0.1:5433/postgres` | Shell script (`_common.sh` → `export_pg_env`) |
| `REDIS_HOST` | `127.0.0.1:6380` | Shell script (`_common.sh` → `export_redis_env`) |

**Hardcoded constants (in source code):**

| Constant | Value | Used For |
|----------|-------|----------|
| Admin password | `admin-pass` | PDS admin authentication |
| JWT secret | `jwt-secret` | PDS session token signing |
| BSKY private key | `3f916c70dc69e4c5e83877f013325b11ecac31742e6a42f5c4fb240d0703d9d5` | Static DID generation for AppView |

**Lifecycle notes:**
- The thirdPartyPds is ephemeral — created during startup, used to establish service DIDs, then shut down
- All in-process services share a single Node.js event loop — no separate processes
- DataPlane and BSYNC ports are allocated via the `get-port` npm package (random available port each run)
- On Ctrl+C: Node.js process exits → shell trap fires → Docker containers are force-removed
- All data is ephemeral: PostgreSQL has no volume, temp dirs are in `/tmp`

---

### 3.1 PostgreSQL

**PostgreSQL** is an open-source relational database management system (RDBMS). It stores structured data in tables with SQL query access, supports ACID transactions, and provides advanced features like JSON columns, full-text search, and schemas for namespace isolation.

In this stack, PostgreSQL serves as the persistence layer for all AT Protocol services. Each service uses a separate **schema** (a namespace within the same database) to isolate its tables:
- `appview_bsky` — BSKY AppView's indexed social data (posts, follows, likes, profiles)
- `ozone_db` — Ozone moderation data (reports, labels, actions)
- `bsync` — BSYNC background sync data (mute lists, blocks, notification queues)
- `public` — default schema

The database is **ephemeral** (no Docker volume) — all data is lost when the container stops. This is intentional for development: each restart gives a clean state.

**How other services use it:** The dev-env Node.js process connects via the connection string `postgresql://pg:password@127.0.0.1:5433/postgres` and creates schemas on startup. BSKY, Ozone, and BSYNC all write to and read from their respective schemas.

| Field | Value |
|-------|-------|
| Container name | `dev-infra-db_test-1` |
| Image | `postgres:14.4-alpine` |
| Image source | Pulled from Docker Hub |
| Image size | 301 MB |
| Image date | 2022-08-09 |
| Network | `dev-infra_default` (bridge, 172.20.0.0/16) |
| Network alias | `db_test` |
| IP address | 172.20.0.2 |
| Status | healthy |
| Storage | Ephemeral (no persistent volume) |
| Compose file | `/services/f1r3sky-backend/packages/dev-infra/docker-compose.yaml` |
| Compose project | `dev-infra` |

**Ports:**

| Host | Container | Protocol | Purpose |
|------|-----------|----------|---------|
| 5433 | 5432 | TCP | PostgreSQL wire protocol |

**Environment Variables:**

| Variable | Value | Purpose |
|----------|-------|---------|
| `POSTGRES_USER` | `pg` | Database superuser |
| `POSTGRES_PASSWORD` | `password` | Database password |
| `PG_MAJOR` | `14` | PostgreSQL major version |
| `PG_VERSION` | `14.4` | Full version |
| `PGDATA` | `/var/lib/postgresql/data` | Data directory |

**Healthcheck:**
```
CMD-SHELL: pg_isready -U pg
Interval: 500ms, Timeout: 10s, Retries: 20
```

**Connection string used by dev-env:**
```
postgresql://pg:password@127.0.0.1:5433/postgres
```

**Database schemas created by dev-env services:**
- `appview_bsky` (BSKY AppView)
- `ozone_db` (Ozone moderation)
- `public` (default)

---

### 3.2 Redis

**Redis** (Remote Dictionary Server) is an in-memory key-value data store used as a cache, message broker, and session store. It keeps data in RAM for sub-millisecond access times, making it ideal for caching frequently-accessed data and coordinating between services.

In this stack, Redis is used by the **BSKY AppView** for:
- Caching resolved DIDs and handles (avoiding repeated PLC lookups)
- Storing temporary session data
- Rate limiting and deduplication of firehose events
- Cursor tracking for subscription consumers

Like PostgreSQL, this Redis instance is **ephemeral** (no volume, no persistence configured) — all cached data is lost on container restart.

**How other services use it:** The BSKY AppView connects via `127.0.0.1:6380`. Redis is not used directly by PDS, Ozone, or other services in the dev-env configuration.

| Field | Value |
|-------|-------|
| Container name | `dev-infra-redis_test-1` |
| Image | `redis:7.0-alpine` |
| Image source | Pulled from Docker Hub |
| Image size | 50.1 MB |
| Image date | 2024-05-22 |
| Network | `dev-infra_default` |
| Network alias | `redis_test` |
| Status | healthy |
| Storage | Ephemeral (no persistent volume) |
| Compose file | `/services/f1r3sky-backend/packages/dev-infra/docker-compose.yaml` |
| Compose project | `dev-infra` |

**Ports:**

| Host | Container | Protocol | Purpose |
|------|-----------|----------|---------|
| 6380 | 6379 | TCP | Redis protocol |

**Environment Variables:**

| Variable | Value |
|----------|-------|
| `REDIS_VERSION` | `7.0.15` |

**Healthcheck:**
```
CMD-SHELL: [ "$(redis-cli ping)" = "PONG" ]
Interval: 500ms, Timeout: 10s, Retries: 20
```

**Connection used by dev-env:**
```
REDIS_HOST=127.0.0.1:6380
```

---

### 3.3 PLC Server (DID Placeholder Registry)

**PLC** (Placeholder) is a DID method created by Bluesky specifically for AT Protocol. A **DID** (Decentralized Identifier) is a W3C standard for self-sovereign identity — a globally unique identifier that doesn't depend on any central authority. The format looks like `did:plc:abc123xyz`.

The PLC server acts as a **registry** — it maps DIDs to their current hosting location (which PDS holds the user's data), their signing keys, and their human-readable handles. Think of it like DNS for decentralized identity: given a DID, the PLC server tells you where to find that user's data.

In production, Bluesky operates `plc.directory` as the public PLC registry. In this local dev-env, a local PLC server provides the same functionality without depending on external infrastructure.

**How other services use it:**
- **PDS** registers new user DIDs here when accounts are created
- **BSKY AppView** resolves DIDs to find user data locations
- **Ozone** resolves DIDs when processing moderation reports
- All handle-to-DID resolution flows through PLC

| Field | Value |
|-------|-------|
| Type | In-process (Node.js) |
| Created by | `TestNetwork.create()` in dev-env bin.ts |
| Port | 2582 |
| URL | `http://localhost:2582` |
| Parent process | PID 13874 (node dist/bin.js) |

**Configuration:**
```typescript
plc: { port: 2582 }
```

---

### 3.4 PDS (Personal Data Server)

The **PDS** is the core data storage component of AT Protocol. It is where a user's **repository** lives — a signed Merkle tree containing all their records (posts, likes, follows, profile info, etc.). Each user has exactly one PDS that is authoritative for their data.

The PDS provides:
- **Account management**: User registration, authentication (via JWT tokens), session management
- **Repository hosting**: Stores and serves user data repositories as signed, content-addressed data structures
- **Blob storage**: Handles binary data (images, videos) uploaded by users
- **Firehose**: A WebSocket stream (`com.atproto.sync.subscribeRepos`) that broadcasts every repository change in real-time to downstream consumers (like the BSKY AppView)
- **XRPC endpoints**: The AT Protocol API surface for reading/writing records

In the F1R3FLY production configuration, the PDS is extended with blockchain integration — user data is synchronized to the F1R3FLY shard for decentralized persistence. **In this local dev-env, blockchain integration is NOT active** — data is stored in temporary SQLite databases and `/tmp` directories that are recreated each run.

**How other services use it:**
- **F1R3Sky Frontend** authenticates users and manages sessions via PDS XRPC endpoints
- **BSKY AppView** subscribes to the PDS firehose to index all user activity in real-time
- **Ozone** sends moderation actions (takedowns, labels) to the PDS
- **PLC** is called by PDS to register new user DIDs

| Field | Value |
|-------|-------|
| Type | In-process (Node.js) |
| Created by | `TestNetwork.create()` → `TestPds.create()` |
| Port | 2583 |
| URL | `http://localhost:2583` |
| DID | `did:web:localhost` |
| Parent process | PID 13874 |

**Configuration:**
```typescript
pds: {
  port: 2583,
  hostname: 'localhost',
  enableDidDocWithSession: true,
}
```

**Key internal settings:**
- `didPlcUrl`: `http://localhost:2582` (PLC server)
- `bskyAppViewUrl`: `http://localhost:2584`
- `bskyAppViewDid`: `did:plc:dw4kbjf5mn7nhenabiqpkyh3`
- `modServiceUrl`: `http://localhost:2587`
- `inviteRequired`: false
- Admin password: `admin-pass`
- JWT secret: `jwt-secret`

**Data storage:** Temporary directories in `/tmp` (created fresh each run)
- Blobs: random `/tmp` path
- SQLite: random `/tmp` path

**Protocols served:**
- HTTP REST API (XRPC endpoints)
- WebSocket (`com.atproto.sync.subscribeRepos` firehose)

---

### 3.5 BSKY AppView

An **AppView** in AT Protocol is a read-only aggregation service that indexes data from multiple repositories into a queryable view optimized for a specific application. The BSKY AppView specifically implements the `app.bsky.*` lexicon — the schema definitions for Bluesky's social networking features.

The AppView:
- **Subscribes** to the PDS firehose (WebSocket at `ws://localhost:2583`) and processes every repository event (new posts, likes, follows, deletes, etc.)
- **Indexes** this data into PostgreSQL tables optimized for social queries (feeds, notifications, thread views, author profiles)
- **Serves** the indexed data via XRPC endpoints that the frontend calls (e.g., `app.bsky.feed.getTimeline`, `app.bsky.actor.getProfile`)
- **Applies labels** from Ozone (moderation flags on content/accounts)

The AppView is the layer that makes AT Protocol feel like a social network — without it, you'd have to query each user's PDS individually. It aggregates everything into one queryable index.

It uses a static private key (`3f916c70...`) so its DID remains consistent across restarts — this is important because other services reference the AppView by its DID.

**How other services use it:**
- **F1R3Sky Frontend** calls BSKY endpoints for feed, notifications, search, and profile data
- **Ozone** pushes moderation labels to BSKY for content filtering
- **PDS** reports its AppView DID to clients during authentication (so clients know where to query)
- Internally spawns **DataPlane** and **BSYNC** as sub-services (see 3.8)

| Field | Value |
|-------|-------|
| Type | In-process (Node.js) |
| Created by | `TestNetwork.create()` → `TestBsky.create()` |
| Port | 2584 |
| URL | `http://localhost:2584` |
| DID | `did:plc:dw4kbjf5mn7nhenabiqpkyh3` |
| Parent process | PID 13874 |

**Configuration:**
```typescript
bsky: {
  port: 2584,
  publicUrl: 'http://localhost:2584',
  dbPostgresSchema: 'bsky',
  // Static private key for consistent DID across restarts:
  privateKey: '3f916c70dc69e4c5e83877f013325b11ecac31742e6a42f5c4fb240d0703d9d5=',
}
```

**Key internal settings:**
- `plcUrl`: `http://localhost:2582`
- `repoProvider`: `ws://localhost:2583` (subscribes to PDS firehose)
- `dbPostgresUrl`: `postgresql://pg:password@127.0.0.1:5433/postgres`
- `dbPostgresSchema`: `appview_bsky`
- `redisHost`: `127.0.0.1:6380`
- `modServiceDid`: Ozone DID
- `labelsFromIssuerDids`: [Ozone DID, `did:example:labeler`]

**Sub-services created by BSKY:**
- **DataPlane** (gRPC server, dynamic port ~53677): Serves indexed data to the AppView
- **BSYNC** (HTTP server, dynamic port ~53678): Background synchronization service

---

### 3.6 Ozone (Moderation Service)

**Ozone** is AT Protocol's moderation infrastructure. It provides tools for content moderation at the protocol level — applying labels to content and accounts that downstream services (like the AppView) can use to filter, warn, or hide content.

Ozone provides:
- **Labeling**: Applies semantic labels to records or accounts (e.g., "nudity", "spam", "misleading"). These labels propagate to the AppView which uses them for content filtering.
- **Reports**: Receives user-submitted moderation reports about content or accounts
- **Actions**: Moderators can take actions like takedowns (removing content from the AppView index), account suspensions, or escalations
- **Policies**: Configurable moderation policies that define automated labeling rules
- **Materialized views**: Periodically refreshes aggregated moderation data (every 30 seconds in this config)

Ozone operates as an independent service with its own DID identity, allowing the moderation layer to be operated by different entities than the PDS or AppView operators.

**How other services use it:**
- **BSKY AppView** receives labels from Ozone and applies them when serving content
- **PDS** receives takedown requests from Ozone (to remove content from a user's repo)
- **F1R3Sky Frontend** could display moderation UI (report buttons, label indicators)
- **PLC** provides DID resolution for accounts being moderated

| Field | Value |
|-------|-------|
| Type | In-process (Node.js) |
| Created by | `TestNetwork.create()` → `TestOzone.create()` |
| Port | 2587 |
| URL | `http://localhost:2587` |
| DID | `did:plc:l6bvhs5s6sy6fj64otlc54ec` |
| Parent process | PID 13874 |

**Configuration:**
```typescript
ozone: {
  port: 2587,
  chatUrl: 'http://localhost:2590',  // chat service (not running)
  chatDid: 'did:example:chat',
  dbMaterializedViewRefreshIntervalMs: 30_000,
}
```

**Key internal settings:**
- `plcUrl`: `http://localhost:2582`
- `dbPostgresUrl`: `postgresql://pg:password@127.0.0.1:5433/postgres`
- `dbPostgresSchema`: `ozone_db`
- `appviewUrl`: `http://localhost:2584`
- `appviewDid`: BSKY DID
- `pdsUrl`: `http://localhost:2583`
- `pdsDid`: `did:web:localhost`

---

### 3.7 Introspect Server

The Introspect Server is a **development-only** utility that provides a single JSON endpoint listing all running AT Protocol service URLs, DIDs, and database connection strings. It exists solely for developer convenience — you can `curl localhost:2581` to quickly see where every service is running and what identity (DID) it has.

This is particularly useful because DIDs are generated dynamically (except BSKY's which uses a static key) and would otherwise require checking console output or logs to discover.

**How other services use it:** No service depends on it. It's purely for human debugging.

| Field | Value |
|-------|-------|
| Type | In-process (Node.js) |
| Created by | `TestNetwork.create()` → `IntrospectServer.start()` |
| Port | 2581 |
| URL | `http://localhost:2581` |
| Parent process | PID 13874 |

**Response format:**
```json
{
  "plc": { "url": "http://localhost:2582" },
  "pds": { "url": "http://localhost:2583", "did": "did:web:localhost" },
  "bsky": { "url": "http://localhost:2584", "did": "did:plc:dw4kbjf5mn7nhenabiqpkyh3" },
  "ozone": { "url": "http://localhost:2587", "did": "did:plc:l6bvhs5s6sy6fj64otlc54ec" },
  "db": { "url": "postgresql://pg:password@127.0.0.1:5433/postgres" }
}
```

---

### 3.8 DataPlane + BSYNC (Dynamic Ports)

These are internal sub-services created by the BSKY AppView. They are architecturally separate to allow independent scaling in production, but in the dev-env they run in the same Node.js process.

**DataPlane** is a **gRPC** server that provides an abstraction layer between the BSKY AppView's API logic and its PostgreSQL database. Rather than having the AppView query the database directly, it goes through the DataPlane, which:
- Exposes indexed data as gRPC service methods (efficient binary serialization via Protocol Buffers)
- Handles the subscription to the PDS firehose (`ws://localhost:2583/xrpc/com.atproto.sync.subscribeRepos`) and writes incoming events to PostgreSQL
- Provides batched, optimized database reads for feed generation and search

**gRPC** (Google Remote Procedure Call) is a high-performance RPC framework that uses Protocol Buffers (a binary serialization format) instead of JSON for data transfer. It supports streaming, which the DataPlane uses for real-time event processing.

**BSYNC** (Background Sync) handles asynchronous processing tasks that don't need to happen in the request path:
- **Mute lists**: Syncing which accounts a user has muted/blocked
- **List membership**: Processing list additions/removals
- **Notification generation**: Creating notifications from indexed events
- **Cross-service coordination**: Ensuring consistency between PDS and AppView state

BSYNC uses its own PostgreSQL schema (`bsync`) and exposes an HTTP API for internal communication.

**How other services use them:**
- Both are internal to the BSKY AppView — no other service connects to them directly
- The AppView routes all database queries through DataPlane
- BSYNC is triggered by events from the firehose subscription

| Field | DataPlane | BSYNC |
|-------|-----------|-------|
| Type | In-process sub-service | In-process sub-service |
| Created by | `TestBsky.create()` | `TestBsky.create()` |
| Port | Dynamic (~53677) | Dynamic (~53678) |
| Protocol | gRPC | HTTP |
| Parent | BSKY AppView | BSKY AppView |
| DB Schema | `appview_bsky` (shared with AppView) | `bsync` (own schema) |

**Note:** These ports are allocated dynamically via `get-port` npm package and change on each restart. They are internal to the BSKY service and not directly accessed by other components in this stack.

---

## 4. F1R3Sky Frontend

### What This Is

F1R3Sky Frontend is a fork of the **Bluesky Social** mobile/web application (the official Bluesky client). It is a cross-platform app built with **React Native** (a framework for building native mobile apps using React/JavaScript) and **Expo** (a platform that simplifies React Native development and adds web support).

The app provides a social networking interface (feeds, posts, profiles, notifications, messaging) that communicates with the AT Protocol backend services. The F1R3FLY fork extends it with blockchain-related features via the Embers SDK — allowing users to interact with wallets and blockchain agents through the social UI.

**Key technologies:**
- **React Native**: Facebook's framework for building native iOS/Android apps with JavaScript/TypeScript. Components render to native UI elements on mobile.
- **Expo**: A toolchain and platform that wraps React Native, adding web support, over-the-air updates, and simplified build configuration. The `expo start --web` command starts a webpack-based development server that serves the app as a website.
- **Metro Bundler** (port 8081): React Native's JavaScript bundler. It transforms and bundles the TypeScript/JSX source code into browser-compatible JavaScript, providing hot module replacement (live code updates without page refresh).
- **`@atproto/api`**: The official AT Protocol client SDK for JavaScript — handles authentication, XRPC calls, and data types.
- **`@f1r3fly-io/embers-client-sdk`** (v0.0.79): F1R3FLY's SDK for interacting with the Embers API — provides wallet management, agent operations, and blockchain interaction from the frontend.

In development mode (`expo start --web`), the app is served directly from source with hot reloading. In production, it compiles to a static JavaScript bundle served by a Go binary (`bskyweb`).

### How Other Services Interact With It

- Calls **Embers API** at `http://[::1]:8080` for blockchain wallet/agent operations (via embers-client-sdk)
- Calls **PDS** at port 2583 for user authentication, session management, and writing records (posts, likes, follows)
- Calls **BSKY AppView** at port 2584 for reading social data (feeds, notifications, profiles, search)
- References `EXPO_PUBLIC_EMBERS_URL=http://localhost:5173` for the Embers frontend (not currently running)

| Field | Value |
|-------|-------|
| Process | `expo start --web` (PID 18811) |
| Binary | Node.js via NVM (v20.19.5) |
| Built locally | Yes (live dev server, no pre-build needed) |
| Source | `/services/f1r3sky/` |
| Framework | React Native / Expo (Web target) |
| Package manager | Yarn 1.22.22 |

**How to start:**
```bash
cd services/f1r3sky
yarn install
yarn web
```

**How to verify:**
Open `http://localhost:19006` in browser.

**Ports:**

| Host | Protocol | Purpose |
|------|----------|---------|
| 19006 | HTTP | Expo web dev server (serves the app) |
| 8081 | HTTP | Metro bundler (hot reload, module resolution) |

**Environment Variables (from `.env`):**

| Variable | Value | Purpose |
|----------|-------|---------|
| `EXPO_PUBLIC_ENV` | `development` | Environment mode |
| `EXPO_PUBLIC_EMBERS_URL` | `http://localhost:5173` | Embers frontend URL (not running) |
| `EXPO_PUBLIC_EMBERS_API_URL` | `http://[::1]:8080` | Embers API URL |

**Key dependencies:**
- `@atproto/api` - AT Protocol client library
- `@f1r3fly-io/embers-client-sdk` v0.0.79 - Embers SDK for blockchain interaction
- `expo` - React Native for web platform

**Backend connections:**
- Embers API at `http://[::1]:8080` (for blockchain/wallet operations)
- BSKY AppView at port 2584 (for social data, via AT Protocol client)
- PDS at port 2583 (for user authentication/data)

---

## TLS Certificates

**TLS** (Transport Layer Security) is a cryptographic protocol that provides encrypted communication between network endpoints. In this stack, TLS is used for node-to-node identity verification in the blockchain network — each node presents its certificate to prove its identity when establishing connections with peers. The certificate's Common Name (CN) contains the node's unique ID, which is derived from its public key.

Note: TLS is NOT used for the HTTP APIs (ports 40401-40403) — those are plaintext. TLS is only used for the internal protocol-level communication (port 40400) between nodes.

**Location:** `/services/embers/docker/certs/`

| File | Size | Purpose |
|------|------|---------|
| `node.certificate.pem` | 533 bytes | Node TLS certificate |
| `node.key.pem` | 147 bytes | Node TLS private key |

**Certificate Details:**
- **Algorithm:** ECDSA with SHA-256
- **Curve:** P-256 (prime256v1)
- **CN (Subject/Issuer):** `ebffd419dea60220734ccea8875e86d87bac10a7` (node ID)
- **Type:** Self-signed
- **Valid:** Nov 16, 2019 - Nov 15, 2020 (EXPIRED but still functional for dev)
- **Serial:** 5046750928895092258

**Used by:** Mainnet validator and testnet validator for node-to-node TLS communication. Both validators share the same certificate (same node ID identity). Read-only nodes don't mount certs (they connect as clients to the validator's bootstrap address).

---

## Docker Networks

**Docker networks** provide isolated virtual networks for containers. Containers on the same network can communicate using their container names or service aliases as hostnames (Docker's built-in DNS). Containers on different networks cannot reach each other except via published ports on the host.

| Network | Driver | Subnet | Used by |
|---------|--------|--------|---------|
| `docker_default` | bridge | 172.19.0.0/16 | All 4 shard containers |
| `dev-infra_default` | bridge | 172.20.0.0/16 | PostgreSQL + Redis containers |
| `f1r3fly` | bridge | (exists but unused in current local stack) | Used in docker-compose production configs |

**Note:** The shard containers and dev-infra containers are on separate Docker networks. They communicate with the host (and each other) only via published ports. The local Node.js/Rust processes connect via `localhost` port mappings.

---

## Startup Order & Dependencies

```
1. Docker Shard (docker compose up from services/embers/docker/)
   └── Mainnet Validator starts first (genesis)
       └── Mainnet Read-only bootstraps from validator
   └── Testnet Validator starts independently (separate genesis)
       └── Testnet Read-only bootstraps from testnet validator

2. F1R3Sky Backend Dev-Env (make run-dev-env from services/f1r3sky-backend/)
   └── Docker: PostgreSQL (db_test) + Redis (redis_test) start first
       └── Script waits for healthchecks to pass
           └── Node.js process starts
               └── PLC server starts
               └── BSKY AppView starts (creates DataPlane + BSYNC)
               └── PDS starts (connects to PLC + BSKY)
               └── Ozone starts (connects to PLC + BSKY + PDS)
               └── Introspect server starts last

3. Embers API (cargo make embers run from services/embers/)
   └── Depends on: Shard running (connects to localhost:14401/02/03/13, 15401/02/03/13)
   └── No dependency on F1R3Sky backend

4. F1R3Sky Frontend (yarn web from services/f1r3sky/)
   └── Depends on: Embers API running (connects to [::1]:8080)
   └── Depends on: F1R3Sky Backend running (connects to PDS/BSKY)
```

---

## Quick Reference: How to Start Everything

```bash
# Terminal 1: Start shard
cd services/embers/docker
docker compose up

# Terminal 2: Start F1R3Sky backend (starts postgres + redis containers, then Node.js dev-env)
cd services/f1r3sky-backend
make run-dev-env

# Terminal 3: Start Embers API
cd services/embers
cargo make embers run

# Terminal 4: Start F1R3Sky frontend
cd services/f1r3sky
yarn web
```

---

## Quick Reference: How to Verify Services

| Service | Check Command |
|---------|--------------|
| Shard (all nodes) | `docker ps \| grep f1r3fly-scala-node` |
| Mainnet validator API | `curl http://localhost:14403/status` |
| Testnet validator API | `curl http://localhost:15403/status` |
| PostgreSQL | `psql postgresql://pg:password@localhost:5433/postgres -c 'SELECT 1'` |
| Redis | `redis-cli -h localhost -p 6380 ping` |
| Dev-env services | `curl http://localhost:2581` (introspect) |
| PDS | `curl http://localhost:2583/xrpc/_health` |
| BSKY | `curl http://localhost:2584/xrpc/_health` |
| Embers API | `curl http://[::1]:8080/api/service/ready` |
| F1R3Sky Frontend | Open `http://localhost:19006` |
