# Docker Compose Unification Requirements

> Notes on what's needed to run the entire working stack (shard, embers, f1r3sky-backend, f1r3sky-frontend) in a single Docker compose setup.

## Current State vs Target

Currently the stack runs across 4 terminals with mixed Docker + local processes:
- **Docker**: 4 shard nodes (from `services/embers/docker/docker-compose.yaml`), PostgreSQL + Redis (from `services/f1r3sky-backend/packages/dev-infra/docker-compose.yaml`)
- **Local processes**: Embers (Rust binary), F1R3Sky Backend dev-env (7 Node.js in-process services), F1R3Sky Frontend (Expo dev server)

Existing partial compose files that cover parts of this:
- `docker-compose.yml` — F1R3node shard (multi-validator, different from embers/docker version)
- `docker-compose.embers.yml` — Embers API + Embers Frontend
- `docker-compose.f1r3sky.yml` — Full AT Protocol stack (PostgreSQL, Redis, PDS, BSKY, DataPlane, BSYNC, Ozone, F1R3Sky Frontend)

## Changes Required

### 1. All localhost URLs must become Docker service hostnames

Every environment variable currently pointing to `localhost:PORT` would change to use Docker's internal DNS resolution (container service names). Affected variables:

| Current (local) | Target (compose) | Service |
|-----------------|-------------------|---------|
| `http://localhost:14401` | `http://firefly:40401` | Embers → Shard deploy |
| `http://localhost:14402` | `http://firefly:40402` | Embers → Shard propose |
| `ws://localhost:14403` | `ws://firefly:40403` | Embers → Shard validator WS |
| `http://localhost:14413` | `http://firefly-read:40403` | Embers → Shard observer |
| `ws://localhost:14413` | `ws://firefly-read:40403` | Embers → Shard observer WS |
| `http://localhost:15401` | `http://firefly-testnet:40401` | Embers → Testnet deploy |
| `http://localhost:15402` | `http://firefly-testnet:40402` | Embers → Testnet propose |
| `ws://localhost:15403` | `ws://firefly-testnet:40403` | Embers → Testnet validator WS |
| `http://localhost:15413` | `http://firefly-read-testnet:40403` | Embers → Testnet observer |
| `ws://localhost:15413` | `ws://firefly-read-testnet:40403` | Embers → Testnet observer WS |
| `postgresql://pg:password@127.0.0.1:5433/postgres` | `postgresql://pg:password@postgres:5432/postgres` | AT Proto → DB |
| `127.0.0.1:6380` | `redis:6379` | BSKY → Redis |
| `http://[::1]:8080` | `http://embers:3000` | Frontend → Embers API |
| `ws://localhost:2583` | `ws://pds:2583` | BSKY → PDS firehose |
| `http://localhost:2584` | `http://bsky:2584` | Frontend → BSKY AppView |
| `http://localhost:2582` | `http://plc:2582` | All → PLC registry |
| `http://localhost:2587` | `http://ozone:2587` | PDS → Ozone |

### 2. Dev-env monolith must be split into separate containers

The current dev-env runs 7 services in one Node.js process. For compose, each should be a separate container:

| Dev-env in-process service | Compose container | Image needed |
|----------------------------|-------------------|--------------|
| PLC | `plc` | `f1r3flyindustries/f1r3sky-plc:latest` (needs building) |
| PDS | `pds` | `f1r3flyindustries/f1r3sky-pds:latest` (exists) |
| BSKY AppView | `bsky` | `f1r3flyindustries/f1r3sky-bsky:latest` (exists) |
| DataPlane | `dataplane` | `f1r3flyindustries/f1r3sky-bsky:latest` (same image, different entrypoint) |
| BSYNC | `bsync` | `f1r3flyindustries/f1r3sky-bsync:latest` (exists) |
| Ozone | `ozone` | `f1r3flyindustries/f1r3sky-ozone:latest` (exists) |
| Introspect | (optional, dev-only) | Could be omitted in compose |

### 3. Dynamic ports must be fixed

DataPlane and BSYNC currently use random ports via `get-port`. In compose they'd have fixed ports since containers address each other by hostname:

| Service | Fixed Port |
|---------|-----------|
| DataPlane | 2585 (matches existing docker-compose.f1r3sky.yml) |
| BSYNC | 3000 (internal, mapped to 3100 externally) |

### 4. thirdPartyPds bootstrap is problematic

