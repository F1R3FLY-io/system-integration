"""
Contract Lifecycle Integration Test

Comprehensive test suite that deploys multiple contracts in parallel,
queries them via both real deploy and exploratory deploy across all nodes,
verifies cross-node state agreement at every phase, exercises
contract-to-contract interaction, vault transfers interleaved with
queries, and multi-block state evolution.

Contracts deployed (module-scoped fixture):
- bridge-v2.rho x2 (V1, V2) -- complex registry + persistent state channels
- store-data.rho (V3) -- simple registry storage
- data-provider.rho (V1) -- exposes getData/getNumber for cross-contract reads

Test phases:
1. Parallel deployment + cross-node state agreement
2. Cross-validator queries via real deploy
3. Exploratory queries on readonly
4. Contract-to-contract interaction (consumer reads from provider)
5. Vault transfers interleaved with bridge queries
6. Multi-block state evolution (bridge lock from multiple validators)
7. Final cross-node state verification
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import pytest
from f1r3fly.par import par_as_int, par_as_list, par_as_string, par_as_uri

from ...infra.assertions import (
    assert_all_nodes_agree_on_block,
    assert_all_nodes_agree_on_lfb,
    assert_contracts_consistent_across_nodes,
)
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import deploy_and_read, wait_for_deploy_finalized, wait_for_finalized

pytestmark = pytest.mark.xdist_group("shared")


# ── Constants ─────────────────────────────────────────────────────────

BRIDGE_CONTRACT = "resources/bridge-v2.rho"
STORE_DATA_CONTRACT = "resources/storage/store-data.rho"
READ_DATA_CONTRACT = "resources/storage/read-data.rho"
DATA_PROVIDER_CONTRACT = "resources/lifecycle/data-provider.rho"
DATA_CONSUMER_CONTRACT = "resources/lifecycle/data-consumer.rho"

VALIDATOR_KEYS = [VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID]


# ── Bridge URI extraction ─────────────────────────────────────────────

def _extract_bridge_uris(pars) -> Tuple[str, str, str]:
    """Extract (queryUri, lockUri, unlockUri) from bridge deploy data."""
    for par in pars:
        try:
            items = par_as_list(par)
        except ValueError:
            continue
        if len(items) != 3:
            continue
        try:
            uris = [par_as_uri(item) for item in items]
        except ValueError:
            continue
        if all(u.startswith("rho:id:") for u in uris):
            return uris[0], uris[1], uris[2]

    par_summaries = [str(p)[:80] for p in pars]
    raise AssertionError(
        f"Could not find [queryUri, lockUri, unlockUri] in deploy data. "
        f"Got {len(pars)} par entries: {par_summaries}"
    )


# ── Rholang builders (bridge-specific) ───────────────────────────────

def _make_query_rho(query_uri: str, method: str, param: str = "Nil") -> str:
    """Build Rholang for a bridge query via real deploy (uses deployId)."""
    return f"""
new deployId(`rho:system:deployId`),
    lookup(`rho:registry:lookup`),
    queryCh, ret
in {{
  lookup!(`{query_uri}`, *queryCh) |
  for (q <- queryCh) {{
    q!("{method}", {param}, *ret) |
    for (@result <- ret) {{ deployId!(result) }}
  }}
}}
"""


def _make_lock_rho(
    lock_uri: str,
    amount: int,
    eth_recipient: str,
    from_addr: str,
) -> str:
    """Build Rholang for a bridge lock via real deploy.

    The bridge's lock contract internally calls
    ``userVault.transfer(bridgeAddr, amount, fromAuthKey, ret)`` which
    requires a valid auth key. ``SystemVault.deployerAuthKey`` derives
    the auth key for the vault address that corresponds to the deploy
    signer's public key — so signing this deploy with the same key that
    owns ``from_addr`` produces an auth key that satisfies the vault's
    ``AuthKey.check``. With a fresh ``new authKey``, the check call
    hangs (no responder for ``key!("challenge", ...)``) and the lock
    chain never completes.
    """
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
        lock!({amount}, "{eth_recipient}", "{from_addr}", *authKey, *ret) |
        for (@result <- ret) {{ deployId!(result) }}
      }}
    }}
  }}
}}
"""


