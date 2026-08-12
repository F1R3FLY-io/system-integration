# Local Development Setup

Complete steps to run the full F1R3FLY stack locally for the embers demo: Rust standalone node, embers backend/frontend, F1R3Sky backend/frontend.

This setup uses a **single Rust standalone node** (not the 5-node shard). Standalone is simpler, faster, more memory-efficient, and the E2E demo passes 10/10 stable on it.

## Quick Start

```bash
# 1. Build all 4 service images (one-time, see Build section)
# 2. Configure OpenAI key in .env.node (see Configuration section)

# 3. Start the Rust standalone node (pinned to v0.4.5)
F1R3FLY_RUST_IMAGE=f1r3flyindustries/f1r3fly-rust-node:v0.4.5 \
STANDALONE_HOST=rnode.rust-standalone \
docker compose -p rust-standalone -f compose/f1r3node-rust-standalone.yml \
  --env-file .env.node up -d

# 4. Start all other services (embers + f1r3sky)
./scripts/start-all.sh --node rust-standalone

# 5. Verify status
./scripts/status.sh

# 6. Stop services (preserves volumes)
./scripts/stop-all.sh --node rust-standalone

# 7. Stop services and wipe data
./scripts/stop-all.sh --node rust-standalone --clean

# 8. Stop the Rust node
docker compose -p rust-standalone -f compose/f1r3node-rust-standalone.yml down -v
```

## Prerequisites

- Docker Desktop running (allocate at least **2 GB RAM** — standalone is much lighter than the shard)
- GitHub PAT with `read:packages` scope (needed to build embers-frontend and f1r3sky frontend; both pull `@f1r3fly-io/*` packages from GitHub Packages)
- OpenAI API key (for AI features in agent runs)

## Rust Node Version

**Pin to `v0.4.5`.** The `:dev` and `:latest` tags as of 2026-04-22 (v0.4.13) changed the explore-deploy response format and break embers' deserializer with `failed to deserialize intermediate model`. Embers' compatibility code on the `feat/rust-node-compatibility` branch was last verified against v0.4.5 (released 2026-03-29).

```bash
docker pull f1r3flyindustries/f1r3fly-rust-node:v0.4.5
```

If you need to use a newer node, embers' `firefly-client/src/read_node_client.rs` deserialization needs updates to handle the new `ExprBundle`/`ExprUnforg` shape.

## Build

Build all 4 service images. Required branch checkouts:

| Repo | Required branch |
|---|---|
| `services/embers` | `feat/rust-node-compatibility` |
| `services/embers-frontend` | `docs/known-issues` |
| `services/f1r3sky` | `docs/known-issues-and-dockerfile` |
| `services/f1r3sky-backend` | `main` |

### 1. Embers backend (no PAT needed)

```bash
cd services/embers
docker build -f docker/embers.dockerfile -t f1r3flyio/embers:local .
```

### 2. Embers frontend (requires GitHub PAT)

The Dockerfile uses BuildKit secrets for private packages. Create a temporary `.npmrc` and pass via `--secret`:

```bash
printf "//npm.pkg.github.com/:_authToken=%s\n@f1r3fly-io:registry=https://npm.pkg.github.com/\n" \
  "<github-pat>" > /tmp/.npmrc-ef

cd services/embers-frontend
DOCKER_BUILDKIT=1 docker build \
  -f apps/embers/Dockerfile \
  -t f1r3flyio/embers-frontend:local \
  --secret id=npmrc,src=/tmp/.npmrc-ef \
  .

rm /tmp/.npmrc-ef
```

**Note:** The `docs/known-issues` branch currently has TypeScript errors in the embers app code (`Header.tsx`, `queries.ts`) that don't match the latest SDK API. If the build fails with `error TS2339: Property 'waitForFinalization' does not exist`, fall back to patching the pre-built image:

```bash
# Fallback: patch :latest from Docker Hub (15s → 120s finalization timeout)
docker pull f1r3flyio/embers-frontend:latest
docker create --name ef-patch f1r3flyio/embers-frontend:latest
docker cp ef-patch:/usr/share/nginx/html/assets/ /tmp/ef-assets/

# Find the file (filename may change between releases)
JS_FILE=$(grep -l aitForFinalisation /tmp/ef-assets/*.js | head -1)

python3 -c "
with open('$JS_FILE', 'r') as f: c = f.read()
c = c.replace('aitForFinalisation??15e3', 'aitForFinalisation??12e4')
with open('$JS_FILE', 'w') as f: f.write(c)
"

docker cp "$JS_FILE" ef-patch:/usr/share/nginx/html/assets/
docker commit ef-patch f1r3flyio/embers-frontend:local
docker rm ef-patch && rm -rf /tmp/ef-assets
```

### 3. F1R3Sky backend (no PAT needed)

```bash
cd services/f1r3sky-backend
docker build -f Dockerfile.dev -t f1r3flyindustries/firesky-ts:local .
```

