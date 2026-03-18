# Local Development Setup

Complete steps to run the full F1R3FLY stack locally: Rust shard, embers, embers-frontend, f1r3sky backend, and f1r3sky frontend.

## Quick Start

```bash
# 1. Start the shard
cd services/f1r3node-rust/docker
docker compose -f shard.yml up -d
cd -

# 2. Start all other services
./scripts/start-all.sh

# 3. Check status
./scripts/status.sh

# 4. Stop all (preserves volumes)
./scripts/stop-all.sh

# 5. Stop all and wipe data
./scripts/stop-all.sh --clean
```

## Prerequisites

- Docker Desktop running (allocate at least 12GB RAM)
- Docker images built (see Build section below)

## Build

Images must be built before running the scripts. The scripts do not build images.

```bash
# Embers backend
cd services/embers
docker build -f docker/embers.dockerfile -t f1r3flyio/embers:local .

# F1R3Sky backend (must match frontend API version)
cd services/f1r3sky-backend
docker build -f Dockerfile.dev -t f1r3flyindustries/firesky-ts:local .

# F1R3Sky frontend (requires GitHub PAT with read:packages scope)
cd services/f1r3sky
docker build \
  --build-arg NPM_TOKEN=<github-pat> \
  --build-arg EXPO_PUBLIC_EMBERS_API_URL=http://localhost:8080 \
  -t f1r3flyio/firesky-frontend:local .
```

### Docker Image Tags

| Image | Tag | Source |
|---|---|---|
| `f1r3flyio/embers` | `:local` | Built from `services/embers` |
| `f1r3flyio/embers-frontend` | `:latest` | Pre-built from Docker Hub |
| `f1r3flyindustries/firesky-ts` | `:local` | Built from `services/f1r3sky-backend` |
| `f1r3flyio/firesky-frontend` | `:local` | Built from `services/f1r3sky` |
| `postgres:16-alpine` | — | Docker Hub |
| `redis:7-alpine` | — | Docker Hub |

**Important:** The f1r3sky backend and frontend must be matching versions. The frontend uses `getPostThreadV2` which only exists in the locally-built backend.

## Architecture

```
Rust Shard (f1r3fly network)
├── rnode.bootstrap    :40400-40405
├── rnode.validator1   :40400-40405
├── rnode.validator2   :40400-40405
├── rnode.validator3   :40400-40405
├── rnode.readonly     :40400-40405
├── prometheus         :9090
└── grafana            :3000

Embers Backend (port 8080)
├── gRPC → rnode.validator1:40401 (deploys)
├── REST → rnode.readonly:40403 (reads + WebSocket events)
└── API → localhost:8080

Embers Frontend (port 8081)
└── API_URL → http://localhost:8080

F1R3Sky Backend (single container, dev-env)
├── PDS           :2583
├── BSKY AppView  :2584
├── Ozone         :2587
├── DID PLC       :2582
├── PostgreSQL (f1r3sky-postgres, internal)
└── Redis (f1r3sky-redis, internal)

F1R3Sky Frontend (port 8100)
└── ATP_APPVIEW_HOST → http://f1r3sky:2584
```

## What `start-all.sh` Does

1. Verifies the shard is running
2. Starts f1r3sky-postgres and f1r3sky-redis (internal, no host ports)
3. Starts f1r3sky backend (PDS:2583, BSKY:2584, Ozone:2587)
4. Starts embers with `--add-host localhost:<f1r3sky-ip>` (for AT Protocol DID resolution)
5. Starts embers-frontend (port 8081)
6. Starts f1r3sky-frontend (port 8100)
7. Creates a f1r3sky user account via PDS API
8. Waits for embers init deploys to finalize on chain

## Credentials

### Embers Frontend (`http://localhost:8081`)

Sign in with the bootstrap wallet private key:
```
5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657
```

### F1R3Sky Frontend (`http://localhost:8100`)

Created automatically by `start-all.sh`:
- **Hosting provider**: `http://localhost:2583`
- **Username**: `user1.test`
- **Password**: `password123`

Handles must be in `name.test` format for the local dev PDS.

Additional accounts can be created via the PDS API (frontend captcha doesn't work locally):
```bash
curl -X POST http://localhost:2583/xrpc/com.atproto.server.createAccount \
  -H 'Content-Type: application/json' \
  -d '{"handle": "user2.test", "email": "user2@test.com", "password": "password123"}'
```

## E2E Demo Flow

1. **Embers frontend** → Create agent team → build graph (input → text model → TTI model → compress → output) → save → wait ~20s → deploy
2. **Embers frontend** → Publish agent team:
   - PDS Address: `http://f1r3sky:2583`
   - Handle: `myagent.test`
   - Email: any
   - Password: any
3. **F1R3Sky frontend** → Sign in → post tagging `@myagent.test <your prompt>`
4. Agent replies with GPT-4 text + DALL-E 3 image

## Enabling OpenAI (AI Agent Execution)

See [enable-openai-on-node.md](enable-openai-on-node.md) for configuring GPT-4, DALL-E 3, and TTS on the Rust node validators. Without OpenAI enabled, agent runs return empty results.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/start-all.sh` | Start all services, create user, wait for init |
| `scripts/stop-all.sh` | Stop all non-shard services |
| `scripts/stop-all.sh --clean` | Stop + remove volumes + prune build cache |
| `scripts/status.sh` | Show status of all services |

## Port Reference

| Service | Port | Purpose |
|---|---|---|
| Embers API | 8080 | Blockchain API bridge |
| Embers Frontend | 8081 | Embers React UI |
| F1R3Sky PDS | 2583 | AT Protocol Personal Data Server |
| F1R3Sky AppView | 2584 | AT Protocol feed/profile API |
| F1R3Sky Ozone | 2587 | AT Protocol moderation |
| F1R3Sky DID PLC | 2582 | DID directory |
| F1R3Sky Frontend | 8100 | F1R3Sky web UI |
| Grafana | 3000 | Monitoring dashboards |
| Prometheus | 9090 | Metrics |

## Docker Maintenance

After heavy Docker builds, prune build cache to prevent Docker daemon from wedging:
```bash
docker builder prune -f
```

## Known Issues

- **Embers**: See `services/embers/docs/embers-rust-node-updates.md` (13 fixes applied)
- **Embers Frontend**: See `services/embers-frontend/docs/known-issues.md` (10 issues)
- **F1R3Sky Frontend**: See `services/f1r3sky/docs/known-issues.md` (4 issues)

## Related PRs

- Embers: https://github.com/F1R3FLY-io/embers/pull/168
- Embers Frontend: https://github.com/F1R3FLY-io/embers-frontend/pull/196
- System-integration: https://github.com/F1R3FLY-io/system-integration/pull/37
- F1R3Sky: https://github.com/F1R3FLY-io/f1r3sky/pull/33
