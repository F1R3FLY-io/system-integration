"""Wire-level tests of how nodes judge and punish misbehaving validators.

Each test builds a block a validator must not produce, sends it to a node
over the P2P transport as that validator, and checks the node's verdict and
its consequence. Only equivocation is slashable: every other offence is judged
against the receiving node's own state, so the block is rejected and its
sender keeps its stake.

A block's ``bonds`` field records the committee at its finalized floor, not
the stake after the block's own system deploys, so stake is read from PoS
state at a block through the shard's read-only node.

Proposer fairness is not tested for its liveness: deploys are not gossiped, so
a proposer cannot withhold another validator's deploys, and there is nothing
to detect. ``test_no_false_positive_slash_on_propose_imbalance`` covers its
safety: uneven proposing is never punished.
"""

from __future__ import annotations

import re
import sys
import time
from contextlib import contextmanager
from typing import Dict, Generator, Optional, Tuple

import pytest
from f1r3fly.client import F1r3flyClientException
from f1r3fly.crypto import PrivateKey, gen_block_hash_from_block
from f1r3fly.pb.CasperMessage_pb2 import BlockMessageProto as BlockMessage
from f1r3fly.pb.CasperMessage_pb2 import JustificationProto as Justification
from f1r3fly.pb.CasperMessage_pb2 import (
    ProcessedSystemDeployProto,
    SlashSystemDeployDataProto,
    SystemDeployDataProto,
)
from f1r3fly.util import create_deploy_data

from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.node import Node
from ...infra.p2p_client import (
    NodeClient,
    generate_block_hash,
    is_exist_slash_deploy,
    p2p_protocol_client,
)
from ...infra.polling import wait_for_block_visible, wait_for_log_match
from ...infra.providers.docker import DockerProvider
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

KEY_1: PrivateKey = VALIDATOR1_ID.private_key()
KEY_2: PrivateKey = VALIDATOR2_ID.private_key()
KEY_3: PrivateKey = VALIDATOR3_ID.private_key()

STAKE = 100

HELLO_CONTRACT = "new x in { x!(1) }"