The frontend uses `getPostThreadV2` which only exists in the locally-built backend, so `:local` and the matching f1r3sky frontend must be used together.

### 4. F1R3Sky frontend (requires GitHub PAT)

`EXPO_PUBLIC_EMBERS_API_URL` is an Expo build-time variable baked into the JS bundle — it cannot be set at runtime. Without it, auto-reply on @mentions (`runOnFiresky`) is broken because the embers SDK has no API URL.

```bash
cd services/f1r3sky
docker build \
  --build-arg NPM_TOKEN=<github-pat> \
  --build-arg EXPO_PUBLIC_EMBERS_API_URL=http://localhost:8080 \
  -t f1r3flyio/firesky-frontend:local .
```

This is the longest build (~10-15 min for the React Native/Expo bundle).

### Image tags reference

| Image | Tag | Source |
|---|---|---|
| `f1r3flyindustries/f1r3fly-rust-node` | `v0.4.5` | Docker Hub (do not use `:dev`/`:latest`) |
| `f1r3flyio/embers` | `:local` | Built from `services/embers` |
| `f1r3flyio/embers-frontend` | `:local` | Built from `services/embers-frontend` (or patched from `:latest`) |
| `f1r3flyindustries/firesky-ts` | `:local` | Built from `services/f1r3sky-backend` |
| `f1r3flyio/firesky-frontend` | `:local` | Built from `services/f1r3sky` |
| `postgres:16-alpine` | — | Docker Hub |
| `redis:7-alpine` | — | Docker Hub |

## Configuration

### OpenAI key (required for AI features)

Edit `.env.node`:

```bash
OPENAI_ENABLED=true
OPENAI_SCALA_CLIENT_API_KEY="sk-proj-..."
```

The Rust node loads these at startup via `--env-file .env.node`. Without them, agent runs complete but produce empty results (no GPT-4 / DALL-E calls).

### Embers env file

`services/embers/embers.rust-standalone.env` points embers at `http://rnode.rust-standalone:40401` (gRPC) and `http://rnode.rust-standalone:40403` (HTTP). Used automatically by `start-all.sh --node rust-standalone`.

## Architecture

```
Rust Standalone Node (rust-standalone_f1r3fly-standalone network)
└── rnode.rust-standalone   :40400 protocol, :40401 gRPC, :40402 internal-gRPC,
                            :40403 HTTP, :40404 discovery, :40405 admin

Embers Backend (port 8080) — compose-embers-1
├── gRPC → rnode.rust-standalone:40401 (deploys)
├── HTTP → rnode.rust-standalone:40403 (reads + WebSocket events)
└── API → http://localhost:8080

Embers Frontend (port 8081) — compose-embers-frontend-1
└── API_URL → http://localhost:8080

F1R3Sky Backend (single container, dev-env) — compose-f1r3sky-1
├── PDS           :2583
├── BSKY AppView  :2584
├── Ozone         :2587
├── DID PLC       :2582
├── PostgreSQL    (compose-f1r3sky-postgres-1, internal-only)
└── Redis         (compose-f1r3sky-redis-1, internal-only)

F1R3Sky Frontend (port 8100) — compose-f1r3sky-frontend-1
├── Web app served by bskyweb
└── EXPO_PUBLIC_EMBERS_API_URL → http://localhost:8080 (baked at build time)
```

`start-all.sh --node rust-standalone` joins all services on the `rust-standalone_f1r3fly-standalone` network so embers can reach `rnode.rust-standalone` by hostname.

## What `start-all.sh` Does

1. Verifies the Rust standalone node is running
2. Starts f1r3sky-postgres + f1r3sky-redis (internal, no host ports)
3. Starts f1r3sky backend (PDS:2583, BSKY:2584, Ozone:2587)
4. Resolves f1r3sky container IP, starts embers with `--add-host localhost:<f1r3sky-ip>` so the AT Protocol DID `did:web:localhost` resolves to the f1r3sky PDS from inside the embers container
5. Starts embers-frontend (port 8081) and f1r3sky-frontend (port 8100)
6. Creates `user1.test` account via PDS API
7. Waits for embers init deploys to finalize on the blockchain

## Service URLs

| Service | URL | Credentials |
|---|---|---|
| Embers Frontend | http://localhost:8081 | Sign-in key: `5f668a7ee96d944a4494cc947e4005e172d7ab3461ee5538f1f2a45a835e9657` |
| F1R3Sky Frontend | http://localhost:8100 | `user1.test` / `password123` (created by `start-all.sh`) |
| Embers API | http://localhost:8080 | — |
| Embers Swagger | http://localhost:8080/swagger-ui/index.html | — |
| F1R3Sky PDS | http://localhost:2583 | — |

