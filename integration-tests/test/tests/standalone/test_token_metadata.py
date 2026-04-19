"""
Native Token Metadata Standalone/Custom Tests

Groups B-F: joiner mismatch, config validation, restart drift,
multi-shard isolation, genesis ceremony mismatch. All use standalone
nodes or custom shards with specific token CLI flags.
"""
from __future__ import annotations

import logging

import pytest

from ...infra.config import NodeConfig, ShardConfig
from ...infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ...infra.log_events import find_event, iter_json_events
from ...infra.node import Node
from ...infra.token_metadata import (
    fetch_api_status_token,
    query_token_metadata_all,
)
from ...infra.types import NodeRole

# No file-level xdist_group — most tests create/destroy their own nodes.
# Group B tests share a module-scoped baseline fixture and must stay together.


def _token_cli_options(name=None, symbol=None, decimals=None):
    opts = {}
    if name is not None:
        opts["--native-token-name"] = str(name)
    if symbol is not None:
        opts["--native-token-symbol"] = str(symbol)
    if decimals is not None:
        opts["--native-token-decimals"] = str(decimals)
    return opts


def _standalone_config(name=None, symbol=None, decimals=None, extra_options=None, extra_flags=None):
    opts = _token_cli_options(name, symbol, decimals)
    if extra_options:
        opts.update(extra_options)
    flags = frozenset(extra_flags or set())
    return NodeConfig(role=NodeRole.STANDALONE, cli_options=opts, cli_flags=flags)


# ═══════════════════════════════════════════════════════════════════════
# Group B -- Joiner mismatch against a baseline standalone
# ═══════════════════════════════════════════════════════════════════════

BASELINE_NAME = "TOKEN_A"
BASELINE_SYMBOL = "SYMA"
BASELINE_DECIMALS = 8


@pytest.fixture(scope="module")
def group_b_baseline(provider, timeouts):
    """A single baseline standalone shared by every Group B test."""
    config = _standalone_config(
        BASELINE_NAME, BASELINE_SYMBOL, BASELINE_DECIMALS,
        extra_options={"--required-signatures": "0"},
    )
    handle = provider.create_standalone(config, use_shard_conf=True)
    node = Node(handle=handle, role=NodeRole.STANDALONE)
    logging.info("Group B baseline created: %s/%s/%d",
                 BASELINE_NAME, BASELINE_SYMBOL, BASELINE_DECIMALS)
    yield handle, node
    node.close()
    provider.destroy_standalone(handle)


@pytest.mark.xdist_group("token_metadata_b")
@pytest.mark.parametrize(
    "override,expected_field",
    [
        ({"--native-token-name": "TOKEN_B"}, "native-token-name"),
        ({"--native-token-symbol": "SYMB"}, "native-token-symbol"),
        ({"--native-token-decimals": "10"}, "native-token-decimals"),
    ],
    ids=["name_only", "symbol_only", "decimals_only"],
)
def test_joiner_mismatch_fails_startup(
    provider, timeouts, group_b_baseline, override, expected_field
) -> None:
    """A joiner disagreeing on ONE field logs a mismatch event."""
    baseline_handle, _ = group_b_baseline

    joiner_opts = _token_cli_options(BASELINE_NAME, BASELINE_SYMBOL, BASELINE_DECIMALS)
    joiner_opts.update(override)

    joiner_config = NodeConfig(role=NodeRole.JOINER, cli_options=joiner_opts)
    joiner_handle = provider.add_node(
        shard_network=baseline_handle.network_name,
        node_config=joiner_config,
        bootstrap_handle=baseline_handle,
        wait_running=False,
    )
    joiner_node = Node(handle=joiner_handle, role=NodeRole.JOINER)
    try:
        joiner_handle.wait_for_exit(timeout=timeouts.command)

        event = find_event(
            joiner_node.logs(),
            event="native_token_metadata_mismatch",
        )
        assert event is not None, (
            f"Expected native_token_metadata_mismatch event. "
            f"Exit code={joiner_handle.exit_code()}. Log events:\n"
            + "\n".join(
                repr(e) for e in iter_json_events(joiner_node.logs())
                if "native_token" in (e.get("event") or "")
            )
        )
        mismatched = set(
            f for f in (event.get("mismatched_fields") or "").split(",") if f
        )
        assert expected_field in mismatched, (
            f"Expected {expected_field!r} in mismatched_fields, got {mismatched!r}"
        )
        logging.info("Joiner mismatch detected: override=%s, mismatched=%s",
                     override, mismatched)
    finally:
        joiner_node.close()
        provider.remove_node(joiner_handle)