The dev-env creates a temporary PDS to establish service identities (Ozone DID, Lexicon authority DID), then shuts it down. Options for compose:

**Option A: Pre-configured DIDs** (recommended)
- Use fixed signing keys for all services (like BSKY already does)
- Pre-register DIDs in PLC via an init container
- No bootstrap PDS needed

**Option B: Init container**
- Run a one-shot container that does the bootstrap dance
- Depends on PLC being ready
- Creates profiles, migrates to main PDS, exits
- More complex but matches current behavior

**Option C: Use `did:web:` for all services**
- Services identify themselves via their hostname
- No PLC registration needed for service DIDs
- Simpler but different from dev-env behavior

### 5. PDS blockchain integration decision

The local dev-env does NOT connect PDS to the shard. The existing `docker-compose.f1r3sky.yml` does:
```
DEPLOY_SERVICE_URL=http://rnode.validator1:40401
PROPOSE_SERVICE_URL=http://rnode.validator1:40402
READ_NODE_URL=http://rnode.readonly:40403
READ_NODE_WS_URL=ws://rnode.readonly:40403
DEFAULT_WALLET_KEY=232DADA5...
```

**Decision needed:** Should the unified compose include blockchain integration in the PDS? This enables on-chain data persistence but adds complexity and a hard dependency on the shard being healthy.

### 6. Database migrations needed

The dev-env handles schema creation in-process. Compose needs explicit migration containers:

| Migration | Creates Schema | Depends On |
|-----------|---------------|------------|
| `bsky-migrate` | `appview_bsky` (or `bsky`) | PostgreSQL healthy |
| `bsync-migrate` | `bsync` | PostgreSQL healthy |
| `ozone-migrate` | `ozone_db` | PostgreSQL healthy |
| `pds-migrate` | PDS tables | PostgreSQL healthy |

These are run-once containers (`restart: no`) that exit after completion.

### 7. Healthchecks needed for every service

| Service | Healthcheck | Currently Has One? |
|---------|-------------|-------------------|
| PostgreSQL | `pg_isready -U pg` | Yes |
| Redis | `redis-cli ping` | Yes |
| Shard Validators | `grpcurl + curl /status` | Yes |
| Shard Read-only | `grpcurl + curl /status` | Yes |
| PLC | `curl http://localhost:2582/health` or TCP check | No — needs adding |
| PDS | `curl http://localhost:2583/xrpc/_health` | No — needs adding |
| BSKY | `curl http://localhost:2584/xrpc/_health` | No — needs adding |
| Ozone | `curl http://localhost:2587/xrpc/_health` | No — needs adding |
| DataPlane | gRPC health check on port 2585 | No — needs adding |
| BSYNC | `curl http://localhost:3000/health` | No — needs adding |
| Embers | `curl http://localhost:3000/api/service/ready` | No — needs adding |
| F1R3Sky Frontend | `curl http://localhost:8100` | No — needs adding |

### 8. DID consistency across restarts

Services identify themselves by DIDs. If DIDs change on restart, cross-service trust breaks.

| Service | Current DID Source | Compose Strategy |
|---------|-------------------|-----------------|
| PDS | `did:web:localhost` | Use `did:web:pds.f1r3fly.local` (fixed) |
| BSKY | Static private key → consistent DID | Keep static key in env var |
| Ozone | Generated by thirdPartyPds bootstrap | Use fixed signing key → consistent DID |
| PLC | N/A (registry, not identified) | N/A |

### 9. TLS certificate is expired

