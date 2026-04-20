"""
Native Token Metadata Happy Path Tests

Verifies that the shard's token metadata (parsed from defaults.conf +
rust.conf) is correctly exposed via API, on-chain contract, startup
logs, and is consistent across all nodes.

Expected values are derived from the node_conf fixture (parsed HOCON),
not hardcoded.

On-chain queries are tested via two paths:
- Exploratory deploy on readonly (fast, no blocks)
- Real deploy on one validator (exercises deploy pipeline)
"""

import logging

import pytest

from ...infra.keys import VALIDATOR1_ID
from ...infra.log_events import find_event, iter_json_events
from ...infra.token_metadata import (
    fetch_api_status_token,
    query_token_metadata_all,
    query_token_metadata_name,
    query_token_metadata_symbol,
    query_token_metadata_decimals,
)

pytestmark = pytest.mark.xdist_group("shared")


def test_api_status_returns_configured_token(shared_shard, node_conf) -> None:
    """/api/status reports the configured token metadata on all nodes."""
    for node in shared_shard.all_nodes:
        status = fetch_api_status_token(node.http_url)
        assert status.name == node_conf.native_token_name, (
            f"{node.name}: expected name '{node_conf.native_token_name}', got '{status.name}'"
        )
        assert status.symbol == node_conf.native_token_symbol, (
            f"{node.name}: expected symbol '{node_conf.native_token_symbol}', got '{status.symbol}'"
        )
        assert status.decimals == node_conf.native_token_decimals, (
            f"{node.name}: expected decimals {node_conf.native_token_decimals}, got {status.decimals}"
        )


def test_on_chain_all_exploratory(shared_shard, node_conf) -> None:
    """TokenMetadata!("all", ret) via exploratory deploy matches config."""
    ro = shared_shard.readonly
    on_chain = query_token_metadata_all(ro.grpc_host, ro.external_grpc_port)
    assert on_chain.name == node_conf.native_token_name, (
        f"{ro.name}: on-chain name '{on_chain.name}' != config '{node_conf.native_token_name}'"
    )
    assert on_chain.symbol == node_conf.native_token_symbol, (
        f"{ro.name}: on-chain symbol '{on_chain.symbol}' != config '{node_conf.native_token_symbol}'"
    )
    assert on_chain.decimals == node_conf.native_token_decimals, (
        f"{ro.name}: on-chain decimals {on_chain.decimals} != config {node_conf.native_token_decimals}"
    )
    logging.info("%s (exploratory): name=%s symbol=%s decimals=%d",
                 ro.name, on_chain.name, on_chain.symbol, on_chain.decimals)


def test_on_chain_all_real_deploy(shared_shard, node_conf, timeouts) -> None:
    """TokenMetadata!("all", ret) via real deploy on V1 matches config."""
    from f1r3fly.system_contracts import deploy_query_token_metadata

    node = shared_shard.validators[0]
    key = VALIDATOR1_ID
    on_chain = deploy_query_token_metadata(
        node._external_client(), key.private_key(),
        timeouts.deploy_inclusion, timeouts.finalization,
    )
    assert on_chain.name == node_conf.native_token_name, (
        f"{node.name}: on-chain name '{on_chain.name}' != config '{node_conf.native_token_name}'"
    )
    assert on_chain.symbol == node_conf.native_token_symbol, (
        f"{node.name}: on-chain symbol '{on_chain.symbol}' != config '{node_conf.native_token_symbol}'"
    )
    assert on_chain.decimals == node_conf.native_token_decimals, (
        f"{node.name}: on-chain decimals {on_chain.decimals} != config {node_conf.native_token_decimals}"
    )
    logging.info("%s (deploy): name=%s symbol=%s decimals=%d",
                 node.name, on_chain.name, on_chain.symbol, on_chain.decimals)


def test_on_chain_individual_methods_match_all(shared_shard) -> None:
    """name/symbol/decimals individually equal the all tuple.

    Uses exploratory deploy on the readonly node.
    """
    node = shared_shard.readonly
    host, port = node.grpc_host, node.external_grpc_port
    all_tuple = query_token_metadata_all(host, port)
    name = query_token_metadata_name(host, port)
    symbol = query_token_metadata_symbol(host, port)
    decimals = query_token_metadata_decimals(host, port)
    assert name == all_tuple.name, (
        f"individual name '{name}' != all tuple '{all_tuple.name}'"
    )
    assert symbol == all_tuple.symbol, (
        f"individual symbol '{symbol}' != all tuple '{all_tuple.symbol}'"
    )
    assert decimals == all_tuple.decimals, (
        f"individual decimals {decimals} != all tuple {all_tuple.decimals}"
    )


def test_startup_log_announces_token_metadata(shared_shard, node_conf) -> None:
    """All validator nodes log a structured native_token_metadata_startup event."""
    for node in [shared_shard.boot] + shared_shard.validators:
        event = find_event(
            node.logs(),
            event="native_token_metadata_startup",
        )
        assert event is not None, (
            f"{node.name}: expected native_token_metadata_startup event. "
            f"Log events:\n"
            + "\n".join(
                repr(e) for e in iter_json_events(node.logs())
                if "native_token" in (e.get("event") or "")
            )
        )
        assert event.get("native_token_name") == node_conf.native_token_name, (
            f"{node.name}: log name '{event.get('native_token_name')}' != '{node_conf.native_token_name}'"
        )
        assert event.get("native_token_symbol") == node_conf.native_token_symbol, (
            f"{node.name}: log symbol '{event.get('native_token_symbol')}' != '{node_conf.native_token_symbol}'"
        )
        assert event.get("native_token_decimals") == node_conf.native_token_decimals, (
            f"{node.name}: log decimals {event.get('native_token_decimals')} != {node_conf.native_token_decimals}"
        )