@pytest.mark.xdist_group("token_metadata_b")
def test_joiner_mismatch_all_three_fields(
    provider, timeouts, group_b_baseline
) -> None:
    """A joiner disagreeing on all three fields has all three reported."""
    baseline_handle, _ = group_b_baseline

    joiner_opts = _token_cli_options("TOKEN_B", "SYMB", 12)
    joiner_config = NodeConfig(role=NodeRole.JOINER, cli_options=joiner_opts)
    joiner_handle = provider.add_node(
        shard_network=baseline_handle.network_name,
        node_config=joiner_config,
        bootstrap_handle=baseline_handle,
        wait_running=False,
    )
    joiner_node = Node(handle=joiner_handle, role=NodeRole.JOINER)
    try:
        joiner_handle.wait_for_exit(timeout=timeouts.command)
        event = find_event(joiner_node.logs(), event="native_token_metadata_mismatch")
        assert event is not None, "Expected native_token_metadata_mismatch event"
        mismatched = set(
            f for f in (event.get("mismatched_fields") or "").split(",") if f
        )
        assert mismatched == {
            "native-token-name",
            "native-token-symbol",
            "native-token-decimals",
        }, f"Expected all three fields mismatched, got {mismatched!r}"
        logging.info("All three fields mismatched as expected: %s", mismatched)
    finally:
        joiner_node.close()
        provider.remove_node(joiner_handle)


@pytest.mark.xdist_group("token_metadata_b")
def test_joiner_matching_config_succeeds(
    provider, timeouts, group_b_baseline
) -> None:
    """Sanity check: a joiner whose config matches baseline reaches Running."""
    baseline_handle, _ = group_b_baseline

    joiner_opts = _token_cli_options(BASELINE_NAME, BASELINE_SYMBOL, BASELINE_DECIMALS)
    joiner_config = NodeConfig(role=NodeRole.JOINER, cli_options=joiner_opts)
    joiner_handle = provider.add_node(
        shard_network=baseline_handle.network_name,
        node_config=joiner_config,
        bootstrap_handle=baseline_handle,
        wait_running=True,
    )
    joiner_node = Node(handle=joiner_handle, role=NodeRole.JOINER)
    try:
        event = find_event(
            joiner_node.logs(),
            event="native_token_metadata_verified",
        )
        assert event is not None, (
            "Expected native_token_metadata_verified event on a joiner with matching config"
        )
        assert event.get("native_token_name") == BASELINE_NAME
        assert event.get("native_token_symbol") == BASELINE_SYMBOL
        assert event.get("native_token_decimals") == BASELINE_DECIMALS
        logging.info("Matching joiner verified: %s/%s/%d",
                     BASELINE_NAME, BASELINE_SYMBOL, BASELINE_DECIMALS)
    finally:
        joiner_node.close()
        provider.remove_node(joiner_handle)


# ═══════════════════════════════════════════════════════════════════════
# Group C -- Config validation / CLI parse errors
# ═══════════════════════════════════════════════════════════════════════


def test_decimals_negative_rejected(provider, timeouts) -> None:
    """--native-token-decimals=-1 is rejected at clap parse time."""
    config = _standalone_config(decimals=-1)
    handle = provider.create_standalone(config, wait_running=False)
    node = Node(handle=handle, role=NodeRole.STANDALONE)
    try:
        exit_code = handle.wait_for_exit(timeout=timeouts.command)
        assert exit_code is not None and exit_code != 0, (
            f"Expected non-zero exit for decimals=-1, got {exit_code}"
        )
        logs = node.logs()
        assert "native-token-decimals" in logs.lower() or "decimals" in logs.lower(), (
            "Expected error message mentioning decimals"
        )
        logging.info("decimals=-1 rejected with exit_code=%d", exit_code)
    finally:
        node.close()
        provider.destroy_standalone(handle)


def test_decimals_above_max_rejected(provider, timeouts) -> None:
    """--native-token-decimals=19 exceeds MAX (18), rejected by clap."""
    config = _standalone_config(decimals=19)
    handle = provider.create_standalone(config, wait_running=False)
    node = Node(handle=handle, role=NodeRole.STANDALONE)
    try:
        exit_code = handle.wait_for_exit(timeout=timeouts.command)
        assert exit_code is not None and exit_code != 0, (
            f"Expected non-zero exit for decimals=19, got {exit_code}"
        )
        logs = node.logs()
        assert "native-token-decimals" in logs.lower() or "decimals" in logs.lower(), (
            "Expected error message mentioning decimals"
        )
        logging.info("decimals=19 rejected with exit_code=%d", exit_code)
    finally:
        node.close()
        provider.destroy_standalone(handle)


