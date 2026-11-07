"""How a live shard judges and punishes misbehaving validators.

Each test runs a shard as it runs in production: heartbeat on, the shard's
fault-tolerance threshold, four validators of distinct stake and a read-only
observer. Misbehaviour is injected over the P2P transport as blocks signed
with a validator's key, built from blocks that validator really produced.

Only equivocation is slashable: two blocks one key signed at one sequence
number. Every other offence is judged against the receiving node's own state,
so the block is rejected and its sender keeps its stake. Each test checks the
outcome the way the network must agree on it: every receiving node reaches
the verdict, a slash finalizes on every node, finalized stake and the
committee are identical everywhere, and the chain keeps finalizing.
"""

from __future__ import annotations

import logging
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

import pytest
from f1r3fly.client import F1r3flyClientException
from f1r3fly.crypto import PrivateKey, block_commitment, commitment_hash, gen_block_hash_from_block
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

Evidence = Tuple[bytes, bytes]


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


def _silence(client: NodeClient, node: Node, identity, ro: Node, running, timeouts, min_seq=0):
    """Pause ``identity``'s node once it has a block at ``min_seq`` or above,
    and return its latest block once every running node holds it."""

    def _latest():
        own = [b for b in ro.get_blocks(10) if b.sender == identity.public_hex]
        return max(own, key=lambda b: b.seqNum) if own else None

    poll_until(
        predicate=lambda: (lambda b: b if b and b.seqNum >= min_seq else None)(_latest()),
        timeout=timeouts.finalization * 5,
        interval=timeouts.poll_interval,
        description=f"{identity.name} at sequence number {min_seq}",
    )
    node.pause()
    time.sleep(timeouts.blocks(2))
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


def _own_justification(block: BlockMessage) -> Justification:
    return next(j for j in block.justifications if j.validator == block.sender)


def _sort_justifications(block: BlockMessage) -> None:
    ordered = sorted(block.justifications, key=lambda j: j.validator)
    del block.justifications[:]
    block.justifications.extend(ordered)


_nonce = iter(range(1_000, 1_000_000))


def _next_block(latest: BlockMessage, sender) -> BlockMessage:
    """``sender``'s next block after ``latest``, its latest block: one sequence
    number on, built on ``latest`` alone, with a deploy no block carries. Once
    sent, the sender cannot sign another block at that sequence number without
    equivocating."""
    block = _copy(latest)
    block.seqNum = latest.seqNum + 1
    block.body.state.blockNumber = latest.body.state.blockNumber + 1
    block.header.ClearField("parentsHashList")
    block.header.parentsHashList.append(latest.blockHash)
    _own_justification(block).latestBlockHash = latest.blockHash
    block.header.timestamp = int(time.time() * 1000)
    del block.body.deploys[:]
    deploy = block.body.deploys.add()
    deploy.deploy.CopyFrom(
        create_deploy_data(sender.private_key(), _HELLO, next(_nonce), 1000000, shard_id="root")
    )
    deploy.cost.SetInParent()
    return block


def _seal(block: BlockMessage, identity) -> bytes:
    """Re-hash ``block`` and sign it as ``identity``; returns the new hash."""
    block_hash = gen_block_hash_from_block(block)
    block.blockHash = block_hash
    block.sig = identity.private_key().sign_block_hash(block_hash)
    return block_hash


def _evidence(a: BlockMessage, b: BlockMessage) -> Evidence:
    first, second = sorted((a.blockHash, b.blockHash))
    return first, second


def _slash_deploy(a: BlockMessage, b: BlockMessage, issuer) -> ProcessedSystemDeployProto:
    """A slash whose evidence is the commitments of ``a`` and ``b``, ordered by
    the hash each commits to."""
    first, second = sorted((block_commitment(a), block_commitment(b)), key=commitment_hash)
    return ProcessedSystemDeployProto(
        systemDeploy=SystemDeployDataProto(
            slashSystemDeploy=SlashSystemDeployDataProto(
                issuerPublicKey=bytes.fromhex(issuer.public_hex),
                first=first,
                second=second,
            )
        ),
    )


def _equivocate(client: NodeClient, node: Node, identity, ro: Node, running, timeouts):
    """Silence ``identity``'s node after its latest block and return that block
    and a sibling of it the validator signed: an equivocator that signs twice
    and goes quiet."""
    latest = _silence(client, node, identity, ro, running, timeouts)
    sibling = _copy(latest)
    sibling.extraBytes = sibling.extraBytes + b"\x00"
    _seal(sibling, identity)
    return latest, sibling


# ── Verdicts ───────────────────────────────────────────────────────────────


def _recorded(block_hash: bytes, verdict: str) -> re.Pattern:
    return re.compile(
        rf"Recording invalid block {block_hash.hex()[:10]}\.\.\. for (?:{verdict})[.,]"
    )


