"""How a live shard judges and punishes misbehaving validators.

Each test runs a shard as it runs in production: heartbeat on, the shard's
fault-tolerance threshold, four validators of distinct stake and a read-only
observer. Misbehaviour is injected over the P2P transport as blocks signed
with a validator's key, built from blocks that validator really produced.

Only equivocation is slashable. Every other offence is judged against the
receiving node's own state, so the block is rejected and its sender keeps its
stake. Each test checks the outcome the way the network must agree on it:
every receiving node reaches the verdict, a slash finalizes on every node,
finalized stake and the committee are identical everywhere, and the chain
keeps finalizing.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

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

from ...infra.assertions import (
    assert_block_finalized_on_all_nodes,
    assert_chain_advances,
    assert_deploy_block_finalized_on_all_nodes,
    collect_forensics,
    common_finalized_anchor,
)
from ...infra.config import ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID, VALIDATOR4_ID
from ...infra.node import Node
from ...infra.p2p_client import (
    NodeClient,
    generate_block_hash,
    p2p_protocol_client,
    slash_evidence,
)
from ...infra.polling import poll_until, wait_for_block_visible, wait_for_log_match
from ...infra.providers.docker import DockerProvider
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

_BONDS = [(VALIDATOR1_ID, 100), (VALIDATOR2_ID, 200), (VALIDATOR3_ID, 300), (VALIDATOR4_ID, 400)]
_GENESIS_BONDS = {identity.public_hex: stake for identity, stake in _BONDS}
# The default genesis wallets fund validators 1-3 only; every validator here
# signs its own deploys.
_EXTRA_WALLETS = [
    (VALIDATOR4_ID.private_key().get_public_key().get_vault_address(), 50_000_000_000_000_000)
]

_HELLO = "new x in { x!(1) }"

# Blocks an equivocation needs from its sibling to a finalized slash: the
# equivocator's pause, delivery, and the next honest proposals.
_SLASH_ROOM = 20


@contextmanager
def _live_shard(
    provider, timeouts, label: str, attackers: Sequence[str] = ()
) -> Iterator[Tuple[Shard, NodeClient]]:
    """A production-configured shard and a P2P client; a failure carries the
    shard's state at that moment.

    An attacker validator (named by role) proposes only when the test tells
    it to, so a block it is about to forge from is still its latest.
    """
    if isinstance(provider, DockerProvider) and sys.platform != "linux":
        pytest.skip("Docker containers reach the test process only with native Linux networking")
    config = ShardConfig(
        bonds=_BONDS,
        heartbeat=True,
        include_readonly=True,
        extra_wallets=_EXTRA_WALLETS,
        per_node_cli_options={role: {"--heartbeat-disabled": ""} for role in attackers},
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        with p2p_protocol_client(shard.boot.p2p_reply_host, float(timeouts.command)) as client:
            try:
                yield shard, client
            except Exception as exc:
                try:
                    forensics = collect_forensics(shard.all_nodes, label=label)
                except Exception as probe_exc:  # noqa: BLE001 — never mask the failure
                    forensics = f"(forensics unavailable: {type(probe_exc).__name__}: {probe_exc})"
                raise AssertionError(f"{exc}\n{forensics}") from exc
    finally:
        shard.destroy()


# ── Blocks ─────────────────────────────────────────────────────────────────


def _fetch(client: NodeClient, block_hash: str, node: Node) -> BlockMessage:
    block = client.block_request(block_hash, node)
    assert gen_block_hash_from_block(block) == block.blockHash, (
        "pyf1r3fly's block hash diverged from the node's"
    )
    return block


def _attacker_block(client: NodeClient, node: Node, identity, nodes: Sequence[Node], timeouts):
    """A block the attacker ``identity`` proposes with a fresh deploy, finalized
    on every node in ``nodes``: its latest block until it proposes again."""
    node.deploy_string(_HELLO, identity.private_key())
    block_hash = node.propose()
    for peer in nodes:
        wait_for_block_visible(peer, block_hash, timeouts.finalization)
    assert_block_finalized_on_all_nodes(nodes, block_hash, timeout=timeouts.finalization * 5)
    return _fetch(client, block_hash, node)


def _silence(client: NodeClient, node: Node, identity, ro: Node, running, timeouts):
    """Pause ``identity``'s node and return its latest block once every running
    node holds it."""
    node.pause()
    time.sleep(timeouts.blocks(2))

    def _latest():
        own = [b for b in ro.get_blocks(10) if b.sender == identity.public_hex]
        return max(own, key=lambda b: b.seqNum) if own else None

    latest = poll_until(
        predicate=_latest,
        timeout=timeouts.finalization,
        interval=timeouts.poll_interval,
        description=f"{identity.name}'s latest block on the observer",
    )
    for peer in running:
        wait_for_block_visible(peer, latest.blockHash, timeouts.finalization)
    return _fetch(client, latest.blockHash, ro)


def _copy(block: BlockMessage) -> BlockMessage:
    forged = BlockMessage()
    forged.CopyFrom(block)
    return forged


def _seal(block: BlockMessage, identity) -> bytes:
    """Re-hash ``block`` and sign it as ``identity``; returns the new hash."""
    block_hash = gen_block_hash_from_block(block)
    block.blockHash = block_hash
    block.sig = identity.private_key().sign_block_hash(block_hash)
    return block_hash


def _slash_deploy(evidence: bytes, issuer, epoch: int) -> ProcessedSystemDeployProto:
    return ProcessedSystemDeployProto(
        systemDeploy=SystemDeployDataProto(
            slashSystemDeploy=SlashSystemDeployDataProto(
                invalidBlockHash=evidence,
                issuerPublicKey=bytes.fromhex(issuer.public_hex),
                targetActivationEpoch=epoch,
            )
        ),
    )


def _await_epoch_room(ro: Node, epoch_length: int, room: int, timeouts) -> None:
    """Wait until the tip is at least ``room`` blocks before the next epoch
    boundary: a slash is authorized only in its evidence's epoch."""
    assert room < epoch_length, f"an epoch of {epoch_length} blocks never has {room} to spare"
    poll_until(
        predicate=lambda: (
            True if ro.get_blocks(1)[0].blockNumber % epoch_length <= epoch_length - room else None
        ),
        timeout=timeouts.blocks(epoch_length),
        interval=timeouts.poll_interval,
        description=f"{room} blocks left in the current epoch",
    )