def test_empty_string_name_rejected(provider, timeouts) -> None:
    """Empty native-token-name fails config validation at startup."""
    config = _standalone_config(name="", symbol="GOOD", decimals=8)
    handle = provider.create_standalone(config, wait_running=False)
    node = Node(handle=handle, role=NodeRole.STANDALONE)
    try:
        exit_code = handle.wait_for_exit(timeout=timeouts.command)
        assert exit_code is not None and exit_code != 0, (
            f"Expected non-zero exit for empty name, got {exit_code}"
        )
        logs = node.logs()
        assert (
            "native-token-name must be non-empty" in logs
            or "native token config invalid" in logs
            or "a value is required for '--native-token-name" in logs
        ), f"Unexpected failure output:\n{logs[-2000:]}"
        logging.info("Empty name rejected with exit_code=%d", exit_code)
    finally:
        node.close()
        provider.destroy_standalone(handle)


def test_whitespace_only_symbol_rejected(provider, timeouts) -> None:
    """Whitespace-only symbol is rejected the same as empty."""
    config = _standalone_config(name="GOOD", symbol="   ", decimals=8)
    handle = provider.create_standalone(config, wait_running=False)
    node = Node(handle=handle, role=NodeRole.STANDALONE)
    try:
        exit_code = handle.wait_for_exit(timeout=timeouts.command)
        assert exit_code is not None and exit_code != 0, (
            f"Expected non-zero exit for whitespace symbol, got {exit_code}"
        )
        logs = node.logs()
        assert (
            "native-token-symbol must be non-empty" in logs
            or "native token config invalid" in logs
        ), f"Unexpected failure output:\n{logs[-2000:]}"
        logging.info("Whitespace symbol rejected with exit_code=%d", exit_code)
    finally:
        node.close()
        provider.destroy_standalone(handle)


def test_special_characters_in_token_name_round_trip(provider, timeouts) -> None:
    """A name with special chars survives the full round-trip unchanged."""
    weird_name = "F1R3-CAP/v2!"
    weird_symbol = "FC.v2"
    config = _standalone_config(weird_name, weird_symbol, 4)
    handle = provider.create_standalone(config)
    node = Node(handle=handle, role=NodeRole.STANDALONE)
    try:
        api_status = fetch_api_status_token(node.http_url)
        assert api_status.name == weird_name, (
            f"API name '{api_status.name}' != '{weird_name}'"
        )
        assert api_status.symbol == weird_symbol, (
            f"API symbol '{api_status.symbol}' != '{weird_symbol}'"
        )
        assert api_status.decimals == 4, (
            f"API decimals {api_status.decimals} != 4"
        )

        on_chain = query_token_metadata_all(node.grpc_host, node.external_grpc_port)
        assert on_chain.name == weird_name, (
            f"On-chain name '{on_chain.name}' != '{weird_name}'"
        )
        assert on_chain.symbol == weird_symbol, (
            f"On-chain symbol '{on_chain.symbol}' != '{weird_symbol}'"
        )
        assert on_chain.decimals == 4, (
            f"On-chain decimals {on_chain.decimals} != 4"
        )
        logging.info("Special chars round-trip verified: name=%s symbol=%s decimals=%d",
                     weird_name, weird_symbol, 4)
    finally:
        node.close()
        provider.destroy_standalone(handle)


# ═══════════════════════════════════════════════════════════════════════
# Group D -- Restart drift (config change after genesis)
# ═══════════════════════════════════════════════════════════════════════


def test_restart_with_changed_token_config_fails_verification(
    provider, timeouts
) -> None:
    """Restarting with different --native-token-name against the same
    data volume trips the on-chain verification check."""
    volume_name = f"test-{provider.session_id}-drift-test"
    initial_config = _standalone_config("INITIAL", "INI", 4)
    handle = provider.create_standalone(
        initial_config, volume_name=volume_name,
    )
    node = Node(handle=handle, role=NodeRole.STANDALONE)
    try:
        baseline = fetch_api_status_token(node.http_url)
        assert baseline.name == "INITIAL"
        on_chain = query_token_metadata_all(node.grpc_host, node.external_grpc_port)
        assert on_chain.name == "INITIAL"
        logging.info("Initial node started with token INITIAL/INI/4")

        drift_config = _standalone_config("DIFFERENT", "DIF", 4)
        new_handle = provider.recreate_standalone(
            handle, drift_config, wait_running=False,
        )
        new_node = Node(handle=new_handle, role=NodeRole.STANDALONE)

        exit_code = new_handle.wait_for_exit(timeout=timeouts.finalization * 3)
        assert exit_code != 0, (
            f"Expected non-zero exit on token metadata drift, got exit_code={exit_code}"
        )
        mismatch = find_event(new_node.logs(), event="native_token_metadata_mismatch")
        assert mismatch is not None, "Expected native_token_metadata_mismatch event"
        mismatched = set(
            f for f in (mismatch.get("mismatched_fields") or "").split(",") if f
        )
        assert "native-token-name" in mismatched, (
            f"Expected native-token-name in mismatched, got {mismatched}"
        )
        assert "native-token-symbol" in mismatched, (
            f"Expected native-token-symbol in mismatched, got {mismatched}"
        )
        assert "native-token-decimals" not in mismatched, (
            f"Decimals unchanged (4), should not be in mismatched: {mismatched}"
        )
        logging.info("Restart drift detected: mismatched=%s, exit_code=%d",
                     mismatched, exit_code)

        new_node.close()
        provider.destroy_standalone(new_handle)
        handle = None
    finally:
        if handle is not None:
            node.close()
            provider.destroy_standalone(handle)


