"""Concurrent bridge locks preserve exact nonce, accounting, and vault state.

Twelve locks are submitted at once against one bridge contract, so every one of
them is a read-modify-write on the same set of single-value cells — the shape
multi-parent merge has to get right. The claims gated here:

- **No starvation**: no lock may expire at zero rejections (never adjudicated —
  the deploy-play-budget's pile-up class), be refund-quarantined, or diverge
  across nodes. Always fatal.
- **Bounded contention**: mutually-conflicting locks must serialize through
  merge rounds, so a lock can legally lose every round its validity window
  fits and expire WITH rejections on record. At least ``_FINALIZE_FLOOR`` of
  the twelve must still land.
- **Exact accounting**: the nonce, total-locked counter, and bridge vault
  delta must equal EXACTLY the number of locks that finalized — no double
  count, no lost count — with the source debited by at least that (the
  surplus being gas).
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from f1r3fly.par import par_as_int, par_as_string

from ...infra.assertions import assert_all_deploys_finalized_on_all_nodes
from ...infra.bridge import (
    BRIDGE_CONTRACT,
    extract_bridge_uris,
    query_one,
    wait_for_registry_visible,
)
from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import deploy_and_read, poll_until, wait_for_block_visible
from ...infra.shard import Shard

pytestmark = [
    pytest.mark.xdist_group("custom"),
    pytest.mark.requires_node_capabilities("concurrent-bridge-lock-accounting"),
]

_LOCK_COUNT = 12
# Contention floor: merge rounds serialize conflicting locks at roughly one
# winner per round, and a round costs a finality lag (re-homing waits for the
# rejection to finalize) plus carrier play latency — so on a slow leg the
# validity window fits only a couple of attempts for the last stragglers.
# Losing up to two lock(s) to recorded contention is legal; anything lost any
# other way stays fatal.
_FINALIZE_FLOOR = 10
_PHLO_LIMIT = 500_000_000


def _lock_term(lock_uri: str, amount: int, recipient: str, from_addr: str) -> str:
    return f"""
new deployId(`rho:system:deployId`),
    deployerId(`rho:rchain:deployerId`),
    rl(`rho:registry:lookup`),
    sysVaultCh, authKeyCh, lockCh, ret