@contextmanager
def _slash_shard(
    provider,
    timeouts,
    *,
    three_validators: bool = False,
    global_cli_options: Optional[Dict[str, str]] = None,
) -> Generator[Tuple[Shard, Node, Node, NodeClient], None, None]:
    """A shard whose validators propose only when a test asks, with a
    read-only node for PoS reads. Yields ``(shard, validator1, validator2,
    client)``."""
    if isinstance(provider, DockerProvider) and sys.platform != "linux":
        pytest.skip("Docker containers reach the test process only with native Linux networking")
    bonds = [(VALIDATOR1_ID, STAKE), (VALIDATOR2_ID, STAKE)]
    if three_validators:
        bonds.append((VALIDATOR3_ID, STAKE))
    config = ShardConfig(
        bonds=bonds,
        ftt=-1.0,
        heartbeat=False,
        include_readonly=True,
        global_cli_options=global_cli_options or {},
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        with p2p_protocol_client(
            shard.boot.p2p_reply_host, receive_timeout=float(timeouts.command)
        ) as client:
            yield (shard, shard.node("validator1"), shard.node("validator2"), client)
    finally:
        shard.destroy()


def _fetch(client: NodeClient, block_hash: str, node: Node) -> BlockMessage:
    block = client.block_request(block_hash, node)
    assert gen_block_hash_from_block(block) == block.blockHash, (
        "pyf1r3fly's block hash diverged from the node's"
    )
    return block


def _seal(block: BlockMessage, key: PrivateKey) -> bytes:
    """Re-hash ``block`` and sign it with ``key``; returns the new hash."""
    block_hash = gen_block_hash_from_block(block)
    block.blockHash = block_hash
    block.sig = key.sign_block_hash(block_hash)
    return block_hash


def _sibling(block: BlockMessage, key: PrivateKey) -> BlockMessage:
    """An equivocating sibling of ``block``: its content, one extra byte in
    ``extraBytes``, which no system deploy or replay reads."""
    sibling = BlockMessage()
    sibling.CopyFrom(block)
    sibling.extraBytes = block.extraBytes + b"\x00"
    _seal(sibling, key)
    return sibling


def _wait_recorded(node: Node, block_hash: bytes, verdict: str, timeouts) -> None:
    wait_for_log_match(
        node,
        re.compile(rf"Recording invalid block {block_hash.hex()[:10]}\.\.\. for (?:{verdict})\."),
        timeouts.command,
    )


def _propose(node: Node, key: PrivateKey) -> str:
    node.deploy_string(HELLO_CONTRACT, key)
    return node.propose()


def _stake(shard: Shard, block_hash: str, key: PrivateKey, timeouts) -> int:
    """``key``'s stake in PoS state after ``block_hash``."""
    readonly = shard.readonly
    wait_for_block_visible(readonly, block_hash, timeouts.node_startup)
    return readonly.pos.get_bonds(block_hash)[key.get_public_key().to_hex()]


def _assert_rejected_without_slash(
    shard: Shard, client: NodeClient, judge: Node, judge_key: PrivateKey, timeouts
) -> None:
    """``judge``'s next block carries no slash and every validator keeps its
    stake."""
    next_hash = _propose(judge, judge_key)
    assert not is_exist_slash_deploy(_fetch(client, next_hash, judge))
    for key in [KEY_1, KEY_2, KEY_3][: len(shard.validators)]:
        assert _stake(shard, next_hash, key, timeouts) == STAKE


# ── Offences judged against the receiver's own state: rejected, not slashed ──


@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_invalid_block_hash(provider, timeouts) -> None:
    """v1 sends a block whose declared hash does not match its content. v2
    ignores it without recording a verdict, and v1 keeps its stake: a
    signature over a hash the content does not produce pins nothing on its
    signer.
    """
    with _slash_shard(provider, timeouts) as (shard, validator1, validator2, client):
        blockhash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, blockhash, timeouts.node_startup)
        block = _fetch(client, blockhash, validator1)

        evil_block_hash = generate_block_hash()
        block.blockHash = evil_block_hash
        block.sig = KEY_1.sign_block_hash(evil_block_hash)
        block.header.timestamp = int(time.time() * 1000)
        client.send_block(block, validator2)

        wait_for_log_match(
            validator2,
            re.compile(
                rf"Ignoring block {evil_block_hash.hex()[:10]}\.\.\. because block hash "
                rf"{evil_block_hash.hex()[:10]}\.\.\. does not match"
            ),
            timeouts.command,
        )
        _assert_rejected_without_slash(shard, client, validator2, KEY_2, timeouts)


@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_invalid_block_number(provider, timeouts) -> None:
    """v1 sends a block whose number is not one above its parents'. v2
    records :class:`InvalidBlockNumber` and drops it; v1 keeps its stake."""
    with _slash_shard(provider, timeouts) as (shard, validator1, validator2, client):
        blockhash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, blockhash, timeouts.node_startup)
        block = _fetch(client, blockhash, validator1)

        invalid = BlockMessage()
        invalid.CopyFrom(block)
        invalid.body.state.blockNumber = 5
        invalid.header.timestamp = block.header.timestamp + 1
        invalid_hash = _seal(invalid, KEY_1)
        client.send_block(invalid, validator2)

        _wait_recorded(validator2, invalid_hash, "InvalidBlockNumber", timeouts)
        _assert_rejected_without_slash(shard, client, validator2, KEY_2, timeouts)


@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_invalid_block_seq(provider, timeouts) -> None:
    """v1 sends a block whose sequence number is not one above its previous
    block's. v2 records :class:`InvalidSequenceNumber` and drops it; v1 keeps
    its stake."""
    with _slash_shard(provider, timeouts) as (shard, validator1, validator2, client):
        blockhash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, blockhash, timeouts.node_startup)
        block = _fetch(client, blockhash, validator1)

        invalid = BlockMessage()
        invalid.CopyFrom(block)
        invalid.seqNum = 1000
        invalid.header.timestamp = block.header.timestamp + 1
        invalid_hash = _seal(invalid, KEY_1)
        client.send_block(invalid, validator2)

        _wait_recorded(validator2, invalid_hash, "InvalidSequenceNumber", timeouts)
        _assert_rejected_without_slash(shard, client, validator2, KEY_2, timeouts)


