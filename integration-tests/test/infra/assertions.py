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