# ── Rolling verification helper ──────────────────────────────────────

def _verify_all_nodes_consistent(
    shard,
    contract_queries: List[Tuple[str, str, str]],
    phase_name: str,
):
    """Run cross-node consistency check after a phase.

    Verifies all nodes agree on LFB and that all contracts return
    expected results via exploratory deploy on readonly.
    """
    ro = shard.readonly
    lfb = ro.last_finalized_block().blockInfo
    lfb_hash = lfb.blockHash
    logging.info(
        "[%s] Verifying consistency at LFB #%d (%s...)",
        phase_name, lfb.blockNumber, lfb_hash[:16],
    )

    # Verify all nodes agree on this block's post-state
    assert_all_nodes_agree_on_block(shard.all_nodes, lfb_hash)

    # Query all contracts via exploratory on readonly
    results = assert_contracts_consistent_across_nodes(
        ro, contract_queries, block_hash=lfb_hash,
    )
    logging.info(
        "[%s] All nodes consistent, %d contracts verified",
        phase_name, len(results),
    )
    return results


# ── Module-scoped fixture: deploy all contracts ──────────────────────

@pytest.fixture(scope="module")
def deployed_contracts(shared_shard, timeouts) -> Dict:
    """Deploy bridge x2, storage, and data-provider in parallel.

    Returns dict with contract metadata for use by all tests.
    """
    validators = shared_shard.validators
    all_nodes = shared_shard.all_nodes
    find_timeout = timeouts.deploy_inclusion
    lfb_timeout = timeouts.finalization

    results = {}
    errors = []

    def _deploy(name, node, key, **kwargs):
        """Deploy a contract and return (name, pars, block_hash, block_number)."""
        logging.info("Deploying %s on %s...", name, node.name)
        pars, block_hash, block_number = deploy_and_read(
            node, "", key.private_key(),
            find_timeout, lfb_timeout,
            **kwargs,
        )
        logging.info(
            "  %s deployed in block #%d (%s...)",
            name, block_number, block_hash[:16],
        )
        return name, pars, block_hash, block_number

    # Submit deploys in parallel
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(
                _deploy, "bridge1", validators[0], VALIDATOR1_ID,
                rho_file=BRIDGE_CONTRACT, phlo_limit=500_000_000,
            ): "bridge1",
            executor.submit(
                _deploy, "bridge2", validators[1], VALIDATOR2_ID,
                rho_file=BRIDGE_CONTRACT, phlo_limit=500_000_000,
            ): "bridge2",
            executor.submit(
                _deploy, "storage", validators[2], VALIDATOR3_ID,
                rho_file=STORE_DATA_CONTRACT, phlo_limit=100_000_000,
            ): "storage",
        }

        for f in as_completed(futures):
            tag = futures[f]
            try:
                name, pars, block_hash, block_number = f.result()
                results[name] = {
                    "pars": pars,
                    "block_hash": block_hash,
                    "block_number": block_number,
                }
            except Exception as e:
                errors.append(f"{tag}: {e}")

    assert not errors, f"Deploy failures: {errors}"

    # Extract bridge URIs
    for bridge_name in ("bridge1", "bridge2"):
        query_uri, lock_uri, unlock_uri = _extract_bridge_uris(
            results[bridge_name]["pars"]
        )
        results[bridge_name]["query_uri"] = query_uri
        results[bridge_name]["lock_uri"] = lock_uri
        results[bridge_name]["unlock_uri"] = unlock_uri
        logging.info("  %s URIs: query=%s", bridge_name, query_uri)

    # Extract storage URI
    storage_pars = results["storage"]["pars"]
    assert storage_pars, "Storage deploy returned no URI"
    storage_uri = par_as_uri(storage_pars[0])
    results["storage"]["uri"] = storage_uri
    logging.info("  storage URI: %s", storage_uri)

    # Deploy data-provider (sequential — needs to complete before consumer)
    logging.info("Deploying data-provider on V1...")
    provider_pars, provider_hash, provider_num = deploy_and_read(
        validators[0], "", VALIDATOR1_ID.private_key(),
        find_timeout, lfb_timeout,
        rho_file=DATA_PROVIDER_CONTRACT, phlo_limit=100_000_000,
    )
    provider_uri = par_as_uri(provider_pars[0])
    results["provider"] = {
        "pars": provider_pars,
        "block_hash": provider_hash,
        "block_number": provider_num,
        "uri": provider_uri,
    }
    logging.info("  provider URI: %s", provider_uri)

    # Wait for all nodes to finalize past all deployment blocks
    max_block = max(r["block_number"] for r in results.values())
    target = max_block + 1
    for node in all_nodes:
        wait_for_finalized(node, target, lfb_timeout)
    logging.info("All nodes finalized past block #%d", max_block)

    yield results