@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_justification_not_correct(provider, timeouts) -> None:
    """v1 sends a block with an extra justification from a key that is not
    bonded. v2 records :class:`InvalidFollows` and drops it; v1 keeps its
    stake. Three validators, so every other justification is well-formed."""
    with _slash_shard(provider, timeouts, three_validators=True) as (
        shard,
        validator1,
        validator2,
        client,
    ):
        blockhash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, blockhash, timeouts.node_startup)
        block = _fetch(client, blockhash, validator1)

        invalid = BlockMessage()
        invalid.CopyFrom(block)
        invalid.justifications.append(
            Justification(
                validator=PrivateKey.generate().to_bytes(), latestBlockHash=block.blockHash
            )
        )
        invalid.header.timestamp = block.header.timestamp + 1
        invalid_hash = _seal(invalid, KEY_1)
        client.send_block(invalid, validator2)

        _wait_recorded(validator2, invalid_hash, "InvalidFollows", timeouts)
        _assert_rejected_without_slash(shard, client, validator2, KEY_2, timeouts)


def _forged_slash(invalid_block_hash: bytes, issuer: PrivateKey) -> ProcessedSystemDeployProto:
    return ProcessedSystemDeployProto(
        systemDeploy=SystemDeployDataProto(
            slashSystemDeploy=SlashSystemDeployDataProto(
                invalidBlockHash=invalid_block_hash,
                issuerPublicKey=issuer.get_public_key().to_bytes(),
                targetActivationEpoch=0,
            )
        ),
    )


@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_unauthorized_slash_deploy(provider, timeouts) -> None:
    """v2 sends a block carrying a slash of v1 whose evidence is a block no
    node holds. v3 records :class:`UnauthorizedSlashDeploy` and drops it;
    v1, the target of the fabricated evidence, and v2 keep their stake."""
    with _slash_shard(provider, timeouts, three_validators=True) as (
        shard,
        validator1,
        validator2,
        client,
    ):
        validator3 = shard.node("validator3")
        v1_hash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, v1_hash, timeouts.node_startup)
        wait_for_block_visible(validator3, v1_hash, timeouts.node_startup)
        v2_hash = _propose(validator2, KEY_2)
        wait_for_block_visible(validator3, v2_hash, timeouts.node_startup)
        v2_block = _fetch(client, v2_hash, validator2)

        forged = BlockMessage()
        forged.CopyFrom(v2_block)
        forged.body.systemDeploys.append(_forged_slash(b"\xde" * 32, KEY_2))
        forged.header.timestamp = v2_block.header.timestamp + 1
        forged_hash = _seal(forged, KEY_2)
        client.send_block(forged, validator3)

        _wait_recorded(validator3, forged_hash, "UnauthorizedSlashDeploy", timeouts)
        _assert_rejected_without_slash(shard, client, validator3, KEY_3, timeouts)


@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_references_valid_block(provider, timeouts) -> None:
    """v2 sends a block carrying a slash of v1 whose evidence is v1's valid
    block. v3 records :class:`UnauthorizedSlashDeploy` and drops it; nobody
    is slashed."""
    with _slash_shard(provider, timeouts, three_validators=True) as (
        shard,
        validator1,
        validator2,
        client,
    ):
        validator3 = shard.node("validator3")
        v1_hash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, v1_hash, timeouts.node_startup)
        wait_for_block_visible(validator3, v1_hash, timeouts.node_startup)
        v2_hash = _propose(validator2, KEY_2)
        wait_for_block_visible(validator3, v2_hash, timeouts.node_startup)
        v2_block = _fetch(client, v2_hash, validator2)

        forged = BlockMessage()
        forged.CopyFrom(v2_block)
        forged.body.systemDeploys.append(_forged_slash(bytes.fromhex(v1_hash), KEY_2))
        forged.header.timestamp = v2_block.header.timestamp + 1
        forged_hash = _seal(forged, KEY_2)
        client.send_block(forged, validator3)

        _wait_recorded(validator3, forged_hash, "UnauthorizedSlashDeploy", timeouts)
        _assert_rejected_without_slash(shard, client, validator3, KEY_3, timeouts)


