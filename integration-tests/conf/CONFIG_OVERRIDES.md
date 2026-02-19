# Integration Test Config Overrides

These config files are based on `system-integration/conf/` (the dev shard configs) with overrides tuned for the CI integration test environment. This document tracks the functional differences.

## Baseline

Base configs: `../../conf/*.conf` (system-integration root)

## Overrides (all files)

| Setting                                                 | Upstream    | Integration Tests | Reason                                                                                                                                                                                   |
| ------------------------------------------------------- | ----------- | ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `protocol-client.network-timeout`                       | `5 seconds` | `15 seconds`      | 5 JVM containers share 2 OCPUs on CI runners. GC pauses and CPU contention cause false-positive heartbeat timeouts at 5s, leading to bootstrap eviction from the Kademlia routing table. |
| `casper.genesis-block-data.number-of-active-validators` | `100`       | `10000`           | Ensures all test validators can be active simultaneously without rotation.                                                                                                               |

## Per-file overrides

### bootstrap-ceremony.conf

| Setting                    | Upstream | Integration Tests | Reason                                                                            |
| -------------------------- | -------- | ----------------- | --------------------------------------------------------------------------------- |
| `casper.heartbeat.enabled` | `true`   | `false`           | Bootstrap is a ceremony master, not a bonded validator. It cannot propose blocks. |

### standalone-dev.conf

Heavily expanded with documentation comments. Upstream is a minimal ~130-line config; integration-tests version is ~376 lines with the same functional values plus detailed explanations.

## Non-functional differences

All integration-tests configs include expanded comments for:
- UPnP disabled rationale (Docker networking)
- `enable-mergeable-channel-gc` Scala race condition note
- General documentation improvements