# ── Phase 2: Cross-node state agreement after deployment ─────────────

def test_cross_node_state_after_deployment(shared_shard, deployed_contracts):
    """All nodes agree on post-state hash for every deployment block."""
    for name, info in deployed_contracts.items():
        block_hash = info["block_hash"]
        logging.info("Verifying %s block %s...", name, block_hash[:16])
        assert_all_nodes_agree_on_block(shared_shard.all_nodes, block_hash)

    logging.info(
        "All %d deployment blocks consistent across all nodes",
        len(deployed_contracts),
    )


# ── Phase 3: Cross-validator queries via real deploy ─────────────────

def test_cross_validator_queries_real_deploy(
    shared_shard, timeouts, deployed_contracts,
):
    """Query bridge contracts from validators that didn't deploy them."""
    validators = shared_shard.validators
    find_timeout = timeouts.deploy_inclusion
    lfb_timeout = timeouts.finalization

    queries = [
        ("bridge1 getNonce", deployed_contracts["bridge1"]["query_uri"],
         "getNonce", validators[1], VALIDATOR2_ID),
        ("bridge1 getTotalLocked", deployed_contracts["bridge1"]["query_uri"],
         "getTotalLocked", validators[2], VALIDATOR3_ID),
        ("bridge2 getNonce", deployed_contracts["bridge2"]["query_uri"],
         "getNonce", validators[0], VALIDATOR1_ID),
        ("bridge2 getAddress", deployed_contracts["bridge2"]["query_uri"],
         "getAddress", validators[2], VALIDATOR3_ID),
    ]

    errors = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {}
        for label, uri, method, node, key in queries:
            f = executor.submit(
                deploy_and_read,
                node, _make_query_rho(uri, method),
                key.private_key(), find_timeout, lfb_timeout,
                phlo_limit=500_000_000,
            )
            futures[f] = label

        for f in as_completed(futures):
            label = futures[f]
            try:
                pars, _, _ = f.result()
                assert pars, f"{label}: empty result"
                logging.info("  %s: %s", label, str(pars[0])[:80])
            except Exception as e:
                errors.append(f"{label}: {e}")

    assert not errors, f"Cross-validator query failures: {errors}"

    # Verify state consistency after queries
    _verify_all_nodes_consistent(
        shared_shard,
        [
            ("bridge1", deployed_contracts["bridge1"]["query_uri"], "getNonce"),
            ("bridge2", deployed_contracts["bridge2"]["query_uri"], "getNonce"),
        ],
        "Phase 3",
    )


# ── Phase 4: Exploratory queries on readonly ─────────────────────────