def _equivocate(client: NodeClient, node: Node, identity, ro: Node, running, timeouts):
    """Silence ``identity``'s node after its latest block and return a sibling
    of that block it signed: an equivocator that signs twice and goes quiet."""
    sibling = _copy(_silence(client, node, identity, ro, running, timeouts))
    sibling.extraBytes = sibling.extraBytes + b"\x00"
    _seal(sibling, identity)
    return sibling


# ── Verdicts ───────────────────────────────────────────────────────────────


def _recorded(block_hash: bytes, verdict: str) -> re.Pattern:
    return re.compile(rf"Recording invalid block {block_hash.hex()[:10]}\.\.\. for (?:{verdict})\.")


def _hash_ignored(block_hash: bytes) -> re.Pattern:
    return re.compile(rf"Ignoring block {block_hash.hex()[:10]}\.\.\. because block hash")


def _parked(block_hash: bytes) -> re.Pattern:
    return re.compile(rf"Block {block_hash.hex()[:10]}\.\.\. delayed: missing dependencies\.")


def _inject(client: NodeClient, block: BlockMessage, receivers, verdict: re.Pattern, timeouts):
    """Send ``block`` to every receiver and wait for each to reach ``verdict``."""
    for node in receivers:
        client.send_block(block, node)
    for node in receivers:
        wait_for_log_match(node, verdict, timeouts.command)