in {{
  rl!(`rho:vault:system`, *sysVaultCh) |
  for (@(_, SystemVault) <- sysVaultCh) {{
    @SystemVault!("deployerAuthKey", *deployerId, *authKeyCh) |
    for (authKey <- authKeyCh) {{
      rl!(`{lock_uri}`, *lockCh) |
      for (lock <- lockCh) {{
        lock!({amount}, "{recipient}", "{from_addr}", *authKey, *ret) |
        for (@result <- ret) {{ deployId!(result) }}
      }}
    }}
  }}
}}
"""


@pytest.fixture(scope="module")
def bridge_shard(provider, timeouts):
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
        ],
        heartbeat=True,
        include_readonly=True,
    )
    shard = Shard.create(provider, config, timeouts)
    yield shard
    shard.destroy()


def test_concurrent_bridge_locks_exact_accounting(bridge_shard, timeouts) -> None:
    """Twelve simultaneous locks: no starvation, and exact accounting for every lock that lands."""
    v1 = bridge_shard.node("validator1")
    readonly = bridge_shard.readonly
    key = VALIDATOR1_ID.private_key()
    from_addr = key.get_public_key().get_vault_address()

    deploy_pars, deploy_block_hash, _ = deploy_and_read(
        v1,
        "",
        key,
        timeouts.custom(120),
        timeouts.finalization * 4,
        rho_file=BRIDGE_CONTRACT,
        phlo_limit=_PHLO_LIMIT,
    )
    query_uri, lock_uri, _ = extract_bridge_uris(deploy_pars)
    wait_for_block_visible(readonly, deploy_block_hash, timeouts.finalization)
    # Block visibility is not effect visibility — see wait_for_registry_visible.
    wait_for_registry_visible([readonly], query_uri, timeouts.finalization)
    bridge_addr = par_as_string(query_one(readonly, query_uri, "getAddress", deploy_block_hash))
    # Pin the "before" reads to the same block the deltas are measured against;
    # get_balance defaults to latest, which drifts as heartbeat blocks land.
    source_before = readonly.vault.get_balance(from_addr, deploy_block_hash)
    bridge_before = readonly.vault.get_balance(bridge_addr, deploy_block_hash)

    def submit(index: int) -> str:
        term = _lock_term(lock_uri, 1, f"0x{index:040x}", from_addr)
        return v1.deploy_string(term, key, phlo_limit=_PHLO_LIMIT)

    with ThreadPoolExecutor(max_workers=_LOCK_COUNT) as executor:
        deploy_ids = list(executor.map(submit, range(_LOCK_COUNT)))

    assert len(set(deploy_ids)) == _LOCK_COUNT

    # A finalized lock must be finalized on EVERY node, the readonly observer
    # included. Two reasons: twelve concurrent writes to the same cells is where
    # per-node merge divergence appears and one node cannot see it; and the
    # reconciliation below reads the observer's own LFB, which may lag the
    # proposer, so waiting only on the proposer would let the observer choose a
    # block that predates some locks. Re-homing-aware, so a lock re-included in
    # a finalized descendant still counts. Recorded contention losses down to
    # the floor are tolerated; starvation, quarantine, and divergence are not.
    finalized_ids = assert_all_deploys_finalized_on_all_nodes(
        bridge_shard.all_nodes,
        deploy_ids,
        timeouts.finalization * 8,
        label="concurrent-bridge-locks",
        contention_floor=_FINALIZE_FLOOR,
    )
    landed = len(finalized_ids)

    # Every read below is pinned to one block. Sampling the counters at an LFB and
    # the balances at "latest" would reconcile two different states, and the drift
    # is unbounded while heartbeat blocks keep landing.
    #
    # Each read gets a bounded retry: right after digesting twelve concurrent
    # lock finalizations the readonly can transiently exceed a single gRPC
    # deadline (soak preflight 31919610258 died on exactly one such
    # DEADLINE_EXCEEDED from last_finalized_block). poll_until retries on
    # exception, so a transient deadline is absorbed while a persistent one
    # still fails the test with the underlying error.
    def _read(description, fn):
        # 1-tuple wrap: poll_until retries on falsy, but a read that
        # legitimately returns 0 (a balance) is still a success.
        return poll_until(
            predicate=lambda: (fn(),),
            timeout=timeouts.command * 3,
            interval=2.0,
            description=description,
        )[0]

    lfb_hash = _read(
        "readonly LFB for pinned reconciliation reads",
        lambda: readonly.last_finalized_block().blockInfo.blockHash,
    )
    nonce = par_as_int(
        _read(
            "getNonce at pinned LFB", lambda: query_one(readonly, query_uri, "getNonce", lfb_hash)
        )
    )
    total_locked = par_as_int(
        _read(
            "getTotalLocked at pinned LFB",
            lambda: query_one(readonly, query_uri, "getTotalLocked", lfb_hash),
        )
    )
    bridge_after = _read(
        "bridge vault balance at pinned LFB",
        lambda: readonly.vault.get_balance(bridge_addr, lfb_hash),
    )
    source_after = _read(
        "source vault balance at pinned LFB",
        lambda: readonly.vault.get_balance(from_addr, lfb_hash),
    )

    # Reconcile against exactly the locks that FINALIZED: an expired lock's
    # effects must be fully absent (no half-applied lock), a finalized lock's
    # fully present (no lost count), so all three counters agree with `landed`.
    assert nonce == landed, f"expected nonce {landed} (finalized locks), got {nonce}"
    assert total_locked == landed, (
        f"expected total locked {landed} (finalized locks), got {total_locked}"
    )
    assert bridge_after - bridge_before == landed, (
        f"bridge vault moved by {bridge_after - bridge_before}, expected exactly "
        f"{landed} ({bridge_before} -> {bridge_after})"
    )
    assert source_before - source_after >= landed, (
        f"source vault fell by {source_before - source_after}, expected at least "
        f"{landed} plus gas ({source_before} -> {source_after})"
    )