def test_cross_validator_queries_exploratory(shared_shard, deployed_contracts):
    """Same queries via exploratory deploy on readonly node."""
    ro = shared_shard.readonly
    lfb_hash = ro.last_finalized_block().blockInfo.blockHash

    # Bridge 1
    nonce1 = par_as_int(
        ro.registry_query(
            deployed_contracts["bridge1"]["query_uri"], "getNonce",
            block_hash=lfb_hash,
        )[0]
    )
    assert nonce1 >= 0, f"bridge1 getNonce: {nonce1}"
    logging.info("bridge1 getNonce (exploratory): %d", nonce1)

    locked1 = par_as_int(
        ro.registry_query(
            deployed_contracts["bridge1"]["query_uri"], "getTotalLocked",
            block_hash=lfb_hash,
        )[0]
    )
    assert locked1 >= 0, f"bridge1 getTotalLocked: {locked1}"
    logging.info("bridge1 getTotalLocked (exploratory): %d", locked1)

    # Bridge 2
    nonce2 = par_as_int(
        ro.registry_query(
            deployed_contracts["bridge2"]["query_uri"], "getNonce",
            block_hash=lfb_hash,
        )[0]
    )
    assert nonce2 >= 0, f"bridge2 getNonce: {nonce2}"
    logging.info("bridge2 getNonce (exploratory): %d", nonce2)

    addr2 = par_as_string(
        ro.registry_query(
            deployed_contracts["bridge2"]["query_uri"], "getAddress",
            block_hash=lfb_hash,
        )[0]
    )
    assert len(addr2) > 0, "bridge2 getAddress empty"
    logging.info("bridge2 getAddress (exploratory): %s", addr2)

    # Storage
    storage_val = ro.registry_lookup(
        deployed_contracts["storage"]["uri"], block_hash=lfb_hash,
    )
    assert storage_val, "storage lookup returned empty"
    logging.info("storage value (exploratory): %s", str(storage_val[0])[:80])

    # Provider — 2-arg pattern: (@method, ret)
    provider_data = ro.registry_query(
        deployed_contracts["provider"]["uri"], "getData",
        param=None,
        block_hash=lfb_hash,
    )
    assert par_as_string(provider_data[0]) == "hello_from_provider"
    logging.info("provider getData (exploratory): %s", par_as_string(provider_data[0]))


# ── Phase 5: Contract-to-contract interaction ────────────────────────

def test_contract_to_contract_interaction(
    shared_shard, timeouts, deployed_contracts,
):
    """Contract B reads from Contract A via registry after merge."""
    validators = shared_shard.validators
    find_timeout = timeouts.deploy_inclusion
    lfb_timeout = timeouts.finalization
    provider_uri = deployed_contracts["provider"]["uri"]

    # Deploy consumer on V2 — it looks up provider's URI and calls getData
    logging.info("Deploying data-consumer on V2 (provider_uri=%s)...", provider_uri)
    consumer_pars, consumer_hash, consumer_num = deploy_and_read(
        validators[1], "", VALIDATOR2_ID.private_key(),
        find_timeout, lfb_timeout,
        rho_file=DATA_CONSUMER_CONTRACT,
        substitutions={"@provider_uri@": provider_uri},
        phlo_limit=100_000_000,
    )

    assert consumer_pars, "Consumer deploy returned no data"
    consumer_result = par_as_string(consumer_pars[0])
    assert consumer_result == "hello_from_provider", (
        f"Consumer got '{consumer_result}', expected 'hello_from_provider'"
    )
    logging.info("Consumer received from provider: %s", consumer_result)

    # Verify cross-node agreement
    wait_for_finalized(
        shared_shard.readonly, consumer_num + 1, lfb_timeout,
    )
    assert_all_nodes_agree_on_block(shared_shard.all_nodes, consumer_hash)

    # Rolling verification
    _verify_all_nodes_consistent(
        shared_shard,
        [
            ("bridge1", deployed_contracts["bridge1"]["query_uri"], "getNonce"),
            ("bridge2", deployed_contracts["bridge2"]["query_uri"], "getNonce"),
            ("provider", deployed_contracts["provider"]["uri"], "getData", None),
        ],
        "Phase 5",
    )


# ── Phase 6: Vault transfers interleaved with queries ────────────────