def _assert_never_finalized(nodes: Sequence[Node], block_hash: bytes) -> None:
    for node in nodes:
        try:
            info = node.get_block(block_hash.hex()).blockInfo
        except F1r3flyClientException:
            continue
        assert not info.isFinalized, f"{node.name} finalized forged block {block_hash.hex()[:10]}"


# ── Finalized state ────────────────────────────────────────────────────────


def _finalized_slashes(client: NodeClient, ro: Node, since: int) -> Dict[bytes, List[str]]:
    """Every slash in a block finalized above height ``since``, by the evidence
    it cites."""
    lfb = ro.last_finalized_block().blockInfo.blockNumber
    slashes: Dict[bytes, List[str]] = {}
    for info in ro.get_blocks(lfb - since + 20):
        if info.blockNumber <= since or not ro.is_finalized(info.blockHash):
            continue
        for evidence in slash_evidence(_fetch(client, info.blockHash, ro)):
            slashes.setdefault(evidence, []).append(info.blockHash)
    return slashes


def _await_finalized_stake(ro: Node, expected: Dict[str, int], timeouts, label: str) -> None:
    poll_until(
        predicate=lambda: True if ro.pos.get_bonds() == expected else None,
        timeout=timeouts.finalization * 5,
        interval=timeouts.poll_interval,
        description=f"finalized stake {label}",
    )


def _assert_committee_agreed(nodes: Sequence[Node], expected: Dict[str, int], timeouts) -> None:
    """The committee recorded in a block every node finalized converges to
    ``expected``, identically on every node."""

    def _agreed():
        anchor = common_finalized_anchor(nodes, timeouts.finalization)
        views = {
            node.name: {
                bond.validator: bond.stake
                for bond in node.get_block(anchor).blockInfo.bonds
                if bond.stake > 0
            }
            for node in nodes
        }
        assert len({tuple(sorted(view.items())) for view in views.values()}) == 1, (
            f"nodes disagree on the committee of finalized block {anchor[:10]}: {views}"
        )
        return True if next(iter(views.values())) == expected else None

    poll_until(
        predicate=_agreed,
        timeout=timeouts.finalization * 5,
        interval=timeouts.poll_interval,
        description=f"committee {expected} on every node",
    )


def _assert_validators_live(
    validators, nodes: Sequence[Node], timeouts, attackers: Sequence[Node] = ()
) -> None:
    """Every listed validator still gets a block finalized on every node; an
    attacker is told to propose."""
    for node, identity in validators:
        deploy_id = node.deploy_string(_HELLO, identity.private_key())
        if node in attackers:
            node.propose()
        assert_deploy_block_finalized_on_all_nodes(
            node, deploy_id, nodes, timeouts.finalization * 5
        )


def _assert_slashed_once_everywhere(
    client: NodeClient,
    ro: Node,
    nodes: Sequence[Node],
    offender,
    evidence: bytes,
    expected_stake: Dict[str, int],
    since: int,
    timeouts,
) -> None:
    """The offender's slash finalizes on every node and takes effect once;
    finalized stake and the committee then agree everywhere.

    Honest proposers that each see the evidence first slash in parallel
    blocks, and the merge keeps one; no block may slash again on top of a
    block that already did."""
    blocks = poll_until(
        predicate=lambda: _finalized_slashes(client, ro, since).get(evidence),
        timeout=timeouts.finalization * 5,
        interval=timeouts.poll_interval,
        description=f"a finalized block slashing {offender.name}",
    )
    for block_hash in blocks:
        assert_block_finalized_on_all_nodes(nodes, block_hash, timeout=timeouts.finalization * 5)
    _await_finalized_stake(ro, expected_stake, timeouts, f"after slashing {offender.name}")
    _assert_committee_agreed(nodes, {k: v for k, v in expected_stake.items() if v > 0}, timeouts)
    settled = assert_chain_advances(
        nodes,
        ro.last_finalized_block().blockInfo.blockNumber + 10,
        timeouts.blocks(30),
        label=f"after slashing {offender.name}",
    )
    slashing_blocks = _finalized_slashes(client, ro, since).get(evidence, [])
    for block_hash in slashing_blocks:
        repeated = _slashing_ancestor(client, ro, block_hash, set(slashing_blocks) - {block_hash})
        assert repeated is None, (
            f"{offender.name} was slashed again in {block_hash[:10]} on top of "
            f"{repeated[:10]}, finalized up to #{settled}"
        )


