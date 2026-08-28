import secrets

import pytest
from f1r3fly.cost_accounting import (
    AUTHORITY_ACCOUNTING_PROTOCOL_VERSION,
    BYTE_COST_SCHEDULE_DIGEST,
    BYTE_COST_SCHEDULE_VERSION,
    CapabilityRegistration,
    CostAuthorityEvidence,
    FundingSlotGrant,
)
from f1r3fly.deploy import find_deploy_in_block
from f1r3fly.par import par_as_bool, par_as_int, par_as_tuple

from ...infra.assertions import (
    assert_all_nodes_agree_on_block,
    assert_all_nodes_agree_on_lfb,
)
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.polling import wait_for_deploy_finalized

pytestmark = pytest.mark.xdist_group("shared")


def _finalized_on_every_node(shard, deploy_id, timeout, absolute_timeout):
    occurrence_hashes = {}
    for node in shard.all_nodes:
        status = wait_for_deploy_finalized(
            node,
            deploy_id,
            timeout,
            absolute_timeout=absolute_timeout,
        )
        assert status.latestBlockHash
        occurrence_hashes[node.name] = status.latestBlockHash.hex()
    unique_occurrence_hashes = set(occurrence_hashes.values())
    assert len(unique_occurrence_hashes) == 1, (
        f"Nodes disagree on canonical deploy block for {deploy_id}: {occurrence_hashes}"
    )
    occurrence_hash = next(iter(unique_occurrence_hashes))
    assert_all_nodes_agree_on_block(shard.all_nodes, occurrence_hash, timeout=timeout)
    lfb_hash = assert_all_nodes_agree_on_lfb(shard.all_nodes, timeout=timeout)
    assert_all_nodes_agree_on_block(shard.all_nodes, lfb_hash, timeout=timeout)
    return lfb_hash, occurrence_hash


