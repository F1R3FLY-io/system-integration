"""
Web API Integration Tests

Strict assertions on all HTTP API endpoints across all nodes. Values
are derived from the shard's ShardConfig so tests adapt to different
topologies (validator count, bond weights, etc.).

HTTP endpoints tested:
  /api/status
  /api/prepare-deploy
  /api/last-finalized-block
  /api/last-finalized-block?view=summary
  /api/block/<hash>
  /api/blocks/<depth>
  /api/deploy/<id>              (full view, default)
  /api/deploy/<id>?view=summary
  /api/explore-deploy
  /api/deploy (POST)
"""

import logging
import re
import time

import pytest
from f1r3fly.pb.CasperMessage_pb2 import DeployDataProto
from f1r3fly.util import sign_deploy_data

from ...infra.keys import VALIDATOR1_ID
from ...infra.polling import poll_until, wait_for_deploy_finalized, wait_for_deploy_included

pytestmark = pytest.mark.xdist_group("shared")

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_HEX_130 = re.compile(r"^[0-9a-f]{130}$")


def _shard_expectations(shard, node_conf):
    """Derive expected API values from the shard's config and node conf."""
    config = shard.config
    bonds = config.bonds
    validator_pubkeys = {identity.public_hex for identity, _ in bonds}
    stakes = {identity.public_hex: stake for identity, stake in bonds}
    return {
        "shard_id": node_conf.shard_id,
        "ftt": node_conf.ftt,
        "min_phlo_price": node_conf.min_phlo_price,
        "native_token_name": node_conf.native_token_name,
        "native_token_symbol": node_conf.native_token_symbol,
        "native_token_decimals": node_conf.native_token_decimals,
        "bond_count": len(bonds),
        "stakes": stakes,
        "validator_pubkeys": validator_pubkeys,
        "total_stake": sum(stake for _, stake in bonds),
        "validator_count": len(bonds),
        "sig_algorithm": "secp256k1",
    }


def _deploy_and_wait(node, timeouts, count=1, all_nodes=None):
    """Deploy `count` contracts and wait for each deploy to reach canonical
    state via sig-based finalization tracking. Returns (deploy_ids, block_hashes)
    where each block_hash is the resolver's `latestBlockHash` — the canonical
    block containing the deploy after multi-parent merge / re-inclusion.

    When `all_nodes` is provided, each deploy is also polled on every peer
    until that peer's resolver reports `DEPLOY_STATE_FINALIZED`. Use this
    whenever the test iterates per-node assertions on the deploy (e.g.
    `/deploy/<id>` returning `isFinalized=true`) — finalization can complete
    on the submitting node a few seconds before peers in multi-parent DAGs.
    """
    deploy_ids = []
    for i in range(count):
        did = node.deploy_string(
            f"@{2000 + i}!({i})",
            VALIDATOR1_ID.private_key(),
            phlo_limit=100_000,
            phlo_price=1,
        )
        deploy_ids.append(did)

    block_hashes = []
    for did in deploy_ids:
        status = wait_for_deploy_finalized(node, did, timeouts.finalization)
        canonical = status.latestBlockHash.hex() if status.latestBlockHash else None
        if canonical is None:
            # Fallback: resolver finalized but didn't populate latestBlockHash.
            # Read the inclusion record directly.
            info = wait_for_deploy_included(node, did, timeouts.deploy_inclusion)
            canonical = info.blockHash
        block_hashes.append(canonical)

    if all_nodes is not None:
        for did in deploy_ids:
            for other in all_nodes:
                if other.name == node.name:
                    continue
                wait_for_deploy_finalized(other, did, timeouts.finalization)

    return deploy_ids, block_hashes


def _assert_valid_block_hash(value, context=""):
    assert isinstance(value, str) and _HEX_64.match(value), (
        f"{context}: expected 64-char hex block hash, got '{value}'"
    )


def _assert_light_block_info(block, expect, context=""):
    """Assert all fields of a LightBlockInfoSerde against shard expectations."""
    _assert_valid_block_hash(block["blockHash"], f"{context} blockHash")
    assert block["shardId"] == expect["shard_id"], (
        f"{context}: shardId '{block['shardId']}' != '{expect['shard_id']}'"
    )
    assert block["sigAlgorithm"] == expect["sig_algorithm"], (
        f"{context}: sigAlgorithm '{block['sigAlgorithm']}' != '{expect['sig_algorithm']}'"
    )
    assert isinstance(block["blockNumber"], int) and block["blockNumber"] >= 0, (
        f"{context}: blockNumber should be non-negative int, got {block['blockNumber']}"
    )
    assert isinstance(block["timestamp"], int) and block["timestamp"] > 0, (
        f"{context}: timestamp should be positive, got {block['timestamp']}"
    )
    assert isinstance(block["seqNum"], int) and block["seqNum"] >= 0, (
        f"{context}: seqNum should be non-negative, got {block['seqNum']}"
    )
    assert block["version"] == 1, f"{context}: version should be 1, got {block['version']}"
    _assert_valid_block_hash(block["preStateHash"], f"{context} preStateHash")
    _assert_valid_block_hash(block["postStateHash"], f"{context} postStateHash")

    # Sender should be a valid secp256k1 uncompressed pubkey (130 hex chars)
    assert isinstance(block["sender"], str) and _HEX_130.match(block["sender"]), (
        f"{context}: sender should be 130-char hex pubkey, got len={len(block.get('sender', ''))}"
    )

    # Bonds
    bonds = block["bonds"]
    assert len(bonds) == expect["bond_count"], (
        f"{context}: expected {expect['bond_count']} bonds, got {len(bonds)}"
    )
    for bond in bonds:
        validator = bond["validator"]
        assert validator in expect["stakes"], (
            f"{context}: unknown validator in bonds: {validator[:24]}..."
        )
        assert bond["stake"] == expect["stakes"][validator], (
            f"{context}: stake for {validator[:24]}... is {bond['stake']}, "
            f"expected {expect['stakes'][validator]}"
        )

    # Fault tolerance
    ft = block["faultTolerance"]
    assert isinstance(ft, (int, float)), (
        f"{context}: faultTolerance should be numeric, got {type(ft)}"
    )

    # Justifications — list of {validator, latestBlockHash}
    for j in block.get("justifications", []):
        assert "validator" in j and "latestBlockHash" in j, (
            f"{context}: justification missing fields: {j}"
        )


