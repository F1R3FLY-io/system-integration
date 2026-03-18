# Local Development Setup

Complete steps to run the full F1R3FLY stack locally: Rust shard, embers, embers-frontend, f1r3sky backend, and f1r3sky frontend.

## Quick Start

```bash
# Start the shard first
cd services/f1r3node-rust/docker && docker compose -f shard.yml up -d && cd -

# Start all other services (embers, f1r3sky, frontends, creates user account)
./scripts/start-all.sh

# Check status
./scripts/status.sh

# Stop all (preserves volumes)
./scripts/stop-all.sh

# Stop all and wipe data
./scripts/stop-all.sh --clean
```

## Prerequisites

- Docker Desktop running (allocate at least 12GB RAM)
- Nightly Rust toolchain (nightly-2026-02-09) — installed automatically via `rust-toolchain.toml` in embers

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

F1R3Sky Backend (dev-env, single container)
├── PDS           :2583
├── BSKY AppView  :2584
├── Ozone         :2587
├── DID PLC       :2582
├── PostgreSQL (f1r3sky-postgres)
└── Redis (f1r3sky-redis)

F1R3Sky Frontend (port 8100)
└── ATP_APPVIEW_HOST → http://f1r3sky:2584
```

## Step 1: Start the Rust Shard

```bash
cd services/f1r3node-rust/docker
docker compose -f shard.yml up -d
```

Wait for nodes to be healthy:
```bash
docker ps --format "table {{.Names}}\t{{.Status}}" | grep rnode
```

## Step 2: Build and Start Embers

### Create environment file

```bash
cd services/embers
cp embers.env.example embers.env
```

Key settings for local dev (already in `embers.env`):
- Deploy/propose → `rnode.validator1:40401` / `:40402`
- Reads → `rnode.readonly:40403`
- WebSocket events → `rnode.validator1:40403` and `rnode.readonly:40403` (NOT port 40405)
- SERVICE_KEY: bootstrap wallet private key from shard genesis

### Build Docker image

```bash
docker build -f docker/embers.dockerfile -t f1r3flyio/embers:local .
```

### Start embers

Without f1r3sky (basic):
```bash
docker run -d \
  --env-file ./embers.env \
  --network f1r3fly \
  -p 8080:3000 \
  --name embers \
  f1r3flyio/embers:local
```

With f1r3sky (adds localhost→f1r3sky alias for AT Protocol DID resolution):
```bash
docker run -d \
  --env-file ./embers.env \
  --network f1r3fly \
  -p 8080:3000 \
  --name embers \
  --add-host localhost:$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' f1r3sky) \
  f1r3flyio/embers:local
```

### Verify

```bash
docker logs embers 2>&1 | grep -E '(ERROR|INFO)'
# Should see: server started
# WARN about "started" WebSocket event variant is harmless

curl http://localhost:8080/api/service/ready
# Should return 200

curl http://localhost:8080/api/ai-agents/1111AtahZeefej4tvVR6ti9TJtv8yxLebT31SCEVDCKMNikBk5r3g
# Should return {"agents":[]}
```

## Step 3: Start Embers Frontend

```bash
docker run -d \
  -p 8081:80 \
  -e API_URL="http://localhost:8080" \
  --name embers-frontend \
  f1r3flyio/embers-frontend:latest
```

Access at `http://localhost:8081`. Sign in with the bootstrap wallet key:
```
5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657
```

## Step 4: Start F1R3Sky Backend

Start infrastructure:
```bash
docker run -d --name f1r3sky-postgres --network f1r3fly \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=atproto \
  postgres:16-alpine

docker run -d --name f1r3sky-redis --network f1r3fly \
  redis:7-alpine
```

Wait 5 seconds, then start the AT Protocol services:
```bash
docker run -d --name f1r3sky --network f1r3fly \
  -p 2581:2581 -p 2582:2582 -p 2583:2583 -p 2584:2584 -p 2587:2587 \
  -e ENABLE_PDS=1 \
  -e DB_POSTGRES_URL=postgresql://postgres:postgres@f1r3sky-postgres:5432/atproto \
  -e REDIS_HOST=f1r3sky-redis \
  -e PDS_HOSTNAME=f1r3sky \
  f1r3flyindustries/firesky-ts:latest
```

