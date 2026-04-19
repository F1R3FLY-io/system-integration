# test_token_metadata

## Purpose

Comprehensive test suite for the native token metadata feature (Option B: on-chain TokenMetadata contract). Covers 6 groups spanning the full feature surface: happy path, joiner mismatch detection, config validation, restart drift, multi-shard isolation, and genesis ceremony blocking.

## Architecture

Group A (happy path) uses the session-scoped `shared_shard` fixture, asserting against token values from the `node_conf` fixture (`shard_id`, `native_token_name`, `native_token_symbol`, `native_token_decimals` parsed from `defaults.conf` + `conf/rust.conf` via pyhocon). No hardcoded `DEFAULT_NAME`/`DEFAULT_SYMBOL`/`DEFAULT_DECIMALS` constants. Groups B-F use standalone nodes and custom shards with failure-mode extensions:

- `provider.create_standalone(config, wait_running=False)` — nodes expected to fail at startup
- `provider.recreate_standalone(handle, new_config, wait_running=False)` — restart drift tests
- `provider.add_node(..., wait_running=False)` — joiners expected to fail
- `handle.wait_for_exit(timeout)` / `handle.exit_code()` — exit code tracking
- `log_events.find_event(node.logs(), event="...")` — structured log event parsing

## Tests (17)

### Group A -- Happy path (4 tests, shared shard)

Uses the session-scoped `shared_shard` with token metadata from the `node_conf` fixture. All tests check **every node** in the shard (validators + readonly) unless noted.

| Test | What it verifies |
|------|-----------------|
| `test_api_status_returns_configured_token` | `/api/status` reports default name/symbol/decimals on all nodes |
| `test_on_chain_all_method_matches_config` | `TokenMetadata!("all", ret)` tuple matches defaults on all nodes (gRPC) |
| `test_on_chain_individual_methods_match_all` | `name`/`symbol`/`decimals` methods individually match `all` tuple on all nodes |
| `test_startup_log_announces_token_metadata` | Boot + all validators log `native_token_metadata_startup` event with default values (boot as `ceremony_master`, validators as `genesis_validator`; readonly excluded — it doesn't emit this event) |

### Group B -- Joiner mismatch (5 tests, standalone baseline + joiners)

Uses a module-scoped baseline standalone. Joiners with mismatched configs must emit `native_token_metadata_mismatch` events naming the disagreeing fields.

| Test | Override | Expected mismatch field |
|------|----------|------------------------|
| `test_joiner_mismatch_fails_startup[name_only]` | name | `native-token-name` |
| `test_joiner_mismatch_fails_startup[symbol_only]` | symbol | `native-token-symbol` |
| `test_joiner_mismatch_fails_startup[decimals_only]` | decimals | `native-token-decimals` |
| `test_joiner_mismatch_all_three_fields` | all three | all three fields |
| `test_joiner_matching_config_succeeds` | none (control) | `native_token_metadata_verified` event |

### Group C -- Config validation (5 tests, standalone failure mode)

| Test | Input | Expected behavior |
|------|-------|-------------------|
| `test_decimals_negative_rejected` | `--native-token-decimals=-1` | Clap rejects, non-zero exit |
| `test_decimals_above_max_rejected` | `--native-token-decimals=19` | Clap rejects (max 18) |
| `test_empty_string_name_rejected` | `--native-token-name=""` | Config validation, non-zero exit |
| `test_whitespace_only_symbol_rejected` | `--native-token-symbol="   "` | Config validation, non-zero exit |
| `test_special_characters_in_token_name_round_trip` | `F1R3-CAP/v2!` | Survives CLI -> template -> on-chain -> API |

### Group D -- Restart drift (1 test)

`test_restart_with_changed_token_config_fails_verification` — starts a node with INITIAL token, stops it, restarts with DIFFERENT token against the same data volume. The immutable on-chain contract disagrees with the new config, triggering `native_token_metadata_mismatch` and a non-zero exit.

### Group E -- Multi-shard isolation (1 test)

`test_two_shards_with_different_tokens_dont_interfere` — two concurrent standalone nodes with different tokens (ALPHA/BETA) each report their own values correctly via both API and on-chain queries.

### Group F -- Genesis ceremony mismatch (1 test)

`test_genesis_validator_with_wrong_token_blocks_ceremony` — bootstrap + V1 use MASTER_TOKEN, V2 + V3 use WRONG. With `--required-signatures=2`, the ceremony needs 2/3 validator signatures. The disagreeing validators refuse to sign (blessed-contract hash mismatch), stalling the ceremony so no node reaches Running.

## What it proves

- Token metadata is correctly threaded from CLI -> config -> genesis -> on-chain contract -> API
- All four contract methods (name, symbol, decimals, all) are consistent
- Every node (validators + readonly) reports identical metadata via both HTTP API and on-chain gRPC
- Mismatched joiners are detected and logged with specific field names
- CLI validation catches invalid inputs (range, empty, whitespace)
- Special characters survive the full round-trip without corruption
- Immutable on-chain contracts prevent config drift after genesis
- Genesis ceremony blocks when validators disagree on token metadata
- Multiple shards with different tokens coexist without interference

## Infrastructure used

- `node_conf` — session-scoped fixture providing parsed config values (name, symbol, decimals, shard_id, ftt)
- `shared_shard` — session-scoped shard for Group A happy-path tests
- `provider.create_standalone()` — standalone nodes for Groups B-E
- `provider.add_node(..., wait_running=False)` — observer joiners for Group B
- `provider.recreate_standalone()` — restart drift for Group D
- `provider.create_shard(wait_running=False)` — ceremony mismatch for Group F
- `infra/log_events.py` — structured log event parsing
- `infra/token_metadata.py` — `fetch_api_status_token()`, on-chain query helpers via pyf1r3fly

## Related

- [Native Token Metadata implementation](../../../docs/session-context-2026-04-14-16.md)
- f1r3node-rust PR #481 — on-chain contract, config, API, startup verification