def test_transfers_interleaved_with_queries(
    shared_shard, timeouts, deployed_contracts,
):
    """Vault transfers and bridge queries in parallel."""
    validators = shared_shard.validators
    ro = shared_shard.readonly
    find_timeout = timeouts.deploy_inclusion
    lfb_timeout = timeouts.finalization

    v1_key = VALIDATOR1_ID.private_key()
    v2_key = VALIDATOR2_ID.private_key()
    # Use V3 to sign the parallel query so V2's balance assertion below
    # measures only the transfer effect, not query phlo cost.
    v3_key = VALIDATOR3_ID.private_key()
    v1_vault = v1_key.get_public_key().get_vault_address()
    v2_vault = v2_key.get_public_key().get_vault_address()

    # Record balances before
    v1_balance_before = ro.vault.get_balance(v1_vault)
    v2_balance_before = ro.vault.get_balance(v2_vault)
    logging.info("Before: V1=%d, V2=%d", v1_balance_before, v2_balance_before)

    transfer_amount = 1_000_000

    # Submit transfer + bridge query in parallel
    errors = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        transfer_future = executor.submit(
            lambda: (
                validators[0].vault.transfer_ensure(
                    v1_vault, v2_vault, transfer_amount, v1_key,
                ),
                "transfer",
            ),
        )
        query_future = executor.submit(
            deploy_and_read,
            validators[1],
            _make_query_rho(
                deployed_contracts["bridge1"]["query_uri"], "getNonce",
            ),
            v3_key, find_timeout, lfb_timeout,
            phlo_limit=500_000_000,
        )

        for f in as_completed([transfer_future, query_future]):
            try:
                f.result()
            except Exception as e:
                errors.append(str(e))

    assert not errors, f"Interleaved failures: {errors}"

    # Wait for the transfer's canonical-state inclusion. Using
    # wait_for_deploy_finalized (honest sig polling) instead of LFB-advance
    # polling is deliberate here: the parallel bridge query creates the
    # merge-contention shape that can reject the transfer; Phase D's
    # rejected-deploy buffer recovers it in a later block. LFB polling
    # would return as soon as the initial (possibly-rejected) block
    # finalized, before the canonical inclusion landed.
    transfer_deploy_id = transfer_future.result()[0]
    wait_for_deploy_finalized(ro, transfer_deploy_id, lfb_timeout)

    # Verify balances
    v2_balance_after = ro.vault.get_balance(v2_vault)
    assert v2_balance_after == v2_balance_before + transfer_amount, (
        f"V2 balance: expected {v2_balance_before + transfer_amount}, "
        f"got {v2_balance_after}"
    )
    logging.info(
        "Transfer verified: V2 %d -> %d (+%d)",
        v2_balance_before, v2_balance_after, transfer_amount,
    )

    # Rolling verification
    _verify_all_nodes_consistent(
        shared_shard,
        [
            ("bridge1", deployed_contracts["bridge1"]["query_uri"], "getNonce"),
            ("provider", deployed_contracts["provider"]["uri"], "getData", None),
        ],
        "Phase 6",
    )


# ── Phase 8: Multi-block state evolution ─────────────────────────────

def test_multi_block_state_evolution(
    shared_shard, timeouts, deployed_contracts,
):
    """Lock tokens on bridge from different validators, verify state at each step."""
    validators = shared_shard.validators
    ro = shared_shard.readonly
    find_timeout = timeouts.deploy_inclusion
    lfb_timeout = timeouts.finalization
    query_uri = deployed_contracts["bridge1"]["query_uri"]
    lock_uri = deployed_contracts["bridge1"]["lock_uri"]

    # Get initial nonce
    lfb_hash = ro.last_finalized_block().blockInfo.blockHash
    initial_nonce = par_as_int(
        ro.registry_query(query_uri, "getNonce", block_hash=lfb_hash)[0]
    )
    logging.info("Initial nonce: %d", initial_nonce)

    # Lock 1: V1 locks 100 tokens
    v1_key = VALIDATOR1_ID.private_key()
    v1_addr = v1_key.get_public_key().get_vault_address()
    lock1_rho = _make_lock_rho(lock_uri, 100, "0xabc123", v1_addr)

    logging.info("Lock 1: V1 locking 100 tokens...")
    lock1_pars, lock1_hash, lock1_num = deploy_and_read(
        validators[0], lock1_rho, v1_key,
        find_timeout, lfb_timeout,
        phlo_limit=500_000_000,
    )
    logging.info("Lock 1 result: %s", str(lock1_pars[0])[:120] if lock1_pars else "empty")

    # Verify after lock 1
    wait_for_finalized(ro, lock1_num + 1, lfb_timeout)
    lfb_hash = ro.last_finalized_block().blockInfo.blockHash
    nonce_after_1 = par_as_int(
        ro.registry_query(query_uri, "getNonce", block_hash=lfb_hash)[0]
    )
    logging.info("Nonce after lock 1: %d (expected %d)", nonce_after_1, initial_nonce + 1)

    # Verify all nodes agree
    assert_all_nodes_agree_on_block(shared_shard.all_nodes, lock1_hash)

    # Lock 2: V2 locks 200 tokens
    v2_key = VALIDATOR2_ID.private_key()
    v2_addr = v2_key.get_public_key().get_vault_address()
    lock2_rho = _make_lock_rho(lock_uri, 200, "0xdef456", v2_addr)

    logging.info("Lock 2: V2 locking 200 tokens...")
    lock2_pars, lock2_hash, lock2_num = deploy_and_read(
        validators[1], lock2_rho, v2_key,
        find_timeout, lfb_timeout,
        phlo_limit=500_000_000,
    )
    logging.info("Lock 2 result: %s", str(lock2_pars[0])[:120] if lock2_pars else "empty")

    # Verify after lock 2
    wait_for_finalized(ro, lock2_num + 1, lfb_timeout)
    lfb_hash = ro.last_finalized_block().blockInfo.blockHash
    nonce_after_2 = par_as_int(
        ro.registry_query(query_uri, "getNonce", block_hash=lfb_hash)[0]
    )
    logging.info("Nonce after lock 2: %d (expected %d)", nonce_after_2, initial_nonce + 2)

    assert_all_nodes_agree_on_block(shared_shard.all_nodes, lock2_hash)

    # Final rolling verification
    _verify_all_nodes_consistent(
        shared_shard,
        [
            ("bridge1", query_uri, "getNonce"),
            ("bridge1", query_uri, "getTotalLocked"),
            ("bridge2", deployed_contracts["bridge2"]["query_uri"], "getNonce"),
            ("provider", deployed_contracts["provider"]["uri"], "getData", None),
        ],
        "Phase 8",
    )