# ===========================================================================
# Tests
# ===========================================================================


def test_status(shared_shard, node_conf, timeouts) -> None:
    """HTTP /api/status returns consistent node info across all nodes."""
    expect = _shard_expectations(shared_shard, node_conf)
    all_nodes = shared_shard.all_nodes

    # Wait for Kademlia discovery to converge across every node before
    # asserting routing-table size. Without this poll, test_status running
    # as the first test against a fresh shard (e.g. `shardctl test --rust
    # test_web_api` filtered via -k) can fire before every node has filled
    # its routing table to validator_count, producing a transient count
    # mismatch. Once each node reaches the threshold, the subsequent
    # assertions read post-convergence state.
    def _discovery_converged():
        for node in all_nodes:
            status = node.api_get("/status")
            if status.get("nodes", 0) < expect["validator_count"]:
                return None
        return True

    poll_until(
        predicate=_discovery_converged,
        timeout=timeouts.node_startup,
        interval=2.0,
        description=(f"every node's discovery table has >= {expect['validator_count']} peers"),
    )

    statuses = {}
    for node in all_nodes:
        status = node.api_get("/status")
        statuses[node.name] = status

        # Version info
        assert "version" in status and isinstance(status["version"], dict), (
            f"{node.name}: missing or invalid version"
        )
        assert status["version"].get("api"), f"{node.name}: empty api version"
        assert status["version"].get("node"), f"{node.name}: empty node version"

        # Network identity
        assert status["shardId"] == expect["shard_id"], (
            f"{node.name}: shardId '{status['shardId']}' != '{expect['shard_id']}'"
        )
        assert status.get("networkId"), f"{node.name}: empty networkId"
        assert status.get("address"), f"{node.name}: empty address"

        # Peers
        assert isinstance(status["peers"], int) and status["peers"] >= 1, (
            f"{node.name}: expected peers >= 1, got {status['peers']}"
        )
        assert isinstance(status["nodes"], int) and status["nodes"] >= expect["validator_count"], (
            f"{node.name}: expected nodes >= {expect['validator_count']}, got {status['nodes']}"
        )

        # Phlo
        assert status["minPhloPrice"] == expect["min_phlo_price"], (
            f"{node.name}: minPhloPrice {status['minPhloPrice']} != "
            f"config {expect['min_phlo_price']}"
        )

        # Token metadata
        assert status["nativeTokenName"] == expect["native_token_name"], (
            f"{node.name}: nativeTokenName '{status['nativeTokenName']}' != '{expect['native_token_name']}'"
        )
        assert status["nativeTokenSymbol"] == expect["native_token_symbol"], (
            f"{node.name}: nativeTokenSymbol '{status['nativeTokenSymbol']}' != '{expect['native_token_symbol']}'"
        )
        assert status["nativeTokenDecimals"] == expect["native_token_decimals"], (
            f"{node.name}: nativeTokenDecimals {status['nativeTokenDecimals']} != {expect['native_token_decimals']}"
        )

        # Operational state (Phase 4b)
        assert (
            isinstance(status["lastFinalizedBlockNumber"], int)
            and status["lastFinalizedBlockNumber"] >= 0
        ), (
            f"{node.name}: lastFinalizedBlockNumber should be >= 0, got {status.get('lastFinalizedBlockNumber')}"
        )
        assert isinstance(status["isReady"], bool) and status["isReady"] is True, (
            f"{node.name}: isReady should be True (shard is running)"
        )
        assert isinstance(status["isValidator"], bool), f"{node.name}: isValidator should be bool"
        assert isinstance(status["isReadOnly"], bool), f"{node.name}: isReadOnly should be bool"
        assert isinstance(status["currentEpoch"], int) and status["currentEpoch"] >= 0, (
            f"{node.name}: currentEpoch should be >= 0"
        )
        assert isinstance(status["epochLength"], int) and status["epochLength"] > 0, (
            f"{node.name}: epochLength should be > 0"
        )

        # Node role consistency
        if node == shared_shard.readonly:
            assert status["isReadOnly"] is True, (
                f"{node.name}: readonly node should have isReadOnly=True"
            )
        else:
            assert status["isReadOnly"] is False, (
                f"{node.name}: non-readonly node should have isReadOnly=False"
            )

    # Cross-node consistency
    versions = {n: s["version"]["node"] for n, s in statuses.items()}
    assert len(set(versions.values())) == 1, f"Nodes disagree on version: {versions}"

    network_ids = {n: s["networkId"] for n, s in statuses.items()}
    assert len(set(network_ids.values())) == 1, f"Nodes disagree on networkId: {network_ids}"

    shard_ids = {n: s["shardId"] for n, s in statuses.items()}
    assert len(set(shard_ids.values())) == 1, f"Nodes disagree on shardId: {shard_ids}"

    phlo_prices = {n: s["minPhloPrice"] for n, s in statuses.items()}
    assert len(set(phlo_prices.values())) == 1, f"Nodes disagree on minPhloPrice: {phlo_prices}"

    epoch_lengths = {n: s["epochLength"] for n, s in statuses.items()}
    assert len(set(epoch_lengths.values())) == 1, f"Nodes disagree on epochLength: {epoch_lengths}"

    # Addresses should be unique per node
    addresses = {n: s["address"] for n, s in statuses.items()}
    assert len(set(addresses.values())) == len(all_nodes), (
        f"Node addresses should be unique: {addresses}"
    )

    logging.info("Status verified on %d nodes", len(all_nodes))


