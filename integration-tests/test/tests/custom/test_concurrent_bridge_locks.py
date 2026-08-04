from concurrent.futures import ThreadPoolExecutor

import pytest
from f1r3fly.par import par_as_int, par_as_list, par_as_string, par_as_uri

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import deploy_and_read, wait_for_deploy_finalized
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

_LOCK_COUNT = 12
_PHLO_LIMIT = 500_000_000


def _extract_bridge_uris(pars):
    for par in pars:
        try:
            items = par_as_list(par)
            uris = [par_as_uri(item) for item in items]
        except ValueError:
            continue
        if len(uris) == 3 and all(uri.startswith("rho:id:") for uri in uris):
            return uris
    raise AssertionError(f"bridge deployment did not return three URIs: {pars}")


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
    v1 = bridge_shard.node("validator1")
    readonly = bridge_shard.readonly
    key = VALIDATOR1_ID.private_key()
    from_addr = key.get_public_key().get_vault_address()

    deploy_pars, _, _ = deploy_and_read(
        v1,
        "",
        key,
        timeouts.custom(120),
        timeouts.finalization,
        rho_file="resources/bridge-v2.rho",
        phlo_limit=_PHLO_LIMIT,
    )
    query_uri, lock_uri, _ = _extract_bridge_uris(deploy_pars)
    lfb_hash = readonly.last_finalized_block().blockInfo.blockHash
    bridge_addr = par_as_string(
        readonly.registry_query(query_uri, "getAddress", block_hash=lfb_hash)[0]
    )
    source_before = readonly.vault.get_balance(from_addr)
    bridge_before = readonly.vault.get_balance(bridge_addr)

    def submit(index: int) -> str:
        term = _lock_term(lock_uri, 1, f"0x{index:040x}", from_addr)
        return v1.deploy_string(term, key, phlo_limit=_PHLO_LIMIT)

    with ThreadPoolExecutor(max_workers=_LOCK_COUNT) as executor:
        deploy_ids = list(executor.map(submit, range(_LOCK_COUNT)))

    assert len(set(deploy_ids)) == _LOCK_COUNT

    def wait_for_finalization(deploy_id: str):
        return wait_for_deploy_finalized(
            v1,
            deploy_id,
            timeouts.finalization * 8,
        )

    with ThreadPoolExecutor(max_workers=_LOCK_COUNT) as executor:
        statuses = list(executor.map(wait_for_finalization, deploy_ids))
    assert len(statuses) == _LOCK_COUNT

    lfb_hash = readonly.last_finalized_block().blockInfo.blockHash
    nonce = par_as_int(readonly.registry_query(query_uri, "getNonce", block_hash=lfb_hash)[0])
    total_locked = par_as_int(
        readonly.registry_query(query_uri, "getTotalLocked", block_hash=lfb_hash)[0]
    )
    bridge_after = readonly.vault.get_balance(bridge_addr)
    source_after = readonly.vault.get_balance(from_addr)

    assert nonce == _LOCK_COUNT
    assert total_locked == _LOCK_COUNT
    assert bridge_after - bridge_before == _LOCK_COUNT
    assert source_before - source_after >= _LOCK_COUNT