# ── Phase 9: Final cross-node state verification ────────────────────

def test_final_cross_node_state_agreement(shared_shard, deployed_contracts, timeouts):
    """All nodes agree on final LFB and all contract state."""
    ro = shared_shard.readonly

    # All nodes agree on LFB. Opt into polling — normal propagation can
    # leave one validator's finalizer a beat ahead of the others at the
    # moment of the snapshot; timeouts.finalization gives the rest a
    # window to catch up before the assertion fires.
    lfb_hash = assert_all_nodes_agree_on_lfb(
        shared_shard.all_nodes, timeout=timeouts.finalization,
    )
    logging.info("All nodes agree on LFB: %s...", lfb_hash[:16])

    # Verify all nodes agree on LFB post-state
    assert_all_nodes_agree_on_block(shared_shard.all_nodes, lfb_hash)

    # Verify all contracts accessible from readonly
    results = assert_contracts_consistent_across_nodes(
        ro,
        [
            ("bridge1 getNonce", deployed_contracts["bridge1"]["query_uri"], "getNonce"),
            ("bridge1 getTotalLocked", deployed_contracts["bridge1"]["query_uri"], "getTotalLocked"),
            ("bridge1 getAddress", deployed_contracts["bridge1"]["query_uri"], "getAddress"),
            ("bridge2 getNonce", deployed_contracts["bridge2"]["query_uri"], "getNonce"),
            ("bridge2 getTotalLocked", deployed_contracts["bridge2"]["query_uri"], "getTotalLocked"),
            ("bridge2 getAddress", deployed_contracts["bridge2"]["query_uri"], "getAddress"),
            ("provider getData", deployed_contracts["provider"]["uri"], "getData", None),
            ("provider getNumber", deployed_contracts["provider"]["uri"], "getNumber", None),
        ],
        block_hash=lfb_hash,
    )

    logging.info(
        "Final verification: %d contracts consistent across all nodes",
        len(results),
    )

    # Verify specific values
    assert par_as_string(results["provider getData"][0]) == "hello_from_provider"
    assert par_as_int(results["provider getNumber"][0]) == 42

    bridge1_addr = par_as_string(results["bridge1 getAddress"][0])
    bridge2_addr = par_as_string(results["bridge2 getAddress"][0])
    assert len(bridge1_addr) > 0
    assert len(bridge2_addr) > 0
    assert bridge1_addr != bridge2_addr, "Bridge instances should have different vault addresses"

    logging.info("All assertions passed. Contract lifecycle test complete.")
