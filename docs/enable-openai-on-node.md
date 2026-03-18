# Enable AI System Processes on Rust Node

## Context

The f1r3fly Rust node has three AI system processes built in but disabled by default:
- `rho:ai:gpt4` (byte_name 20) — GPT-4o-mini text completion
- `rho:ai:dalle3` (byte_name 21) — DALL-E 3 image generation (1024x1024)
- `rho:ai:textToAudio` (byte_name 22) — TTS-1 speech synthesis

These are implemented in:
- `rholang/src/rust/interpreter/system_processes.rs` (lines 833-959)
- `rholang/src/rust/interpreter/external_services.rs` (OpenAIService enum)
- `rholang/src/rust/interpreter/rho_runtime.rs` (lines 850-933, dispatch registration)

They use the `OpenAIService` which has two variants:
- `Enabled { client, model_config }` — makes real API calls
- `NoOp` — returns empty strings gracefully (used when disabled or on observer nodes)

## What Needs to Change

**Only configuration — no code changes required.**

### Option A: Environment Variables (recommended for Docker)

Add to validator node services in `services/f1r3node-rust/docker/shard.yml` or `docker/.env`:

```
OPENAI_ENABLED=true
OPENAI_API_KEY=sk-...your-key...
```

Only add to **validator** nodes (bootstrap, validator1, validator2, validator3). Do NOT add to observer/readonly — observer nodes use NoOp for security (`ExternalServices::for_observer()`).

### Option B: Config File

Add to `services/f1r3node-rust/docker/conf/default.conf`:

```conf
openai {
  enabled = true
  api-key = "sk-...your-key..."
  validate-api-key = true
  validation-timeout-sec = 15
}
```

## Steps

1. OpenAI API key is available from the Oracle staging instance (`192.9.243.149:/home/ubuntu/projects/embers/docker/.env`). The variable is `OPENAI_SCALA_CLIENT_API_KEY` (legacy name, Rust node accepts it as fallback).
2. Add `OPENAI_ENABLED=true` and `OPENAI_SCALA_CLIENT_API_KEY=<key>` to validator node environment
3. Restart the shard: `cd services/f1r3node-rust/docker && docker compose -f shard.yml down && docker compose -f shard.yml up -d`
4. Verify: check validator logs for `OpenAI service enabled` or absence of `OpenAI service disabled`
5. Test: deploy and run an agent team with a text prompt — should return GPT-4 generated text

## Important Notes

- The node validates the API key at startup if `validate-api-key = true`. If the key is invalid, the node will panic with a clear error.
- Default config at `node/src/main/resources/defaults.conf:388-430` has `enabled = false`.
- AI processes are always registered in the dispatch table for replay compatibility. When disabled, they return empty results (NoOp) instead of failing.
- GPT-4o-mini is used (not GPT-4), DALL-E 3 at 1024x1024, TTS-1 with shimmer voice.
- After shard restart, embers needs restart too (fresh blockchain state requires re-bootstrapping init contracts).
- All f1r3sky services (postgres, redis, f1r3sky, f1r3sky-frontend) also need restart after shard restart.
- See `docs/local-dev-setup.md` in the embers repo for full startup sequence.

## Known Issue: WebSocket Block Events Not Flowing

After enabling OpenAI and running an agent team, the deploy executes successfully on the blockchain (confirmed in validator logs — block created in ~15 seconds with AI call), but the WebSocket at `/ws/events` on both validator and readonly nodes only sends the initial `{"event":"started","schema-version":1}` and no subsequent block events (`block-created`, `block-added`, `block-finalised`).

This causes embers' `wait_for_deploy` to timeout with `block is not finalized` even though the block was finalized. The event publisher in the node may not be wired to the WebSocket endpoint.

**Impact:** Manual run of agent teams times out. The AI execution works but embers can't detect when it completes.

**Investigation needed:** Check if the `F1r3flyEvents` publisher (`shared/src/rust/shared/f1r3fly_events.rs`) is connected to the WebSocket handler (`node/src/rust/web/events_info.rs`). The events may be published internally but not forwarded to WebSocket subscribers.

## Files Reference

| File | Purpose |
|---|---|
| `rholang/src/rust/interpreter/system_processes.rs` | AI process implementations (gpt4, dalle3, textToAudio) |
| `rholang/src/rust/interpreter/external_services.rs` | OpenAIService enum (Enabled/NoOp) |
| `rholang/src/rust/interpreter/rho_runtime.rs` | Dispatch table registration |
| `node/src/main/resources/defaults.conf` | Default config (openai section at line 388) |
| `docker/shard.yml` | Docker compose for validator nodes |
| `docker/.env` | Environment variables for shard |