@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_self_regression(provider, timeouts) -> None:
    """v1 proposes blocks A and B, then sends a block whose own justification
    points back behind A. v2 records :class:`JustificationRegression` and
    drops it; v1 keeps its stake.

    The forgery takes A as its template, so its sequence number, number,
    parents and justifications (all at genesis) are consistent and only the
    regression is wrong. It needs a fresh deploy so A's deploy is not
    repeated, and an extra byte so its hash differs from A's.
    """
    with _slash_shard(provider, timeouts) as (shard, validator1, validator2, client):
        a_hash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, a_hash, timeouts.node_startup)
        b_hash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, b_hash, timeouts.node_startup)
        block_a = _fetch(client, a_hash, validator1)

        forged = BlockMessage()
        forged.CopyFrom(block_a)
        forged.body.deploys[0].deploy.CopyFrom(
            create_deploy_data(KEY_1, HELLO_CONTRACT, 999, 1000000, shard_id="root")
        )
        forged.extraBytes = block_a.extraBytes + b"\x00"
        forged_hash = _seal(forged, KEY_1)
        client.send_block(forged, validator2)

        _wait_recorded(validator2, forged_hash, "JustificationRegression", timeouts)
        _assert_rejected_without_slash(shard, client, validator2, KEY_2, timeouts)


@pytest.mark.allow_forbidden_patterns(
    "RecordingInvalidBlock", "InvalidBondsCache", "BondsCacheMismatch"
)
def test_slash_invalid_bonds_cache(provider, timeouts) -> None:
    """v1 sends a block whose bonds field halves its own stake. v2 records
    :class:`InvalidBondsCache` and drops it; v1 keeps its stake.

    Only the bonds field changes: the header timestamp is part of what the
    close-block deploy reads, so changing it would fail replay first.
    """
    with _slash_shard(provider, timeouts) as (shard, validator1, validator2, client):
        blockhash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, blockhash, timeouts.node_startup)
        block = _fetch(client, blockhash, validator1)

        forged = BlockMessage()
        forged.CopyFrom(block)
        v1_pubkey = KEY_1.get_public_key().to_bytes()
        for bond in forged.body.state.bonds:
            if bond.validator == v1_pubkey:
                bond.stake //= 2
        forged_hash = _seal(forged, KEY_1)
        client.send_block(forged, validator2)

        _wait_recorded(validator2, forged_hash, "InvalidBondsCache", timeouts)
        _assert_rejected_without_slash(shard, client, validator2, KEY_2, timeouts)


@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock", "InvalidRepeatDeploy")
def test_slash_invalid_repeat_deploy(provider, timeouts) -> None:
    """v1 sends a successor of its block carrying the same deploy again. v2
    records :class:`InvalidRepeatDeploy` and drops it; v1 keeps its stake."""
    with _slash_shard(provider, timeouts) as (shard, validator1, validator2, client):
        blockhash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, blockhash, timeouts.node_startup)
        block = _fetch(client, blockhash, validator1)

        forged = BlockMessage()
        forged.CopyFrom(block)
        forged.seqNum = block.seqNum + 1
        forged.body.state.blockNumber = block.body.state.blockNumber + 1
        forged.header.timestamp = int(time.time() * 1000)
        forged.header.ClearField("parentsHashList")
        forged.header.parentsHashList.append(bytes.fromhex(blockhash))
        forged_hash = _seal(forged, KEY_1)
        client.send_block(forged, validator2)

        _wait_recorded(validator2, forged_hash, "InvalidRepeatDeploy", timeouts)
        _assert_rejected_without_slash(shard, client, validator2, KEY_2, timeouts)


