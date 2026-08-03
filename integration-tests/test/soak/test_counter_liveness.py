"""Sequential counter state and node-liveness soak test."""

from __future__ import annotations

import logging
import os
import statistics
import time
from typing import Dict

import pytest
from f1r3fly.crypto import PrivateKey
from f1r3fly.par import par_as_int, par_as_uri

from ..infra.config import ShardConfig
from ..infra.keys import VALIDATOR1_ID, VALIDATOR2_ID, VALIDATOR3_ID
from ..infra.polling import wait_for_deploy_finalized, wait_for_deploy_included
from ..infra.shard import Shard

pytestmark = pytest.mark.xdist_group("soak")

COUNTER_CONTRACT = "resources/counter/counter.rho"
DEFAULT_ITERATIONS = 1_000
CHECKPOINT_INTERVAL = 100
INCLUSION_TIMEOUT_SECONDS = 120
FINALIZATION_TIMEOUT_SECONDS = 600
PHLO_LIMIT = 100_000_000
PHLO_PRICE = 1
DEPLOYER_BALANCE = 50_000_000_000_000_000
DEPLOYER_KEY = PrivateKey.from_seed(93_001)


def _iteration_count() -> int:
    raw = os.environ.get("F1R3FLY_COUNTER_ITERATIONS", str(DEFAULT_ITERATIONS))
    try:
        count = int(raw)
    except ValueError as exc:
        raise ValueError("F1R3FLY_COUNTER_ITERATIONS must be a positive integer") from exc
    if count <= 0:
        raise ValueError("F1R3FLY_COUNTER_ITERATIONS must be a positive integer")
    return count


def _increment_term(uri: str) -> str:
    return f"""
new deployId(`rho:system:deployId`),
    lookup(`rho:registry:lookup`),
    ctrlCh,
    ret
in {{
  lookup!(`{uri}`, *ctrlCh) |
  for (ctrl <- ctrlCh) {{
    ctrl!("inc", *ret) |
    for (@value <- ret) {{
      deployId!(value)
    }}
  }}
}}
"""


def _wait_finalized_on_every_node(
    nodes,
    deploy_id: str,
    timeout: int,
    label: str,
) -> Dict[str, object]:
    statuses: Dict[str, object] = {}
    for node in nodes:
        try:
            statuses[node.name] = wait_for_deploy_finalized(node, deploy_id, timeout)
        except Exception as exc:
            pytest.fail(
                f"{label}: deploy {deploy_id[:24]} did not finalize on "
                f"{node.name}: {type(exc).__name__}: {exc}"
            )
    return statuses


def _canonical_hash(node, deploy_id: str, status, inclusion_timeout: int) -> str:
    if status.latestBlockHash:
        return status.latestBlockHash.hex()
    return wait_for_deploy_included(node, deploy_id, inclusion_timeout).blockHash


def _read_counter(readonly, uri: str, block_hash: str, label: str) -> int:
    results = readonly.registry_query(uri, "get", param=None, block_hash=block_hash)
    assert len(results) == 1, (
        f"{label}: exploratory counter query at {block_hash[:16]} returned "
        f"{len(results)} results, expected exactly one"
    )
    try:
        return par_as_int(results[0])
    except ValueError as exc:
        raise AssertionError(
            f"{label}: exploratory counter query did not return an integer: {results[0]}"
        ) from exc


def _checkpoint(nodes, completed: int, total: int) -> None:
    stopped = [node.name for node in nodes if not node.is_running()]
    assert not stopped, f"counter checkpoint {completed}/{total}: stopped nodes: {stopped}"
    lfb_heights = {node.name: node.last_finalized_block().blockInfo.blockNumber for node in nodes}
    logging.info(
        "counter checkpoint %d/%d: all nodes running; LFB heights=%s",
        completed,
        total,
        lfb_heights,
    )