def test_native_application_cost_accounting_workflows(shared_shard, timeouts) -> None:
    initial_outer_funding = 100_000
    initial_slot_funding = 100_000
    outer_top_up = 25_000
    slot_top_up = 25_000
    v1 = shared_shard.node("validator1")
    v2 = shared_shard.node("validator2")
    v3 = shared_shard.node("validator3")
    readonly = shared_shard.readonly
    v1_key = VALIDATOR1_ID.private_key()
    v2_key = VALIDATOR2_ID.private_key()
    v3_key = VALIDATOR3_ID.private_key()
    suffix = secrets.token_hex(8)
    grant = FundingSlotGrant(
        trigger_channel=f"cost-accounting-trigger-{suffix}",
        slot_address_channel=f"cost-accounting-slot-address-{suffix}",
        completion_channel=f"cost-accounting-complete-{suffix}",
        gateway_public_key=v3_key.get_public_key().to_bytes(),
        outer_address_channel=f"cost-accounting-outer-address-{suffix}",
    )
    continuation = (
        f'new x in {{ x!(0) | for (@0 <- x) {{ @"cost-accounting-ran-{suffix}"!(request) }} }}'
    )

    install_id = v1.funding_slots.install(grant, continuation, v1_key)
    install_hash, _ = _finalized_on_every_node(
        shared_shard,
        install_id,
        timeouts.finalization,
        timeouts.deploy_finalization_absolute,
    )
    outer_address, slot_address = readonly.funding_slots.addresses(
        grant,
        install_hash,
    )
    assert readonly.vault.get_balance(outer_address, install_hash) == 0
    assert readonly.vault.get_balance(slot_address, install_hash) == 0

    source_vault = v2_key.get_public_key().get_vault_address()
    funding_id = v2.funding_slots.fund(
        grant,
        source_vault,
        initial_outer_funding,
        initial_slot_funding,
        v2_key,
        resolved_addresses=(outer_address, slot_address),
    )
    funding_state_hash, funding_occurrence_hash = _finalized_on_every_node(
        shared_shard,
        funding_id,
        timeouts.finalization,
        timeouts.deploy_finalization_absolute,
    )
    funding_result = v2.vault.read_transfer_result(
        funding_id,
        block_hash=funding_occurrence_hash,
    )
    assert funding_result.success, funding_result.reason
    assert readonly.vault.get_balance(outer_address, funding_state_hash) == initial_outer_funding
    assert readonly.vault.get_balance(slot_address, funding_state_hash) == initial_slot_funding

    unauthorized_id = v1.funding_slots.trigger(grant, v1_key, "6")
    unauthorized_hash, _ = _finalized_on_every_node(
        shared_shard,
        unauthorized_id,
        timeouts.finalization,
        timeouts.deploy_finalization_absolute,
    )
    assert readonly.vault.get_balance(outer_address, unauthorized_hash) == initial_outer_funding
    assert readonly.vault.get_balance(slot_address, unauthorized_hash) == initial_slot_funding

    top_up_id = v2.funding_slots.fund(
        grant,
        source_vault,
        outer_top_up,
        slot_top_up,
        v2_key,
        resolved_addresses=(outer_address, slot_address),
    )
    top_up_hash, top_up_occurrence_hash = _finalized_on_every_node(
        shared_shard,
        top_up_id,
        timeouts.finalization,
        timeouts.deploy_finalization_absolute,
    )
    top_up_result = v2.vault.read_transfer_result(
        top_up_id,
        block_hash=top_up_occurrence_hash,
    )
    assert top_up_result.success, top_up_result.reason
    funded_outer_balance = initial_outer_funding + outer_top_up
    funded_slot_balance = initial_slot_funding + slot_top_up
    assert readonly.vault.get_balance(outer_address, top_up_hash) == funded_outer_balance
    assert readonly.vault.get_balance(slot_address, top_up_hash) == funded_slot_balance

    trigger_id = v3.funding_slots.trigger(grant, v3_key, "7")
    trigger_hash, trigger_occurrence_hash = _finalized_on_every_node(
        shared_shard,
        trigger_id,
        timeouts.finalization,
        timeouts.deploy_finalization_absolute,
    )
    remaining_outer_balance = readonly.vault.get_balance(outer_address, trigger_hash)
    remaining_slot_balance = readonly.vault.get_balance(slot_address, trigger_hash)
    assert 0 < remaining_outer_balance < funded_outer_balance
    assert 0 < remaining_slot_balance < funded_slot_balance
    assert (
        readonly.funding_slots.client.read_channel(
            grant.completion_channel,
            trigger_hash,
        )
        == 7
    )
    trigger_block = readonly.get_block(trigger_occurrence_hash)
    trigger_deploy = find_deploy_in_block(trigger_block, trigger_id)
    assert trigger_deploy.cost >= 1
    evidence = CostAuthorityEvidence.from_processed_deploy(trigger_deploy)
    assert evidence.certificate.protocol_version == AUTHORITY_ACCOUNTING_PROTOCOL_VERSION
    assert evidence.certificate.byte_cost_schedule_version == BYTE_COST_SCHEDULE_VERSION
    assert evidence.certificate.byte_cost_schedule_digest == BYTE_COST_SCHEDULE_DIGEST
    assert evidence.pre_state_root == trigger_deploy.preStateHash
    assert evidence.post_state_root == trigger_deploy.postStateHash
    assert evidence.byte_cost <= evidence.byte_cost_bound
    assert trigger_deploy.cost == len(evidence.witness.events) + evidence.byte_cost
    total_execution_burn = sum(evidence.settlement.values()) + sum(
        evidence.byte_settlement.values()
    )
    located_execution_burn = (
        funded_outer_balance
        - remaining_outer_balance
        + funded_slot_balance
        - remaining_slot_balance
    )
    assert 0 < located_execution_burn <= total_execution_burn

    exchange_result = v1.exchange.swap_integer_carriers(
        7,
        11,
        v1_key,
        timeouts.deploy_inclusion,
        timeouts.finalization,
    )
    assert exchange_result.client_carrier == 11
    assert exchange_result.vault_carrier == 7

    registration = CapabilityRegistration(
        from_signature=b"agent-input",
        to_signature=b"agent-output",
        transformer_body="for (@value <- fromCh) { toCh!(value + 1) }",
        uses_bound=1,
    )
    registration_id = v1.capabilities.register(registration, v1_key)
    _, registration_occurrence_hash = _finalized_on_every_node(
        shared_shard,
        registration_id,
        timeouts.finalization,
        timeouts.deploy_finalization_absolute,
    )
    registration_data = v1.capabilities.client.get_data_at_deploy_id(
        registration_id,
        block_hash=registration_occurrence_hash,
    )
    assert registration_data is not None
    registration_result = v1.capabilities.registration_result(registration_data.par[0])
    assert registration_result.success
    assert registration_result.handle

    invocation_id = v3.capabilities.invoke(
        registration_result.handle,
        "41",
        v3_key,
    )
    _, invocation_occurrence_hash = _finalized_on_every_node(
        shared_shard,
        invocation_id,
        timeouts.finalization,
        timeouts.deploy_finalization_absolute,
    )
    invocation_data = v3.capabilities.client.get_data_at_deploy_id(
        invocation_id,
        block_hash=invocation_occurrence_hash,
    )
    assert invocation_data is not None
    invocation_result = par_as_tuple(invocation_data.par[0])
    assert par_as_bool(invocation_result[0])
    assert par_as_int(invocation_result[1]) == 42