@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_invalid_validator_approve_evil_block(provider, timeouts) -> None:
    """v1 sends v2 and v3 a block with a wrong number, which both record as
    invalid. v2 then sends v3 a block citing it without slashing. v3 never
    admits v2's block, and nobody is slashed: neither the invalid block nor
    building on it is provable outside the receiver's own records.

    A rejected block is not held as a dependency, so v3 parks v2's block on
    it rather than judging it.
    """
    with _slash_shard(provider, timeouts, three_validators=True) as (
        shard,
        validator1,
        validator2,
        client,
    ):
        validator3 = shard.node("validator3")
        blockhash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, blockhash, timeouts.node_startup)
        wait_for_block_visible(validator3, blockhash, timeouts.node_startup)
        block = _fetch(client, blockhash, validator1)
        genesis_hash = block.header.parentsHashList[0]

        invalid = BlockMessage()
        invalid.CopyFrom(block)
        invalid.body.state.blockNumber = 5
        invalid.header.timestamp = block.header.timestamp + 1
        invalid_hash = _seal(invalid, KEY_1)
        for node in (validator2, validator3):
            client.send_block(invalid, node)
            _wait_recorded(node, invalid_hash, "InvalidBlockNumber", timeouts)

        neglect = BlockMessage()
        neglect.CopyFrom(block)
        neglect.sender = KEY_2.get_public_key().to_bytes()
        neglect.seqNum = 1
        neglect.ClearField("justifications")
        neglect.justifications.extend(
            [
                Justification(
                    validator=KEY_1.get_public_key().to_bytes(), latestBlockHash=invalid_hash
                ),
                Justification(
                    validator=KEY_2.get_public_key().to_bytes(), latestBlockHash=genesis_hash
                ),
                Justification(
                    validator=KEY_3.get_public_key().to_bytes(), latestBlockHash=genesis_hash
                ),
            ]
        )
        neglect.body.deploys[0].deploy.CopyFrom(
            create_deploy_data(KEY_2, HELLO_CONTRACT, 1, 1000000, shard_id="root")
        )
        neglect.header.timestamp = int(time.time() * 1000)
        neglect_hash = _seal(neglect, KEY_2)
        client.send_block(neglect, validator3)

        wait_for_log_match(
            validator3,
            re.compile(rf"Block {neglect_hash.hex()[:10]}\.\.\. delayed: missing dependencies\."),
            timeouts.command,
        )
        _assert_rejected_without_slash(shard, client, validator3, KEY_3, timeouts)
        assert f"Block {neglect_hash.hex()[:10]}... validated" not in validator3.logs()


@pytest.mark.allow_forbidden_patterns(
    "RecordingInvalidBlock", "InvalidTransaction", "ComputedPreStateMismatch"
)
def test_slash_ghost_disobeyed(provider, timeouts) -> None:
    """v1 sends a block whose parents skip the fork-choice winner and whose
    deploy is replaced. v2's replay diverges from the declared post-state, it
    records :class:`InvalidTransaction` and drops the block; v1 keeps its
    stake.

    A wire forgery cannot carry a post-state consistent with mutated parents,
    so replay fails before the parent check: the parent rule itself is
    covered in-process by the node's slashing tests.
    """
    with _slash_shard(provider, timeouts, three_validators=True) as (
        shard,
        validator1,
        validator2,
        client,
    ):
        validator3 = shard.node("validator3")
        hash1 = _propose(validator3, KEY_3)
        wait_for_block_visible(validator1, hash1, timeouts.node_startup)
        wait_for_block_visible(validator2, hash1, timeouts.node_startup)
        hash2 = _propose(validator2, KEY_2)
        wait_for_block_visible(validator1, hash2, timeouts.node_startup)
        wait_for_block_visible(validator3, hash2, timeouts.node_startup)
        hash3 = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, hash3, timeouts.node_startup)
        wait_for_block_visible(validator3, hash3, timeouts.node_startup)
        block3 = _fetch(client, hash3, validator1)

        forged = BlockMessage()
        forged.CopyFrom(block3)
        forged.header.ClearField("parentsHashList")
        forged.header.parentsHashList.append(bytes.fromhex(hash1))
        forged.body.state.blockNumber = 2
        forged.header.timestamp = int(time.time() * 1000)
        forged.body.deploys[0].deploy.CopyFrom(
            create_deploy_data(KEY_2, HELLO_CONTRACT, 1, 1000000, shard_id="root")
        )
        forged_hash = _seal(forged, KEY_1)
        client.send_block(forged, validator2)

        _wait_recorded(validator2, forged_hash, "InvalidTransaction", timeouts)
        _assert_rejected_without_slash(shard, client, validator2, KEY_2, timeouts)