The shared cert (`services/embers/docker/certs/`) expired Nov 15, 2020. Options:
- **Regenerate**: Create a fresh self-signed cert with longer validity
- **Disable verification**: Nodes already use `--allow-private-addresses`, could add TLS skip
- **Keep as-is**: Works in practice for dev (RChain doesn't enforce expiry)

### 10. Embers image differences

The existing `f1r3flyindustries/embers:latest` image differs from the local dev setup:

| Setting | Local (cargo make) | Docker image |
|---------|-------------------|--------------|
| Port | 8080 | 3000 |
| Bind address | `::1` (localhost only) | `::` (all interfaces) |
| Log level | `info,embers=trace` | `info` |
| Build type | debug | release |

The compose would use the Docker image configuration (port 3000, bind all interfaces). External port mapping handles the rest.

### 11. F1R3Sky Frontend: dev vs production

| Mode | Technology | Port | Image |
|------|-----------|------|-------|
| Dev (current) | Expo + Metro + Webpack | 19006 + 8081 | None (local process) |
| Production (compose) | bskyweb Go binary | 8100 | `f1r3flyindustries/f1r3sky:latest` |

The compose version would use the production Go server. Hot reloading is lost but startup is faster and the image is smaller.

### 12. Single unified Docker network

All services should be on one network for DNS resolution:

```yaml
networks:
  f1r3fly:
    driver: bridge
```

Currently the shard is on `docker_default` and dev-infra is on `dev-infra_default`. Merging them eliminates the need for published ports for inter-service communication (only publish ports needed for host access).

### 13. Genesis data access

Shard validators need genesis wallets and bonds. Options:
- **Bind mount** (current): Mount from host filesystem
- **Bake into image**: Build a custom shard image with genesis data
- **Config map**: Copy files into a named volume at startup

### 14. Resource considerations

| Service | Memory concern | Notes |
|---------|---------------|-------|
| Shard Validator (x2) | High — JVM + in-memory blockstore | 2.46 GB image, GraalVM |
| Shard Read-only (x2) | Medium — JVM but no consensus | Less active than validators |
| PostgreSQL | Low-Medium | Depends on data volume |
| Redis | Low | In-memory cache only |
| BSKY + DataPlane | Medium | Indexes all repo data |
| PDS | Low-Medium | SQLite + blob storage |
| Embers | Low | Rust binary, small footprint |
| F1R3Sky Frontend | Low | Static file server |

Total: Running 4 JVM nodes + 6 Node.js services + 1 Rust service + 2 data stores. Expect 8-12 GB RAM usage minimum.

### 15. Embers Frontend inclusion

The F1R3Sky frontend references `EXPO_PUBLIC_EMBERS_URL=http://localhost:5173` but nothing runs there currently. The existing `docker-compose.embers.yml` defines it:
- Build: `./services/embers-frontend` with `apps/embers/Dockerfile`
- Image: `f1r3flyindustries/embers-frontend:latest`
- Port: 5173→80
- Env: `API_URL=http://localhost:8080`

**Decision needed:** Include in unified compose? If yes, the F1R3Sky frontend's EXPO_PUBLIC_EMBERS_URL would point to it.

---

## Proposed Startup Order

```
Phase 1: Infrastructure (no dependencies)
├── postgres         (healthcheck: pg_isready)
└── redis            (healthcheck: redis-cli ping)

Phase 2: Blockchain Shard (no dependency on AT Proto)
├── firefly          (mainnet validator, healthcheck: grpcurl + curl)
├── firefly-testnet  (testnet validator, healthcheck: grpcurl + curl)
├── firefly-read     (depends: firefly healthy)
└── firefly-read-testnet (depends: firefly-testnet healthy)

Phase 3: Database Migrations (depends: postgres healthy)
├── bsky-migrate     (run-once, creates bsky schema)
├── bsync-migrate    (run-once, creates bsync schema)
└── ozone-migrate    (run-once, creates ozone_db schema)

Phase 4: AT Protocol Foundation (depends: postgres, redis, migrations)
├── plc              (healthcheck: /health endpoint)
└── dataplane        (depends: postgres, plc; healthcheck: gRPC)

Phase 5: AT Protocol Services (depends: Phase 4)
├── bsync            (depends: postgres, migrations)
├── bsky             (depends: dataplane, bsync, plc, redis)
├── pds              (depends: bsky, plc, optionally shard)
└── ozone            (depends: pds, bsky, plc)

Phase 6: Applications (depends: Phase 2 + Phase 5)
├── embers           (depends: firefly healthy, firefly-read healthy)
├── embers-frontend  (depends: embers; optional)
└── f1r3sky          (depends: bsky, pds, embers)
```

## Open Questions

1. Should PDS have blockchain integration (DEPLOY_SERVICE_URL, etc.) in the unified compose?
2. Should the embers-frontend be included?
3. Should we use the dev-env monolith container or split services (existing images exist for split)?
4. Should DID bootstrapping use fixed keys, an init container, or did:web?
5. Should the TLS cert be regenerated or kept as-is?
6. Should we support both dev mode (hot reload, debug builds) and production mode (optimized builds, static serving)?

---

# Why the Docker Compose Production Setup is Broken

> Analysis of why the existing `docker-compose.f1r3sky.yml` (and related compose files) fail while the local development setup works.

## Issue 1: Build Path Typo (Critical — Build Fails Immediately)

**File:** `docker-compose.f1r3sky.yml`, f1r3sky build context

**Root cause:** Typo in the Docker build context path (`sevices` vs `services`).

**Symptoms:** `docker compose build f1r3sky` fails immediately with "unable to prepare context: path not found".

**Code path:** Docker daemon resolves the `context:` path relative to the compose file location. When `./sevices/f1r3sky` doesn't exist, the build can't even start.

**Fix:** Corrected to `./services/f1r3sky` in compose file.

---

## Issue 2: Network/Hostname Mismatch (Critical — Services Can't Find Each Other)

**Root cause:** The production compose files reference shard hostnames (`rnode.validator1`, `rnode.readonly`) that only exist in the root `docker-compose.yml` multi-validator setup, but the working local shard uses completely different hostnames (`firefly`, `firefly-read`) from `services/embers/docker/docker-compose.yaml`.

**Symptoms:** Embers containers start but fail all gRPC/HTTP calls with connection refused or DNS resolution failures. Logs show `failed to connect to rnode.validator1:40401`.

**Code path:** Embers' Figment config reads `EMBERS__MAINNET__DEPLOY_SERVICE_URL` → creates tonic gRPC channel → fails DNS resolution for `rnode.validator1` on the Docker network.

**The two shard configurations are architecturally different:**

| | Local Shard (embers/docker) | Production Shard (root docker-compose.yml) |
|---|---|---|
| Network | `docker_default` | `f1r3fly` |
| Validator hostname | `firefly` | `rnode.validator1` |
| Read-only hostname | `firefly-read` | `rnode.readonly` |
| Architecture | Single validator, standalone mode | Multi-validator (boot + 3 validators + readonly) |
| Genesis | `embers/docker/mainnet/genesis/` | `f1r3node/docker/genesis/` |
| Private key | `6a786ec3...` | `357cdc42...` (validator1) |
| Validator ports (host) | 14401-14403 | 40411-40413 |
| Read-only ports (host) | 14413 | 40453 |

**Fix:** Dev compose uses `firefly`/`firefly-read` hostnames matching the working shard. Production compose keeps `rnode.validator1`/`rnode.readonly` but requires the root shard compose to be running first.

---

## Issue 3: No Local PLC Server (DID Resolution Fails)

**Root cause:** `BSKY_DID_PLC_URL=https://plc.directory` (public internet) but service DIDs use `did:web:bsky.f1r3fly.local` which requires DNS resolution for a non-existent domain.

**Symptoms:** Services start but DID resolution fails silently. Cross-service identity verification breaks — BSKY can't verify PDS identity, PDS can't verify Ozone identity. User account creation fails because PLC can't register DIDs for the local domain.

**Code path:** `@atproto/identity` → `DidResolver.resolve(did)` → `did:web` handler → HTTP GET `http://bsky.f1r3fly.local/.well-known/did.json` → DNS failure.

**How local avoids this:** The dev-env runs its own PLC server on port 2582 and uses dynamic `did:plc:` identifiers that don't require DNS.

**Fix:** Dev compose runs a local PLC container (`@did-plc/server` with in-memory SQLite via `Database.mock()`). Service DIDs use `did:web:` with `PDS_DEV_MODE=true` (allows HTTP resolution via Docker DNS). User accounts use `did:plc:` registered against the local PLC. All services point `DID_PLC_URL` to `http://plc:2582`. Production requires either: (a) a local PLC container, (b) proper DNS setup for `*.f1r3fly.local`, or (c) accepting that DID resolution is degraded.

---

## Issue 4: Hardcoded Private IP Addresses

**Root cause:** Developer's local LAN IP (`192.168.1.130`) hardcoded in `.env.f1r3sky` for `BSKY_PUBLIC_URL`, `PDS_HOSTNAME`, and `OZONE_PUBLIC_URL`.

**Symptoms:** On any machine without IP 192.168.1.130 assigned, PDS generates incorrect DID documents, BSKY returns wrong public URLs in API responses, and Ozone's public URL is unreachable.

**Code path:** PDS reads `PDS_HOSTNAME` → writes it into DID documents and session responses → clients try to connect to that hostname → connection refused on other machines.

**Fix:** Replaced with `localhost` in `.env.f1r3sky`. For production deployments, these should be parameterized or set to the actual public hostname.

---

## Issue 5: EXPO_PUBLIC_* Vars Not Baked Into Static Bundle

**Root cause:** The Dockerfile doesn't include `EXPO_PUBLIC_EMBERS_API_URL` or `EXPO_PUBLIC_EMBERS_URL` in the `.env` file written before `yarn build-web`. The bskyweb Go binary serves pre-compiled static JS and has no mechanism to inject env vars at runtime.

**Symptoms:** Wallet and agent features in the F1R3Sky frontend silently fail. `EmbersApiSdk` is instantiated with `basePath: undefined`. All blockchain-related API calls go to the wrong URL or fail with network errors.

**Code path:**
1. `services/f1r3sky/src/state/wallets.tsx:42` — `process.env.EXPO_PUBLIC_EMBERS_API_URL` → webpack DefinePlugin replaces with string literal at build time → if not in `.env` file, replaced with `undefined`
2. `services/f1r3sky/src/view/shell/desktop/RightNav.tsx:29` — `process.env.EXPO_PUBLIC_EMBERS_URL` → same issue, Embers UI link is broken

**How local avoids this:** Expo dev server reads `.env` at runtime and webpack DefinePlugin replaces values during live transpilation.

**Fix:** Added `ARG EXPO_PUBLIC_EMBERS_API_URL` / `ARG EXPO_PUBLIC_EMBERS_URL` to Dockerfile with echo to `.env`. Compose passes values as build args.

---

## Issue 6: Ozone Disabled But Still Referenced

**Root cause:** Ozone service was commented out in compose but its environment variables remain in `.env.f1r3sky`, causing other services to attempt connections to a non-existent container.

**Symptoms:** PDS logs show connection timeouts to `ozone:3001` on moderation-related operations. BSKY may fail to start if it requires a connection to the mod service DID. Label application silently fails.

**Code path:** PDS reads `PDS_MOD_SERVICE_URL=http://ozone:3001` → on moderation actions, attempts HTTP call → DNS resolution fails (no ozone container) → timeout after 30s.

**Fix:** Un-commented Ozone service in compose file. All mod service environment variables are now consistent with a running Ozone container.

---

## Issue 7: PDS Blockchain Integration Likely Doesn't Exist in Source

**Root cause:** The env vars `DEPLOY_SERVICE_URL`, `PROPOSE_SERVICE_URL`, `READ_NODE_URL`, `DEFAULT_WALLET_KEY` are set on the PDS container, but the PDS TypeScript source (`packages/pds/src`) contains no code that reads them.

**Symptoms:** No visible error — the env vars are silently ignored. Blockchain integration simply doesn't happen. Data is stored only in SQLite/PostgreSQL, not on the shard.

**Code path:** PDS uses Figment-style config reading → only reads known env vars defined in its config schema → blockchain vars are not in the schema → ignored.

**Fix:** Dev compose removes these vars entirely. Production compose keeps them documented as aspirational (for when PDS blockchain integration is implemented in the source).

---

## Issue 8: embers-client-sdk Version Drift

**Root cause:** `@f1r3fly-io/embers-client-sdk` version 0.0.79 in `package.json` may not match the current Embers API source. The SDK is pre-1.0 with no stability guarantees.

**Symptoms:** After Docker image build, frontend blockchain features may return 404s, deserialization errors, or validation failures if the Embers API has changed since SDK 0.0.79 was published.

**Code path:** `f1r3sky/src/state/wallets.tsx` → `EmbersApiSdk.walletControllerCreate()` → HTTP POST to endpoint defined in SDK → if endpoint renamed/changed → 404 or 500.

**How local avoids this:** Developer runs both Embers source and frontend simultaneously — API changes are immediately visible.

**Fix:** Dev compose builds Embers from source (`docker/embers.dockerfile` in the embers repo), guaranteeing the running API matches the source code. The embers-client-sdk in the frontend may still drift — if SDK methods don't match the built API, a new SDK version must be generated from the Embers OpenAPI spec and published. For the dev compose, this is mitigated by building from source (API is always current).

---

## Issue 9: npmrc Secret Requirement

**Root cause:** The `@f1r3fly-io/embers-client-sdk` package is hosted on GitHub Packages (private registry). Docker build needs `~/.npmrc` with a valid GitHub Personal Access Token that has `read:packages` scope.

**Symptoms:** `docker compose build f1r3sky` fails at `yarn install` with `401 Unauthorized` or `404 Not Found` for the `@f1r3fly-io` scope.

**Code path:** yarn reads `.npmrc` (mounted as Docker secret) → authenticates to `npm.pkg.github.com` → downloads `@f1r3fly-io/embers-client-sdk@0.0.79` → if token invalid → build fails.

**Fix:** Documented in compose file header comments (both `docker-compose.dev.yml` and `docker-compose.embers.yml`). Users must create `~/.npmrc` with a GitHub Personal Access Token that has `read:packages` scope:

```
//npm.pkg.github.com/:_authToken=ghp_XXXXX
@f1r3fly-io:registry=https://npm.pkg.github.com
```

The compose files reference this as a Docker secret (`secrets: npmrc: file: ~/.npmrc`). Without it, builds of `embers-frontend` and `f1r3sky` will fail at `yarn install`.

---

## Issue 10: EXPO_PUBLIC_EMBERS_* Missing from Dockerfile .env Write Block

**Root cause:** The Dockerfile's `.env` write block (before `yarn build-web`) only writes 5 variables: `EXPO_PUBLIC_ENV`, `EXPO_PUBLIC_RELEASE_VERSION`, `EXPO_PUBLIC_BUNDLE_IDENTIFIER`, `EXPO_PUBLIC_BUNDLE_DATE`, `EXPO_PUBLIC_SENTRY_DSN`. It does NOT write `EXPO_PUBLIC_EMBERS_API_URL` or `EXPO_PUBLIC_EMBERS_URL`.

**Symptoms:** The webpack DefinePlugin sees no value for these variables during the production build. `process.env.EXPO_PUBLIC_EMBERS_API_URL` compiles to the literal string `undefined`. The `EmbersApiSdk` constructor receives `undefined` as `basePath`, causing all SDK calls to fail.

**Code path:**
1. Dockerfile RUN block: `echo "EXPO_PUBLIC_ENV=$EXPO_PUBLIC_ENV" >> .env` (only 5 vars written)
2. `yarn build-web` → webpack reads `.env` via expo's config → DefinePlugin maps `process.env.EXPO_PUBLIC_*` → only finds 5 vars
3. `src/state/wallets.tsx:42`: `new EmbersApiSdk({ basePath: process.env.EXPO_PUBLIC_EMBERS_API_URL })` → compiles to `new EmbersApiSdk({ basePath: undefined })`

**Fix:** Added `ARG EXPO_PUBLIC_EMBERS_API_URL` and `ARG EXPO_PUBLIC_EMBERS_URL` to Dockerfile, with corresponding echo lines in the `.env` write block.

---

## Issue 11: Testnet Points to Mainnet (Intentional but Undocumented)

**Root cause:** In `.env.embers`, all testnet URLs point to the same `rnode.validator1` / `rnode.readonly` hostnames as mainnet. There is no separate testnet shard running.

**Symptoms:** Testnet operations modify mainnet state. No isolation between test and production data. Wallets created on "testnet" consume mainnet REV.

**Code path:** Embers reads `EMBERS__TESTNET__DEPLOY_SERVICE_URL=http://rnode.validator1:40401` → connects to same gRPC endpoint as mainnet → deploys go to same blockchain.

**Fix:** Dev compose runs separate `firefly-testnet` / `firefly-read-testnet` containers with independent genesis. Production compose documents this as intentional (single shard, dual endpoints) until a separate testnet shard is deployed.

---

## Issue 12: Encryption Key Divergence Between Local and Docker

**Root cause:** `.env.embers` uses different encryption keys from the local `Makefile.toml`:

| Key | Makefile.toml (local) | .env.embers (Docker) |
|-----|----------------------|---------------------|
| WALLETS_ENV_KEY | `8BDC54B5...` | `7194fdd6...` |
| AGENTS_ENV_KEY | `69D4BC8E...` | `431609d5...` |
| AGENTS_TEAMS_ENV_KEY | `85348C6D...` | `ee8e8b28...` |

**Symptoms:** Data encrypted by local Embers cannot be decrypted by Docker Embers (and vice versa). Wallet data written to the shard by one environment is unreadable by the other — appears as corrupted ciphertext.

**Code path:** Embers `WalletsService` → `AesGcm::decrypt(ciphertext, key)` → if key doesn't match the one used for encryption → `aead::Error` → wallet data unreadable.

**Fix:** Dev compose uses Makefile.toml keys (matching local dev). Production compose uses its own keys. Document that switching between environments requires a fresh shard (new genesis) since existing encrypted data won't be readable with different keys.

---

## Issue 13: Redundant Port 80 Mapping on Embers

**Root cause:** `docker-compose.embers.yml` maps both `8080:3000` and `80:3000`, exposing Embers on port 80 with no documentation or purpose.

**Symptoms:** Port 80 conflicts with any existing web server on the host. On Linux, port 80 requires root privileges unless `net.ipv4.ip_unprivileged_port_start` is configured. On macOS, may conflict with AirPlay Receiver.

**Code path:** Docker publishes container port 3000 to host port 80 → if port 80 already in use → compose fails to start with "port already allocated".

**Fix:** Removed the `80:3000` port mapping. Only `8080:3000` remains.

---

## Issue 14: External Network Dependency (Startup Ordering)

**Root cause:** `docker-compose.embers.yml` and `docker-compose.f1r3sky.yml` declare `network: f1r3fly` as `external: true`, meaning the network must already exist before these compose files can be used.

**Symptoms:** Running `docker compose -f docker-compose.embers.yml up` without first starting the shard compose fails with: `network f1r3fly declared as external, but could not be found`.

**Code path:** Docker daemon checks for network existence during compose up → if not found → error before any container starts.

**Fix:** Dev compose uses its own `dev` network (not external). Production compose documents that the shard must start first (`docker compose up` creates the `f1r3fly` network).

---

## Issue 15: embers-frontend API_URL is Browser-Side

**Root cause:** `API_URL: "http://localhost:8080"` is set as a runtime env var on the nginx container, but this URL is used by the browser (client-side JavaScript), not the container itself.

**Symptoms:** If API_URL were set to a Docker hostname (e.g., `http://embers:3000`), it would work for inter-container requests but fail in the browser (which can't resolve Docker DNS). Current value (`localhost:8080`) is correct for browser access but the intent isn't documented.

**Code path:** embers-frontend nginx serves static JS → browser loads app → JS reads API_URL (injected at build or runtime) → makes fetch() to that URL → if URL uses Docker hostname → browser DNS fails.

**Fix:** Added comment clarifying this is a browser-side URL. Value remains `http://localhost:8080` (the host-published port of the Embers container).

---

## Issue 16: No Healthcheck on Embers Container

**Root cause:** The Embers Docker image is a minimal Rust binary — it doesn't include `curl`, `wget`, or other HTTP clients needed for traditional healthchecks. The compose file had the healthcheck commented out.

**Symptoms:** `depends_on: embers: condition: service_healthy` can't be used by downstream services. Compose can't tell if Embers is ready to accept connections.

**Code path:** Docker healthcheck requires a command inside the container → no HTTP client available → can't `curl /api/service/ready` → healthcheck can't be defined.

**Fix:** Added TCP healthcheck using bash built-in: `cat < /dev/tcp/localhost/3000 || exit 1`. This checks if the port is accepting connections without needing external tools.

---

## Expo Dev Server vs bskyweb Production Build

Understanding the two modes of serving the F1R3Sky frontend is critical to fixing the EXPO_PUBLIC variable issues:

### Expo Dev Server (local development: `yarn web` / `expo start --web`)

1. **Webpack watch mode**: Starts webpack-dev-server which watches source files for changes
2. **Runtime .env reading**: Expo reads `.env` file on startup and on each rebuild
3. **DefinePlugin injection**: webpack's DefinePlugin replaces all `process.env.EXPO_PUBLIC_*` references with string literals during transpilation
4. **Hot Module Replacement (HMR)**: Changes are pushed to the browser without full page refresh
5. **Port**: Serves on port 19006 (web) with Metro bundler on 8081
6. **Environment changes**: Modifying `.env` and restarting the dev server immediately picks up new values

### Production Build (`yarn build-web`)

1. **One-time webpack build**: Runs webpack in production mode (tree-shaking, minification, chunk splitting)
2. **DefinePlugin bakes values**: At build time, DefinePlugin replaces `process.env.EXPO_PUBLIC_*` with literal strings in the output JS bundle. After build, these values are immutable.
3. **Output**: `web-build/static/` directory containing optimized JS/CSS/media files

### post-web-build.js

After `yarn build-web`, a post-build script copies `web-build/static/` → `bskyweb/static/` (JS, CSS, media files). This is what the Go binary serves.

### bskyweb Go Binary

1. **HTTP server**: Serves static files from an embedded filesystem (`bskyweb/static/`)
2. **Pongo2 templates**: Server-rendered HTML templates — but only injects `staticCDNHost` (for CDN URLs), NOT env vars
3. **`ATP_APPVIEW_HOST`**: Used server-side for OG card meta tag fetching (SEO), not passed to client
4. **No client-side env injection**: The Go binary has no mechanism to inject environment variables into the pre-compiled JavaScript bundle

### The Critical Gap

The Dockerfile only writes these EXPO_PUBLIC vars to `.env` before the build:
- `EXPO_PUBLIC_ENV`
- `EXPO_PUBLIC_RELEASE_VERSION`
- `EXPO_PUBLIC_BUNDLE_IDENTIFIER`
- `EXPO_PUBLIC_BUNDLE_DATE`
- `EXPO_PUBLIC_SENTRY_DSN`

But NOT:
- `EXPO_PUBLIC_EMBERS_API_URL` (used in `src/state/wallets.tsx:42`)
- `EXPO_PUBLIC_EMBERS_URL` (used in `src/view/shell/desktop/RightNav.tsx:29`)

**Result:** In the production build, `process.env.EXPO_PUBLIC_EMBERS_API_URL` compiles to `undefined` → `EmbersApiSdk` has no basePath → all blockchain wallet/agent calls fail silently.

### Dev Compose Solution

For the dev compose, we use the Expo dev server (`Dockerfile.dev`) instead of the Go binary. This means:
- EXPO_PUBLIC vars are passed as runtime Docker environment variables
- Webpack reads them during live transpilation
- No pre-compilation step needed
- Changes can be made by restarting the container with new env vars

---

## Note: Embers Config Model

The Embers API's configuration uses separate URLs for deploy, propose, and observer — this is correct and matches the `Config` struct in the Rust source. The issue is not the URL structure but hostname resolution:

```rust
// Correct config structure (from Figment env parsing):
struct ClusterConfig {
    deploy_service_url: String,    // gRPC deploy endpoint
    propose_service_url: String,   // gRPC propose endpoint
    observer_url: String,          // HTTP read endpoint
    observer_ws_api_url: String,   // WebSocket read endpoint
    validator_ws_api_url: String,  // WebSocket validator endpoint
    service_key: String,           // Wallet private key
    wallets_env_key: String,       // Wallet encryption key
    agents_env_key: String,        // Agent encryption key
    agents_teams_env_key: String,  // Teams encryption key
    oslfs_env_key: String,         // OSLFS encryption key
}
```

In the local dev stack, deploy and propose go to the **validator** (`firefly:40401`, `firefly:40402`) while observer goes to the **read-only node** (`firefly-read:40403`). This separation is intentional: writes go to the consensus participant, reads go to the non-consensus follower (reducing validator load).

---

## Summary: All Issues

| # | Issue | Severity | Status |
|---|-------|----------|--------|
| 1 | Build path typo (`sevices` → `services`) | Critical | Fixed |
| 2 | Network/hostname mismatch (rnode.* vs firefly) | Critical | Fixed (dev compose uses correct hostnames) |
| 3 | No local PLC server (DID resolution fails) | High | Fixed (dev runs local PLC container + did:web: with DEV_MODE) |
| 4 | Hardcoded IP 192.168.1.130 | High | Fixed (replaced with localhost) |
| 5 | EXPO_PUBLIC_* not baked into bundle | High | Fixed (build args + Dockerfile) |
| 6 | Ozone disabled but referenced | Medium | Fixed (un-commented Ozone) |
| 7 | PDS blockchain integration missing in source | Low | Documented (aspirational env vars) |
| 8 | embers-client-sdk version drift | Medium | Mitigated (dev builds Embers from source; SDK still may drift) |
| 9 | npmrc secret requirement | Medium | Documented (compose headers + setup instructions) |
| 10 | EXPO_PUBLIC_EMBERS_* missing from Dockerfile | High | Fixed (ARG + echo) |
| 11 | Testnet == Mainnet | Low | Fixed in dev (separate nodes); documented in prod |
| 12 | Encryption key divergence | Medium | Fixed (dev uses Makefile.toml keys) |
| 13 | Port 80 redundant mapping | Low | Fixed (removed) |
| 14 | External network dependency | Low | Fixed in dev (own network); documented in prod |
| 15 | embers-frontend API_URL confusion | Low | Fixed (documented as browser-side) |
| 16 | No embers healthcheck | Medium | Fixed (TCP healthcheck) |

The Docker compose configuration represents a fundamentally different architecture that was never fully aligned with the working local development setup. The dev compose (`docker-compose.dev.yml`) bridges this gap by matching the local architecture in a single compose file. The production compose files are now fixed to be functional but still require the root shard compose to be running first.
