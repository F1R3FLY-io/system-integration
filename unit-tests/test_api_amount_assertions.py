import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from f1r3fly.token_amount import MAX

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration-tests"))

from test.tests.custom import test_validator_lifecycle as lifecycle  # noqa: E402
from test.tests.shared import test_query_endpoints as queries  # noqa: E402
from test.tests.shared import test_wallets as wallets  # noqa: E402
from test.tests.shared import test_web_api as web_api  # noqa: E402

STAKE = 2**255


def test_validator_endpoint_assertions_accept_exact_stakes():
    identities = [SimpleNamespace(public_hex=f"validator{i}") for i in range(2)]
    validators = [{"publicKey": i.public_hex, "stake": str(STAKE)} for i in identities]
    response = {
        "validators": validators,
        "totalStake": str(STAKE * 2),
        "blockNumber": 1,
        "blockHash": "block",
    }
    node = SimpleNamespace(api_get=lambda _: response)
    shard = SimpleNamespace(
        readonly=node, config=SimpleNamespace(bonds=[(i, STAKE) for i in identities])
    )
    queries.test_validators_endpoint(shard, None)
    assert lifecycle._validators_on(node) == {i.public_hex: STAKE for i in identities}

    pubkey = queries._any_validator_pubkey(shard)
    response = {
        "publicKey": pubkey,
        "stake": str(STAKE),
        "isBonded": True,
        "blockNumber": 1,
        "blockHash": "block",
    }
    queries.test_validator_bonded(shard)


def _block(stake):
    validator = "04" + "ab" * 64
    block = {
        "blockHash": "ab" * 32,
        "shardId": "test",
        "sigAlgorithm": "secp256k1",
        "blockNumber": 1,
        "timestamp": 1,
        "seqNum": 0,
        "version": 1,
        "preStateHash": "ab" * 32,
        "postStateHash": "cd" * 32,
        "sender": validator,
        "bonds": [{"validator": validator, "stake": stake}],
        "faultTolerance": 0.0,
        "justifications": [],
    }
    expect = {
        "shard_id": "test",
        "sig_algorithm": "secp256k1",
        "bond_count": 1,
        "stakes": {validator: MAX},
    }
    return block, expect


def test_block_assertions_accept_exact_bond_stakes():
    block, expect = _block(str(MAX))
    web_api._assert_light_block_info(block, expect)
    block["bonds"][0]["stake"] = str(MAX - 1)
    with pytest.raises(AssertionError, match="stake for"):
        web_api._assert_light_block_info(block, expect)


@pytest.mark.parametrize("stake", [1, "01", "-1", str(MAX + 1)])
def test_block_assertions_reject_numbers_and_non_canonical_stakes(stake):
    block, expect = _block(stake)
    with pytest.raises((TypeError, ValueError)):
        web_api._assert_light_block_info(block, expect)


def test_grpc_status_assertions_accept_exact_min_phlo_price(monkeypatch):
    node = SimpleNamespace(name="validator1")
    shard = SimpleNamespace(all_nodes=[node])
    http_status = {
        "shardId": "root",
        "networkId": "testnet",
        "minPhloPrice": str(MAX),
        "lastFinalizedBlockNumber": 1,
        "isValidator": True,
        "isReadOnly": False,
        "isReady": True,
        "epochLength": 10,
    }
    grpc_status = SimpleNamespace(**http_status)
    monkeypatch.setattr(
        web_api, "_status_pair_over_stable_lfb", lambda _: (http_status, grpc_status)
    )

    web_api.test_grpc_status_matches_http(shard, None)

    http_status["minPhloPrice"] = str(MAX - 1)
    with pytest.raises(AssertionError, match="minPhloPrice mismatch"):
        web_api.test_grpc_status_matches_http(shard, None)


def test_transfer_assertions_accept_exact_amounts(monkeypatch):
    v1_vault = wallets.VALIDATOR1_ID.private_key().get_public_key().get_vault_address()
    v2_vault = wallets.VALIDATOR2_ID.private_key().get_public_key().get_vault_address()
    transfer = {
        "fromAddr": v1_vault,
        "toAddr": v2_vault,
        "amount": "5000000",
        "success": True,
        "failReason": "",
    }
    readonly = SimpleNamespace(
        name="readonly",
        vault=Mock(),
        api_get=lambda _: {"deploys": [{"transfers": [transfer]}]},
    )
    validator = SimpleNamespace(
        vault=Mock(), find_deploy=lambda _: SimpleNamespace(blockHash="block")
    )
    validator.vault.transfer_ensure.return_value = "deploy"
    shard = SimpleNamespace(readonly=readonly, node=lambda _: validator)
    timeouts = SimpleNamespace(deploy_inclusion=1, finalization=1)
    monkeypatch.setattr(wallets, "poll_until", lambda predicate, **_: predicate())

    wallets.test_block_api_returns_transfer_info(shard, timeouts)
    transfer["amount"] = "5000001"
    with pytest.raises(AssertionError, match="amount mismatch"):
        wallets.test_block_api_returns_transfer_info(shard, timeouts)