# ── Equivocation: provable from the two signed blocks alone, so slashed ──


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "the slash is re-issued in later blocks: proposers judge the target bonded by "
        "the parent's bonds field, which lags the slash until the finalized floor passes it"
    ),
)
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_node_working_right_after_slashing(provider, timeouts) -> None:
    """v1 equivocates. v2's next block slashes v1, and v2's block after that
    is ordinary: the slash is issued once per offence."""
    with _slash_shard(provider, timeouts) as (shard, validator1, validator2, client):
        b1_hash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, b1_hash, timeouts.node_startup)
        sibling = _sibling(_fetch(client, b1_hash, validator1), KEY_1)
        client.send_block(sibling, validator2)
        _wait_recorded(validator2, sibling.blockHash, "IgnorableEquivocation", timeouts)

        slashing_hash = _propose(validator2, KEY_2)
        assert is_exist_slash_deploy(_fetch(client, slashing_hash, validator2))
        assert _stake(shard, slashing_hash, KEY_1, timeouts) == 0

        next_hash = _propose(validator2, KEY_2)
        assert not is_exist_slash_deploy(_fetch(client, next_hash, validator2))


@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_ignorable_equivocation(provider, timeouts) -> None:
    """v1 sends v2 a second block at the same sequence number as one v2
    holds. v2 has not asked for it, so it records
    :class:`IgnorableEquivocation`, and its next block slashes v1."""
    with _slash_shard(provider, timeouts) as (shard, validator1, validator2, client):
        b1_hash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, b1_hash, timeouts.node_startup)
        sibling = _sibling(_fetch(client, b1_hash, validator1), KEY_1)
        client.send_block(sibling, validator2)
        _wait_recorded(validator2, sibling.blockHash, "IgnorableEquivocation", timeouts)

        slashing_hash = _propose(validator2, KEY_2)
        assert is_exist_slash_deploy(_fetch(client, slashing_hash, validator2))
        assert _stake(shard, slashing_hash, KEY_1, timeouts) == 0
        assert _stake(shard, slashing_hash, KEY_2, timeouts) == STAKE


@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock", "InvalidRepeatDeploy")
def test_slash_admissible_equivocation(provider, timeouts) -> None:
    """v2 first receives a block citing v1's equivocating sibling, so it asks
    for the sibling as a dependency; when the sibling arrives v2 records
    :class:`AdmissibleEquivocation`, and its next block slashes v1.

    The citing block never validates: it only has to pass the format and
    signature checks that come before dependency resolution.
    """
    with _slash_shard(provider, timeouts) as (shard, validator1, validator2, client):
        b1_hash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, b1_hash, timeouts.node_startup)
        b1 = _fetch(client, b1_hash, validator1)
        genesis_hash = b1.header.parentsHashList[0]
        sibling = _sibling(b1, KEY_1)

        child = BlockMessage()
        child.CopyFrom(b1)
        child.sender = KEY_2.get_public_key().to_bytes()
        child.seqNum = 1
        child.body.state.blockNumber = 2
        child.ClearField("justifications")
        child.justifications.extend(
            [
                Justification(
                    validator=KEY_1.get_public_key().to_bytes(),
                    latestBlockHash=sibling.blockHash,
                ),
                Justification(
                    validator=KEY_2.get_public_key().to_bytes(), latestBlockHash=genesis_hash
                ),
            ]
        )
        child.header.ClearField("parentsHashList")
        child.header.parentsHashList.append(sibling.blockHash)
        child.header.timestamp = b1.header.timestamp + 2
        _seal(child, KEY_2)
        client.send_block(child, validator2)
        wait_for_log_match(
            validator2,
            re.compile(
                rf"Adding {sibling.blockHash.hex()[:10]}.*hash to RequestedBlocks "
                rf"because of MissingDependencyRequested"
            ),
            timeouts.command,
        )

        client.send_block(sibling, validator2)
        _wait_recorded(validator2, sibling.blockHash, "AdmissibleEquivocation", timeouts)

        slashing_hash = _propose(validator2, KEY_2)
        assert is_exist_slash_deploy(_fetch(client, slashing_hash, validator2))
        assert _stake(shard, slashing_hash, KEY_1, timeouts) == 0