def test_prepare_deploy(shared_shard, timeouts) -> None:
    """HTTP /api/prepare-deploy returns incrementing sequence numbers on multiple nodes."""
    v1 = shared_shard.node("validator1")
    v2 = shared_shard.node("validator2")
    _deploy_and_wait(v1, timeouts, count=3)

    for node in [v1, v2]:

        def _seq_ready(n=node):
            resp = n.api_get("/prepare-deploy")
            if resp.get("seqNumber", 0) >= 3:
                return resp
            return None

        result = poll_until(
            predicate=_seq_ready,
            timeout=timeouts.finalization,
            interval=3.0,
            description=f"prepare-deploy seq_number >= 3 on {node.name}",
        )
        seq_number = result["seqNumber"]
        assert seq_number >= 3, f"{node.name}: expected seq_number >= 3, got {seq_number}"
        assert "names" in result, f"{node.name}: response missing 'names'"
        logging.info("%s: prepare-deploy seq_number=%d", node.name, seq_number)

    # POST with deployer params
    pub_key = VALIDATOR1_ID.private_key().get_public_key().to_hex()
    resp = v1.api_post(
        "/prepare-deploy",
        {
            "deployer": pub_key,
            "timestamp": 1,
            "nameQty": 2,
        },
    )
    resp.raise_for_status()
    result = resp.json()
    assert len(result.get("names", [])) == 2, (
        f"Expected 2 names for nameQty=2, got {len(result.get('names', []))}"
    )


def test_last_finalized_block(shared_shard, node_conf, timeouts) -> None:
    """HTTP /api/last-finalized-block returns consistent finalized block across all nodes."""
    expect = _shard_expectations(shared_shard, node_conf)
    v1 = shared_shard.node("validator1")
    _deploy_and_wait(v1, timeouts, all_nodes=shared_shard.all_nodes)

    # Poll until all nodes' HTTP /last-finalized-block returns the same hash.
    # Observers (boot, readonly) lag validators in LFB-pointer updates by a
    # few seconds — validators have direct access to their own finalization
    # votes; observers must receive enough votes via gossip and re-run the
    # finalization decision locally. A one-shot snapshot can land in that
    # observer-lag window and see a 2-vs-3 hash split; polling resolves it
    # within a few seconds.
    def _snapshot_when_agreed():
        snap = {n.name: n.api_get("/last-finalized-block") for n in shared_shard.all_nodes}
        hashes = {n: s["blockInfo"]["blockHash"] for n, s in snap.items()}
        if len(set(hashes.values())) == 1:
            return snap
        return None

    lfb_data_full = poll_until(
        _snapshot_when_agreed,
        timeout=timeouts.finalization,
        interval=2.0,
        description="HTTP /last-finalized-block hash agreement across all nodes",
    )

    # Per-node validations on the agreed snapshot.
    lfb_data = {}
    for node in shared_shard.all_nodes:
        lfb = lfb_data_full[node.name]
        assert "blockInfo" in lfb, f"{node.name}: missing blockInfo"
        assert "deploys" in lfb, f"{node.name}: missing deploys"

        info = lfb["blockInfo"]
        _assert_light_block_info(info, expect, context=f"{node.name} LFB")

        assert info["blockNumber"] > 0, (
            f"{node.name}: LFB blockNumber should be > 0, got {info['blockNumber']}"
        )

        # isFinalized should be True for LFB
        assert info.get("isFinalized") is True, f"{node.name}: LFB should have isFinalized=True"

        # Finalized blocks should have FT >= FTT (finalization threshold)
        assert info["faultTolerance"] >= expect["ftt"], (
            f"{node.name}: finalized block #{info['blockNumber']} has FT={info['faultTolerance']}, "
            f"expected >= FTT={expect['ftt']} for a finalized block"
        )

        # Non-genesis blocks have parents
        assert len(info["parentsHashList"]) > 0, (
            f"{node.name}: LFB should have parents (not genesis)"
        )

        lfb_data[node.name] = info
        logging.info("%s: LFB #%d, FT=%s", node.name, info["blockNumber"], info["faultTolerance"])

    # Cross-check: HTTP FT matches gRPC FT for the same block
    lfb_hash = lfb_data[shared_shard.all_nodes[0].name]["blockHash"]
    for node in shared_shard.all_nodes:
        grpc_block = node.get_block(lfb_hash)
        grpc_ft = float(grpc_block.blockInfo.faultTolerance)
        http_ft = lfb_data[node.name]["faultTolerance"]
        assert abs(grpc_ft - http_ft) < 0.001, (
            f"{node.name}: FT mismatch for LFB {lfb_hash[:16]}: gRPC={grpc_ft}, HTTP={http_ft}"
        )

    logging.info(
        "LFB verified on %d nodes, FT consistent between HTTP and gRPC", len(shared_shard.all_nodes)
    )