def _slashing_ancestor(
    client: NodeClient, ro: Node, block_hash: str, slashing: Set[str]
) -> Optional[str]:
    """A block of ``slashing`` among ``block_hash``'s ancestors, walking no
    lower than the lowest of them."""
    if not slashing:
        return None
    floor = min(_fetch(client, other, ro).body.state.blockNumber for other in slashing)
    seen = set()
    frontier = [block_hash]
    while frontier:
        for parent in _fetch(client, frontier.pop(), ro).header.parentsHashList:
            parent_hash = parent.hex()
            if parent_hash in slashing:
                return parent_hash
            if parent_hash in seen:
                continue
            seen.add(parent_hash)
            if _fetch(client, parent_hash, ro).body.state.blockNumber > floor:
                frontier.append(parent_hash)
    return None


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.timeout(2400)
@pytest.mark.allow_forbidden_patterns(
    "RecordingInvalidBlock",
    "InvalidBondsCache",
    "BondsCacheMismatch",
    "InvalidRepeatDeploy",
    "InvalidTransaction",
    "ComputedPreStateMismatch",
    "ConsumeFailedReplayDivergence",
    "UnableToConsumeSystemDeploy",
    "ObserverReporterUnusedCOMM",
)
def test_rejected_offences_do_not_punish(provider, timeouts) -> None:
    """Every offence that is not an equivocation, sent to every honest node
    while the shard runs: each node reaches the same verdict, no forged block
    finalizes, no slash finalizes, and stake and the committee stay as they
    were on every node."""
    with _live_shard(
        provider, timeouts, "rejected-offences", attackers=("validator1", "validator2")
    ) as (shard, client):
        v1, v2 = shard.node("validator1"), shard.node("validator2")
        v3, v4 = shard.node("validator3"), shard.node("validator4")
        ro, nodes = shard.readonly, shard.all_nodes
        start = ro.last_finalized_block().blockInfo.blockNumber
        epoch_length = ro.pos.get_epoch_length()

        t1 = _attacker_block(client, v1, VALIDATOR1_ID, nodes, timeouts)
        t2 = _attacker_block(client, v2, VALIDATOR2_ID, nodes, timeouts)
        v1_judges, v2_judges = [v2, v3, v4, ro], [v1, v3, v4, ro]
        forged_hashes: List[bytes] = []

        def offence(name: str, block: BlockMessage, sender, judges, verdict) -> bytes:
            block_hash = _seal(block, sender)
            logging.info("offence %s: %s", name, block_hash.hex()[:10])
            _inject(client, block, judges, verdict(block_hash), timeouts)
            forged_hashes.append(block_hash)
            return block_hash

        wrong_hash = _copy(t1)
        wrong_hash.header.timestamp = t1.header.timestamp + 1
        evil = generate_block_hash()
        wrong_hash.blockHash = evil
        wrong_hash.sig = VALIDATOR1_ID.private_key().sign_block_hash(evil)
        _inject(client, wrong_hash, v1_judges, _hash_ignored(evil), timeouts)
        forged_hashes.append(evil)

        wrong_number = _copy(t1)
        wrong_number.body.state.blockNumber = t1.body.state.blockNumber + 7
        wrong_number.header.timestamp = t1.header.timestamp + 1
        rejected = offence(
            "block number",
            wrong_number,
            VALIDATOR1_ID,
            v1_judges,
            lambda h: _recorded(h, "InvalidBlockNumber"),
        )

        wrong_seq = _copy(t1)
        wrong_seq.seqNum = t1.seqNum + 1000
        wrong_seq.header.timestamp = t1.header.timestamp + 1
        offence(
            "sequence number",
            wrong_seq,
            VALIDATOR1_ID,
            v1_judges,
            lambda h: _recorded(h, "InvalidSequenceNumber"),
        )

        unbonded_justification = _copy(t1)
        unbonded_justification.justifications.append(
            Justification(validator=PrivateKey.generate().to_bytes(), latestBlockHash=t1.blockHash)
        )
        unbonded_justification.header.timestamp = t1.header.timestamp + 1
        offence(
            "justification from an unbonded key",
            unbonded_justification,
            VALIDATOR1_ID,
            v1_judges,
            lambda h: _recorded(h, "InvalidFollows"),
        )

        halved_bond = _copy(t1)
        for bond in halved_bond.body.state.bonds:
            if bond.validator.hex() == VALIDATOR1_ID.public_hex:
                bond.stake //= 2
        offence(
            "bonds field",
            halved_bond,
            VALIDATOR1_ID,
            v1_judges,
            lambda h: _recorded(h, "InvalidBondsCache"),
        )

        repeated_deploy = _copy(t1)
        repeated_deploy.seqNum = t1.seqNum + 1
        repeated_deploy.body.state.blockNumber = t1.body.state.blockNumber + 1
        repeated_deploy.header.ClearField("parentsHashList")
        repeated_deploy.header.parentsHashList.append(t1.blockHash)
        repeated_deploy.header.timestamp = int(time.time() * 1000)
        offence(
            "repeated deploy",
            repeated_deploy,
            VALIDATOR1_ID,
            v1_judges,
            lambda h: _recorded(h, "InvalidRepeatDeploy"),
        )

        grandparent = _fetch(
            client,
            _fetch(client, t1.header.parentsHashList[0].hex(), ro).header.parentsHashList[0].hex(),
            ro,
        )
        skipped_parents = _copy(t1)
        skipped_parents.header.ClearField("parentsHashList")
        skipped_parents.header.parentsHashList.append(grandparent.blockHash)
        skipped_parents.body.state.blockNumber = grandparent.body.state.blockNumber + 1
        skipped_parents.header.timestamp = int(time.time() * 1000)
        skipped_parents.body.deploys[0].deploy.CopyFrom(
            create_deploy_data(VALIDATOR1_ID.private_key(), _HELLO, 998, 1000000, shard_id="root")
        )
        offence(
            "parents skipping the fork-choice winner",
            skipped_parents,
            VALIDATOR1_ID,
            v1_judges,
            lambda h: _recorded(h, "InvalidTransaction"),
        )

        t2_epoch = t2.body.state.blockNumber // epoch_length
        fabricated = _copy(t2)
        fabricated.body.systemDeploys.append(_slash_deploy(b"\xde" * 32, VALIDATOR2_ID, t2_epoch))
        fabricated.header.timestamp = t2.header.timestamp + 1
        offence(
            "slash on fabricated evidence",
            fabricated,
            VALIDATOR2_ID,
            v2_judges,
            lambda h: _recorded(h, "UnauthorizedSlashDeploy"),
        )

        framing = _copy(t2)
        framing.body.systemDeploys.append(_slash_deploy(t1.blockHash, VALIDATOR2_ID, t2_epoch))
        framing.header.timestamp = t2.header.timestamp + 1
        offence(
            "slash citing a valid block",
            framing,
            VALIDATOR2_ID,
            v2_judges,
            lambda h: _recorded(h, "UnauthorizedSlashDeploy"),
        )

        builds_on_rejected = _copy(t2)
        for justification in builds_on_rejected.justifications:
            if justification.validator.hex() == VALIDATOR1_ID.public_hex:
                justification.latestBlockHash = rejected
        builds_on_rejected.header.timestamp = t2.header.timestamp + 1
        offence(
            "building on a rejected block", builds_on_rejected, VALIDATOR2_ID, v2_judges, _parked
        )

        # v1 moves on, so a block reusing t1's justifications now regresses them.
        _attacker_block(client, v1, VALIDATOR1_ID, nodes, timeouts)
        regressed = _copy(t1)
        regressed.body.deploys[0].deploy.CopyFrom(
            create_deploy_data(VALIDATOR1_ID.private_key(), _HELLO, 999, 1000000, shard_id="root")
        )
        regressed.extraBytes = t1.extraBytes + b"\x00"
        offence(
            "justification regression",
            regressed,
            VALIDATOR1_ID,
            v1_judges,
            lambda h: _recorded(h, "JustificationRegression"),
        )

        assert_chain_advances(
            nodes,
            ro.last_finalized_block().blockInfo.blockNumber + 5,
            timeouts.blocks(30),
            label="after the offences",
        )
        for block_hash in forged_hashes:
            _assert_never_finalized(nodes, block_hash)
        assert _finalized_slashes(client, ro, start) == {}
        _await_finalized_stake(ro, _GENESIS_BONDS, timeouts, "unchanged")
        _assert_committee_agreed(nodes, _GENESIS_BONDS, timeouts)
        _assert_validators_live(
            [(v1, VALIDATOR1_ID), (v2, VALIDATOR2_ID), (v3, VALIDATOR3_ID), (v4, VALIDATOR4_ID)],
            nodes,
            timeouts,
            attackers=[v1, v2],
        )