@pytest.mark.allow_forbidden_patterns(
    "RecordingInvalidBlock", "InvalidTransaction", "ComputedPreStateMismatch"
)
def test_slash_neglected_equivocation(provider, timeouts) -> None:
    """v1 equivocates to v2 and v3. v2 then sends v3 a block citing the
    sibling without slashing. v3 rejects v2's block, and v3's next block
    slashes v1 but not v2: building on an equivocation is judged against the
    receiver's own records, so it is not slashable.

    v2's block copies v1's post-state while carrying its own deploy, so v3's
    replay diverges before the neglect check runs; either verdict rejects it.
    """
    with _slash_shard(provider, timeouts, three_validators=True) as (
        shard,
        validator1,
        validator2,
        client,
    ):
        validator3 = shard.node("validator3")
        b1_hash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, b1_hash, timeouts.node_startup)
        wait_for_block_visible(validator3, b1_hash, timeouts.node_startup)
        b1 = _fetch(client, b1_hash, validator1)
        genesis_hash = b1.header.parentsHashList[0]
        sibling = _sibling(b1, KEY_1)
        for node in (validator2, validator3):
            client.send_block(sibling, node)
            _wait_recorded(node, sibling.blockHash, "IgnorableEquivocation", timeouts)

        neglect = BlockMessage()
        neglect.CopyFrom(b1)
        neglect.sender = KEY_2.get_public_key().to_bytes()
        neglect.seqNum = 1
        neglect.body.state.blockNumber = 2
        neglect.ClearField("justifications")
        neglect.justifications.extend(
            [
                Justification(
                    validator=KEY_1.get_public_key().to_bytes(),
                    latestBlockHash=sibling.blockHash,
                ),
                Justification(
                    validator=KEY_2.get_public_key().to_bytes(), latestBlockHash=genesis_hash
                ),
                Justification(
                    validator=KEY_3.get_public_key().to_bytes(), latestBlockHash=genesis_hash
                ),
            ]
        )
        neglect.body.deploys[0].deploy.CopyFrom(
            create_deploy_data(KEY_2, HELLO_CONTRACT, 1, 1000000, shard_id="root")
        )
        neglect.header.ClearField("parentsHashList")
        neglect.header.parentsHashList.append(bytes.fromhex(b1_hash))
        neglect.header.timestamp = b1.header.timestamp + 2
        neglect_hash = _seal(neglect, KEY_2)
        client.send_block(neglect, validator3)
        _wait_recorded(
            validator3, neglect_hash, "NeglectedInvalidBlock|InvalidTransaction", timeouts
        )

        slashing_hash = _propose(validator3, KEY_3)
        assert is_exist_slash_deploy(_fetch(client, slashing_hash, validator3))
        assert _stake(shard, slashing_hash, KEY_1, timeouts) == 0
        assert _stake(shard, slashing_hash, KEY_2, timeouts) == STAKE


@pytest.mark.xfail(
    strict=True,
    raises=F1r3flyClientException,
    reason=(
        "node defect: after a late-released equivocation the honest validator's own "
        "slashing block fails self-validation as NeglectedEquivocation"
    ),
)
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock", "ProposeBugError")
def test_slash_late_released_equivocation(provider, timeouts) -> None:
    """v1 withholds an equivocating sibling of its block while v2 builds four
    blocks on the original, then releases it. v2 records
    :class:`IgnorableEquivocation`, and its next block slashes v1: withheld
    evidence is punished whenever it surfaces within the epoch."""
    with _slash_shard(provider, timeouts) as (shard, validator1, validator2, client):
        b1_hash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, b1_hash, timeouts.node_startup)
        sibling = _sibling(_fetch(client, b1_hash, validator1), KEY_1)
        for _ in range(4):
            v2_hash = _propose(validator2, KEY_2)
            wait_for_block_visible(validator1, v2_hash, timeouts.node_startup)

        client.send_block(sibling, validator2)
        _wait_recorded(validator2, sibling.blockHash, "IgnorableEquivocation", timeouts)

        slashing_hash = _propose(validator2, KEY_2)
        assert is_exist_slash_deploy(_fetch(client, slashing_hash, validator2))
        assert _stake(shard, slashing_hash, KEY_1, timeouts) == 0