_HASH_MISMATCHES = re.compile(
    r'^block_integrity_rejected(?:_total)?\{[^}]*reason="hash-mismatch"[^}]*\}\s+(\S+)$',
    re.MULTILINE,
)


def _hash_mismatches(node: Node) -> float:
    """Blocks ``node`` refused on receipt because their hash did not match
    their content. The node records no log line for these."""
    return sum(
        float(count)
        for count in _HASH_MISMATCHES.findall(node.http_get("/metrics", timeout=10).text)
    )


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


def _finalized_slashes(client: NodeClient, ro: Node, since: int) -> Dict[Evidence, List[str]]:
    """Every slash in a block finalized above height ``since``, by the pair of
    blocks its evidence names."""
    lfb = ro.last_finalized_block().blockInfo.blockNumber
    slashes: Dict[Evidence, List[str]] = {}
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


def _assert_validators_live(validators, nodes: Sequence[Node], timeouts) -> None:
    """Every listed validator still gets a block finalized on every node."""
    for node, identity in validators:
        deploy_id = node.deploy_string(_HELLO, identity.private_key())
        assert_deploy_block_finalized_on_all_nodes(
            node, deploy_id, nodes, timeouts.finalization * 5
        )


def _assert_slashed_once_everywhere(
    client: NodeClient,
    ro: Node,
    nodes: Sequence[Node],
    offender,
    evidence: Evidence,
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


# ── Offences that are not equivocations ────────────────────────────────────


@dataclass
class _Attack:
    """One shard's attack: v1 and v2 each send one offence as their next
    block, then stay silent, since any further block at that sequence number
    would be an equivocation."""

    client: NodeClient
    ro: Node
    t1: BlockMessage
    t2: BlockMessage
    v1_judges: List[Node]
    v2_judges: List[Node]
    timeouts: object
    forged: List[bytes] = field(default_factory=list)
    rejected_v1: Optional[bytes] = None

    def offence(self, name: str, block: BlockMessage, sender, verdict) -> bytes:
        block_hash = _seal(block, sender)
        judges = self.v1_judges if sender is VALIDATOR1_ID else self.v2_judges
        logging.info("offence %s: %s", name, block_hash.hex()[:10])
        _inject(self.client, block, judges, verdict(block_hash), self.timeouts)
        self.forged.append(block_hash)
        return block_hash


def _block_number(attack: _Attack) -> None:
    block = _next_block(attack.t1, VALIDATOR1_ID)
    block.body.state.blockNumber += 7
    attack.rejected_v1 = attack.offence(
        "block number", block, VALIDATOR1_ID, lambda h: _recorded(h, "InvalidBlockNumber")
    )


def _hash_and_sequence(attack: _Attack) -> None:
    """Offences that occupy no sequence number v1 will reach."""
    wrong_hash = _copy(attack.t1)
    wrong_hash.header.timestamp = attack.t1.header.timestamp + 1
    evil = generate_block_hash()
    wrong_hash.blockHash = evil
    wrong_hash.sig = VALIDATOR1_ID.private_key().sign_block_hash(evil)
    refused = {node.name: _hash_mismatches(node) for node in attack.v1_judges}
    for node in attack.v1_judges:
        attack.client.send_block(wrong_hash, node)
    for node in attack.v1_judges:
        poll_until(
            predicate=lambda: True if _hash_mismatches(node) > refused[node.name] else None,
            timeout=attack.timeouts.command,
            interval=attack.timeouts.poll_interval,
            description=f"{node.name} refusing a block whose hash does not match",
        )
    attack.forged.append(evil)

    wrong_seq = _copy(attack.t1)
    wrong_seq.seqNum = attack.t1.seqNum + 1000
    wrong_seq.header.timestamp = attack.t1.header.timestamp + 1
    attack.offence(
        "sequence number",
        wrong_seq,
        VALIDATOR1_ID,
        lambda h: _recorded(h, "InvalidSequenceNumber"),
    )


def _builds_on_rejected(attack: _Attack) -> None:
    block = _next_block(attack.t2, VALIDATOR2_ID)
    for justification in block.justifications:
        if justification.validator.hex() == VALIDATOR1_ID.public_hex:
            justification.latestBlockHash = attack.rejected_v1
    attack.offence("building on a rejected block", block, VALIDATOR2_ID, _parked)


def _unbonded_justification(attack: _Attack) -> None:
    block = _next_block(attack.t1, VALIDATOR1_ID)
    block.justifications.append(
        Justification(
            validator=PrivateKey.generate().get_public_key().to_bytes(),
            latestBlockHash=attack.t1.blockHash,
        )
    )
    _sort_justifications(block)
    attack.offence(
        "justification from an unbonded key",
        block,
        VALIDATOR1_ID,
        lambda h: _recorded(h, "InvalidFollows"),
    )


def _badly_signed_evidence(attack: _Attack) -> None:
    unsigned = _copy(attack.t1)
    unsigned.extraBytes = unsigned.extraBytes + b"\x00"
    unsigned.blockHash = gen_block_hash_from_block(unsigned)
    block = _next_block(attack.t2, VALIDATOR2_ID)
    block.body.systemDeploys.append(_slash_deploy(attack.t1, unsigned, VALIDATOR2_ID))
    attack.offence(
        "slash whose second block v1 never signed",
        block,
        VALIDATOR2_ID,
        lambda h: _recorded(h, "UnauthorizedSlashDeploy"),
    )


def _bonds_field(attack: _Attack) -> None:
    block = _next_block(attack.t1, VALIDATOR1_ID)
    for bond in block.body.state.bonds:
        if bond.validator.hex() == VALIDATOR1_ID.public_hex:
            bond.stake //= 2
    attack.offence("bonds field", block, VALIDATOR1_ID, lambda h: _recorded(h, "InvalidBondsCache"))


def _not_an_equivocation(attack: _Attack) -> None:
    earlier = _fetch(attack.client, _own_justification(attack.t1).latestBlockHash.hex(), attack.ro)
    assert earlier.sender == attack.t1.sender and earlier.seqNum < attack.t1.seqNum
    block = _next_block(attack.t2, VALIDATOR2_ID)
    block.body.systemDeploys.append(_slash_deploy(attack.t1, earlier, VALIDATOR2_ID))
    attack.offence(
        "slash citing two of v1's blocks at different sequence numbers",
        block,
        VALIDATOR2_ID,
        lambda h: _recorded(h, "UnauthorizedSlashDeploy"),
    )


def _repeated_deploy(attack: _Attack) -> None:
    block = _next_block(attack.t1, VALIDATOR1_ID)
    block.body.deploys[0].CopyFrom(attack.t1.body.deploys[0])
    attack.offence(
        "repeated deploy", block, VALIDATOR1_ID, lambda h: _recorded(h, "InvalidRepeatDeploy")
    )


def _skipped_parents(attack: _Attack) -> None:
    grandparent = _fetch(
        attack.client,
        _fetch(attack.client, attack.t2.header.parentsHashList[0].hex(), attack.ro)
        .header.parentsHashList[0]
        .hex(),
        attack.ro,
    )
    block = _next_block(attack.t2, VALIDATOR2_ID)
    block.header.ClearField("parentsHashList")
    block.header.parentsHashList.append(grandparent.blockHash)
    block.body.state.blockNumber = grandparent.body.state.blockNumber + 1
    attack.offence(
        "parents skipping the fork-choice winner",
        block,
        VALIDATOR2_ID,
        lambda h: _recorded(h, "InvalidRejectedDeploy|InvalidTransaction"),
    )


_OFFENCE_PAIRS: Dict[str, Tuple[Callable[[_Attack], None], ...]] = {
    "number-hash-sequence-and-rejected-parent": (
        _hash_and_sequence,
        _block_number,
        _builds_on_rejected,
    ),
    "unbonded-justification-and-unsigned-evidence": (
        _unbonded_justification,
        _badly_signed_evidence,
    ),
    "bonds-and-not-an-equivocation": (_bonds_field, _not_an_equivocation),
    "repeated-deploy-and-skipped-parents": (_repeated_deploy, _skipped_parents),
}


# ── Tests ──────────────────────────────────────────────────────────────────


@pytest.mark.timeout(1800)
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
@pytest.mark.parametrize("pair", list(_OFFENCE_PAIRS))
def test_rejected_offences_do_not_punish(provider, timeouts, pair: str) -> None:
    """Offences that are not equivocations, sent to every honest node while the
    shard runs: each node reaches the same verdict, no forged block finalizes,
    no slash finalizes, and stake and the committee stay as they were on every
    node.

    An offence that occupies a sequence number leaves its sender unable to sign
    another block there without equivocating, so v1 and v2 each commit one
    such offence per shard and then stay silent; v3 and v4 hold the stake
    that keeps finalizing."""
    with _live_shard(
        provider, timeouts, f"rejected-{pair}", attackers=("validator1", "validator2")
    ) as (shard, client):
        v1, v2 = shard.node("validator1"), shard.node("validator2")
        v3, v4 = shard.node("validator3"), shard.node("validator4")
        ro, nodes = shard.readonly, shard.all_nodes
        start = ro.last_finalized_block().blockInfo.blockNumber

        _attacker_block(client, v1, VALIDATOR1_ID, nodes, timeouts)
        attack = _Attack(
            client=client,
            ro=ro,
            t1=_attacker_block(client, v1, VALIDATOR1_ID, nodes, timeouts),
            t2=_attacker_block(client, v2, VALIDATOR2_ID, nodes, timeouts),
            v1_judges=[v2, v3, v4, ro],
            v2_judges=[v1, v3, v4, ro],
            timeouts=timeouts,
        )
        for offence in _OFFENCE_PAIRS[pair]:
            offence(attack)

        assert_chain_advances(
            nodes,
            ro.last_finalized_block().blockInfo.blockNumber + 5,
            timeouts.blocks(30),
            label="after the offences",
        )
        for block_hash in attack.forged:
            _assert_never_finalized(nodes, block_hash)
        assert _finalized_slashes(client, ro, start) == {}
        _await_finalized_stake(ro, _GENESIS_BONDS, timeouts, "unchanged")
        _assert_committee_agreed(nodes, _GENESIS_BONDS, timeouts)
        _assert_validators_live([(v3, VALIDATOR3_ID), (v4, VALIDATOR4_ID)], nodes, timeouts)


@pytest.mark.timeout(2400)
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_equivocation_is_slashed_network_wide(provider, timeouts) -> None:
    """Equivocators are slashed once, on every node, whichever node first
    holds the evidence and however late the second block comes.

    1. v1 moves on, then signs a sibling of its previous block and goes quiet;
       only v2 receives the sibling.
    2. v2 signs a sibling of its latest block and goes quiet; v3 first
       receives a block built on the sibling, asks for the sibling as a
       dependency, then receives it.
    3. v3's next block slashes v1 again; v4 and the observer reject it, since
       v1 has no stake left, and v3 stays silent.

    After each slash: it finalizes on every running node, once; finalized
    stake and the committee agree everywhere; every honest validator still
    gets blocks finalized.
    """
    with _live_shard(provider, timeouts, "equivocation") as (shard, client):
        v1, v2 = shard.node("validator1"), shard.node("validator2")
        v3, v4 = shard.node("validator3"), shard.node("validator4")
        ro = shard.readonly
        start = ro.last_finalized_block().blockInfo.blockNumber
        try:
            running = [v2, v3, v4, ro, shard.boot]
            latest = _silence(client, v1, VALIDATOR1_ID, ro, running, timeouts, min_seq=2)
            earlier = _fetch(client, _own_justification(latest).latestBlockHash.hex(), ro)
            assert earlier.sender == latest.sender and earlier.seqNum == latest.seqNum - 1
            sibling1 = _copy(earlier)
            sibling1.extraBytes = sibling1.extraBytes + b"\x00"
            _seal(sibling1, VALIDATOR1_ID)
            _inject(
                client,
                sibling1,
                [v2],
                _recorded(sibling1.blockHash, "IgnorableEquivocation"),
                timeouts,
            )
            evidence1 = _evidence(earlier, sibling1)
            stake = dict(_GENESIS_BONDS, **{VALIDATOR1_ID.public_hex: 0})
            _assert_slashed_once_everywhere(
                client, ro, running, VALIDATOR1_ID, evidence1, stake, start, timeouts
            )
            _assert_validators_live(
                [(v2, VALIDATOR2_ID), (v3, VALIDATOR3_ID), (v4, VALIDATOR4_ID)], running, timeouts
            )

            running = [v3, v4, ro, shard.boot]
            latest2, sibling2 = _equivocate(client, v2, VALIDATOR2_ID, ro, running, timeouts)
            child = _next_block(sibling2, VALIDATOR2_ID)
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
                client,
                ro,
                running,
                VALIDATOR2_ID,
                _evidence(latest2, sibling2),
                stake,
                start,
                timeouts,
            )
            _assert_validators_live([(v3, VALIDATOR3_ID), (v4, VALIDATOR4_ID)], running, timeouts)

            running = [v4, ro, shard.boot]
            t3 = _silence(client, v3, VALIDATOR3_ID, ro, running, timeouts)
            again = _next_block(t3, VALIDATOR3_ID)
            again.body.systemDeploys.append(_slash_deploy(earlier, sibling1, VALIDATOR3_ID))
            again_hash = _seal(again, VALIDATOR3_ID)
            _inject(
                client,
                again,
                [v4, ro],
                _recorded(again_hash, "UnauthorizedSlashDeploy"),
                timeouts,
            )
            assert_chain_advances(
                running,
                ro.last_finalized_block().blockInfo.blockNumber + 5,
                timeouts.blocks(30),
                label="after the second slash of v1",
            )
            _assert_never_finalized(running, again_hash)
            _await_finalized_stake(ro, stake, timeouts, "unchanged by the second slash")
            _assert_validators_live([(v4, VALIDATOR4_ID)], running, timeouts)
        finally:
            v1.unpause()
            v2.unpause()


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
