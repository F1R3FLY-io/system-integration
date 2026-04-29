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


def assert_all_nodes_agree_on_lfb(nodes) -> str:
    """Assert all nodes report the same LFB hash. Returns the common hash."""
    lfb_info = {}
    for node in nodes:
        lfb = node.last_finalized_block().blockInfo
        lfb_info[node.name] = (lfb.blockHash, lfb.blockNumber)
    hashes = {h for h, _ in lfb_info.values()}
    assert len(hashes) == 1, (
        f"Nodes disagree on LFB: {lfb_info}"
    )
    return next(iter(hashes))


def assert_contracts_consistent_across_nodes(
    readonly_node,
    contract_queries,
    block_hash: str = "",
) -> dict[str, list]:
    """Query each contract on readonly via exploratory deploy, return results.

    Args:
        readonly_node: Node with exploratory deploy support.
        contract_queries: Iterable of either ``(name, uri, method)`` for
            3-arg bridge-style contracts or ``(name, uri, method, param)``
            where ``param`` is a Rholang expression string or ``None``
            for the 2-arg ``(@method, ret)`` pattern.
        block_hash: Block hash to query against. Empty for latest.

    Returns:
        Dict mapping contract name to Par results list.

    Raises:
        AssertionError: If any query returns no results.
    """
    results = {}
    for entry in contract_queries:
        if len(entry) == 3:
            name, uri, method = entry
            param = "Nil"
        elif len(entry) == 4:
            name, uri, method, param = entry
        else:
            raise ValueError(
                f"contract_queries entry must be 3- or 4-tuple, got {entry!r}"
            )
        pars = readonly_node.registry_query(
            uri, method, param=param, block_hash=block_hash
        )
        assert pars, (
            f"Contract {name} query {method} returned no results "
            f"on {readonly_node.name} at block {block_hash[:16]}"
        )
        results[name] = pars
    return results