@pytest.mark.xfail(
    strict=True,
    raises=F1r3flyClientException,
    reason=(
        "node defect: once an equivocation arrives after the chain has built past it, the "
        "honest validator's next block fails self-validation as NeglectedEquivocation"
    ),
)
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock", "ProposeBugError")
def test_slash_stale_evidence_rebond(provider, timeouts) -> None:
    """v1 equivocates in epoch 0, and the evidence reaches v2 and v3 only once
    the chain is in a later epoch. v2 then sends v3 a block slashing v1 on
    it. v3 records :class:`UnauthorizedSlashDeploy`, and nobody is slashed:
    evidence counts only in its own epoch, and an unauthorized slash is not
    itself slashable.

    With an epoch of two blocks, v1's block is number 1 (epoch 0). The
    evidence arrives only after v2 and v3 have built on it, so no honest
    proposer holds it while its epoch is current.
    """
    with _slash_shard(
        provider,
        timeouts,
        three_validators=True,
        global_cli_options={"--epoch-length": "2"},
    ) as (shard, validator1, validator2, client):
        validator3 = shard.node("validator3")
        b1_hash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, b1_hash, timeouts.node_startup)
        wait_for_block_visible(validator3, b1_hash, timeouts.node_startup)
        sibling = _sibling(_fetch(client, b1_hash, validator1), KEY_1)

        v2_hash = _propose(validator2, KEY_2)
        wait_for_block_visible(validator3, v2_hash, timeouts.node_startup)
        v3_hash = _propose(validator3, KEY_3)
        wait_for_block_visible(validator2, v3_hash, timeouts.node_startup)

        for node in (validator2, validator3):
            client.send_block(sibling, node)
            _wait_recorded(node, sibling.blockHash, "IgnorableEquivocation", timeouts)

        v2_later_hash = _propose(validator2, KEY_2)
        wait_for_block_visible(validator3, v2_later_hash, timeouts.node_startup)

        v2_later = _fetch(client, v2_later_hash, validator2)
        forged = BlockMessage()
        forged.CopyFrom(v2_later)
        forged.body.systemDeploys.append(_forged_slash(sibling.blockHash, KEY_2))
        forged.header.timestamp = v2_later.header.timestamp + 1
        forged_hash = _seal(forged, KEY_2)
        client.send_block(forged, validator3)
        _wait_recorded(validator3, forged_hash, "UnauthorizedSlashDeploy", timeouts)

        _assert_rejected_without_slash(shard, client, validator3, KEY_3, timeouts)


@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_self_correcting_block_admitted(provider, timeouts) -> None:
    """v1 equivocates to v2 and v3. v2's next block cites the equivocation and
    carries its slash; v3 admits that block rather than rejecting it as
    neglect, so v1 is slashed and v2 keeps its stake."""
    with _slash_shard(provider, timeouts, three_validators=True) as (
        shard,
        validator1,
        validator2,
        client,
    ):
        validator3 = shard.node("validator3")
        b1_hash = _propose(validator1, KEY_1)
        wait_for_block_visible(validator2, b1_hash, timeouts.node_startup)
        wait_for_block_visible(validator3, b1_hash, timeouts.node_startup)
        sibling = _sibling(_fetch(client, b1_hash, validator1), KEY_1)
        for node in (validator2, validator3):
            client.send_block(sibling, node)
            _wait_recorded(node, sibling.blockHash, "IgnorableEquivocation", timeouts)

        slashing_hash = _propose(validator2, KEY_2)
        assert is_exist_slash_deploy(_fetch(client, slashing_hash, validator2))
        wait_for_block_visible(validator3, slashing_hash, timeouts.node_startup)
        assert f"Recording invalid block {slashing_hash[:10]}" not in validator3.logs()
        assert _stake(shard, slashing_hash, KEY_1, timeouts) == 0
        assert _stake(shard, slashing_hash, KEY_2, timeouts) == STAKE
        assert _stake(shard, slashing_hash, KEY_3, timeouts) == STAKE


def test_no_false_positive_slash_on_propose_imbalance(provider, timeouts) -> None:
    """v1 proposes five blocks in a row while v2 stays silent, then v3
    proposes once. Nobody is slashed: neither dominating the chain nor
    staying silent is an offence."""
    with _slash_shard(provider, timeouts, three_validators=True) as (
        shard,
        validator1,
        validator2,
        client,
    ):
        validator3 = shard.node("validator3")
        for _ in range(5):
            v1_hash = _propose(validator1, KEY_1)
            wait_for_block_visible(validator2, v1_hash, timeouts.node_startup)
            wait_for_block_visible(validator3, v1_hash, timeouts.node_startup)

        v3_hash = _propose(validator3, KEY_3)
        for key in (KEY_1, KEY_2, KEY_3):
            assert _stake(shard, v3_hash, key, timeouts) == STAKE