# ═══════════════════════════════════════════════════════════════════════
# Group E -- Multi-shard isolation
# ═══════════════════════════════════════════════════════════════════════


def test_two_shards_with_different_tokens_dont_interfere(
    provider, timeouts
) -> None:
    """Two concurrent standalones with different tokens each report
    their own values correctly via API and on-chain queries."""
    alpha_config = _standalone_config("ALPHA_TOKEN", "ALPHA", 6)
    beta_config = _standalone_config("BETA_TOKEN", "BETA", 9)

    alpha_handle = provider.create_standalone(alpha_config)
    alpha_node = Node(handle=alpha_handle, role=NodeRole.STANDALONE)
    try:
        beta_handle = provider.create_standalone(beta_config)
        beta_node = Node(handle=beta_handle, role=NodeRole.STANDALONE)
        try:
            alpha_status = fetch_api_status_token(alpha_node.http_url)
            beta_status = fetch_api_status_token(beta_node.http_url)

            assert alpha_status.name == "ALPHA_TOKEN"
            assert alpha_status.symbol == "ALPHA"
            assert alpha_status.decimals == 6

            assert beta_status.name == "BETA_TOKEN"
            assert beta_status.symbol == "BETA"
            assert beta_status.decimals == 9

            alpha_on_chain = query_token_metadata_all(
                alpha_node.grpc_host, alpha_node.external_grpc_port,
            )
            beta_on_chain = query_token_metadata_all(
                beta_node.grpc_host, beta_node.external_grpc_port,
            )
            assert alpha_on_chain.name == "ALPHA_TOKEN"
            assert alpha_on_chain.symbol == "ALPHA"
            assert alpha_on_chain.decimals == 6

            assert beta_on_chain.name == "BETA_TOKEN"
            assert beta_on_chain.symbol == "BETA"
            assert beta_on_chain.decimals == 9

            logging.info(
                "Multi-shard isolation verified: alpha=%s/%s/%d, beta=%s/%s/%d",
                alpha_on_chain.name, alpha_on_chain.symbol, alpha_on_chain.decimals,
                beta_on_chain.name, beta_on_chain.symbol, beta_on_chain.decimals,
            )
        finally:
            beta_node.close()
            provider.destroy_standalone(beta_handle)
    finally:
        alpha_node.close()
        provider.destroy_standalone(alpha_handle)


# ═══════════════════════════════════════════════════════════════════════
# Group F -- Genesis ceremony mismatch
# ═══════════════════════════════════════════════════════════════════════


def test_genesis_validator_with_wrong_token_blocks_ceremony(
    provider, timeouts
) -> None:
    """Two genesis validators with mismatched tokens refuse to sign,
    stalling the ceremony so no node reaches Running."""
    from ...infra.polling import _RUNNING_MARKER

    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
        ],
        heartbeat=True,
        global_cli_options=_token_cli_options("MASTER_TOKEN", "MASTER", 8),
        per_node_cli_options={
            "validator2": _token_cli_options("WRONG", "WRG", 8),
            "validator3": _token_cli_options("WRONG", "WRG", 8),
        },
    )

    handles = provider.create_shard(config, wait_running=False)
    try:
        import time

        deadline = time.time() + timeouts.node_startup
        boot_handle = handles[0]
        master_running = False
        while time.time() < deadline:
            if _RUNNING_MARKER in boot_handle.logs():
                master_running = True
                break
            crashed = [h for h in handles if h.exit_code() is not None]
            if crashed:
                logging.info("Nodes crashed during ceremony: %s",
                             [h.name for h in crashed])
                break
            time.sleep(5)

        assert not master_running, (
            "Ceremony master reached Running state despite two genesis "
            "validators having mismatched token configs."
        )

        rejection_signals = [
            "Mismatch", "mismatch", "does not match", "candidate",
            "Rejecting", "not valid", "Invalid", "invalid",
        ]
        any_rejection = False
        for handle in handles[2:4]:
            logs = handle.logs()
            if any(sig in logs for sig in rejection_signals):
                any_rejection = True
                logging.info("Rejection found in %s logs", handle.name)
                break

        assert any_rejection, (
            "Expected the disagreeing validators to log a rejection of the "
            "candidate genesis block."
        )
        logging.info("Genesis ceremony correctly blocked by token mismatch")
    finally:
        provider.destroy_shard(handles)
