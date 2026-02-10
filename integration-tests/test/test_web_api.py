"""Web API integration tests.

Tests the HTTP API endpoints on a compose-managed shard.
Deploys are made against a validator node (not bootstrap, which is a
ceremony master and cannot propose).

Heartbeat auto-proposes blocks so the fixture only deploys and waits for
blocks to appear rather than calling propose() directly.
"""
import time
from typing import Generator, Tuple, List
import pytest
from .rnode import Node, default_shard_id
from .conftest import VALIDATOR1_KEY
from .http_client import HttpClient

pytestmark = pytest.mark.xdist_group("shard")


@pytest.fixture(scope="module")
def node_with_blocks(validator1_node: Node) -> Generator[Tuple[Node, List[str], List[str]], None, None]:
    """Deploy three contracts on validator1 and wait for heartbeat to include
    them in blocks, yielding the node along with deploy hashes and the block
    hashes that contain them."""
    deploy_hashes: List[str] = []

    for _ in range(3):
        dh = validator1_node.deploy_string(
            '@1!(1)',
            VALIDATOR1_KEY,
            phlo_limit=100000,
            phlo_price=1,
            shard_id=default_shard_id,
        )
        deploy_hashes.append(dh)

    # Wait for heartbeat to propose blocks containing all three deploys
    block_hashes: List[str] = []
    deadline = time.time() + 120
    remaining = set(deploy_hashes)
    while remaining and time.time() < deadline:
        for dh in list(remaining):
            try:
                deploy_info = validator1_node.find_deploy(dh)
                if deploy_info.blockHash:
                    block_hashes.append(deploy_info.blockHash)
                    remaining.discard(dh)
            except Exception:
                pass
        if remaining:
            time.sleep(3)

    assert not remaining, (
        f"Deploys not included in blocks within 120s: {remaining}"
    )

    yield (validator1_node, deploy_hashes, block_hashes)


def test_status(validator1_node: Node) -> None:
    """HTTP /api/status returns version info."""
    client = HttpClient('localhost', validator1_node.get_http_port())
    status = client.status()
    assert status.version


def test_prepare_deploy(node_with_blocks: Tuple[Node, List[str], List[str]]) -> None:
    """HTTP /api/prepare-deploy returns incrementing sequence numbers."""
    node = node_with_blocks[0]
    client = HttpClient('localhost', node.get_http_port())

    prepare_rep = client.prepare_deploy()
    assert prepare_rep.seq_number >= 3

    prepare_rep_2 = client.prepare_deploy(
        VALIDATOR1_KEY.get_public_key().to_hex(), 1, 1,
    )
    assert prepare_rep_2.seq_number >= 3


def test_data_at_name(node_with_blocks: Tuple[Node, List[str], List[str]]) -> None:
    """HTTP /api/data-at-name for a deploy hash."""
    node = node_with_blocks[0]
    deploy_hash = node_with_blocks[1]
    client = HttpClient('localhost', node.get_http_port())

    data_at_name = client.data_at_name(deploy_hash[0], 1, "UnforgDeploy")
    assert data_at_name.length == 0
    assert not data_at_name.exprs


def test_last_finalized_block(node_with_blocks: Tuple[Node, List[str], List[str]]) -> None:
    """HTTP /api/last-finalized-block returns block info."""
    node = node_with_blocks[0]
    client = HttpClient('localhost', node.get_http_port())

    last_finalized = client.last_finalized_block()
    assert "blockInfo" in last_finalized
    assert "deploys" in last_finalized


def test_get_block(node_with_blocks: Tuple[Node, List[str], List[str]]) -> None:
    """HTTP /api/block/<hash> returns block info."""
    node = node_with_blocks[0]
    block_hash = node_with_blocks[2]
    client = HttpClient('localhost', node.get_http_port())

    block = client.get_block(block_hash[0])
    assert "blockInfo" in block
    assert "deploys" in block


def test_get_blocks(node_with_blocks: Tuple[Node, List[str], List[str]]) -> None:
    """HTTP /api/blocks/<depth> returns the expected number of blocks."""
    node = node_with_blocks[0]
    client = HttpClient('localhost', node.get_http_port())

    blocks = client.get_blocks(10)
    # Genesis block + 3 deployed blocks
    assert len(blocks) >= 4


def test_get_deploy(node_with_blocks: Tuple[Node, List[str], List[str]]) -> None:
    """HTTP /api/deploy/<id> returns deploy details."""
    node = node_with_blocks[0]
    deploy_hash = node_with_blocks[1]
    client = HttpClient('localhost', node.get_http_port())

    deploy_block = client.get_deploy(deploy_hash[0])
    assert "blockHash" in deploy_block
    assert "seqNum" in deploy_block


def test_deploy_via_http(node_with_blocks: Tuple[Node, List[str], List[str]]) -> None:
    """HTTP /api/deploy accepts a deploy request."""
    node = node_with_blocks[0]
    client = HttpClient('localhost', node.get_http_port())

    ret = client.deploy("@2!(1)", 100000, 1, 5, VALIDATOR1_KEY, shard_id=default_shard_id)
    assert ret is not None