@pytest.mark.timeout(43_200)
def test_counter_state_survives_1000_finalized_increments(provider, timeouts) -> None:
    """Finalize and verify one counter increment at a time for 1,000 rounds."""
    iterations = _iteration_count()
    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, 100),
            (VALIDATOR2_ID, 100),
            (VALIDATOR3_ID, 100),
        ],
        heartbeat=True,
        include_readonly=True,
        extra_wallets=[
            (
                DEPLOYER_KEY.get_public_key().get_vault_address(),
                DEPLOYER_BALANCE,
            )
        ],
    )
    shard = Shard.create(provider, config, timeouts)
    started = time.monotonic()
    latencies: list[float] = []
    try:
        validator = shard.node("validator1")
        readonly = shard.readonly
        assert readonly is not None
        all_nodes = shard.all_nodes
        inclusion_timeout = timeouts.custom(INCLUSION_TIMEOUT_SECONDS)
        finalization_timeout = timeouts.custom(FINALIZATION_TIMEOUT_SECONDS)

        setup_id = validator.deploy_rho_file(
            COUNTER_CONTRACT,
            DEPLOYER_KEY,
            phlo_limit=PHLO_LIMIT,
            phlo_price=PHLO_PRICE,
        )
        wait_for_deploy_included(validator, setup_id, inclusion_timeout)
        setup_statuses = _wait_finalized_on_every_node(
            all_nodes,
            setup_id,
            finalization_timeout,
            "counter setup",
        )
        setup_hash = _canonical_hash(
            readonly,
            setup_id,
            setup_statuses[readonly.name],
            inclusion_timeout,
        )
        setup_data = validator.get_deploy_data(setup_id, block_hash=setup_hash)
        assert setup_data is not None, "counter setup returned no deploy data"
        assert len(setup_data.par) == 1, (
            f"counter setup returned {len(setup_data.par)} deploy-data values, expected one"
        )
        counter_uri = par_as_uri(setup_data.par[0])
        assert counter_uri.startswith("rho:id:"), (
            f"counter setup returned invalid registry URI: {counter_uri}"
        )
        initial_value = _read_counter(readonly, counter_uri, setup_hash, "counter setup")
        assert initial_value == 0, f"counter setup value is {initial_value}, expected 0"
        logging.info(
            "counter soak started: uri=%s iterations=%d nodes=%s",
            counter_uri,
            iterations,
            [node.name for node in all_nodes],
        )

        increment_term = _increment_term(counter_uri)
        last_value = initial_value
        for expected in range(1, iterations + 1):
            round_started = time.monotonic()
            deploy_id = validator.deploy_string(
                increment_term,
                DEPLOYER_KEY,
                phlo_limit=PHLO_LIMIT,
                phlo_price=PHLO_PRICE,
            )
            included = wait_for_deploy_included(validator, deploy_id, inclusion_timeout)
            statuses = _wait_finalized_on_every_node(
                all_nodes,
                deploy_id,
                finalization_timeout,
                f"counter round {expected}/{iterations}",
            )
            canonical_hash = _canonical_hash(
                readonly,
                deploy_id,
                statuses[readonly.name],
                inclusion_timeout,
            )
            observed = _read_counter(
                readonly,
                counter_uri,
                canonical_hash,
                f"counter round {expected}/{iterations}",
            )
            assert observed == expected, (
                f"counter round {expected}/{iterations}: expected {expected}, "
                f"observed {observed}; previous value was {last_value}; "
                f"deploy={deploy_id}; inclusion_block={included.blockHash}; "
                f"canonical_block={canonical_hash}"
            )
            last_value = observed
            latency = time.monotonic() - round_started
            latencies.append(latency)
            logging.info(
                "counter round %d/%d passed: value=%d deploy=%s block=#%d latency=%.2fs",
                expected,
                iterations,
                observed,
                deploy_id[:24],
                readonly.get_block(canonical_hash).blockInfo.blockNumber,
                latency,
            )
            if expected % CHECKPOINT_INTERVAL == 0 or expected == iterations:
                _checkpoint(all_nodes, expected, iterations)

        elapsed = time.monotonic() - started
        assert last_value == iterations
        _checkpoint(all_nodes, iterations, iterations)
        logging.info(
            "counter soak passed: rounds=%d final=%d elapsed=%.2fs "
            "latency_min=%.2fs latency_avg=%.2fs latency_max=%.2fs",
            iterations,
            last_value,
            elapsed,
            min(latencies),
            statistics.mean(latencies),
            max(latencies),
        )
    finally:
        shard.destroy()
