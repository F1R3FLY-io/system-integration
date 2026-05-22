"""End-to-end Docker integration tests for the slashing pipeline.

Each test spawns a custom shard (2 validators by default, 3 for tests that
need a third bonded slot) via :class:`Shard.create`, uses
:class:`infra.p2p_client.NodeClient` to inject a crafted malicious
:class:`BlockMessage` over the TLS-encrypted P2P transport layer, and
asserts that the receiving validator records the block as invalid and
slashes the offending validator's stake to zero.

Block-hash compatibility — :func:`rust_block_hash` mirrors the byte
layout used by the Rust node's
``casper/src/rust/util/proto_util.rs:380 hash_block``. The upstream
Python :func:`pyf1r3fly.crypto.gen_block_hash_from_block` wraps
``sigAlgorithm`` / ``seqNum`` / ``shardId`` in protobuf ``StringValue`` /
``Int32Value`` boxes that add 2-byte tag-length prefixes, producing a
different hash than the Rust node. Without this shim, every re-hashed
forged block would be rejected as :class:`InvalidBlockHash` on the very
first validation step, never reaching the intended offense. Each active
test re-asserts the hash-shim/Rust equivalence on its first
``block_request`` so an upstream divergence surfaces immediately.

Platform support — :class:`NodeClient` opens a raw TCP socket from the
host into the container's transport-layer port (40400 internal). This
requires native Linux Docker bridge routing; macOS, Windows, and WSL2
hosts cannot reach container IPs that way, so every test is skipped
there.

Coverage carve-out — M7 ``test_slash_censoring_proposer_eventually_slashes``
(threat T-37 / T-12PF **liveness arm**) is intentionally NOT implemented:
the threat as conventionally understood is structurally undefined in this
protocol's wire semantics. Conventional-protocol censorship (Ethereum,
Cosmos, Tendermint) is "the proposer refuses to include other
validators' transactions from the shared mempool." In *this* protocol,
**deploys are not gossiped**. The deploy gRPC endpoint
(``casper/src/rust/casper_engine/block_admission.rs::admit_deploy``)
stores deploys in the *local* node's ``KeyValueDeployStorage``; the
block creator
(``casper/src/rust/blocks/proposer/block_creator.rs::prepare_user_deploys``)
reads from that same local storage; no code path broadcasts a deploy to
peers. A deploy submitted to v2 stays on v2 until v2 proposes it. v1
cannot "censor" v2's deploys because v1 never has them. T-12PF is
correctly classified as a *boundary assumption* rather than a runtime
offense.

The **safety arm** of T-12PF — i.e. that no wrongful slashing happens
under proposer unfairness — IS Docker-tested by
:func:`test_no_false_positive_slash_on_propose_imbalance`. That test
pins the absence of over-eager behavioral-pattern detectors against the
deployed binary; if a future regression introduces an "inactive
validator" or "proposer dominance" slashing arm, it fails immediately.
The wire-level withholding theme is additionally covered by
:func:`test_slash_late_released_equivocation` and the symmetric
"didn't act on what you saw" arm by
:func:`test_slash_neglected_equivocation`.
"""
from __future__ import annotations

import re
import sys
import time
from contextlib import contextmanager
from typing import Dict, Generator, Optional, Tuple

import pytest
from f1r3fly.crypto import PrivateKey
from f1r3fly.pb.CasperMessage_pb2 import (  # pylint: disable=no-name-in-module
    BlockMessageProto as BlockMessage,
    JustificationProto as Justification,
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
    rust_block_hash,
)
from ...infra.polling import wait_for_block_visible, wait_for_log_match
from ...infra.shard import Shard


pytestmark = pytest.mark.xdist_group("custom")


# Validator key aliases — readability inside the test bodies. The
# canonical identities live in ``infra.keys.VALIDATOR{N}_ID``; we only
# alias the ``.key`` field so existing call shapes like
# ``KEY.sign_block_hash(...)`` and ``KEY.get_public_key().to_hex()`` work
# unchanged.
BONDED_VALIDATOR_KEY_1: PrivateKey = VALIDATOR1_ID.key
BONDED_VALIDATOR_KEY_2: PrivateKey = VALIDATOR2_ID.key
BONDED_VALIDATOR_KEY_3: PrivateKey = VALIDATOR3_ID.key

# Standard contract used by every slash test. Inlined (rather than read
# from a path inside the container) so the test is portable across node
# images and does not depend on the image bundling any particular example
# .rho file at /opt/docker/examples/.
HELLO_CONTRACT = "new x in { x!(1) }"


