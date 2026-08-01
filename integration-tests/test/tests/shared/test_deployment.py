"""
Deploy Lifecycle Integration Tests

Tests for the full deploy lifecycle on a shared shard:
1. Invalid syntax — rejected at API level, pipeline not poisoned
2. Insufficient phlo — included in block but marked as errored
3. Cross-validator deploy lookup — same deploy resolves to same block
4. Exploratory deploy error handling — invalid Rholang returns error, not empty

Previously, deploying with insufficient phlo triggered NeglectedInvalidBlock
crashes. This was resolved by fixing non-deterministic merge ordering in
the consensus layer (EventLogIndex, DeployChainIndex, ConflictSetMerger)
and adding transient-error recovery in the Proposer.
"""

import logging

import pytest
from f1r3fly.client import F1r3flyClientException

from ...infra.assertions import assert_deploy_errored, assert_deploy_succeeded
from ...infra.keys import VALIDATOR1_ID
from ...infra.polling import poll_until, wait_for_deploy_included

pytestmark = pytest.mark.xdist_group("shared")


def test_deploy_invalid_syntax_rejected(shared_shard, timeouts) -> None:
    """Deploying syntactically invalid Rholang is rejected at the API level.

    After the rejection, deploying a valid contract succeeds normally --
    the prior failure does not poison the deploy pipeline.
    """
    v1 = shared_shard.node("validator1")
    v1_key = VALIDATOR1_ID.private_key()

    # Invalid deploy — parser rejects it
    with pytest.raises(F1r3flyClientException):
        v1.deploy_rho_file(
            rho_file_path="resources/invalid.rho",
            private_key=v1_key,
        )

    # Valid deploy immediately after — must succeed
    deploy_id = v1.deploy_string(
        '@"valid-after-invalid"!(42)',
        v1_key,
        phlo_limit=100_000_000,
        phlo_price=1,
    )

    block_info = wait_for_deploy_included(v1, deploy_id, timeout=timeouts.deploy_inclusion)
    block_hash = block_info.blockHash

    full_block = v1.get_block(block_hash)
    assert_deploy_succeeded(full_block, deploy_id)

    logging.info("Invalid syntax rejected, valid deploy succeeded in block %s", block_hash[:16])


@pytest.mark.allow_forbidden_patterns("ComputationOutOfPhlogistons")
def test_deploy_insufficient_phlo_errored(shared_shard, timeouts) -> None:
    """Deploy with insufficient phlo is included in a block but marked as errored.

    Deploys '@1!(1)' with phlo_limit=10 (too low -- even this minimal
    contract costs ~97 phlo). Heartbeat auto-proposes the block. The deploy
    is in the block with errored=True.

    All nodes must advance LFB by 3+ blocks after the errored deploy,
    proving no NeglectedInvalidBlock crash.
    """
    v1 = shared_shard.node("validator1")

    deploy_id = v1.deploy_string(
        "@1!(1)",
        VALIDATOR1_ID.private_key(),
        phlo_limit=10,
        phlo_price=1,
    )
    logging.info("Deployed with insufficient phlo, deploy_id=%s", deploy_id[:24])

    light_block = wait_for_deploy_included(v1, deploy_id, timeouts.deploy_inclusion)
    block_hash = light_block.blockHash
    logging.info(
        "Deploy found in block %s (blockNumber=%d)",
        block_hash[:16],
        light_block.blockNumber,
    )

    block_info = v1.get_block(block_hash)
    assert_deploy_errored(block_info, deploy_id)

    logging.info("Insufficient phlo deploy correctly marked as errored")

    # All nodes must advance LFB by 3+ blocks — proves no crash
    target = light_block.blockNumber + 3
    for node in shared_shard.all_nodes:
        poll_until(
            predicate=lambda n=node: (
                n.last_finalized_block().blockInfo.blockNumber
                if n.last_finalized_block().blockInfo.blockNumber >= target
                else None
            ),
            timeout=timeouts.finalization * 3,
            interval=5.0,
            description=f"{node.name} LFB >= #{target}",
        )

    logging.info("All nodes advanced LFB past #%d after errored deploy", target)


def test_deploy_lookup_consistent_across_validators(shared_shard, timeouts) -> None:
    """Deploy-to-block lookup returns the same block hash on all validators.

    Deploys on V1, then queries find_deploy on every validator and readonly.
    All must resolve to the same block hash.
    """
    v1 = shared_shard.node("validator1")
    all_nodes = shared_shard.all_nodes

    deploy_id = v1.deploy_string(
        '@"deploy-lookup-test"!(1)',
        VALIDATOR1_ID.private_key(),
        phlo_limit=100_000_000,
        phlo_price=1,
    )

    block_hashes = {}
    for node in all_nodes:
        block = wait_for_deploy_included(node, deploy_id, timeout=timeouts.deploy_inclusion)
        block_hashes[node.name] = block.blockHash
        logging.info(
            "Deploy %s found in block %s on %s",
            deploy_id[:24],
            block.blockHash[:16],
            node.name,
        )

    unique_hashes = set(block_hashes.values())
    assert len(unique_hashes) == 1, (
        f"Deploy {deploy_id[:24]}... resolved to different blocks: {block_hashes}"
    )

    logging.info("Deploy lookup consistent across %d nodes", len(all_nodes))


def test_exploratory_deploy_invalid_syntax_returns_error(shared_shard) -> None:
    """Exploratory deploy with invalid Rholang returns an error response.

    Previously, play_exploratory_deploy silently swallowed parse errors
    and returned empty results. After the fix (PR #484), errors are
    propagated to the client as ExploratoryDeployResponse.Error.

    Also verifies that using Rholang reserved keywords (e.g. ``contract``)
    as variable names is correctly rejected.
    """
    ro = shared_shard.readonly

    # Completely invalid syntax
    with pytest.raises(F1r3flyClientException, match="(?i)pars|syntax|error"):
        ro.exploratory_deploy("this is not valid rholang {{{", "")

    logging.info("Invalid syntax correctly rejected by exploratory deploy")

    # Reserved keyword 'contract' used as variable name
    with pytest.raises(F1r3flyClientException, match="(?i)pars|syntax|error"):
        ro.exploratory_deploy(
            "new ret, lookup(`rho:registry:lookup`), ch in {"
            "  lookup!(`rho:system:pos`, *ch) |"
            '  for (contract <- ch) { contract!("all", *ret) }'
            "}",
            "",
        )

    logging.info("Reserved keyword 'contract' as variable correctly rejected")

    # Valid exploratory deploy still works after errors
    result = ro.exploratory_deploy("new ret in { ret!(42) }", "")
    assert len(result) == 1, f"Valid exploratory deploy should return 1 par, got {len(result)}"

    logging.info("Valid exploratory deploy succeeds after error rejections")