def test_get_block(shared_shard, node_conf, timeouts) -> None:
    """HTTP /api/block/<hash> returns full block info consistent across all nodes."""
    expect = _shard_expectations(shared_shard, node_conf)
    v1 = shared_shard.node("validator1")
    deploy_ids, block_hashes = _deploy_and_wait(v1, timeouts, all_nodes=shared_shard.all_nodes)
    block_hash = block_hashes[0]
    deploy_id = deploy_ids[0]

    blocks = {}
    for node in shared_shard.all_nodes:
        block = node.api_get(f"/block/{block_hash}")
        assert "blockInfo" in block, f"{node.name}: missing blockInfo"
        assert "deploys" in block, f"{node.name}: missing deploys"

        info = block["blockInfo"]
        _assert_light_block_info(info, expect, context=f"{node.name} block")

        assert info["blockHash"] == block_hash, (
            f"{node.name}: returned blockHash doesn't match queried hash"
        )

        # Block should be finalized (we waited for finalization)
        assert info.get("isFinalized") is True, f"{node.name}: queried block should be finalized"

        # Find our deploy in the block
        our_deploy = None
        for d in block["deploys"]:
            if d["sig"] == deploy_id:
                our_deploy = d
                break
        assert our_deploy is not None, f"{node.name}: deploy {deploy_id[:24]} not found in block"
        assert our_deploy["errored"] is False, f"{node.name}: deploy should not be errored"
        assert isinstance(our_deploy["cost"], int) and our_deploy["cost"] > 0, (
            f"{node.name}: deploy cost should be > 0, got {our_deploy['cost']}"
        )
        assert our_deploy["systemDeployError"] == "", (
            f"{node.name}: systemDeployError should be empty"
        )

        blocks[node.name] = info

    # All nodes agree on block content
    post_states = {n: b["postStateHash"] for n, b in blocks.items()}
    assert len(set(post_states.values())) == 1, f"Nodes disagree on postStateHash: {post_states}"

    logging.info("Block %s verified on %d nodes", block_hash[:16], len(shared_shard.all_nodes))


def test_get_blocks(shared_shard, node_conf, timeouts) -> None:
    """HTTP /api/blocks/<depth> returns valid blocks on all nodes.
    Default view is summary (deploys omitted). Response is BlockInfoSerde
    with blockInfo wrapper.
    """
    expect = _shard_expectations(shared_shard, node_conf)
    v1 = shared_shard.node("validator1")
    _deploy_and_wait(v1, timeouts, count=3, all_nodes=shared_shard.all_nodes)

    for node in shared_shard.all_nodes:
        blocks = node.api_get("/blocks/10")
        assert len(blocks) >= 4, f"{node.name}: expected >= 4 blocks, got {len(blocks)}"

        for b in blocks:
            # Response is BlockInfoSerde: {blockInfo: {...}, deploys?: [...]}
            assert "blockInfo" in b, f"{node.name}: block missing blockInfo wrapper"
            info = b["blockInfo"]
            _assert_valid_block_hash(info["blockHash"], f"{node.name} blocks list")
            assert isinstance(info["blockNumber"], int) and info["blockNumber"] >= 0, (
                f"{node.name}: invalid blockNumber {info.get('blockNumber')}"
            )
            assert len(info["bonds"]) == expect["bond_count"], (
                f"{node.name}: block #{info['blockNumber']} has {len(info['bonds'])} bonds, "
                f"expected {expect['bond_count']}"
            )
            # Summary view: deploys should be omitted
            assert "deploys" not in b, (
                f"{node.name}: block #{info['blockNumber']} should not have deploys in summary view"
            )

    logging.info("Blocks list verified on %d nodes", len(shared_shard.all_nodes))


