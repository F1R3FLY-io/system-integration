"""Assertion helpers for integration tests.

Par extraction and deploy checking: re-exported from pyf1r3fly.
Shard assertions: test-specific helpers for multi-node agreement checks.
"""
from __future__ import annotations

import time
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


def assert_bonds_map_consistent_across_nodes(
    nodes,
    block_hash: str,
    expected_bonds: dict,
) -> None:
    """Assert every node's view of ``block_hash`` carries the same bonds map.

    The bonds map is part of the block payload — every node that accepted
    the block at validation time must compute the same map. A divergence
    means at least one node's local replay produced a different result
    (the failure mode of the original ``InvalidBondsCache`` bug, where a
    bond block validated on the proposer but the bonds map computed
    differently elsewhere).

    Args:
        nodes: Iterable of Node wrappers to query.
        block_hash: Finalized block hash to check.
        expected_bonds: ``{public_hex: stake}`` the bonds map must match
            exactly on every node. Stake values must match — not just
            key membership.

    Raises:
        AssertionError: with a per-node diff if any node disagrees.
    """
    per_node: dict = {}
    for node in nodes:
        block = node.get_block(block_hash)
        per_node[node.name] = {
            b.validator: b.stake for b in block.blockInfo.bonds
        }
    mismatches = {
        name: bonds for name, bonds in per_node.items() if bonds != expected_bonds
    }
    assert not mismatches, (
        f"Bonds map divergence at block {block_hash[:16]}... "
        f"expected {expected_bonds}; mismatches: {mismatches}"
    )


def assert_block_finalized_on_all_nodes(
    nodes,
    block_hash: str,
    timeout: int = 0,
    interval: float = 2.0,
) -> None:
    """Assert every node has the block AND reports `isFinalized=True`.

    Stricter than `wait_for_block_visible`, which passes for any block in
    the metadata store regardless of validity. A peer that flagged the
    block invalid still returns it from `get_block` but never finalizes it.

    Catches the case where a peer accepted the block at the protocol level
    (it's in their store) but rejected it at validation time (e.g.
    `Invalid(InvalidBondsCache)`). The proposer's block is finalized
    locally; if any peer's view is not, that's the bug.

    By default does NOT poll (timeout=0) — caller is responsible for waiting
    for finalization first via `wait_for_finalized` or `poll_until`. Set
    ``timeout > 0`` to opt into polling for the per-block ``isFinalized``
    field, which can lag the LFB advance by a few seconds in high-contention
    multi-validator scenarios (see TODO §2.1).
    """
    from f1r3fly.client import F1r3flyClientException

    deadline = time.time() + timeout
    not_finalized: dict = {}
    while True:
        not_finalized = {}
        for node in nodes:
            try:
                block = node.get_block(block_hash)
                if not block.blockInfo.isFinalized:
                    not_finalized[node.name] = {
                        "block_number": block.blockInfo.blockNumber,
                        "fault_tolerance": float(block.blockInfo.faultTolerance),
                    }
            except F1r3flyClientException as e:
                # Transient race during high-contention multi-validator scenarios:
                # a node has received the block hash via the propagation layer but
                # hasn't fully indexed it for state-query gRPC calls yet. Treat as
                # "not finalized yet on this node" and let the polling loop retry.
                # Without this catch, a transient indexing race fails the assertion
                # with a confusing error message that masks the real consensus state.
                msg = str(e)
                if "received but not added yet" in msg or "not added yet" in msg:
                    not_finalized[node.name] = {
                        "block_number": None,
                        "fault_tolerance": None,
                        "transient_error": "received but not added yet",
                    }
                else:
                    raise
        if not not_finalized or time.time() >= deadline:
            break
        time.sleep(interval)
    assert not not_finalized, (
        f"Block {block_hash[:16]}... is not finalized on "
        f"{len(not_finalized)} node(s) after {timeout}s: {not_finalized}"
    )
