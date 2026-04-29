"""Assertion helpers for integration tests.

Par extraction and deploy checking: re-exported from pyf1r3fly.
Shard assertions: test-specific helpers for multi-node agreement checks.
"""
from __future__ import annotations

from typing import Optional

# Re-export Par extraction from pyf1r3fly
from f1r3fly.par import (
    par_as_string,
    par_as_int,
    par_as_bool,
    par_as_tuple,
    par_as_list,
    par_as_map,
    par_as_uri,
    par_value,
)

# Re-export deploy checking from pyf1r3fly
from f1r3fly.deploy import (
    DeployError,
    find_deploy_in_block,
    check_deploy_not_errored,
    check_deploy_succeeded,
    check_deploy_errored,
)


# ── Test assertion wrappers ────────────────────────────────────────────
#
# These wrap the pyf1r3fly check_* functions with pytest-style assert
# messages. Tests can use either style depending on preference.

def assert_deploy_succeeded(block_info, deploy_id: str) -> None:
    """Assert the deploy is in the block, not errored, and has cost > 0."""
    try:
        check_deploy_succeeded(block_info, deploy_id)
    except DeployError as e:
        raise AssertionError(str(e)) from None


def assert_deploy_errored(
    block_info,
    deploy_id: str,
    error_contains: Optional[str] = None,
) -> None:
    """Assert the deploy is in the block and marked as errored."""
    try:
        check_deploy_errored(block_info, deploy_id, error_contains)
    except DeployError as e:
        raise AssertionError(str(e)) from None


# ── Shard assertions (test-specific, needs multiple nodes) ─────────────

def assert_all_nodes_agree_on_block(nodes, block_hash: str) -> None:
    """Assert every node can retrieve the block and has the same post-state."""
    post_states = {}
    for node in nodes:
        block = node.get_block(block_hash)
        post_states[node.name] = block.blockInfo.postStateHash
    unique = set(post_states.values())
    assert len(unique) == 1, (
        f"Nodes disagree on post-state for block {block_hash[:16]}. "
        f"States: {post_states}"
    )


def assert_block_finalized_on_all_nodes(nodes, block_hash: str) -> None:
    """Assert every node has the block AND reports `isFinalized=True`.

    Stricter than `wait_for_block_visible`, which passes for any block in
    the metadata store regardless of validity. A peer that flagged the
    block invalid still returns it from `get_block` but never finalizes it.

    Catches the case where a peer accepted the block at the protocol level
    (it's in their store) but rejected it at validation time (e.g.
    `Invalid(InvalidBondsCache)`). The proposer's block is finalized
    locally; if any peer's view is not, that's the bug.

    Does NOT poll. Caller is responsible for waiting for finalization
    first (use `wait_for_finalized` or `poll_until` on a reference node).
    """
    not_finalized = {}
    for node in nodes:
        block = node.get_block(block_hash)
        if not block.blockInfo.isFinalized:
            not_finalized[node.name] = {
                "block_number": block.blockInfo.blockNumber,
                "fault_tolerance": float(block.blockInfo.faultTolerance),
            }
    assert not not_finalized, (
        f"Block {block_hash[:16]}... is not finalized on "
        f"{len(not_finalized)} node(s): {not_finalized}"
    )