def test_get_deploy_detail(shared_shard, node_conf, timeouts) -> None:
    """HTTP /api/deploy/<id> returns full DeployResponse on all nodes (default=full view)."""
    expect = _shard_expectations(shared_shard, node_conf)
    v1 = shared_shard.node("validator1")
    v1_pubkey = VALIDATOR1_ID.private_key().get_public_key().to_hex()
    deploy_ids, _ = _deploy_and_wait(v1, timeouts, all_nodes=shared_shard.all_nodes)
    deploy_id = deploy_ids[0]

    details = {}
    for node in shared_shard.all_nodes:
        detail = node.api_get(f"/deploy/{deploy_id}")

        # Core fields (always present)
        assert detail["deployId"] == deploy_id, f"{node.name}: deployId should match queried id"
        _assert_valid_block_hash(detail["blockHash"], f"{node.name} deploy detail")
        assert isinstance(detail["blockNumber"], int) and detail["blockNumber"] > 0, (
            f"{node.name}: blockNumber should be > 0"
        )
        assert isinstance(detail["timestamp"], int) and detail["timestamp"] > 0, (
            f"{node.name}: timestamp should be > 0"
        )
        assert isinstance(detail["cost"], int) and detail["cost"] > 0, (
            f"{node.name}: cost should be > 0, got {detail['cost']}"
        )
        assert detail["errored"] is False, f"{node.name}: deploy should not be errored"
        assert detail["isFinalized"] is True, f"{node.name}: deploy should be finalized"

        # Full view fields
        assert detail["deployer"] == v1_pubkey, f"{node.name}: deployer mismatch"
        assert detail["systemDeployError"] == "", f"{node.name}: systemDeployError should be empty"
        assert detail["phloPrice"] == 1, (
            f"{node.name}: phloPrice should be 1, got {detail['phloPrice']}"
        )
        assert detail["phloLimit"] == 100_000, (
            f"{node.name}: phloLimit should be 100000, got {detail['phloLimit']}"
        )
        assert detail["sigAlgorithm"] == expect["sig_algorithm"], (
            f"{node.name}: sigAlgorithm mismatch"
        )

        # Transfers: omitted on validators (block replay unavailable),
        # present as list on readonly
        if node == shared_shard.readonly:
            assert "transfers" in detail, (
                f"{node.name} (readonly): transfers field should be present"
            )
            assert isinstance(detail["transfers"], list), (
                f"{node.name} (readonly): transfers should be a list"
            )
        else:
            assert "transfers" not in detail, (
                f"{node.name} (validator): transfers field should be omitted"
            )

        details[node.name] = detail

    # Cross-node consistency
    block_hashes = {n: d["blockHash"] for n, d in details.items()}
    assert len(set(block_hashes.values())) == 1, f"Nodes disagree on deploy block: {block_hashes}"
    costs = {n: d["cost"] for n, d in details.items()}
    assert len(set(costs.values())) == 1, f"Nodes disagree on deploy cost: {costs}"

    logging.info(
        "Deploy detail verified on %d nodes, cost=%d",
        len(shared_shard.all_nodes),
        list(costs.values())[0],
    )


def test_deploy_summary_view(shared_shard, node_conf, timeouts) -> None:
    """HTTP /api/deploy/<id>?view=summary returns core fields only on all nodes."""
    v1 = shared_shard.node("validator1")
    deploy_ids, _ = _deploy_and_wait(v1, timeouts, all_nodes=shared_shard.all_nodes)
    deploy_id = deploy_ids[0]

    summary_responses = {}
    for node in shared_shard.all_nodes:
        summary = node.api_get(f"/deploy/{deploy_id}?view=summary")

        # Core fields that MUST be present
        assert summary["deployId"] == deploy_id, f"{node.name}: deployId should match queried id"
        _assert_valid_block_hash(summary["blockHash"], f"{node.name} summary view")
        assert isinstance(summary["blockNumber"], int) and summary["blockNumber"] > 0, (
            f"{node.name}: blockNumber should be > 0"
        )
        assert isinstance(summary["timestamp"], int) and summary["timestamp"] > 0, (
            f"{node.name}: timestamp should be > 0"
        )
        assert isinstance(summary["cost"], int) and summary["cost"] > 0, (
            f"{node.name}: cost should be > 0, got {summary.get('cost')}"
        )
        assert isinstance(summary["errored"], bool), f"{node.name}: errored should be bool"
        assert isinstance(summary["isFinalized"], bool), f"{node.name}: isFinalized should be bool"

        # Full-view fields that should NOT be in summary
        for excluded in [
            "deployer",
            "term",
            "phloPrice",
            "phloLimit",
            "sigAlgorithm",
            "systemDeployError",
            "validAfterBlockNumber",
            "transfers",
        ]:
            assert excluded not in summary, (
                f"{node.name}: summary view should not include '{excluded}'"
            )

        summary_responses[node.name] = summary

    # Cross-node consistency
    block_hashes = {n: m["blockHash"] for n, m in summary_responses.items()}
    assert len(set(block_hashes.values())) == 1, f"Nodes disagree on deploy block: {block_hashes}"
    costs = {n: m["cost"] for n, m in summary_responses.items()}
    assert len(set(costs.values())) == 1, f"Nodes disagree on deploy cost: {costs}"

    logging.info(
        "Deploy summary view verified on %d nodes, cost=%d",
        len(shared_shard.all_nodes),
        list(costs.values())[0],
    )


def test_get_data_at_name_empty_payload(shared_shard, timeouts) -> None:
    """gRPC getDataAtName returns empty payload (not error) on multiple nodes.

    The deploys write to channel @N, not to deployId. Querying deployId
    should return empty data, not an error (PR #472).
    """
    v1 = shared_shard.node("validator1")
    v2 = shared_shard.node("validator2")
    deploy_ids, block_hashes = _deploy_and_wait(v1, timeouts)

    for node in [v1, v2]:
        result = node.get_deploy_data(deploy_ids[0], block_hash=block_hashes[0])
        assert result is not None, (
            f"{node.name}: getDataAtName should return empty payload, not None"
        )
        assert len(result.par) == 0, (
            f"{node.name}: expected empty par, got {len(result.par)} entries"
        )

    logging.info("Empty payload verified on V1 and V2")


