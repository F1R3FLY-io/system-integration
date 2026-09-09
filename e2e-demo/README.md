# E2E Demo Test

Automated end-to-end test for the F1R3FLY demo flow. Exercises the full pipeline via API calls:

1. Start services (embers, f1r3sky, frontends)
2. Create an agent team
3. Save a graph (input -> text-model -> output)
4. Deploy to blockchain
5. Run the agent team
6. Publish to F1R3Sky
7. Verify the agent profile exists
8. Create a @mention post and trigger an agent reply
9. Teardown services

## Prerequisites

- Docker running with the shard already started
- Node.js 20+
- The embers-frontend SDK built locally (the wrapper script handles this)

## Setup

1. Ensure embers-frontend dependencies are installed:

```bash
cd services/embers-frontend && pnpm install
```

2. Install e2e-demo dependencies:

```bash
cd e2e-demo && npm install
```

The SDK is referenced as a local file dependency (`file:../services/embers-frontend/packages/client`) — no NPM token needed.

## Running

From the repo root via wrapper script (recommended — builds SDK if needed):

```bash
scripts/e2e-demo-test.sh
```

Or directly from this directory:

```bash
npx tsx demo-test.ts
```

### Options

- `--no-teardown` — Leave services running after the test completes (pass or fail). Useful for debugging failures.

```bash
scripts/e2e-demo-test.sh --no-teardown
```

## Configuration

All config has sensible defaults matching `scripts/start-all.sh`. Override via environment variables or `.env` file. See `.env.example` for the full list.

## What it does

The test manages the full service lifecycle:
- **Startup**: Asserts the shard is healthy, then runs `scripts/start-all.sh` (uses Docker Compose)
- **Test phases**: Exercises create -> save -> deploy -> run -> publish -> verify via the embers API and AT Protocol
- **Teardown**: Runs `scripts/stop-all.sh --clean` on completion (unless `--no-teardown`)

The shard is NOT started or stopped by the test. Start it separately before running.

## Timeouts

Blockchain finalization dominates test duration. Default timeouts (SDK default 120s):
- Create/Save: 120s
- Deploy: 120s
- Run: 120s
- Total expected runtime: ~5-8 minutes

## Known Issues

- Phase 6 (publish) uses direct `fetch` calls instead of the SDK because the SDK's `publishToFiresky` method has a serialization bug with `invite_code` (fixed in local SDK source, pending publish to npm)
- The `pds_url` passed to embers publish must use the Docker-internal hostname (`http://f1r3sky:2583`), not `localhost`, because embers runs inside Docker
- Finalization times vary with shard state — a shard with many accumulated deploys may take longer to finalize