Verify:
```bash
docker logs f1r3sky 2>&1 | tail -10
# Should see: PDS, Ozone, Bsky Appview started

curl http://localhost:2583/xrpc/_health
# Should return {}
```

## Step 5: Start F1R3Sky Frontend

```bash
docker run -d --name f1r3sky-frontend --network f1r3fly \
  -p 8100:8100 \
  -e HTTP_ADDRESS=:8100 \
  -e ATP_APPVIEW_HOST=http://f1r3sky:2584 \
  f1r3flyindustries/firesky-frontend:latest \
  /usr/bin/bskyweb serve
```

Access at `http://localhost:8100`.

Create accounts via PDS API (frontend captcha doesn't work locally):
```bash
curl -X POST http://localhost:2583/xrpc/com.atproto.server.createAccount \
  -H 'Content-Type: application/json' \
  -d '{"handle": "myuser.test", "email": "user@test.com", "password": "password123"}'
```

Then sign in on the frontend with custom hosting provider `http://localhost:2583`.

## Step 6: Restart Embers with F1R3Sky Integration

After f1r3sky is running, restart embers with the localhost alias so AT Protocol DID resolution works:

```bash
docker rm -f embers
docker run -d \
  --env-file services/embers/embers.env \
  --network f1r3fly \
  -p 8080:3000 \
  --name embers \
  --add-host localhost:$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' f1r3sky) \
  f1r3flyio/embers:local
```

## Publishing Agent Teams to F1R3Sky

In the embers frontend (`http://localhost:8081`):
1. Create an agent team (Agent Teams tab → Create)
2. Build the graph (add nodes to container, connect them, save)
3. Deploy
4. Publish → enter:
   - **PDS Address**: `http://f1r3sky:2583`
   - **Handle**: `mybotname.test` (must be `name.test` format)
   - **Email**: any valid email
   - **Password**: any

## Docker Maintenance

After heavy Docker builds, prune build cache to prevent Docker daemon from wedging:
```bash
docker builder prune -f
```

## Port Reference

| Service | Host Port | Internal Port | Purpose |
|---|---|---|---|
| Embers API | 8080 | 3000 | Blockchain API bridge |
| Embers Frontend | 8081 | 80 | Embers React UI |
| F1R3Sky PDS | 2583 | 2583 | AT Protocol Personal Data Server |
| F1R3Sky AppView | 2584 | 2584 | AT Protocol feed/profile API |
| F1R3Sky Ozone | 2587 | 2587 | AT Protocol moderation |
| F1R3Sky DID PLC | 2582 | 2582 | DID directory |
| F1R3Sky Frontend | 8100 | 8100 | F1R3Sky React Native web UI |
| Grafana | 3000 | 3000 | Monitoring dashboards |
| Prometheus | 9090 | 9090 | Metrics |
| PostgreSQL (f1r3sky) | 5433 | 5432 | AT Protocol database |
| Redis (f1r3sky) | 6380 | 6379 | AT Protocol cache |

## Scripts

| Script | Purpose |
|---|---|
| `scripts/start-all.sh` | Start all services (embers, f1r3sky, frontends, create user) |
| `scripts/stop-all.sh` | Stop all non-shard services (use `--clean` to also prune volumes) |
| `scripts/status.sh` | Show status of all services |

Note: The shard is managed separately via `docker compose -f shard.yml` in `services/f1r3node-rust/docker/`.

## Enabling OpenAI (AI Agent Execution)

See [docs/enable-openai-on-node.md](enable-openai-on-node.md) for configuring GPT-4, DALL-E 3, and TTS on the Rust node validators.

## Known Issues

- **Embers**: See `services/embers/docs/embers-rust-node-updates.md` (11 fixes applied) and `services/embers/docs/node-compatibility.md`
- **Embers Frontend**: See `services/embers-frontend/docs/known-issues.md` (10 issues — stubs, session persistence, graph serialization)
- **F1R3Sky Frontend**: See `services/f1r3sky/docs/known-issues.md` (4 issues — captcha, feed error, wallet config, post thread error)

## Related PRs

- Embers: https://github.com/F1R3FLY-io/embers/pull/168
- Embers Frontend: https://github.com/F1R3FLY-io/embers-frontend/pull/196