def test_explore_deploy_returns_cost(shared_shard) -> None:
    """HTTP /api/explore-deploy returns cost on the readonly node.

    Exploratory deploy is restricted to read-only nodes on the Rust node.
    """
    ro = shared_shard.readonly
    term = "new x in { x!(1 + 1) }"

    resp = ro.api_post("/explore-deploy", {"term": term})
    result = resp.json()

    assert "cost" in result, "missing cost"
    assert isinstance(result["cost"], int) and result["cost"] > 0, (
        f"cost should be positive int, got {result['cost']}"
    )
    assert "expr" in result, "missing expr"
    assert "block" in result, "missing block"

    logging.info("Explore-deploy cost=%d on readonly", result["cost"])


def test_deploy_via_http(shared_shard) -> None:
    """HTTP /api/deploy accepts a signed deploy request."""
    v1 = shared_shard.node("validator1")
    key = VALIDATOR1_ID.private_key()
    timestamp = int(time.time() * 1000)

    deploy_proto = DeployDataProto(
        term="@2!(1)",
        timestamp=timestamp,
        phloLimit=100_000,
        phloPrice=1,
        validAfterBlockNumber=5,
        shardId="root",
    )
    deploy_req = {
        "data": {
            "term": "@2!(1)",
            "timestamp": timestamp,
            "phloLimit": 100_000,
            "phloPrice": 1,
            "validAfterBlockNumber": 5,
            "shardId": "root",
        },
        "deployer": key.get_public_key().to_hex(),
        "signature": sign_deploy_data(key, deploy_proto).hex(),
        "sigAlgorithm": "secp256k1",
    }

    resp = v1.api_post("/deploy", deploy_req)
    assert resp.status_code == 200
    logging.info("HTTP deploy accepted: %s", resp.text[:80])


# ===========================================================================
# View parameter tests
# ===========================================================================


def test_block_summary_view(shared_shard, timeouts) -> None:
    """GET /api/block/{hash}?view=summary omits deploys on all nodes."""
    v1 = shared_shard.node("validator1")
    _, block_hashes = _deploy_and_wait(v1, timeouts)
    block_hash = block_hashes[0]

    for node in shared_shard.all_nodes:
        block = node.api_get(f"/block/{block_hash}?view=summary")

        assert "blockInfo" in block, f"{node.name}: missing blockInfo"
        assert "deploys" not in block, f"{node.name}: summary view should not include deploys"
        assert block["blockInfo"]["blockHash"] == block_hash

    logging.info("Block summary view verified on %d nodes", len(shared_shard.all_nodes))


def test_block_list_full_view(shared_shard, timeouts) -> None:
    """GET /api/blocks/{depth}?view=full includes deploys."""
    v1 = shared_shard.node("validator1")
    _, deploy_block_hashes = _deploy_and_wait(v1, timeouts)
    target_hash = deploy_block_hashes[0]

    # Query a depth wide enough to include the deploy's canonical block under
    # production heartbeat cadence (empty blocks accumulate between the
    # deploy's submission and finalization), then locate that exact block in
    # the response and assert its deploys are present.
    blocks = v1.api_get("/blocks/30?view=full")
    assert len(blocks) >= 2, f"expected >= 2 blocks, got {len(blocks)}"

    target = next(
        (b for b in blocks if b.get("blockInfo", {}).get("blockHash") == target_hash),
        None,
    )
    assert target is not None, (
        f"deploy's canonical block {target_hash[:16]}... not in /blocks/30 "
        f"response ({len(blocks)} blocks returned)"
    )
    assert target.get("deploys"), (
        f"full view for block {target_hash[:16]}... should include deploys, got {target!r}"
    )
    logging.info(
        "Block list full view: %d blocks, target block #%d includes deploys",
        len(blocks),
        target["blockInfo"]["blockNumber"],
    )


def test_lfb_summary_view(shared_shard, timeouts) -> None:
    """GET /api/last-finalized-block?view=summary omits deploys."""
    v1 = shared_shard.node("validator1")
    _deploy_and_wait(v1, timeouts)

    lfb = v1.api_get("/last-finalized-block?view=summary")

    assert "blockInfo" in lfb, "missing blockInfo"
    assert "deploys" not in lfb, "summary view should not include deploys"
    assert lfb["blockInfo"]["blockNumber"] > 0

    logging.info("LFB summary view: block #%d, deploys omitted", lfb["blockInfo"]["blockNumber"])


def test_deploy_unknown_view_defaults_full(shared_shard, timeouts) -> None:
    """GET /api/deploy/{id}?view=bogus falls back to full view."""
    v1 = shared_shard.node("validator1")
    deploy_ids, _ = _deploy_and_wait(v1, timeouts)
    deploy_id = deploy_ids[0]

    result = v1.api_get(f"/deploy/{deploy_id}?view=bogus")

    # Should return full view (has deployer, term, etc.)
    assert "deployer" in result, "unknown view should fall back to full"
    assert "deployId" in result
    assert result["deployId"] == deploy_id

    logging.info("Unknown view correctly defaults to full")


