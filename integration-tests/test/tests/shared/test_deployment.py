"""
Deploy Lifecycle Integration Tests

Tests for the full deploy lifecycle on a shared shard:
1. Invalid syntax — rejected at API level, pipeline not poisoned
2. Cross-validator deploy lookup — same deploy resolves to same block
3. Exploratory deploy error handling — invalid Rholang returns error, not empty
"""

import logging

import pytest
from f1r3fly.client import F1r3flyClientException
from f1r3fly.cost_accounting import CostAuthorityEvidence
from f1r3fly.crypto import PrivateKey
from f1r3fly.polling import DeployError

from ...infra.assertions import assert_deploy_succeeded
from ...infra.keys import VALIDATOR1_ID
from ...infra.polling import poll_until, wait_for_deploy_finalized, wait_for_deploy_included

pytestmark = pytest.mark.xdist_group("shared")

_UNFUNDED_DEPLOY_KEY = PrivateKey.from_seed(99001)


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

    deploy_id = v1.deploy_string(
        '@"valid-after-invalid"!(42)',
        v1_key,
    )

    block_info = wait_for_deploy_included(v1, deploy_id, timeout=timeouts.deploy_inclusion)
    block_hash = block_info.blockHash

    full_block = v1.get_block(block_hash)
    assert_deploy_succeeded(full_block, deploy_id)
    deploy = next(d for d in full_block.deploys if d.sig == deploy_id)
    evidence = CostAuthorityEvidence.from_processed_deploy(deploy)
    assert evidence.witness.events == ()
    assert evidence.byte_cost > 0
    assert deploy.cost == evidence.byte_cost

    logging.info("Invalid syntax rejected, valid deploy succeeded in block %s", block_hash[:16])


def test_unfunded_deploy_rejected_without_stopping_finalization(shared_shard, timeouts) -> None:
    """State-bound admission rejects an unfunded deploy without changing liveness."""
    v1 = shared_shard.node("validator1")
    baseline = min(
        node.last_finalized_block().blockInfo.blockNumber for node in shared_shard.all_nodes
    )

    deploy_id = v1.deploy_string(
        '@"unfunded-deploy-must-not-run"!(1)',
        _UNFUNDED_DEPLOY_KEY,
    )
    rejection_blocks = {}
    for node in shared_shard.all_nodes:
        with pytest.raises(DeployError, match="terminal state Failed"):
            wait_for_deploy_finalized(
                node,
                deploy_id,
                timeouts.finalization,
                absolute_timeout=timeouts.deploy_finalization_absolute,
            )
        status = node.deploy_status(deploy_id)
        assert status.latestBlockHash, f"{node.name}: funding rejection has no block"
        rejection_blocks[node.name] = status.latestBlockHash.hex()

    assert len(set(rejection_blocks.values())) == 1, (
        f"nodes disagree on funding rejection block: {rejection_blocks}"
    )

    target = baseline + 3
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