Additional F1R3Sky accounts can be created via PDS API (frontend captcha doesn't work locally):

```bash
curl -X POST http://localhost:2583/xrpc/com.atproto.server.createAccount \
  -H 'Content-Type: application/json' \
  -d '{"handle": "user2.test", "email": "user2@test.com", "password": "password123"}'
```

## Demo Flow

1. **Embers frontend** (`http://localhost:8081`) → Sign in with bootstrap key → Create agent team → Build graph (input → text model → output) → Save → Deploy
2. **Embers frontend** → Publish agent team:
   - PDS URL: `http://f1r3sky:2583`
   - Handle: `myagent.test`
   - Email: any (must be unique per publish)
   - Password: any
3. **F1R3Sky frontend** (`http://localhost:8100`) → Sign in as `user1.test` → Load wallet → Post tagging `@myagent.test <your prompt>`
4. Agent auto-replies with GPT-4 text (~30-60s)

**Note:** A wallet must be loaded in the F1R3Sky frontend for the @mention trigger to work. The wallet signs the runOnFiresky transaction.

## Verifying the setup

After `start-all.sh` completes, run the E2E demo test:

```bash
scripts/e2e-demo-test.sh --skip-start --no-teardown
```

Expected: ~60s runtime, `=== E2E Demo Test PASSED ===`. Exercises all 8 phases programmatically: create → save → deploy → run (GPT-4) → publish → verify profile → post → agent auto-reply.

If Phase 5 result and Phase 8 reply are empty strings, the OpenAI key isn't loaded — recheck `.env.node` and restart the Rust node. See [`e2e-demo/README.md`](../e2e-demo/README.md) for details.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/start-all.sh --node rust-standalone` | Start embers + f1r3sky services |
| `scripts/stop-all.sh --node rust-standalone` | Stop services |
| `scripts/stop-all.sh --node rust-standalone --clean` | Stop + remove volumes |
| `scripts/status.sh` | Show status of all services |
| `scripts/e2e-demo-test.sh --skip-start --no-teardown` | Run E2E demo test |

## Port Reference

| Service | Port | Purpose |
|---|---|---|
| Rust Node | 40400-40405 | Protocol, gRPC, HTTP, discovery, admin |
| Embers API | 8080 | Blockchain API bridge |
| Embers Frontend | 8081 | Embers React UI |
| F1R3Sky PDS | 2583 | AT Protocol Personal Data Server |
| F1R3Sky AppView | 2584 | AT Protocol feed/profile API |
| F1R3Sky DID PLC | 2582 | DID directory |
| F1R3Sky Ozone | 2587 | AT Protocol moderation |
| F1R3Sky Frontend | 8100 | F1R3Sky web UI |

## Troubleshooting

### Embers fails bootstrap with "failed to deserialize intermediate model"

The Rust node version is too new. Pin to `v0.4.5`:

```bash
bash scripts/stop-all.sh --node rust-standalone --clean
docker compose -p rust-standalone -f compose/f1r3node-rust-standalone.yml down -v
docker pull f1r3flyindustries/f1r3fly-rust-node:v0.4.5
F1R3FLY_RUST_IMAGE=f1r3flyindustries/f1r3fly-rust-node:v0.4.5 \
STANDALONE_HOST=rnode.rust-standalone \
docker compose -p rust-standalone -f compose/f1r3node-rust-standalone.yml \
  --env-file .env.node up -d
```

### Phase 8 (auto-reply) doesn't fire on @mention from manual demo

The f1r3sky frontend was built without `EXPO_PUBLIC_EMBERS_API_URL`. Rebuild it with the build arg set (see Build section #4).

### "Email already taken" when publishing agent

The PDS already has an account for that email from a previous publish. Either use a different email or wipe the f1r3sky postgres volume:

```bash
bash scripts/stop-all.sh --node rust-standalone --clean
bash scripts/start-all.sh --node rust-standalone
```

### Agent runs but produces empty results

OpenAI key not loaded. Check container env:

```bash
docker exec rnode.rust-standalone env | grep -i openai
```

If empty, edit `.env.node` and restart the Rust node with `--env-file .env.node`.

### Embers container exits during bootstrap

```bash
docker logs compose-embers-1 | tail -50
```

Common causes:
- Wrong Rust node version (see deserialization error above)
- Rust node not yet ready when embers started — restart embers: `docker restart compose-embers-1`
- Stale volume from prior run with different node version — wipe with `--clean`

### Docker daemon wedging after heavy builds

```bash
docker builder prune -f
```

## Known Issues

- **Embers**: See `services/embers/docs/embers-rust-node-updates.md`
- **Embers Frontend**: See `services/embers-frontend/docs/known-issues.md`
- **F1R3Sky Frontend**: See `services/f1r3sky/docs/known-issues.md`

## Related PRs

- Embers: https://github.com/F1R3FLY-io/embers/pull/168
- Embers Frontend: https://github.com/F1R3FLY-io/embers-frontend/pull/196
- System-integration: https://github.com/F1R3FLY-io/system-integration/pull/37