def test_blocks_by_height_range(shared_shard, timeouts) -> None:
    """GET /api/blocks/{start}/{end} returns blocks in height range."""
    v1 = shared_shard.node("validator1")
    _deploy_and_wait(v1, timeouts)

    lfb = v1.api_get("/last-finalized-block")
    lfb_number = lfb["blockInfo"]["blockNumber"]
    start = max(0, lfb_number - 3)

    blocks = v1.api_get(f"/blocks/{start}/{lfb_number}")

    assert len(blocks) >= 1, f"expected >= 1 block in range {start}-{lfb_number}"
    for b in blocks:
        assert "blockInfo" in b, "block missing blockInfo wrapper"
        bn = b["blockInfo"]["blockNumber"]
        assert start <= bn <= lfb_number, f"block #{bn} outside range {start}-{lfb_number}"
        # Summary default: no deploys
        assert "deploys" not in b, "summary default should not include deploys"

    logging.info("Blocks by height range %d-%d: %d blocks", start, lfb_number, len(blocks))


# ===========================================================================
# HTTP/gRPC parity and misc
# ===========================================================================


def test_is_finalized_http(shared_shard, timeouts) -> None:
    """GET /api/is-finalized/{hash} returns true for finalized block."""
    v1 = shared_shard.node("validator1")
    _, block_hashes = _deploy_and_wait(v1, timeouts)
    block_hash = block_hashes[0]

    result = v1.api_get(f"/is-finalized/{block_hash}")
    assert result is True, f"expected true, got {result}"

    # Cross-check with gRPC
    grpc_result = v1.is_finalized(block_hash)
    assert grpc_result is True, "gRPC is_finalized should agree"

    logging.info("is-finalized verified: HTTP=%s, gRPC=%s", result, grpc_result)


def _status_pair_over_stable_lfb(node, attempts: int = 5):
    """Sample HTTP and gRPC status across a window where the LFB did not move.

    ``lastFinalizedBlockNumber`` advances on a live shard, so reading the two
    endpoints in sequence is a torn read — the later call legitimately observes
    a higher value, and comparing them is not a well-posed assertion. Bracket
    the gRPC call with two HTTP reads and accept the sample only when both
    agree: the value was then stationary for the whole window, so any residual
    difference is a real parity defect rather than elapsed time.

    Deliberately not "retry until the two endpoints agree" — that would also
    converge when the endpoints genuinely disagree, hiding the defect this
    test exists to catch.
    """
    for _ in range(attempts):
        before = node.api_get("/status")
        grpc_status = node.grpc_status()
        after = node.api_get("/status")
        if before["lastFinalizedBlockNumber"] == after["lastFinalizedBlockNumber"]:
            return before, grpc_status

    raise AssertionError(
        f"{node.name}: lastFinalizedBlockNumber advanced during all {attempts} "
        "sampling attempts; could not compare HTTP and gRPC status over a "
        "stable window"
    )


def test_grpc_status_matches_http(shared_shard, node_conf) -> None:
    """gRPC status() returns same fields as HTTP /api/status on all nodes."""
    for node in shared_shard.all_nodes:
        http_status, grpc_status = _status_pair_over_stable_lfb(node)

        assert grpc_status.shardId == http_status["shardId"], f"{node.name}: shardId mismatch"
        assert grpc_status.networkId == http_status["networkId"], f"{node.name}: networkId mismatch"
        assert grpc_status.minPhloPrice == http_status["minPhloPrice"], (
            f"{node.name}: minPhloPrice mismatch"
        )
        assert grpc_status.lastFinalizedBlockNumber == http_status["lastFinalizedBlockNumber"], (
            f"{node.name}: lastFinalizedBlockNumber mismatch: "
            f"gRPC={grpc_status.lastFinalizedBlockNumber}, HTTP={http_status['lastFinalizedBlockNumber']}"
        )
        assert grpc_status.isValidator == http_status["isValidator"], (
            f"{node.name}: isValidator mismatch"
        )
        assert grpc_status.isReadOnly == http_status["isReadOnly"], (
            f"{node.name}: isReadOnly mismatch"
        )
        assert grpc_status.isReady == http_status["isReady"], f"{node.name}: isReady mismatch"
        assert grpc_status.epochLength == http_status["epochLength"], (
            f"{node.name}: epochLength mismatch"
        )

    logging.info("gRPC/HTTP status parity verified on %d nodes", len(shared_shard.all_nodes))


def test_transfers_null_on_validator_http(shared_shard, timeouts) -> None:
    """Block API returns transfers=null on validator, populated on readonly."""
    v1 = shared_shard.node("validator1")
    ro = shared_shard.readonly
    _, block_hashes = _deploy_and_wait(v1, timeouts)
    block_hash = block_hashes[0]

    # Validator: transfers should be null (omitted) on deploys
    v1_block = v1.api_get(f"/block/{block_hash}")
    for deploy in v1_block.get("deploys", []):
        assert deploy.get("transfers") is None, (
            f"validator: deploy transfers should be null, got {deploy.get('transfers')}"
        )

    # Readonly: transfers should be present as list
    ro_block = ro.api_get(f"/block/{block_hash}")
    for deploy in ro_block.get("deploys", []):
        assert "transfers" in deploy, "readonly: deploy should have transfers field"
        assert isinstance(deploy["transfers"], list), "readonly: transfers should be a list"

    logging.info("Transfer null/populated behavior verified: validator=null, readonly=list")