@pytest.mark.timeout(2400)
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_equivocation_is_slashed_network_wide(provider, timeouts) -> None:
    """Equivocators are slashed once, on every node, whichever node first
    holds the evidence.

    A slash is authorized only in its evidence's epoch, so each equivocation
    starts with room left in the shard's epoch.

    1. v1 signs a sibling of its latest block and goes quiet; only v2 receives
       the sibling.
    2. v2 does the same; v3 first receives a block built on the sibling, asks
       for the sibling as a dependency, then receives it.
    3. A later epoch's block re-citing v1's evidence is rejected.

    After each slash: it finalizes on every running node, once; finalized
    stake and the committee agree everywhere; every honest validator still
    gets blocks finalized.
    """
    with _live_shard(provider, timeouts, "equivocation") as (shard, client):
        v1, v2 = shard.node("validator1"), shard.node("validator2")
        v3, v4 = shard.node("validator3"), shard.node("validator4")
        ro = shard.readonly
        start = ro.last_finalized_block().blockInfo.blockNumber
        epoch_length = ro.pos.get_epoch_length()
        try:
            running = [v2, v3, v4, ro, shard.boot]
            _await_epoch_room(ro, epoch_length, _SLASH_ROOM, timeouts)
            sibling1 = _equivocate(client, v1, VALIDATOR1_ID, ro, running, timeouts)
            _inject(
                client,
                sibling1,
                [v2],
                _recorded(sibling1.blockHash, "IgnorableEquivocation"),
                timeouts,
            )
            stake = dict(_GENESIS_BONDS, **{VALIDATOR1_ID.public_hex: 0})
            _assert_slashed_once_everywhere(
                client, ro, running, VALIDATOR1_ID, sibling1.blockHash, stake, start, timeouts
            )
            _assert_validators_live(
                [(v2, VALIDATOR2_ID), (v3, VALIDATOR3_ID), (v4, VALIDATOR4_ID)], running, timeouts
            )

            running = [v3, v4, ro, shard.boot]
            _await_epoch_room(ro, epoch_length, _SLASH_ROOM, timeouts)
            sibling2 = _equivocate(client, v2, VALIDATOR2_ID, ro, running, timeouts)
            child = _copy(sibling2)
            child.seqNum = sibling2.seqNum + 1
            child.body.state.blockNumber = sibling2.body.state.blockNumber + 1
            child.header.ClearField("parentsHashList")
            child.header.parentsHashList.append(sibling2.blockHash)
            for justification in child.justifications:
                if justification.validator.hex() == VALIDATOR2_ID.public_hex:
                    justification.latestBlockHash = sibling2.blockHash
            child.header.timestamp = sibling2.header.timestamp + 1
            _seal(child, VALIDATOR2_ID)
            client.send_block(child, v3)
            wait_for_log_match(
                v3,
                re.compile(
                    rf"Adding {sibling2.blockHash.hex()[:10]}.*hash to RequestedBlocks "
                    rf"because of MissingDependencyRequested"
                ),
                timeouts.command,
            )
            _inject(
                client,
                sibling2,
                [v3],
                _recorded(sibling2.blockHash, "AdmissibleEquivocation"),
                timeouts,
            )
            stake[VALIDATOR2_ID.public_hex] = 0
            _assert_slashed_once_everywhere(
                client, ro, running, VALIDATOR2_ID, sibling2.blockHash, stake, start, timeouts
            )
            _assert_validators_live([(v3, VALIDATOR3_ID), (v4, VALIDATOR4_ID)], running, timeouts)

            evidence_epoch = sibling1.body.state.blockNumber // epoch_length
            poll_until(
                predicate=lambda: (
                    True
                    if ro.last_finalized_block().blockInfo.blockNumber // epoch_length
                    > evidence_epoch
                    else None
                ),
                timeout=timeouts.blocks(2 * epoch_length),
                interval=timeouts.poll_interval,
                description=f"the finalized chain past epoch {evidence_epoch}",
            )
            t3 = _silence(client, v3, VALIDATOR3_ID, ro, [v4, ro, shard.boot], timeouts)
            replay = _copy(t3)
            replay.body.systemDeploys.append(
                _slash_deploy(sibling1.blockHash, VALIDATOR3_ID, evidence_epoch)
            )
            replay.header.timestamp = t3.header.timestamp + 1
            replay_hash = _seal(replay, VALIDATOR3_ID)
            _inject(
                client,
                replay,
                [v4, ro],
                _recorded(replay_hash, "UnauthorizedSlashDeploy"),
                timeouts,
            )
            v3.unpause()
            assert_chain_advances(
                running,
                ro.last_finalized_block().blockInfo.blockNumber + 5,
                timeouts.blocks(30),
                label="after the stale slash",
            )
            _assert_never_finalized(running, replay_hash)
            _await_finalized_stake(ro, stake, timeouts, "unchanged by the stale slash")
            _assert_validators_live([(v3, VALIDATOR3_ID), (v4, VALIDATOR4_ID)], running, timeouts)
        finally:
            v1.unpause()
            v2.unpause()
            v3.unpause()


@pytest.mark.timeout(2400)
def test_uneven_proposing_is_not_punished(provider, timeouts) -> None:
    """v2 stays silent for 40 blocks while the others propose, then returns.
    Nobody is slashed, stake and the committee are unchanged on every node,
    and v2 gets blocks finalized again."""
    with _live_shard(provider, timeouts, "uneven-proposing") as (shard, client):
        v1, v2 = shard.node("validator1"), shard.node("validator2")
        v3, v4 = shard.node("validator3"), shard.node("validator4")
        ro, nodes = shard.readonly, shard.all_nodes
        start = ro.last_finalized_block().blockInfo.blockNumber
        running = [v1, v3, v4, ro, shard.boot]
        v2.pause()
        try:
            assert_chain_advances(running, start + 40, timeouts.blocks(60), label="v2 silent")
        finally:
            v2.unpause()
        _assert_validators_live([(v2, VALIDATOR2_ID)], nodes, timeouts)
        assert _finalized_slashes(client, ro, start) == {}
        _await_finalized_stake(ro, _GENESIS_BONDS, timeouts, "unchanged")
        _assert_committee_agreed(nodes, _GENESIS_BONDS, timeouts)