@contextmanager
def _slash_shard(
    provider,
    timeouts,
    *,
    three_validators: bool = False,
    global_cli_options: Optional[Dict[str, str]] = None,
) -> Generator[Tuple[Shard, Node, Node, NodeClient], None, None]:
    """Spawn a custom shard for a single slash test.

    Yields ``(shard, validator1, validator2, client)``. The bootstrap
    (ceremony master) node is on ``shard.boot`` and ``validator3``
    (when ``three_validators=True``) on ``shard.node("validator3")``.
    ``ftt=-1.0`` keeps fault-tolerance enforcement out of the way and
    ``heartbeat=False`` keeps the propose loop quiet so the test can
    inject crafted blocks deterministically.

    ``global_cli_options`` is plumbed through to :class:`ShardConfig` so
    tests like :func:`test_slash_stale_evidence_rebond` can override
    ``--epoch-length`` (and reach the second epoch in 3 proposes instead
    of 10) without forking the helper.
    """
    bonds = [(VALIDATOR1_ID, 100), (VALIDATOR2_ID, 100)]
    if three_validators:
        bonds.append((VALIDATOR3_ID, 100))
    config = ShardConfig(
        bonds=bonds,
        ftt=-1.0,
        heartbeat=False,
        global_cli_options=global_cli_options or {},
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        v1 = shard.node("validator1")
        v2 = shard.node("validator2")
        with p2p_protocol_client(
            shard.network_name, receive_timeout=float(timeouts.command)
        ) as client:
            yield (shard, v1, v2, client)
    finally:
        shard.destroy()


_SKIP_ON_NON_LINUX = pytest.mark.skipif(
    sys.platform in ("win32", "cygwin", "darwin"),
    reason="NodeClient requires native Linux Docker bridge routing for host-to-container traffic.",
)




@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_invalid_block_hash(provider, timeouts) -> None:
    """v1 re-signs a valid block with a tampered hash; v2 records
    :class:`InvalidBlockHash` and slashes v1.
    """
    with _slash_shard(provider, timeouts) as (_shard, validator1, validator2, client):
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        blockhash = validator1.propose()

        wait_for_block_visible(validator2, blockhash, timeouts.node_startup)

        block_info = validator1.get_block(blockhash)
        block_msg = client.block_request(block_info.blockInfo.blockHash, validator1)

        # Foundational invariant: the Python hash shim must agree with the
        # Rust node's hash on every unmodified block. If this fails, the
        # Rust hash_block algorithm has changed and the shim needs to be
        # updated — see module docstring.
        assert rust_block_hash(block_msg) == block_msg.blockHash, (
            "rust_block_hash() shim diverged from Rust hash_block(); "
            "see casper/src/rust/util/proto_util.rs:380 in f1r3node-rust."
        )

        evil_block_hash = generate_block_hash()
        block_msg.blockHash = evil_block_hash
        block_msg.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(evil_block_hash)
        block_msg.header.timestamp = int(time.time() * 1000)

        client.send_block(block_msg, validator2)

        record_invalid = re.compile(
            rf"Recording invalid block {evil_block_hash.hex()[:10]}\.\.\. for InvalidBlockHash\."
        )
        wait_for_log_match(validator2, record_invalid, timeouts.command)

        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        slashed_block_hash = validator2.propose()

        slashed_block_info = validator2.get_block(slashed_block_hash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_invalid_block_number(provider, timeouts) -> None:
    """v1 ships a block whose ``blockNumber`` is not ``max(parents)+1``; v2
    records :class:`InvalidBlockNumber` and slashes v1.
    """
    with _slash_shard(provider, timeouts) as (_shard, validator1, validator2, client):
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        blockhash = validator1.propose()

        wait_for_block_visible(validator2, blockhash, timeouts.node_startup)

        block_info = validator1.get_block(blockhash)
        block_msg = client.block_request(block_info.blockInfo.blockHash, validator1)
        assert rust_block_hash(block_msg) == block_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        invalid = BlockMessage()
        invalid.CopyFrom(block_msg)
        # block_number must trip Validate::block_number (which checks
        # `max_parent + 1 == number`) but stay inside the **same epoch**
        # as the current LFB so the slash-deploy proposer's
        # `slash_evidence_epoch_matches_target` predicate
        # (casper/src/rust/slashing_authorization.rs:174-181) accepts it
        # under the seven-rule authorization contract. Docker config sets
        # `epoch-length = 10` (docker/conf/default.conf), so picking a
        # value in [2, 9] gives `evidence_epoch = 0 == current_epoch`
        # while still being structurally invalid (`max_parent + 1 = 2`).
        # An older value of 1000 here put evidence in epoch 100 vs
        # current epoch 0, silently skipping V1 from
        # `authorized_slash_candidates` and producing no slash deploy.
        invalid.body.state.blockNumber = 5  # pylint: disable=maybe-no-member
        invalid.header.timestamp = block_msg.header.timestamp + 1  # pylint: disable=maybe-no-member
        invalid_block_hash = rust_block_hash(invalid)
        invalid.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(invalid_block_hash)
        invalid.blockHash = invalid_block_hash

        client.send_block(invalid, validator2)

        record_invalid = re.compile(
            rf"Recording invalid block {invalid_block_hash.hex()[:10]}\.\.\. for InvalidBlockNumber\."
        )
        wait_for_log_match(validator2, record_invalid, timeouts.command)
        wait_for_block_visible(validator2, invalid_block_hash.hex(), timeouts.node_startup)

        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        slashed_block_hash = validator2.propose()

        slashed_block_info = validator2.get_block(slashed_block_hash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_invalid_block_seq(provider, timeouts) -> None:
    """v1 ships a block whose ``seqNum`` is not its previous +1; v2 records
    :class:`InvalidSequenceNumber` and slashes v1.
    """
    with _slash_shard(provider, timeouts) as (_shard, validator1, validator2, client):
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        blockhash = validator1.propose()

        wait_for_block_visible(validator2, blockhash, timeouts.node_startup)

        block_info = validator1.get_block(blockhash)
        block_msg = client.block_request(block_info.blockInfo.blockHash, validator1)
        assert rust_block_hash(block_msg) == block_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        invalid = BlockMessage()
        invalid.CopyFrom(block_msg)
        invalid.seqNum = 1000
        invalid.header.timestamp = block_msg.header.timestamp + 1  # pylint: disable=maybe-no-member
        invalid_block_hash = rust_block_hash(invalid)
        invalid.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(invalid_block_hash)
        invalid.blockHash = invalid_block_hash

        client.send_block(invalid, validator2)

        record_invalid = re.compile(
            rf"Recording invalid block {invalid_block_hash.hex()[:10]}\.\.\. for InvalidSequenceNumber\."
        )
        wait_for_log_match(validator2, record_invalid, timeouts.command)
        wait_for_block_visible(validator2, invalid_block_hash.hex(), timeouts.node_startup)

        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        slashed_block_hash = validator2.propose()

        slashed_block_info = validator2.get_block(slashed_block_hash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_justification_not_correct(provider, timeouts) -> None:
    """v1 ships a block with an extra justification entry from an unknown
    validator; v2 records :class:`InvalidFollows` and slashes v1.

    Bonds three validators so the forged block's existing
    justifications (from V1/V2/V3) are well-formed; the appended
    random-key justification is the only nonconforming entry, isolating
    the InvalidFollows offense.
    """
    with _slash_shard(provider, timeouts, three_validators=True) as (_shard, validator1, validator2, client):
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        blockhash = validator1.propose()

        wait_for_block_visible(validator2, blockhash, timeouts.node_startup)

        block_info = validator1.get_block(blockhash)
        block_msg = client.block_request(block_info.blockInfo.blockHash, validator1)
        assert rust_block_hash(block_msg) == block_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        invalid = BlockMessage()
        invalid.CopyFrom(block_msg)
        error_justification = Justification(
            validator=PrivateKey.generate().to_bytes(),
            latestBlockHash=block_msg.blockHash,
        )
        invalid.justifications.append(error_justification)  # pylint: disable=maybe-no-member
        invalid.header.timestamp = block_msg.header.timestamp + 1  # pylint: disable=maybe-no-member
        invalid_block_hash = rust_block_hash(invalid)
        invalid.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(invalid_block_hash)
        invalid.blockHash = invalid_block_hash

        client.send_block(invalid, validator2)

        record_invalid = re.compile(
            rf"Recording invalid block {invalid_block_hash.hex()[:10]}\.\.\. for InvalidFollows\."
        )
        wait_for_log_match(validator2, record_invalid, timeouts.command)

        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        slashed_block_hash = validator2.propose()

        slashed_block_info = validator2.get_block(slashed_block_hash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_unauthorized_slash_deploy(provider, timeouts) -> None:
    """v2 proposes a block carrying a forged ``SlashSystemDeploy`` that
    cites a non-existent invalid_block_hash; v3 records v2's block as
    :class:`UnauthorizedSlashDeploy` and slashes v2 (not v1).

    Threat: T-11 (attack-tree branch A2 — wrongful slashing via fake
    evidence). The slash-authorization layer must reject fabricated
    evidence; otherwise a malicious proposer could drain any peer's
    bond by inventing slash evidence against them.

    The Rust validation pipeline runs ``slash_deploy_authorization``
    (``validate.rs:932``) inside ``block_summary`` BEFORE
    ``validate_block_checkpoint`` runs (``validation_dispatcher.rs``),
    so the variant fires deterministically as
    :class:`UnauthorizedSlashDeploy` rather than degrading to
    :class:`InvalidTransaction` via post-state divergence.

    Inside ``validate_received_slash_deploys``
    (``slashing_authorization.rs:335``) the rejection ordering is:

    1. ``issuer == block.sender``  (set to v2)
    2. ``target_activation_epoch == current_epoch`` (set to 0, since
       ``epoch_length=10`` per ``docker/conf/default.conf`` and
       ``block.body.state.block_number == 2`` ⇒ epoch 0)
    3. ``invalid_block_hash`` known to the DAG → **THIS RULE TRIPS**
       (the forged hash ``b'\\xde' * 32`` is unknown)
    4. ... (subsequent rules unreached)

    The in-tree predicate analog is
    ``casper/tests/slashing/slash_authorization_regressions.rs::
    received_slash_deploy_rejects_unknown_invalid_hash``; H3 is the
    wire-level counterpart that pins the same authorization contract
    against the deployed binary.
    """
    with _slash_shard(provider, timeouts, three_validators=True) as (shard, validator1, validator2, client):
        validator3 = shard.node("validator3")

        # v1 proposes first so v2's natural propose has a parent.
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        v1_blockhash = validator1.propose()
        wait_for_block_visible(validator2, v1_blockhash, timeouts.node_startup)
        wait_for_block_visible(validator3, v1_blockhash, timeouts.node_startup)

        # v2 proposes a natural block (its system_deploys list will
        # contain only the runtime-generated CloseBlock entry). We
        # graft a forged Slash entry onto that list.
        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        v2_blockhash = validator2.propose()
        wait_for_block_visible(validator1, v2_blockhash, timeouts.node_startup)
        wait_for_block_visible(validator3, v2_blockhash, timeouts.node_startup)

        v2_block_info = validator2.get_block(v2_blockhash)
        v2_block_msg = client.block_request(v2_block_info.blockInfo.blockHash, validator2)
        assert rust_block_hash(v2_block_msg) == v2_block_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        # Forge: a SlashSystemDeploy citing an invalid_block_hash that
        # the DAG has never seen. Issuer must equal block.sender (v2's
        # pubkey) so rule #1 passes; target_activation_epoch must equal
        # current_epoch (block_number 2 / epoch_length 10 = 0) so rule
        # #2 passes; the unknown hash trips rule #3.
        forged_slash = ProcessedSystemDeployProto(
            systemDeploy=SystemDeployDataProto(
                slashSystemDeploy=SlashSystemDeployDataProto(
                    invalidBlockHash=b'\xde' * 32,
                    issuerPublicKey=BONDED_VALIDATOR_KEY_2.get_public_key().to_bytes(),
                    # The installed pyf1r3fly proto declares only two
                    # fields for SlashSystemDeployDataProto; the Rust
                    # source proto adds ``targetActivationEpoch`` (field
                    # 3). The missing-field wire format deserializes on
                    # the Rust side as the proto3 default (0), which
                    # matches current_epoch == 0 — exactly what we want
                    # to pass rule #2 of validate_received_slash_deploys
                    # and reach the intended rule #3 trip.
                )
            ),
            deployLog=[],
            errorMsg='',
        )

        # The block stays v2-proposed; we just append a system deploy
        # and rebuild hash + sig. Other fields (parents, justifications,
        # block_number, sender) all stay valid.
        forged_block = BlockMessage()
        forged_block.CopyFrom(v2_block_msg)
        forged_block.body.systemDeploys.append(forged_slash)  # pylint: disable=maybe-no-member
        forged_block.header.timestamp = v2_block_msg.header.timestamp + 1  # pylint: disable=maybe-no-member
        forged_block_hash = rust_block_hash(forged_block)
        forged_block.sig = BONDED_VALIDATOR_KEY_2.sign_block_hash(forged_block_hash)
        forged_block.blockHash = forged_block_hash

        client.send_block(forged_block, validator3)

        record_invalid = re.compile(
            rf"Recording invalid block {forged_block_hash.hex()[:10]}\.\.\. for UnauthorizedSlashDeploy\."
        )
        wait_for_log_match(validator3, record_invalid, timeouts.command)

        # v3 proposes its slash response. The offender is v2 (proposer
        # of the unauthorized block), NOT v1 (the original victim of
        # the fake-evidence slash deploy never ran on v3 because the
        # block was rejected pre-checkpoint).
        validator3.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_3)
        slashed_blockhash = validator3.propose()
        slashed_block_info = validator3.get_block(slashed_blockhash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}

        assert bonds_validators[BONDED_VALIDATOR_KEY_2.get_public_key().to_hex()] == 0, (
            "v2 (proposer of unauthorized slash) must be slashed"
        )
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 100, (
            "v1 (target of v2's fake evidence) must remain bonded"
        )


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_references_valid_block(provider, timeouts) -> None:
    """v2 proposes a block carrying a ``SlashSystemDeploy`` whose
    ``invalidBlockHash`` references a perfectly valid block in the DAG;
    v3 records v2's block as :class:`UnauthorizedSlashDeploy` and
    slashes v2.

    Threat: a malicious proposer attempts to "frame" v1 by citing v1's
    legitimate block as if it were slashable evidence. The slash-
    authorization predicate must distinguish "this hash is in the DAG"
    from "this hash is in the DAG AND flagged invalid" — rule #4 of
    ``validate_received_slash_deploys``
    (``slashing_authorization.rs:92-97``), which returns
    :class:`SlashAuthError::ReferencesValidBlock`.

    Originally specified in the plan as ``test_slash_spoofed_auth_token``
    (T-13, attack-tree A1). The T-Auth check lives inside ``PoS.rhox``
    (Rholang runtime), not at the block-validation layer — auth tokens
    are unforgeable Rholang names that only the PoS contract holds. The
    spoofed-token attack is therefore not testable from the wire
    protocol; the in-process Rust analog
    (``casper/tests/slashing/uc_21_auth_token_check.rs``) covers it via
    a harness that artificially injects a fake-auth call. This Docker
    test substitutes a sibling rejection rule
    (``ReferencesValidBlock``) that IS Docker-testable and pins another
    branch of the same authorization predicate. The original H4
    coverage gap remains owned by ``uc_21`` in-tree.

    Inside ``validate_received_slash_deploys`` the rejection ordering
    must reach rule #4. Therefore the forged slash deploy must pass:
    1. ``issuer == block.sender`` (set to v2)
    2. ``target_activation_epoch == current_epoch`` (set to 0 with
       ``epoch_length=10`` and ``block_number==2``)
    3. ``invalid_block_hash`` known to the DAG → **passes** (we point
       at v1's legitimate first block)
    4. referenced block flagged invalid → **THIS RULE TRIPS** (v1's
       first block is healthy, not flagged Invalid)
    5. ... (subsequent rules unreached)

    The in-tree predicate analog is
    ``slash_authorization_regressions.rs::
    received_slash_deploy_rejects_valid_block_reference``.
    """
    with _slash_shard(provider, timeouts, three_validators=True) as (shard, validator1, validator2, client):
        validator3 = shard.node("validator3")

        # v1 proposes a perfectly valid block. This block's hash is
        # what v2 will reference in the forged slash deploy.
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        v1_blockhash = validator1.propose()
        wait_for_block_visible(validator2, v1_blockhash, timeouts.node_startup)
        wait_for_block_visible(validator3, v1_blockhash, timeouts.node_startup)

        # v2 proposes a natural block; we'll graft a forged slash entry
        # onto its system_deploys list.
        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        v2_blockhash = validator2.propose()
        wait_for_block_visible(validator1, v2_blockhash, timeouts.node_startup)
        wait_for_block_visible(validator3, v2_blockhash, timeouts.node_startup)

        v2_block_info = validator2.get_block(v2_blockhash)
        v2_block_msg = client.block_request(v2_block_info.blockInfo.blockHash, validator2)
        assert rust_block_hash(v2_block_msg) == v2_block_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        # The slash references v1's *valid* first block. The DAG has it
        # (rule #3 passes), but it's not flagged Invalid (rule #4
        # trips). Issuer = v2 (sender match, rule #1). Target epoch = 0
        # (block_number 2 / epoch_length 10, rule #2 match).
        forged_slash = ProcessedSystemDeployProto(
            systemDeploy=SystemDeployDataProto(
                slashSystemDeploy=SlashSystemDeployDataProto(
                    invalidBlockHash=bytes.fromhex(v1_blockhash),
                    issuerPublicKey=BONDED_VALIDATOR_KEY_2.get_public_key().to_bytes(),
                    # targetActivationEpoch missing from installed
                    # pyf1r3fly proto; deserializes to 0 on Rust side.
                )
            ),
            deployLog=[],
            errorMsg='',
        )

        forged_block = BlockMessage()
        forged_block.CopyFrom(v2_block_msg)
        forged_block.body.systemDeploys.append(forged_slash)  # pylint: disable=maybe-no-member
        forged_block.header.timestamp = v2_block_msg.header.timestamp + 1  # pylint: disable=maybe-no-member
        forged_block_hash = rust_block_hash(forged_block)
        forged_block.sig = BONDED_VALIDATOR_KEY_2.sign_block_hash(forged_block_hash)
        forged_block.blockHash = forged_block_hash

        client.send_block(forged_block, validator3)

        record_invalid = re.compile(
            rf"Recording invalid block {forged_block_hash.hex()[:10]}\.\.\. for UnauthorizedSlashDeploy\."
        )
        wait_for_log_match(validator3, record_invalid, timeouts.command)

        validator3.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_3)
        slashed_blockhash = validator3.propose()
        slashed_block_info = validator3.get_block(slashed_blockhash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}

        assert bonds_validators[BONDED_VALIDATOR_KEY_2.get_public_key().to_hex()] == 0, (
            "v2 (proposer of unauthorized slash) must be slashed"
        )
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 100, (
            "v1 (target of v2's framing attempt) must remain bonded"
        )


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_self_regression(provider, timeouts) -> None:
    """v1 proposes block A then block B; v1 then forges a third block
    whose self-justification points back to genesis (regressing below
    v1's current latest in v2's view). v2 records
    :class:`JustificationRegression` and slashes v1.

    Threat: T-7 (bug #6 — self-regression). A validator that points its
    own self-justification backwards to an older block is signalling
    DAG-state amnesia, either accidentally or as a precursor to
    equivocation. The detector at ``validate.rs:973
    justification_regressions`` compares new-block justifications
    against current-latest-message justifications per-validator; any
    sequence-number regression trips the arm.

    Forge requires v1 to propose TWICE before regression is observable.
    The third forged block is a V1-signed seq=1 block whose
    justifications point at genesis. v2's ``cur_lms[V1]`` (read from
    block B's justifications) is block A at seq 1; the forge's
    ``new_lms[V1]`` is genesis at seq 0. ``0 < 1`` →
    ``JustificationRegression``.

    Forge sequence-number subtlety: the forge MUST use seq=1 (not
    seq=3) because the ``sequence_number`` validator
    (``validate.rs::block_summary`` position 13) runs strictly before
    ``justification_regressions`` (position 14) and requires
    ``forge.seqNum == forge.creator_justification.seqNum + 1`` — with
    creator_just at genesis (seq 0), the only valid forge seqNum is
    1. The forge is therefore structurally an equivocation with
    block A (same V1, same seq 1, different content). The detector
    classifies it as JustificationRegression rather than equivocation
    because ``justification_regressions`` (#14) fires inside
    ``block_summary``, which returns Invalid; ``check_equivocations``
    runs only on blocks that pass ``block_summary``.

    The in-tree predicate analog is ``casper/src/rust/validate.rs:973``
    plus the property test ``prop_t_9_6_self_regression.rs``.
    """
    with _slash_shard(provider, timeouts) as (_shard, validator1, validator2, client):
        # First propose: v1 builds atop genesis.
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        v1_blockhash_a = validator1.propose()
        wait_for_block_visible(validator2, v1_blockhash_a, timeouts.node_startup)

        # Second propose: v1's self-justification now points at block A.
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        v1_blockhash_b = validator1.propose()
        wait_for_block_visible(validator2, v1_blockhash_b, timeouts.node_startup)

        # We don't need an explicit genesis hash here — block A's
        # justifications already point at genesis (as V1's first block,
        # that's its natural state). The forge inherits those
        # justifications via CopyFrom from block A and uses them to
        # achieve the regression relative to v2's
        # ``cur_lms[V1] = block A`` (read from block B's justifications).
        v1_block_info_a = validator1.get_block(v1_blockhash_a)
        block_msg_a = client.block_request(v1_block_info_a.blockInfo.blockHash, validator1)
        assert rust_block_hash(block_msg_a) == block_msg_a.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        # Forge a NEW V1-signed block claiming seq=1 (NOT seq=3) with
        # justifications regressed to genesis. The forge must satisfy
        # ``sequence_number`` (which expects ``seq ==
        # creator_just.seq + 1`` — 0 + 1 = 1) BEFORE
        # ``justification_regressions`` can fire (the validator order
        # in ``validate.rs::block_summary`` is sequence_number at #13
        # then justification_regressions at #14). Using
        # ``block_msg_a`` as the template gives us a self-consistent
        # seq=1 / block_number=1 / parents=[genesis] /
        # justifications=[V1→genesis, V2→genesis] structure for free
        # — we just need a fresh deploy (so repeat_deploy doesn't fire
        # at #5) and a hash-distinguishing tweak (extraBytes).
        #
        # cur_lms[V1] in V2's view comes from block_msg_b's
        # justifications: cur_lms[V1] = block A (seq 1).
        # new_lms[V1] in the forge = genesis (seq 0).
        # 0 < 1 → JustificationRegression at #14.
        forged = BlockMessage()
        forged.CopyFrom(block_msg_a)
        # Replace the deploy with a fresh one so repeat_deploy at #5
        # doesn't find block_msg_a's deploy in its ancestry. Note:
        # block_msg_a's parents == [genesis], so bf_traverse_find from
        # [genesis] returns no blocks above earliest_block_number; the
        # forge passes repeat_deploy iff its own deploys aren't in
        # genesis (they aren't), regardless of the source deploy. The
        # fresh-deploy step is belt-and-suspenders.
        fresh_deploy = create_deploy_data(
            BONDED_VALIDATOR_KEY_1, HELLO_CONTRACT, 999, 1000000, shard_id='root',
        )
        forged.body.deploys[0].deploy.CopyFrom(fresh_deploy)  # pylint: disable=maybe-no-member
        # block_msg_a's justifications already point at genesis (its
        # natural state as V1's first block), so the forge's
        # justifications IS the regression relative to V2's cur_lms[V1]
        # (which is block A via block B's justifications). No
        # explicit rewrite needed.
        # extraBytes mutation gives the forge a distinct hash from
        # block A without perturbing replay (not that replay runs —
        # justification_regressions fires in block_summary before
        # validate_block_checkpoint).
        forged.extraBytes = block_msg_a.extraBytes + b'\x00'  # pylint: disable=maybe-no-member
        forged_hash = rust_block_hash(forged)
        forged.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(forged_hash)
        forged.blockHash = forged_hash

        client.send_block(forged, validator2)

        record_invalid = re.compile(
            rf"Recording invalid block {forged_hash.hex()[:10]}\.\.\. for JustificationRegression\."
        )
        wait_for_log_match(validator2, record_invalid, timeouts.command)

        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        slashed_blockhash = validator2.propose()
        slashed_block_info = validator2.get_block(slashed_blockhash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_invalid_bonds_cache(provider, timeouts) -> None:
    """v1 proposes a valid block; v1 then mutates the block's
    ``body.state.bonds`` cache (halves its own stake) without updating
    the post-state, re-hashes, re-signs, and ships to v2. v2's
    ``bonds_cache`` validator computes the actual bonds from the
    post-state tuple space, finds the divergence, and records
    :class:`InvalidBondsCache`. v2 slashes v1.

    Threat: T-26. The bonds cache in ``body.state.bonds`` is an
    operator-facing summary of what's stored at the PoS contract's
    bonds channel in the tuple space; if a malicious proposer ships a
    block whose cache disagrees with the actual tuple-space bonds,
    downstream readers (block explorers, gRPC clients) would see a
    false bond table.

    Forge mechanics: ``Validate::bonds_cache`` (``validate.rs:1095``)
    runs AFTER ``validate_block_checkpoint`` in the dispatcher
    (``validation_dispatcher.rs:120``). Mutating ``body.state.bonds``
    does NOT affect the post-state replay because bonds are stored in
    the tuple space (which the deploys read/write), not in the cache
    field. So the replay still produces the same ``post_state_hash``,
    ``validate_block_checkpoint`` passes, and ``bonds_cache``
    immediately detects the cache-vs-tuplespace divergence.

    The in-tree analog is
    ``casper/tests/slashing/integration_t_invalid_bonds_cache.rs``.
    """
    with _slash_shard(provider, timeouts) as (_shard, validator1, validator2, client):
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        v1_blockhash = validator1.propose()
        wait_for_block_visible(validator2, v1_blockhash, timeouts.node_startup)

        v1_block_info = validator1.get_block(v1_blockhash)
        block_msg = client.block_request(v1_block_info.blockInfo.blockHash, validator1)
        assert rust_block_hash(block_msg) == block_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        # Forge: halve v1's stake in the bonds cache. The post-state
        # tuple space still reports v1=100 because we don't change
        # ``post_state_hash``; the set comparison
        # ``bonds_set != computed_bonds_set`` trips InvalidBondsCache.
        forged = BlockMessage()
        forged.CopyFrom(block_msg)
        v1_pubkey = BONDED_VALIDATOR_KEY_1.get_public_key().to_bytes()
        for bond in forged.body.state.bonds:  # pylint: disable=maybe-no-member
            if bond.validator == v1_pubkey:
                bond.stake = bond.stake // 2  # 100 → 50
                break
        # Bonds mutation alone changes body.SerializeToString() →
        # changes rust_block_hash output. We must NOT also mutate
        # header.timestamp here: the closeBlock system deploy reads
        # rho:block:data which exposes timestamp; the recorded
        # deployLog was produced with block_msg's original timestamp;
        # any change makes the replay's events diverge from the
        # deployLog and the variant fires as InvalidTransaction
        # instead of InvalidBondsCache.
        forged_hash = rust_block_hash(forged)
        forged.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(forged_hash)
        forged.blockHash = forged_hash

        client.send_block(forged, validator2)

        record_invalid = re.compile(
            rf"Recording invalid block {forged_hash.hex()[:10]}\.\.\. for InvalidBondsCache\."
        )
        wait_for_log_match(validator2, record_invalid, timeouts.command)

        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        slashed_blockhash = validator2.propose()
        slashed_block_info = validator2.get_block(slashed_blockhash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_invalid_repeat_deploy(provider, timeouts) -> None:
    """v1 proposes a block; v1 then forges a second block at the next
    sequence number whose body contains the same deploy (same
    ``deployId``) as the first. v2's ``repeat_deploy`` validator walks
    the sender's chain, finds the duplicate, and records
    :class:`InvalidRepeatDeploy`. v2 slashes v1.

    Threat: T-29. A repeat deploy in a single sender's chain is a
    replay attack — the deploy has already been processed and its
    side effects committed; processing it again would either fail
    (idempotent contracts) or double-charge / double-credit (non-
    idempotent contracts). Either is a protocol violation.

    Forge mechanics: copy block A, increment seqNum and block_number,
    update parent to A, update timestamp. CRUCIALLY do not replace the
    deploy. v2's ``repeat_deploy`` validator checks the recent chain
    (up to ``deploy_lifespan`` blocks back) and finds block A also
    carries the same deploy → ``InvalidRepeatDeploy``.

    The in-tree analog is ``integration_t_invalid_repeat_deploy.rs``.
    """
    with _slash_shard(provider, timeouts) as (_shard, validator1, validator2, client):
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        v1_blockhash = validator1.propose()
        wait_for_block_visible(validator2, v1_blockhash, timeouts.node_startup)

        v1_block_info = validator1.get_block(v1_blockhash)
        block_msg = client.block_request(v1_block_info.blockInfo.blockHash, validator1)
        assert rust_block_hash(block_msg) == block_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        # Forge a successor block carrying the same deploy.
        forged = BlockMessage()
        forged.CopyFrom(block_msg)
        forged.seqNum = block_msg.seqNum + 1
        forged.body.state.blockNumber = block_msg.body.state.blockNumber + 1  # pylint: disable=maybe-no-member
        forged.header.timestamp = int(time.time() * 1000)  # pylint: disable=maybe-no-member
        forged.header.ClearField("parentsHashList")  # pylint: disable=maybe-no-member
        forged.header.parentsHashList.append(bytes.fromhex(v1_blockhash))  # pylint: disable=maybe-no-member
        forged_hash = rust_block_hash(forged)
        forged.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(forged_hash)
        forged.blockHash = forged_hash

        client.send_block(forged, validator2)

        record_invalid = re.compile(
            rf"Recording invalid block {forged_hash.hex()[:10]}\.\.\. for InvalidRepeatDeploy\."
        )
        wait_for_log_match(validator2, record_invalid, timeouts.command)

        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        slashed_blockhash = validator2.propose()
        slashed_block_info = validator2.get_block(slashed_blockhash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_invalid_validator_approve_evil_block(provider, timeouts) -> None:
    """v1 ships a hash-tampered block to v2; v2 builds on it without
    slashing v1 and ships the result to v3; v3 records v2's block as
    :class:`InvalidTransaction` (because v2's deploy replays to a
    post-state that does not match v1's copied post-state hash) and
    slashes BOTH v1 and v2 in a single propose round.

    This is the Level-2 closure ("neglect of an invalid block"): a
    validator that builds atop an invalid block without slashing the
    proposer is itself slashable. The Rust pipeline classifies v2's
    "approve" block as :class:`InvalidTransaction` because
    :func:`validate_block_checkpoint` (which compares the block's
    declared ``post_state_hash`` to the replay result) runs strictly
    before :func:`neglected_invalid_block` in :mod:`validate`. v2's
    block carries v1's stale ``post_state_hash`` (copied via
    ``CopyFrom``) but a different deploy, so replay diverges. The
    Level-2 closure is exercised by the observable outcome — v3 emits
    two slash deploys in one block — not by the variant label.

    The in-process Rust equivalent
    (``casper/tests/slashing/integration_t_neglected_invalid_block.rs``)
    asserts ``NeglectedInvalidBlock`` because it crafts a
    self-consistent block via a test-only helper that recomputes
    post-state. Doing the same over the wire is not possible without a
    Python-side rspace replay.
    """
    with _slash_shard(provider, timeouts, three_validators=True) as (shard, validator1, validator2, client):
        validator3 = shard.node("validator3")

        # `get_blocks(N)` returns a list of `LightBlockInfo` items with
        # fields at the top level (no `.blockInfo` wrapper, unlike
        # `get_block(hash)` which returns `BlockInfoResponse` wrapping
        # `LightBlockInfo` under `.blockInfo`). Extract the genesis hash
        # once and reuse it as raw bytes throughout the test.
        genesis_block = validator3.get_blocks(2)[0]
        genesis_hash_bytes = bytes.fromhex(genesis_block.blockHash)

        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        blockhash = validator1.propose()

        wait_for_block_visible(validator3, blockhash, timeouts.node_startup)
        wait_for_block_visible(validator2, blockhash, timeouts.node_startup)

        block_info = validator1.get_block(blockhash)
        block_msg = client.block_request(block_info.blockInfo.blockHash, validator1)
        assert rust_block_hash(block_msg) == block_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        evil_block_hash = generate_block_hash()

        # v1's invalid block: tamper only `blockHash` + `sig`. The
        # block's contents stay identical to its predecessor; the hash
        # check fires first in `block_summary` (validate.rs:291) and v2
        # records `InvalidBlockHash` against v1. The parents/
        # justifications mutation that the Scala port carried at this
        # site was dead code — `block_hash` short-circuits the
        # validation chain before `parents` runs, so the mutation never
        # affected the recorded variant. Removing it makes the test
        # deterministic and matches `test_slash_invalid_block_hash`'s
        # pattern at lines 184-221 above.
        invalid_block = BlockMessage()
        invalid_block.CopyFrom(block_msg)
        invalid_block.blockHash = evil_block_hash
        invalid_block.header.timestamp = int(time.time() * 1000)  # pylint: disable=maybe-no-member
        invalid_block.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(evil_block_hash)
        client.send_block(invalid_block, validator2)

        # Both v2 (the direct recipient) AND v3 must have v1's evil
        # block in their DAG (recorded as Invalid via the gossip-then-
        # fetch path) before v2's "approve" block ships, otherwise v3
        # quarantines v2's block as MissingDependency
        # (block_processor.rs:511) and the test stalls.
        wait_for_block_visible(validator2, evil_block_hash.hex(), timeouts.node_startup)
        wait_for_block_visible(validator3, evil_block_hash.hex(), timeouts.node_startup)

        # v2's "approve" block: builds atop genesis (skipping v1's evil
        # block) and cites v1's evil block in justifications. v2 doesn't
        # emit a slash deploy — exactly the neglect we want v3 to
        # observe. `shard_id='root'` matches the running shard; without
        # it v3 would reject the embedded deploy as `InvalidShardId`
        # before reaching the post-state replay check.
        block_not_slash = BlockMessage()
        block_not_slash.CopyFrom(block_msg)
        block_not_slash.seqNum = 1
        block_not_slash.body.state.blockNumber = 1  # pylint: disable=maybe-no-member
        block_not_slash.sender = BONDED_VALIDATOR_KEY_2.get_public_key().to_bytes()
        block_not_slash.ClearField("justifications")
        block_not_slash.justifications.extend([  # pylint: disable=maybe-no-member
            Justification(
                validator=BONDED_VALIDATOR_KEY_1.get_public_key().to_bytes(),
                latestBlockHash=evil_block_hash,
            ),
            Justification(
                validator=BONDED_VALIDATOR_KEY_2.get_public_key().to_bytes(),
                latestBlockHash=genesis_hash_bytes,
            ),
            Justification(
                validator=BONDED_VALIDATOR_KEY_3.get_public_key().to_bytes(),
                latestBlockHash=genesis_hash_bytes,
            ),
        ])
        deploy_data = create_deploy_data(
            BONDED_VALIDATOR_KEY_2, HELLO_CONTRACT, 1, 1000000, shard_id='root',
        )
        block_not_slash.body.deploys[0].deploy.CopyFrom(deploy_data)  # pylint: disable=maybe-no-member
        block_not_slash.header.ClearField("parentsHashList")  # pylint: disable=maybe-no-member
        block_not_slash.header.parentsHashList.append(  # pylint: disable=maybe-no-member
            genesis_hash_bytes
        )
        block_not_slash.header.timestamp = int(time.time() * 1000)  # pylint: disable=maybe-no-member
        invalid_block_hash = rust_block_hash(block_not_slash)
        block_not_slash.sig = BONDED_VALIDATOR_KEY_2.sign_block_hash(invalid_block_hash)
        block_not_slash.blockHash = invalid_block_hash

        client.send_block(block_not_slash, validator3)

        # v2 built on a block it should have slashed; v3 detects the
        # post-state divergence on replay and records InvalidTransaction
        # (validation_dispatcher.rs classifies a checkpoint failure with
        # `Either::Right(None)` as InvalidTransaction, per the order in
        # `validate.rs:301-346` which runs checkpoint before
        # neglected_invalid_block).
        record_invalid = re.compile(
            rf"Recording invalid block {invalid_block_hash.hex()[:10]}\.\.\. for InvalidTransaction\."
        )
        wait_for_log_match(validator3, record_invalid, timeouts.command)

        validator3.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_3)
        slashed_blockhash = validator3.propose()
        slashed_block_info = validator3.get_block(slashed_blockhash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}

        # Two slash deploys emitted in one propose: prepare_slashing_deploys
        # (block_creator.rs:287) iterates `dag.invalid_blocks()` and
        # `authorized_slash_candidates` (slashing_authorization.rs:253)
        # is uncapped, so all invalid offenders are slashed in the same
        # block. This is the Level-2 enforcement signature.
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0
        assert bonds_validators[BONDED_VALIDATOR_KEY_2.get_public_key().to_hex()] == 0


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_GHOST_disobeyed(provider, timeouts) -> None:
    """v1 ships a block that mutates parents away from the GHOST winner
    plus replaces the embedded deploy; v2 records the block as
    :class:`InvalidTransaction` (post-state replay diverges from v1's
    copied post-state) and slashes v1.

    Wire-level limitation. The Scala harness's original intent was to
    test :class:`InvalidParents` (the GHOST-violation arm at
    ``validate.rs:846``). In the Rust pipeline, :class:`InvalidParents`
    is observable only when ``parents_hash_list`` is empty AND
    ``block_number == 0`` (so the empty-parents short-circuit in
    ``justification_follows`` fires before ``validate_block_checkpoint``
    runs) OR when the forged block carries a self-consistent
    post-state computed against the mutated parents. The Docker
    NodeClient has no rspace replay so it can't produce a
    self-consistent post-state, and the empty-parents path additionally
    requires a deploy with ``valid_after_block_number = -1`` (not
    exposed by :func:`create_deploy_data` in pyf1r3fly).

    What this test does verify on the wire: forging an off-GHOST block
    with a replaced deploy produces a slashable offense (the variant
    label is :class:`InvalidTransaction` rather than
    :class:`InvalidParents`, but both are in the
    :meth:`InvalidBlock::is_slashable` list — see
    ``casper/src/rust/block_status.rs:196``). v2 slashes v1's bond to
    zero, which is the operationally meaningful outcome. Formal
    :class:`InvalidParents` semantics are covered by the in-process
    Rust test ``casper/tests/slashing/integration_t_invalid_parents.rs``
    via ``propose_with_block_mutation`` which sidesteps the wire-level
    replay constraint.

    Three validators: v1 + v2 + v3 (instead of bootstrap + 2 validators
    in the legacy fixture), since ``start_custom_shard`` does not bond
    the boot node.
    """
    with _slash_shard(provider, timeouts, three_validators=True) as (shard, validator1, validator2, client):
        validator3 = shard.node("validator3")

        validator3.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_3)
        blockhash1 = validator3.propose()

        wait_for_block_visible(validator1, blockhash1, timeouts.node_startup)
        wait_for_block_visible(validator2, blockhash1, timeouts.node_startup)

        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        blockhash2 = validator2.propose()

        wait_for_block_visible(validator1, blockhash2, timeouts.node_startup)
        wait_for_block_visible(validator3, blockhash2, timeouts.node_startup)

        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        blockhash3 = validator1.propose()

        wait_for_block_visible(validator2, blockhash3, timeouts.node_startup)
        wait_for_block_visible(validator3, blockhash3, timeouts.node_startup)

        block_info1 = validator1.get_block(blockhash1)
        block_info3 = validator1.get_block(blockhash3)
        block_msg3 = client.block_request(block_info3.blockInfo.blockHash, validator1)
        assert rust_block_hash(block_msg3) == block_msg3.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        invalid = BlockMessage()
        invalid.CopyFrom(block_msg3)
        invalid.header.ClearField("parentsHashList")  # pylint: disable=maybe-no-member
        invalid.body.state.blockNumber = 2  # pylint: disable=maybe-no-member
        invalid.header.parentsHashList.append(  # pylint: disable=maybe-no-member
            bytes.fromhex(block_info1.blockInfo.blockHash)
        )
        invalid.header.timestamp = int(time.time() * 1000)  # pylint: disable=maybe-no-member
        # The replacement deploy's `shard_id` must match the running
        # shard ('root', the default in docker/conf/default.conf). With
        # `shard_id='test'` the deploy short-circuits validation as
        # `InvalidShardId` and v2 never reaches the post-state replay
        # that lets us observe the slashable offense.
        deploy_data = create_deploy_data(
            BONDED_VALIDATOR_KEY_2, HELLO_CONTRACT, 1, 1000000, shard_id='root',
        )
        invalid.body.deploys[0].deploy.CopyFrom(deploy_data)  # pylint: disable=maybe-no-member
        invalid_block_hash = rust_block_hash(invalid)
        invalid.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(invalid_block_hash)
        invalid.blockHash = invalid_block_hash

        client.send_block(invalid, validator2)

        # Expect `InvalidTransaction` (not `InvalidParents`): copying
        # block_msg3's `post_state_hash` and then replacing the deploy
        # makes v2's replay diverge from the declared post-state. The
        # checkpoint validator fires before `parents` and `justification
        # _follows`, so this is the variant that actually surfaces on
        # the wire. See the test docstring for the rationale.
        record_invalid = re.compile(
            rf"Recording invalid block {invalid_block_hash.hex()[:10]}\.\.\. for InvalidTransaction\."
        )
        wait_for_log_match(validator2, record_invalid, timeouts.command)

        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        slashed_blockhash = validator2.propose()
        slashed_block_info = validator2.get_block(slashed_blockhash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}

        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_node_working_right_after_slashing(provider, timeouts) -> None:
    """After slashing v1 for :class:`InvalidBlockHash`, v2 must keep
    proposing normally and the slash deploy must not be included in
    subsequent blocks.
    """
    with _slash_shard(provider, timeouts) as (_shard, validator1, validator2, client):
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        blockhash = validator1.propose()

        wait_for_block_visible(validator2, blockhash, timeouts.node_startup)

        block_info = validator1.get_block(blockhash)
        block_msg = client.block_request(block_info.blockInfo.blockHash, validator1)
        assert rust_block_hash(block_msg) == block_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        evil_block_hash = generate_block_hash()
        block_msg.blockHash = evil_block_hash
        block_msg.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(evil_block_hash)
        block_msg.header.timestamp = int(time.time() * 1000)

        client.send_block(block_msg, validator2)

        record_invalid = re.compile(
            rf"Recording invalid block {evil_block_hash.hex()[:10]}\.\.\. for InvalidBlockHash\."
        )
        wait_for_log_match(validator2, record_invalid, timeouts.command)

        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        slashed_block_hash = validator2.propose()

        slashed_block_info = validator2.get_block(slashed_block_hash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0

        slash_block = client.block_request(slashed_block_info.blockInfo.blockHash, validator2)
        assert is_exist_slash_deploy(slash_block), \
            "system deploys should contain a slashSystemDeploy on the slashing block"

        # Next block must NOT contain another slash deploy.
        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        new_block_hash = validator2.propose()
        new_block_info = validator2.get_block(new_block_hash)
        normal_block = client.block_request(new_block_info.blockInfo.blockHash, validator2)
        assert not is_exist_slash_deploy(normal_block), \
            "slash deploy should be emitted only once per offense"


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_ignorable_equivocation(provider, timeouts) -> None:
    """v1 ships two distinct blocks at the same ``(sender, seqNum,
    justifications)``; v2 records the second as
    :class:`IgnorableEquivocation` and slashes v1.

    Threat: T-2 (Bug #1 wire-level closure). Historically
    ``IgnorableEquivocation`` was non-slashable (DOS-vector loophole);
    after the bug-#1 fix it is slashable
    (``block_status.rs:202-208 is_slashable()``). This test is the
    wire-level regression that pins the post-fix behaviour against the
    deployed binary; without it, a future revert of Bug #1 would pass
    every in-tree test.

    Forge pattern: the "sibling forge". v1 honestly proposes ``b1``
    with HELLO_CONTRACT and a closeBlock system deploy; the test
    fetches ``b1`` over the wire, copies its proto, appends a single
    zero byte to ``block.extraBytes`` (top-level), re-hashes with
    :func:`rust_block_hash`, and re-signs with v1's key.

    Why mutate ``block.extraBytes`` rather than ``header.timestamp``:
    :func:`rust_block_hash` includes ``block.extraBytes`` in the
    hashed buffer, so a single-byte tweak produces a distinct hash.
    But ``block.extraBytes`` is NOT read by any system deploy or by
    the rspace replay. By contrast, the closeBlock system deploy
    queries ``rho:block:data`` which exposes ``(blockNumber, sender,
    timestamp)``; the recorded ``deployLog`` was produced with v1's
    original timestamp, and mutating ``header.timestamp`` causes the
    replay's events to diverge from the deployLog, surfacing as
    :class:`SystemRuntimeError(ConsumeFailed)` →
    :class:`InvalidTransaction` instead of the intended
    :class:`IgnorableEquivocation`. The ``extraBytes`` mutation
    sidesteps this because no replay event references it.

    Validation-pipeline trace on v2 for the sibling ``b1p``:

    * ``block_summary`` passes: hash matches (we re-hashed),
      ``timestamp`` strictly-monotonic against parents (ts+1), parents
      / block_number / seqNum / justification_follows all identical to
      ``b1``, ``justification_regressions`` finds ``new_lms ==
      cur_lms`` (same justifications) → no regression.
    * ``validate_block_checkpoint`` passes (post-state matches).
    * ``bonds_cache`` passes (same bonds).
    * ``neglected_invalid_block`` passes (no invalid justifications).
    * ``check_neglected_equivocations_with_update`` returns oblivious
      (no pre-existing record for v1).
    * ``check_equivocations``: ``latest_message_hash(v1) == b1.hash``
      (v2 already has b1), ``creator_justification_hash(b1p) ==
      genesis``. They differ → equivocation.
      ``requested_as_dependency(b1p_hash) == false`` (the test does not
      seed v2's buffer with anything citing b1p) → variant resolves to
      :class:`IgnorableEquivocation`.

    In-tree analog: ``casper/tests/slashing/
    integration_t_ignorable_equivocation.rs``. That test stubs the
    process-block path; this one routes the equivocating block over
    the deployed gRPC/TCP transport.
    """
    with _slash_shard(provider, timeouts) as (_shard, validator1, validator2, client):
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        b1_hash = validator1.propose()
        wait_for_block_visible(validator2, b1_hash, timeouts.node_startup)

        b1_block_info = validator1.get_block(b1_hash)
        b1_msg = client.block_request(b1_block_info.blockInfo.blockHash, validator1)
        assert rust_block_hash(b1_msg) == b1_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        # Sibling b1p: same body, +1 byte in block.extraBytes, V1-signed.
        # We mutate block.extraBytes (top-level, NOT header.timestamp)
        # because rust_block_hash includes block.extraBytes in the hash
        # but the rho:block:data system deploy does NOT read it. The
        # closeBlock system deploy in b1's deployLog produces (blockNum,
        # sender, timestamp) on blockDataCh during V1's original
        # execution; on replay we re-produce the same triple iff
        # header.timestamp is unchanged. Empirically, mutating
        # header.timestamp breaks the replay with SystemRuntimeError
        # (ConsumeFailed) and the variant fires as InvalidTransaction
        # instead of IgnorableEquivocation. Mutating block.extraBytes
        # changes the hash but does not perturb replay.
        b1p = BlockMessage()
        b1p.CopyFrom(b1_msg)
        b1p.extraBytes = b1_msg.extraBytes + b'\x00'  # pylint: disable=maybe-no-member
        b1p_hash = rust_block_hash(b1p)
        b1p.blockHash = b1p_hash
        b1p.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(b1p_hash)

        client.send_block(b1p, validator2)
        record_invalid = re.compile(
            rf"Recording invalid block {b1p_hash.hex()[:10]}\.\.\. for IgnorableEquivocation\."
        )
        wait_for_log_match(validator2, record_invalid, timeouts.command)

        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        slashed_block_hash = validator2.propose()
        slashed_block_info = validator2.get_block(slashed_block_hash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0, (
            "v1 (equivocator) must be slashed; Bug #1 fix wire-level regression."
        )


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_admissible_equivocation(provider, timeouts) -> None:
    """v1 ships two distinct blocks at the same ``(sender, seqNum,
    justifications)``; v2 records the second as
    :class:`AdmissibleEquivocation` and slashes v1.

    Threat: T-1 (canonical direct equivocation, the central threat
    slashing defends against). The Admissible variant fires when the
    receiver has already asked for the equivocating block by hash
    (``requested_as_dependency == true``); the Ignorable variant when
    not (:func:`test_slash_ignorable_equivocation` covers that branch).

    Wire path:

    1. v1 proposes b1 honestly; v2 sees it via gossip.
    2. The test forges the sibling ``b1p`` via the timestamp-+1 trick
       (see :func:`test_slash_ignorable_equivocation` for the
       post-state self-consistency argument).
    3. The test forges a v2-signed "child of b1p" that names ``b1p`` as
       its parent in ``header.parentsHashList`` and in
       ``justifications``. The child is shipped to v2 first.
    4. v2's ``block_processor`` runs ``check_dependencies_with_effects``
       on the child, finds ``b1p`` missing, calls
       ``commit_to_buffer(child, {b1p_hash})`` and
       ``request_missing_dependencies({b1p_hash})``. The casper buffer
       storage now reports ``requested_as_dependency(b1p_hash) ==
       true``.
    5. The test waits for the ``"waiting on missing dependencies"`` log
       line (emitted by ``block_processor.rs`` when ``is_ready ==
       false``) to confirm the buffer flip before shipping b1p.
    6. The test then ships ``b1p``; v2's detector finds
       ``creator_justification(b1p) != latest_message(v1)`` (== b1),
       sees ``requested_as_dependency == true``, returns
       :class:`AdmissibleEquivocation`.

    The child block does not need to be fully valid — only valid enough
    to pass ``format_of_fields`` + ``block_signature`` before reaching
    ``commit_to_buffer`` (block_processor.rs:223-296). It will sit in
    the buffer forever because b1p is invalid; that is intentional and
    does not affect the slash assertion.

    In-tree analog:
    ``casper/tests/slashing/integration_t_admissible_equivocation.rs``.
    """
    with _slash_shard(provider, timeouts) as (_shard, validator1, validator2, client):
        # Genesis hash for the v2-self justification entry on
        # child_of_b1p (v2 hasn't proposed yet, so its latest message
        # in the child's justifications must point at genesis).
        genesis_block = validator2.get_blocks(2)[0]
        genesis_hash_bytes = bytes.fromhex(genesis_block.blockHash)

        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        b1_hash = validator1.propose()
        wait_for_block_visible(validator2, b1_hash, timeouts.node_startup)

        b1_block_info = validator1.get_block(b1_hash)
        b1_msg = client.block_request(b1_block_info.blockInfo.blockHash, validator1)
        assert rust_block_hash(b1_msg) == b1_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        # Sibling b1p: same body, +1 byte in block.extraBytes, V1-signed.
        # See test_slash_ignorable_equivocation docstring for why
        # block.extraBytes is the safe mutation site (not
        # header.timestamp — which the closeBlock system deploy reads
        # via rho:block:data, causing the replay to diverge from the
        # recorded deployLog and surface as InvalidTransaction).
        b1p = BlockMessage()
        b1p.CopyFrom(b1_msg)
        b1p.extraBytes = b1_msg.extraBytes + b'\x00'  # pylint: disable=maybe-no-member
        b1p_hash = rust_block_hash(b1p)
        b1p.blockHash = b1p_hash
        b1p.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(b1p_hash)

        # child_of_b1p: V2-signed block naming b1p as parent. Shipped
        # first so v2 buffers it pending the missing-dep b1p, flipping
        # requested_as_dependency(b1p) to true. The child is buffered
        # by block_processor before any validator runs, so its body
        # never reaches replay; the timestamp+2 on the child is only
        # to keep header.timestamp strictly monotonic if validation
        # ever did run (it won't reach that far).
        child = BlockMessage()
        child.CopyFrom(b1_msg)
        child.sender = BONDED_VALIDATOR_KEY_2.get_public_key().to_bytes()
        child.seqNum = 1
        child.body.state.blockNumber = 2  # pylint: disable=maybe-no-member
        child.ClearField("justifications")
        child.justifications.extend([  # pylint: disable=maybe-no-member
            Justification(
                validator=BONDED_VALIDATOR_KEY_1.get_public_key().to_bytes(),
                latestBlockHash=b1p_hash,
            ),
            Justification(
                validator=BONDED_VALIDATOR_KEY_2.get_public_key().to_bytes(),
                latestBlockHash=genesis_hash_bytes,
            ),
        ])
        child.header.ClearField("parentsHashList")  # pylint: disable=maybe-no-member
        child.header.parentsHashList.append(b1p_hash)  # pylint: disable=maybe-no-member
        child.header.timestamp = b1_msg.header.timestamp + 2  # pylint: disable=maybe-no-member
        child_hash = rust_block_hash(child)
        child.sig = BONDED_VALIDATOR_KEY_2.sign_block_hash(child_hash)
        child.blockHash = child_hash

        client.send_block(child, validator2)

        # Wait for v2 to register b1p as a missing dependency on the
        # buffered child, ensuring requested_as_dependency(b1p) == true
        # before we ship b1p. The block_processor's "waiting on missing
        # dependencies" line is logged at DEBUG (block_processor.rs:606)
        # and is not visible at the deployed INFO log level. Instead we
        # wait for the block_retriever's INFO-level
        # ``Adding <hash> hash to RequestedBlocks because of
        # MissingDependencyRequested`` line (block_retriever.rs:809
        # via ``AdmitHashStatus::NewRequestAdded``), which fires when
        # ``request_missing_dependencies({b1p_hash})`` is called from
        # the block_processor's missing-deps branch.
        wait_for_log_match(validator2, re.compile(
            rf"Adding {b1p_hash.hex()[:10]}.*hash to RequestedBlocks "
            rf"because of MissingDependencyRequested"
        ), timeouts.command)

        client.send_block(b1p, validator2)
        record_invalid = re.compile(
            rf"Recording invalid block {b1p_hash.hex()[:10]}\.\.\. for AdmissibleEquivocation\."
        )
        wait_for_log_match(validator2, record_invalid, timeouts.command)

        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        slashed_block_hash = validator2.propose()
        slashed_block_info = validator2.get_block(slashed_block_hash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0, (
            "v1 (admissible equivocator) must be slashed."
        )


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_neglected_equivocation(provider, timeouts) -> None:
    """v1 equivocates (b1 / b1p). v2 builds a block citing b1p in
    justifications WITHOUT a SlashDeploy. v3 records v2's block as
    invalid and slashes BOTH v1 (for equivocating) and v2 (for
    neglecting) in a single propose round.

    Threat: T-33, Level-2 closure on the equivocation arm. A validator
    that builds atop a known equivocation without acting on it is
    itself slashable; this is the "neglect of a slashable offense"
    rule that makes the protocol Byzantine-resistant past the
    single-attacker threshold.

    Wire-level variant carve-out — the recorded variant alternates
    between :class:`NeglectedInvalidBlock` and
    :class:`InvalidTransaction` depending on the order of validator
    execution inside ``dispatch_validate`` (validation_dispatcher.rs).
    ``Validate::neglected_invalid_block`` runs strictly before
    ``check_neglected_equivocations_with_update``, and
    ``validate_block_checkpoint`` runs before
    ``neglected_invalid_block``. v2's "neglect" block copies b1's
    ``post_state_hash`` (via :func:`BlockMessage.CopyFrom`) but the
    fresh v2-signed deploy replays to a different post-state, so
    checkpoint divergence is the typical trip:

    * If checkpoint diverges first → :class:`InvalidTransaction`.
    * If for some reason it doesn't → :class:`NeglectedInvalidBlock`
      (b1p is in v3's DAG flagged Invalid, b1 cites b1p, v1 still
      bonded > 0, no slash deploy in v2's block).

    The pure :class:`NeglectedEquivocation` variant is wire-level
    unreachable — it requires transitive citation through another
    block, which cannot be assembled from a single ``send_block``.
    The semantic outcome (v1=0 AND v2=0 in v3's slash propose) is
    identical under either variant; that is what the test asserts.
    This carve-out mirrors the pattern in
    :func:`test_slash_invalid_validator_approve_evil_block` and
    :func:`test_slash_GHOST_disobeyed`.

    In-tree analog:
    ``casper/tests/slashing/uc_04_neglect_two_level.rs`` (which uses a
    test-only helper to construct self-consistent post-states and
    therefore CAN reach the pure :class:`NeglectedEquivocation`
    variant in-process).
    """
    with _slash_shard(provider, timeouts, three_validators=True) as (shard, validator1, validator2, client):
        validator3 = shard.node("validator3")

        genesis_block = validator3.get_blocks(2)[0]
        genesis_hash_bytes = bytes.fromhex(genesis_block.blockHash)

        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        b1_hash = validator1.propose()
        wait_for_block_visible(validator2, b1_hash, timeouts.node_startup)
        wait_for_block_visible(validator3, b1_hash, timeouts.node_startup)

        b1_block_info = validator1.get_block(b1_hash)
        b1_msg = client.block_request(b1_block_info.blockInfo.blockHash, validator1)
        assert rust_block_hash(b1_msg) == b1_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        # Sibling b1p (V1-signed equivocation). Mutate block.extraBytes
        # instead of header.timestamp — see
        # test_slash_ignorable_equivocation docstring for the
        # closeBlock-replay rationale.
        b1p = BlockMessage()
        b1p.CopyFrom(b1_msg)
        b1p.extraBytes = b1_msg.extraBytes + b'\x00'  # pylint: disable=maybe-no-member
        b1p_hash = rust_block_hash(b1p)
        b1p.blockHash = b1p_hash
        b1p.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(b1p_hash)

        # Ship b1p to both v2 AND v3 so both record IgnorableEquivocation
        # (and both have b1p in their DAGs flagged Invalid). v3 must have
        # b1p before the neglect block arrives or the missing-dependency
        # buffer logic stalls the test.
        client.send_block(b1p, validator2)
        client.send_block(b1p, validator3)
        for v in (validator2, validator3):
            wait_for_log_match(v, re.compile(
                rf"Recording invalid block {b1p_hash.hex()[:10]}\.\.\. for IgnorableEquivocation\."
            ), timeouts.command)

        # v2's "neglect" block: builds atop b1, cites b1p in
        # justifications, carries no SlashDeploy. v2 won't propose this
        # naturally (the proposer's prepare_slashing_deploys would graft a
        # slash for v1 onto it), so we forge it directly on the wire.
        # post_state_hash is copied from b1; v2's fresh deploy will
        # replay to a divergent post-state on v3 → InvalidTransaction
        # (or NeglectedInvalidBlock if for some reason checkpoint passes).
        neglect = BlockMessage()
        neglect.CopyFrom(b1_msg)
        neglect.sender = BONDED_VALIDATOR_KEY_2.get_public_key().to_bytes()
        neglect.seqNum = 1
        neglect.body.state.blockNumber = 2  # pylint: disable=maybe-no-member
        neglect.ClearField("justifications")
        neglect.justifications.extend([  # pylint: disable=maybe-no-member
            Justification(
                validator=BONDED_VALIDATOR_KEY_1.get_public_key().to_bytes(),
                latestBlockHash=b1p_hash,
            ),
            Justification(
                validator=BONDED_VALIDATOR_KEY_2.get_public_key().to_bytes(),
                latestBlockHash=genesis_hash_bytes,
            ),
            Justification(
                validator=BONDED_VALIDATOR_KEY_3.get_public_key().to_bytes(),
                latestBlockHash=genesis_hash_bytes,
            ),
        ])
        neglect_deploy = create_deploy_data(
            BONDED_VALIDATOR_KEY_2, HELLO_CONTRACT, 1, 1000000, shard_id='root',
        )
        neglect.body.deploys[0].deploy.CopyFrom(neglect_deploy)  # pylint: disable=maybe-no-member
        neglect.header.ClearField("parentsHashList")  # pylint: disable=maybe-no-member
        neglect.header.parentsHashList.append(bytes.fromhex(b1_hash))  # pylint: disable=maybe-no-member
        neglect.header.timestamp = b1_msg.header.timestamp + 2  # pylint: disable=maybe-no-member
        neglect_hash = rust_block_hash(neglect)
        neglect.sig = BONDED_VALIDATOR_KEY_2.sign_block_hash(neglect_hash)
        neglect.blockHash = neglect_hash

        client.send_block(neglect, validator3)

        # Variant alternation absorbed: either checkpoint or
        # neglected_invalid_block fires; both lead to the Level-2 slash
        # outcome. See test docstring.
        wait_for_log_match(validator3, re.compile(
            rf"Recording invalid block {neglect_hash.hex()[:10]}\.\.\. for "
            rf"(NeglectedInvalidBlock|InvalidTransaction)\."
        ), timeouts.command)

        validator3.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_3)
        slashed_blockhash = validator3.propose()
        slashed_block_info = validator3.get_block(slashed_blockhash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0, (
            "v1 (equivocator) must be slashed"
        )
        assert bonds_validators[BONDED_VALIDATOR_KEY_2.get_public_key().to_hex()] == 0, (
            "v2 (neglecter) must be slashed in the same propose round"
        )


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_late_released_equivocation(provider, timeouts) -> None:
    """v1 proposes b1, withholds the equivocating sibling b1p. v2
    proposes 4 blocks atop b1, advancing the chain. v1 then releases
    b1p; v2 detects the equivocation and slashes v1.

    Threat: T-36 §5.A.5 — late-released equivocation evidence within
    the retention window. The protocol cannot let an attacker erase
    their offense by hiding the second branch until "the moment passes";
    the detector must fire whenever the equivocating block arrives,
    independent of when it was signed.

    Wire path:

    1. v1 proposes b1 honestly; v2 sees it.
    2. Test pre-builds (but does NOT ship) the sibling ``b1p`` using
       the timestamp-+1 forge.
    3. v2 proposes 4 blocks atop b1. v1 stays silent
       (``heartbeat=False`` + no explicit deploy/propose by v1). v1's
       latest-message in v2's DAG remains b1 throughout.
    4. Test releases b1p via :func:`client.send_block`.
    5. v2's pipeline: ``cur_senders_block(v1) == b1``; b1p's
       justifications match b1's justifications (both point at genesis
       for v1-self and v2 since they were crafted together); so
       ``justification_regressions`` finds ``new_lms == cur_lms`` and
       passes. ``check_equivocations`` finds
       ``creator_just(b1p) == genesis != latest_message(v1) == b1`` →
       equivocation; ``requested_as_dependency == false`` →
       :class:`IgnorableEquivocation`.

    Epoch boundary — the test caps v2's intermediate proposes at 4 so
    block_number progression stays b1=1, v2_*=2..5, slash propose at
    6. With ``epoch_length=10`` (docker default) the entire test sits
    in epoch 0; evidence_epoch == current_epoch → the slash deploy
    authorization predicate accepts evidence on the receive side, and
    ``authorized_slash_candidates`` proposer-side does not filter v1
    out.

    Variant note: this test asserts the recorded variant is
    :class:`IgnorableEquivocation` (and not :class:`AdmissibleEquivocation`)
    because we do not seed v2's casper buffer with a child-of-b1p
    block. The semantic outcome (v1 slashed) is what T-36 actually
    defends; the variant label distinguishes this test from H1.
    """
    with _slash_shard(provider, timeouts) as (_shard, validator1, validator2, client):
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        b1_hash = validator1.propose()
        wait_for_block_visible(validator2, b1_hash, timeouts.node_startup)

        b1_block_info = validator1.get_block(b1_hash)
        b1_msg = client.block_request(b1_block_info.blockInfo.blockHash, validator1)
        assert rust_block_hash(b1_msg) == b1_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        # Pre-build b1p; DO NOT release yet. Mutate block.extraBytes
        # (not header.timestamp — see
        # test_slash_ignorable_equivocation for the closeBlock replay
        # rationale).
        b1p = BlockMessage()
        b1p.CopyFrom(b1_msg)
        b1p.extraBytes = b1_msg.extraBytes + b'\x00'  # pylint: disable=maybe-no-member
        b1p_hash = rust_block_hash(b1p)
        b1p.blockHash = b1p_hash
        b1p.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(b1p_hash)

        # Advance the chain: v2 proposes 4 blocks atop b1. v1 stays
        # silent so v1's latest-message in v2's DAG remains b1.
        # block_number progression: b1=1, v2_*=2..5. Slash propose
        # later lands at 6, safely inside epoch 0 (epoch_length=10).
        for _ in range(4):
            validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
            v2_blockhash = validator2.propose()
            wait_for_block_visible(validator1, v2_blockhash, timeouts.node_startup)

        # Release the withheld equivocating block.
        client.send_block(b1p, validator2)
        record_invalid = re.compile(
            rf"Recording invalid block {b1p_hash.hex()[:10]}\.\.\. for IgnorableEquivocation\."
        )
        wait_for_log_match(validator2, record_invalid, timeouts.command)

        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        slashed_block_hash = validator2.propose()
        slashed_block_info = validator2.get_block(slashed_block_hash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0, (
            "v1 (late-releasing equivocator) must be slashed within the retention window."
        )


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_stale_evidence_rebond(provider, timeouts) -> None:
    """v2 attempts to slash v1 in the current epoch using evidence from
    a previous epoch; v3 records v2's block as
    :class:`UnauthorizedSlashDeploy` and slashes v2 (not v1).

    Threat: T-12, attack-tree A3, bug #15 — rebonding stale evidence.
    The slash-authorization predicate must reject any slash deploy
    whose ``target_activation_epoch`` differs from the receiver's
    current epoch. Pairs with the symmetric proposer-side filter
    (``slashing_authorization.rs:285-287``) which prevents an honest
    proposer from emitting a stale-epoch slash even if the evidence
    is genuine.

    Shard runs with ``--epoch-length=2`` so the chain crosses epoch
    boundaries in 3 natural proposes instead of 10. Block-number /
    epoch mapping under that override (floor division):

    * 0..1 → epoch 0
    * 2..3 → epoch 1
    * 4..5 → epoch 2

    Phase trace:

    1. v1 proposes block A at block_number=1 (epoch 0); v2 and v3
       see it.
    2. v1 forges ``evil`` from A by setting
       ``body.state.blockNumber = 0``. The block-number validator
       computes ``max_parent_number + 1 == 1 ≠ 0`` and trips
       :class:`InvalidBlockNumber`. v2 and v3 record it. Evidence
       lives at block_number=0 → epoch 0.
    3. v2 proposes naturally at block_number=2 (epoch 1). The
       proposer-side ``authorized_slash_candidates`` filter rejects
       v1's epoch-0 evidence (``evidence_epoch != current_epoch``),
       so v2's block does NOT include a slash for v1. v1 stays bonded.
    4. v3 proposes naturally at block_number=3 (epoch 1). Same logic
       — v3's block does not include a slash for v1.
    5. v2 proposes naturally at block_number=4 (epoch 2). The test
       takes this block and forges a slash deploy onto it:
       ``targetActivationEpoch = 0`` (stale; matches the evidence's
       actual epoch). v3 processes the forged block:
       :func:`validate_received_slash_deploys` rule #2 trips at
       ``target_activation_epoch (0) != current_epoch (2)`` →
       :class:`SlashAuthError::EpochMismatch` → v2's block recorded
       as :class:`UnauthorizedSlashDeploy`.
    6. v3 proposes the final slash response. Its
       ``authorized_slash_candidates`` includes the forged block
       (current-epoch invalid, V2 as offender) but excludes v1's
       evil block (stale evidence). Bonds: v2 → 0, v1 → 100.

    In-tree analogs:
    ``casper/tests/slashing/slash_authorization_regressions.rs::
    received_stale_slash_deploy_is_rejected_before_replay`` (receive
    side) and ``proposer_skips_stale_evidence`` (proposer side, same
    file). This test pins both predicates against the deployed
    binary.
    """
    with _slash_shard(
        provider, timeouts,
        three_validators=True,
        global_cli_options={"--epoch-length": "2"},
    ) as (shard, validator1, validator2, client):
        validator3 = shard.node("validator3")

        # Phase 1 — v1 forges an InvalidBlockNumber block in epoch 0.
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        v1_a_hash = validator1.propose()
        wait_for_block_visible(validator2, v1_a_hash, timeouts.node_startup)
        wait_for_block_visible(validator3, v1_a_hash, timeouts.node_startup)

        v1_a_block_info = validator1.get_block(v1_a_hash)
        v1_a_msg = client.block_request(v1_a_block_info.blockInfo.blockHash, validator1)
        assert rust_block_hash(v1_a_msg) == v1_a_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        evil = BlockMessage()
        evil.CopyFrom(v1_a_msg)
        evil.body.state.blockNumber = 0  # pylint: disable=maybe-no-member
        evil.header.timestamp = v1_a_msg.header.timestamp + 1  # pylint: disable=maybe-no-member
        evil_hash = rust_block_hash(evil)
        evil.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(evil_hash)
        evil.blockHash = evil_hash
        client.send_block(evil, validator2)
        client.send_block(evil, validator3)
        for v in (validator2, validator3):
            wait_for_log_match(v, re.compile(
                rf"Recording invalid block {evil_hash.hex()[:10]}\.\.\. for InvalidBlockNumber\."
            ), timeouts.command)

        # Phase 2 — advance into epoch ≥1 with two natural proposes.
        # Neither v2 nor v3 includes a slash for v1 here because the
        # proposer-side authorized_slash_candidates filter rejects
        # epoch-0 evidence against the current (epoch 1) chain.
        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        v2_hash = validator2.propose()
        wait_for_block_visible(validator3, v2_hash, timeouts.node_startup)
        validator3.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_3)
        v3_hash = validator3.propose()
        wait_for_block_visible(validator2, v3_hash, timeouts.node_startup)

        # Phase 3 — v2 forges a stale-evidence slash deploy onto its
        # next natural propose. Current epoch ≥ 2; evidence is at
        # epoch 0; targetActivationEpoch lies about staleness by
        # claiming 0.
        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        v2_b_hash = validator2.propose()
        wait_for_block_visible(validator3, v2_b_hash, timeouts.node_startup)
        v2_b_block_info = validator2.get_block(v2_b_hash)
        v2_b_msg = client.block_request(v2_b_block_info.blockInfo.blockHash, validator2)
        assert rust_block_hash(v2_b_msg) == v2_b_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        forged_slash = ProcessedSystemDeployProto(
            systemDeploy=SystemDeployDataProto(
                slashSystemDeploy=SlashSystemDeployDataProto(
                    invalidBlockHash=evil_hash,
                    issuerPublicKey=BONDED_VALIDATOR_KEY_2.get_public_key().to_bytes(),
                    # targetActivationEpoch missing from installed
                    # pyf1r3fly proto; deserializes to proto3 default
                    # (0) on Rust side. With current_epoch ≥ 2 from
                    # --epoch-length=2 and 3 natural proposes, that is
                    # the deliberate stale 0 the test wants — rule #2
                    # of validate_received_slash_deploys trips as
                    # EpochMismatch.
                )
            ),
            deployLog=[],
            errorMsg='',
        )
        forged = BlockMessage()
        forged.CopyFrom(v2_b_msg)
        forged.body.systemDeploys.append(forged_slash)  # pylint: disable=maybe-no-member
        forged.header.timestamp = v2_b_msg.header.timestamp + 1  # pylint: disable=maybe-no-member
        forged_hash = rust_block_hash(forged)
        forged.sig = BONDED_VALIDATOR_KEY_2.sign_block_hash(forged_hash)
        forged.blockHash = forged_hash

        client.send_block(forged, validator3)
        wait_for_log_match(validator3, re.compile(
            rf"Recording invalid block {forged_hash.hex()[:10]}\.\.\. for UnauthorizedSlashDeploy\."
        ), timeouts.command)

        validator3.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_3)
        slashed_blockhash = validator3.propose()
        slashed_block_info = validator3.get_block(slashed_blockhash)
        bonds_validators = {b.validator: b.stake for b in slashed_block_info.blockInfo.bonds}
        assert bonds_validators[BONDED_VALIDATOR_KEY_2.get_public_key().to_hex()] == 0, (
            "v2 (proposer of stale slash) must be slashed"
        )
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 100, (
            "v1's stale-epoch invalid block is filtered by proposer-side "
            "authorized_slash_candidates; v1 must remain bonded"
        )


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_slash_self_correcting_block_admitted(provider, timeouts) -> None:
    """v1 ships an invalid block; v2's natural propose response
    includes a SlashDeploy against v1. v3 admits v2's self-correcting
    block (the bug-#9 widening permits a block whose justifications
    reference an invalid predecessor IF that block also carries a
    slash deploy targeting the offender). v1 is slashed via the slash
    deploy embedded in v2's block.

    Threat: T-34, bug #9 (historical Scala-rejected self-correcting
    blocks → liveness regression). Spec §10.9. Without the widening,
    a validator that observed an invalid block could never propose
    again because every subsequent block would inherit an invalid
    justification and be rejected as
    :class:`NeglectedInvalidBlock` — even though that validator IS
    correctly emitting the slash. The widening is at
    ``validate.rs:1080-1092``:

    .. code-block:: rust

        let neglected_invalid_justification = ...;
        let has_slash_system_deploys = ...;
        if neglected_invalid_justification && !has_slash_system_deploys {
            return Either::Left(Invalid(NeglectedInvalidBlock));
        }

    Reinterpretation note — the task statement's "v1 ships a corrected
    block at the same ``(sender, seqNum)``" framing is structurally
    impossible (two distinct blocks at the same seqNum by the same
    sender constitute equivocation, surfaced by
    :func:`check_equivocations`, not the bug-#9 widening). The faithful
    bug-#9 test pattern is: a DIFFERENT validator (v2) issues the
    self-correction by slashing v1. v2's block "corrects" the chain
    even though its justifications may reference v1's invalid block.

    In-tree analog: ``casper/tests/slashing/uc_23_self_correcting.rs``
    (in-process, uses a test-only helper to construct self-consistent
    post-states). This Docker test asserts the post-condition (V3
    admits V2's slashing block + V1 slashed inside it) without
    relying on a specific justification topology, because V2's
    latest-message-for-V1 may point at either V1's honest natural
    block or V1's invalid block depending on DAG ordering; either way
    V3 must admit V2's block.

    The test does NOT assert the recorded variant — it asserts the
    behavioural outcome (V3 has V2's block in its canonical DAG with
    V1's bond at 0). If the widening regresses, v3 records V2's
    block as :class:`NeglectedInvalidBlock` and the get_block /
    assertion at the bottom surfaces the failure.
    """
    with _slash_shard(provider, timeouts, three_validators=True) as (shard, validator1, validator2, client):
        validator3 = shard.node("validator3")

        # Phase 1 — V1 forges an InvalidBlockHash (mirrors
        # test_slash_invalid_block_hash's pattern). Both v2 and v3
        # must record the invalid block before v2 proposes its
        # self-correcting response.
        validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
        v1_hash = validator1.propose()
        wait_for_block_visible(validator2, v1_hash, timeouts.node_startup)
        wait_for_block_visible(validator3, v1_hash, timeouts.node_startup)

        v1_block_info = validator1.get_block(v1_hash)
        block_msg = client.block_request(v1_block_info.blockInfo.blockHash, validator1)
        assert rust_block_hash(block_msg) == block_msg.blockHash, (
            "rust_block_hash() shim diverged; see module docstring."
        )

        evil_hash = generate_block_hash()
        block_msg.blockHash = evil_hash
        block_msg.sig = BONDED_VALIDATOR_KEY_1.sign_block_hash(evil_hash)
        block_msg.header.timestamp = int(time.time() * 1000)  # pylint: disable=maybe-no-member

        client.send_block(block_msg, validator2)
        client.send_block(block_msg, validator3)
        for v in (validator2, validator3):
            wait_for_log_match(v, re.compile(
                rf"Recording invalid block {evil_hash.hex()[:10]}\.\.\. for InvalidBlockHash\."
            ), timeouts.command)

        # Phase 2 — V2 proposes naturally. prepare_slashing_deploys
        # (block_creator.rs:287-308) detects V1's invalid block in
        # the DAG with current-epoch evidence and grafts a
        # SlashSystemDeploy onto V2's body.systemDeploys. v2's
        # natural propose IS the self-correcting block.
        validator2.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_2)
        v2_self_correcting_hash = validator2.propose()
        wait_for_block_visible(validator3, v2_self_correcting_hash, timeouts.node_startup)

        v2_block = client.block_request(
            validator2.get_block(v2_self_correcting_hash).blockInfo.blockHash,
            validator2,
        )
        assert is_exist_slash_deploy(v2_block), (
            "v2's natural propose must emit a SlashSystemDeploy against v1; "
            "without it the bug-#9 widening doesn't apply and the test "
            "premise breaks"
        )

        # Positive presence: v3 admitted v2's self-correcting block
        # into its canonical DAG (i.e. v3 did NOT flag it as
        # NeglectedInvalidBlock per the bug-#9 widening). v3's
        # get_block returns the block; bonds reflect the slash
        # applied during v2's checkpoint replay.
        v2_block_on_v3_info = validator3.get_block(v2_self_correcting_hash)
        bonds_validators = {
            b.validator: b.stake
            for b in v2_block_on_v3_info.blockInfo.bonds
        }
        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 0, (
            "v1 must be slashed via the slash deploy embedded in v2's block"
        )
        assert bonds_validators[BONDED_VALIDATOR_KEY_2.get_public_key().to_hex()] == 100, (
            "v2 (the self-correcting validator) must remain bonded — "
            "bug-#9 widening at validate.rs:1080-1092 admits the block"
        )
        assert bonds_validators[BONDED_VALIDATOR_KEY_3.get_public_key().to_hex()] == 100, (
            "v3 (observer) must remain bonded"
        )


@_SKIP_ON_NON_LINUX
@pytest.mark.allow_forbidden_patterns("RecordingInvalidBlock")
def test_no_false_positive_slash_on_propose_imbalance(provider, timeouts) -> None:
    """Regression test for the *safety arm* of T-12PF (proposer
    fairness). The threat model treats proposer fairness as a
    boundary assumption: the protocol's *liveness* depends on
    eventually-fair proposer rotation, but its *safety* does not. The
    Rust binary deliberately has no runtime censorship detector
    because attempting to detect censorship at runtime is the kind of
    thing that introduces wrongful-slash bugs (false-positive on slow
    gossip, network partitions, etc.).

    This test pins the **absence** of over-eager behavioral-pattern
    detectors. If a future regression adds one — e.g., a check that
    fires when a validator stays silent for N rounds, or when one
    proposer dominates the chain, or when a validator's submitted
    deploys aren't included quickly enough — this test catches it
    immediately and points at the new code.

    Scenarios exercised in one shard:

    1. **Proposer dominance.** v1 proposes 5 blocks in a row; v2 and
       v3 stay silent during this window. None should be slashed —
       a dominant proposer is not a slashable offense.
    2. **Validator silence.** v2 never deploys or proposes throughout
       the test. v2 should remain bonded — "inactive validator" is
       not a slashable offense in this protocol.
    3. **Healthy chain recovery.** v3 proposes once at the end to
       confirm the chain is still healthy (not stuck in some
       degenerate state). v3 should not be slashed.

    Assertion: after all of this activity, every validator's bond is
    still 100. The test PASSES iff no over-eager detector exists; it
    FAILS the moment one is added.

    Coverage rationale: see T-12PF discussion at
    ``docs/theory/slashing/slashing-threat-model.md:380-394`` and the
    Docker-coverage carve-out at the top of this file. T-12PF's
    *liveness* arm (eventual slashing under fairness) cannot be
    tested at the wire level because there is no detector to fire;
    its *safety* arm (no wrongful slashing under unfairness) IS
    testable, and that's what this test does.
    """
    with _slash_shard(provider, timeouts, three_validators=True) as (shard, validator1, validator2, client):
        validator3 = shard.node("validator3")

        # Scenario 1 — v1 dominates the propose chain (5 proposes in a
        # row). v2 stays silent throughout (scenario 2). With
        # heartbeat=False, only the explicit propose() calls below
        # produce blocks.
        for _ in range(5):
            validator1.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_1)
            v1_blockhash = validator1.propose()
            wait_for_block_visible(validator2, v1_blockhash, timeouts.node_startup)
            wait_for_block_visible(validator3, v1_blockhash, timeouts.node_startup)

        # Scenario 3 — v3 proposes once to confirm chain health and to
        # produce a non-v1 block from which we can read the canonical
        # bonds view.
        validator3.deploy_string(HELLO_CONTRACT, BONDED_VALIDATOR_KEY_3)
        v3_blockhash = validator3.propose()
        wait_for_block_visible(validator1, v3_blockhash, timeouts.node_startup)
        wait_for_block_visible(validator2, v3_blockhash, timeouts.node_startup)

        # Read bonds from v3's view (most-recent block).
        v3_block_info = validator3.get_block(v3_blockhash)
        bonds_validators = {b.validator: b.stake for b in v3_block_info.blockInfo.bonds}

        assert bonds_validators[BONDED_VALIDATOR_KEY_1.get_public_key().to_hex()] == 100, (
            "v1 (dominant proposer, 5 blocks in a row) must NOT be slashed — "
            "proposer dominance is not a slashable offense under the current "
            "protocol; a regression that introduced an over-eager fairness "
            "detector would fail here."
        )
        assert bonds_validators[BONDED_VALIDATOR_KEY_2.get_public_key().to_hex()] == 100, (
            "v2 (silent throughout, no deploys, no proposes) must NOT be "
            "slashed — validator inactivity is not a slashable offense; a "
            "regression that introduced an 'inactive validator' detector "
            "would fail here."
        )
        assert bonds_validators[BONDED_VALIDATOR_KEY_3.get_public_key().to_hex()] == 100, (
            "v3 (single propose at the end) must NOT be slashed"
        )