def test_removed_endpoints_404(shared_shard) -> None:
    """Removed endpoints return 404."""
    import requests

    v1 = shared_shard.node("validator1")

    # POST /api/data-at-name — removed
    resp = requests.post(
        f"{v1.http_url}/api/data-at-name",
        json={"name": {"UnforgDeploy": {"data": "abc"}}, "depth": 1},
        timeout=10,
    )
    assert resp.status_code == 404, f"/api/data-at-name should return 404, got {resp.status_code}"

    # GET /api/transactions/{hash} — removed
    resp = requests.get(
        f"{v1.http_url}/api/transactions/abc123",
        timeout=10,
    )
    assert resp.status_code == 404, f"/api/transactions should return 404, got {resp.status_code}"

    logging.info("Removed endpoints correctly return 404")


# ===========================================================================
# gRPC-only method tests
# ===========================================================================


def test_show_main_chain(shared_shard, timeouts) -> None:
    """gRPC showMainChain returns blocks on the main chain path."""
    v1 = shared_shard.node("validator1")
    _deploy_and_wait(v1, timeouts, count=2)

    blocks = v1.show_main_chain(depth=5)

    assert len(blocks) >= 2, f"expected >= 2 blocks on main chain, got {len(blocks)}"

    # Block numbers should be in descending order (most recent first)
    for i in range(len(blocks) - 1):
        assert blocks[i].blockNumber >= blocks[i + 1].blockNumber, (
            f"main chain blocks not in descending order: "
            f"#{blocks[i].blockNumber} followed by #{blocks[i + 1].blockNumber}"
        )

    # Each block should have a valid hash
    for b in blocks:
        assert len(b.blockHash) == 64, (
            f"block hash should be 64-char hex, got len={len(b.blockHash)}"
        )

    logging.info(
        "showMainChain: %d blocks, heights %d-%d",
        len(blocks),
        blocks[-1].blockNumber,
        blocks[0].blockNumber,
    )


def test_preview_private_names(shared_shard) -> None:
    """gRPC previewPrivateNames generates deterministic unforgeable names."""
    v1 = shared_shard.node("validator1")
    timestamp = 1700000000000

    # Request 3 names
    response = v1.preview_private_names(timestamp=timestamp, name_qty=3)

    ids = list(response.payload.ids)
    assert len(ids) == 3, f"expected 3 names, got {len(ids)}"

    # Each ID should be non-empty bytes
    for i, name_id in enumerate(ids):
        assert len(name_id) > 0, f"name {i} should be non-empty"

    # All IDs should be unique
    assert len(set(bytes(n) for n in ids)) == 3, "all 3 names should be unique"

    # Deterministic: same inputs produce same outputs
    response2 = v1.preview_private_names(timestamp=timestamp, name_qty=3)
    ids2 = list(response2.payload.ids)
    for i in range(3):
        assert bytes(ids[i]) == bytes(ids2[i]), f"name {i} not deterministic across calls"

    logging.info("previewPrivateNames: 3 unique, deterministic names generated")


def test_get_event_data(shared_shard, timeouts) -> None:
    """gRPC getEventByHash returns block execution trace with deploy events."""
    v1 = shared_shard.node("validator1")
    ro = shared_shard.readonly
    deploy_ids, block_hashes = _deploy_and_wait(v1, timeouts)
    block_hash = block_hashes[0]

    # getEventByHash requires readonly (block replay)
    response = ro.get_event_data(block_hash)

    result = response.result
    assert result is not None, "getEventByHash should return result"

    # Block info should match
    assert result.blockInfo.blockHash == block_hash, (
        f"blockHash mismatch: {result.blockInfo.blockHash[:16]} != {block_hash[:16]}"
    )

    # Should have deploy execution data
    assert len(result.deploys) > 0, "block should have at least 1 deploy in event data"

    # Each deploy should have report events
    for deploy in result.deploys:
        assert deploy.deployInfo is not None, "deploy should have deployInfo"
        assert len(deploy.deployInfo.sig) > 0, "deploy should have sig"

    logging.info(
        "getEventByHash: block %s has %d deploys, %d system deploys",
        block_hash[:16],
        len(result.deploys),
        len(result.systemDeploys),
    )


def test_get_continuation(shared_shard, timeouts) -> None:
    """gRPC listenForContinuationAtName returns continuations on a channel."""
    v1 = shared_shard.node("validator1")
    from f1r3fly.pb.RhoTypes_pb2 import Par

    # Deploy a contract that listens on a channel (creates a continuation)
    rholang = "new ch in { for (x <- ch) { Nil } }"
    deploy_id = v1.deploy_string(
        rholang,
        VALIDATOR1_ID.private_key(),
        phlo_limit=100_000,
    )
    wait_for_deploy_finalized(v1, deploy_id, timeouts.finalization)

    # Query for continuations on a public name (@0)
    par = Par(exprs=[])
    par.exprs.append(__import__("f1r3fly.pb.RhoTypes_pb2", fromlist=["Expr"]).Expr(g_int=0))
    response = v1.get_continuation(par, depth=10)

    # Response should not error
    assert response.WhichOneof("message") != "error", "get_continuation returned error"

    logging.info("listenForContinuationAtName: query completed without error")
