"""
Shard Degradation Test

Production-readiness gate: deploys 150 non-trivial Rholang contracts across
3 validators and asserts the shard maintains acceptable performance throughout.

Monitors all 5 nodes (bootstrap, 3 validators, readonly) for:
- LFB advancement rate (blocks per minute)
- Validator desync (LFB divergence between nodes)
- Finalizer timeout count
- Deploy-to-block inclusion latency
- Deploy finalization latency
- API responsiveness

Strict assertions (all must pass):
1. Zero deploy send failures
2. Zero finalizer timeouts
3. LFB rate must not drop below 50% of initial rate
4. Validator desync must stay under 5 blocks
5. Zero LFB stalls (2+ consecutive batches with no advancement)
6. Sampled deploys included in blocks within 15s
7. Sampled deploy blocks finalized within 30s
8. API latency under 2s

Usage:
    DEFAULT_IMAGE=f1r3fly-rust-node:local poetry run pytest \
        integration-tests/test/test_shard_degradation.py -v -s \
        --log-cli-level=INFO --timeout=3600
"""

import logging
import os
import re
import time
from typing import List, Tuple, Optional

import pytest
from docker.client import DockerClient

from .common import TestingContext
from .conftest import (
    assert_containers_running,
    VALIDATOR1_KEY,
    VALIDATOR2_KEY,
    VALIDATOR3_KEY,
    ALL_CONTAINERS,
)
from .rnode import Node

pytestmark = pytest.mark.xdist_group("shard")

FINALIZER_TIMEOUT_PATTERN = re.compile(
    r"Finalizer run exceeded timeout|finalizer-run exceeded"
)

# ---------------------------------------------------------------------------
# Thresholds (production-readiness criteria)
# ---------------------------------------------------------------------------

TOTAL_DEPLOYS = 150
BATCH_SIZE = 10
DEPLOY_PAUSE_SECS = 1          # 1s between deploys
BATCH_PROPAGATION_SECS = 30    # allow blocks to propagate and finalize
PHLO_LIMIT = 500_000
PHLO_PRICE = 1

MIN_RATE_RATIO = 0.50
MAX_DESYNC_BLOCKS = 5
MAX_STALL_BATCHES = 1
MAX_DEPLOY_INCLUSION_SECS = 15
MAX_DEPLOY_FINALIZATION_SECS = 30
MAX_API_LATENCY_SECS = 2
INCLUSION_CHECK_COUNT = 10


# ---------------------------------------------------------------------------
# Rholang contracts
# ---------------------------------------------------------------------------

def _registry_contract(i: int) -> str:
    return f"""
new register(`rho:registry:insertArbitrary`), rl(`rho:registry:lookup`),
    stdout(`rho:io:stdout`), uriCh, valueCh in {{
  register!(bundle+{{*valueCh}}, *uriCh) |
  valueCh!("registry-payload-{i}") |
  for (@uri <- uriCh) {{
    stdout!(("registered", uri)) |
    rl!(uri, *valueCh) |
    for (@value <- valueCh) {{
      stdout!(("lookup-result", value))
    }}
  }}
}}
"""

def _map_contract(i: int) -> str:
    return f"""
new mapCh, stdout(`rho:io:stdout`) in {{
  mapCh!({{}}) |
  for (@m <- mapCh) {{
    mapCh!(m.set("key-{i}-a", "value-a-{i}").set("key-{i}-b", {i * 100})) |
    for (@m2 <- mapCh) {{
      mapCh!(m2.delete("key-{i}-a")) |
      for (@m3 <- mapCh) {{
        stdout!(("map-final", m3))
      }}
    }}
  }}
}}
"""

def _channel_join_contract(i: int) -> str:
    return f"""
new ch1, ch2, ch3, stdout(`rho:io:stdout`) in {{
  ch1!({i}) | ch2!({i + 1}) | ch3!({i + 2}) |
  for (@a <- ch1; @b <- ch2; @c <- ch3) {{
    stdout!(("join-result", a + b + c)) |
    new result in {{
      result!(a * b + c) |
      for (@r <- result) {{
        stdout!(("computed", r))
      }}
    }}
  }}
}}
"""

def _nested_contract(i: int) -> str:
    return f"""
new outer, stdout(`rho:io:stdout`) in {{
  new inner1, inner2 in {{
    inner1!({{"id": {i}, "type": "alpha"}}) |
    inner2!({{"id": {i}, "type": "beta"}}) |
    for (@data1 <- inner1; @data2 <- inner2) {{
      match (data1, data2) {{
        ({{"id": id1, "type": t1}}, {{"id": id2, "type": t2}}) => {{
          stdout!(("nested-match", id1, t1, id2, t2)) |
          outer!(id1 + id2)
        }}
      }}
    }}
  }} |
  for (@sum <- outer) {{
    stdout!(("outer-received", sum))
  }}
}}
"""

def _recursive_contract(i: int, depth: int = 50) -> str:
    return f"""
new loop, stdout(`rho:io:stdout`) in {{
  contract loop(@n, @acc) = {{
    if (n <= 0) {{
      stdout!(("recursive-done-{i}", acc))
    }} else {{
      loop!(n - 1, acc + n)
    }}
  }} |
  loop!({depth}, 0)
}}
"""

def _set_operations_contract(i: int) -> str:
    return f"""
new stdout(`rho:io:stdout`) in {{
  new setCh in {{
    setCh!(Set({i}, {i+1}, {i+2}, {i+3}, {i+4})) |
    for (@s <- setCh) {{
      stdout!(("set-size", s.size())) |
      setCh!(s.union(Set({i+5}, {i+6}))) |
      for (@s2 <- setCh) {{
        stdout!(("union-size", s2.size())) |
        setCh!(s2.diff(Set({i}, {i+1}))) |
        for (@s3 <- setCh) {{
          stdout!(("diff-result", s3))
        }}
      }}
    }}
  }}
}}
"""

CONTRACT_FACTORIES = [
    _registry_contract,
    _map_contract,
    _channel_join_contract,
    _nested_contract,
    _recursive_contract,
    _set_operations_contract,
]

BRIDGE_PHLO = 500_000_000


def _load_bridge_contract() -> str:
    integration_tests_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(integration_tests_dir, "resources", "bridge.rho")
    with open(path) as f:
        return f.read()


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------

def _count_finalizer_timeouts(nodes: List[Node]) -> dict:
    counts = {}
    for node in nodes:
        node_logs = node.logs()
        matches = FINALIZER_TIMEOUT_PATTERN.findall(node_logs)
        counts[node.name] = len(matches)
    return counts


def _get_lfb_numbers(nodes: List[Node]) -> dict:
    lfbs = {}
    for node in nodes:
        try:
            lfb = node.last_finalized_block()
            lfbs[node.name] = lfb.blockInfo.blockNumber
        except Exception:
            lfbs[node.name] = -1
    return lfbs


def _measure_api_latency(node: Node) -> float:
    start = time.time()
    try:
        node.last_finalized_block()
    except Exception:
        pass
    return time.time() - start


def _check_deploy_lifecycle(
    node: Node, deploy_id: str, inclusion_timeout: float, finalization_timeout: float,
) -> Tuple[Optional[float], Optional[int], Optional[float]]:
    start = time.time()
    block_number = None
    inclusion_time = None
    while time.time() - start < inclusion_timeout:
        try:
            light_block = node.find_deploy(deploy_id)
            block_number = light_block.blockNumber
            inclusion_time = time.time() - start
            break
        except Exception:
            time.sleep(3)

    if block_number is None:
        return None, None, None

    fin_start = time.time()
    while time.time() - fin_start < finalization_timeout:
        try:
            lfb = node.last_finalized_block()
            if lfb.blockInfo.blockNumber >= block_number:
                finalization_time = time.time() - start
                return inclusion_time, block_number, finalization_time
        except Exception:
            pass
        time.sleep(5)

    return inclusion_time, block_number, None


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.timeout(1800)
def test_shard_degradation(
    docker_client: DockerClient,
    testing_context: TestingContext,
    validator1_node: Node,
    validator2_node: Node,
    validator3_node: Node,
    bootstrap_node: Node,
    readonly_node: Node,
) -> None:
    """Deploy 150 non-trivial contracts and assert production-readiness.

    Monitors all 5 nodes (bootstrap, 3 validators, readonly) for degradation.
    Logs batch-by-batch metrics and deploy lifecycle results.
    Fails if any production-readiness assertion is violated.
    """
    assert_containers_running(docker_client, ALL_CONTAINERS)

    validators = [validator1_node, validator2_node, validator3_node]
    keys = [VALIDATOR1_KEY, VALIDATOR2_KEY, VALIDATOR3_KEY]
    all_nodes = [bootstrap_node] + validators + [readonly_node]

    baseline_lfbs = _get_lfb_numbers(all_nodes)
    baseline_timeouts = _count_finalizer_timeouts(all_nodes)
    test_start = time.time()

    logging.info("=" * 70)
    logging.info("SHARD DEGRADATION TEST")
    logging.info("=" * 70)
    logging.info("Deploying %d contracts in batches of %d", TOTAL_DEPLOYS, BATCH_SIZE)
    logging.info("Thresholds: min_rate_ratio=%.0f%%, max_desync=%d, max_stall=%d batches",
                 MIN_RATE_RATIO * 100, MAX_DESYNC_BLOCKS, MAX_STALL_BATCHES)
    logging.info("Baseline LFBs: %s", baseline_lfbs)
    logging.info("-" * 70)

    bridge_contract = _load_bridge_contract()

    deploy_records: List[Tuple[str, float, str, str]] = []
    deploy_failures: List[Tuple[int, str, str]] = []
    batch_reports = []
    initial_lfb_rate: Optional[float] = None
    prev_v1_lfb = baseline_lfbs.get(validator1_node.name, 0)
    consecutive_stalls = 0
    max_consecutive_stalls = 0
    max_desync_seen = 0
    max_api_latency = 0.0

    inclusion_indices = set(
        range(0, TOTAL_DEPLOYS, max(1, TOTAL_DEPLOYS // INCLUSION_CHECK_COUNT))
    )

    for batch_num in range(TOTAL_DEPLOYS // BATCH_SIZE):
        batch_start = time.time()

        for i in range(BATCH_SIZE):
            deploy_index = batch_num * BATCH_SIZE + i
            validator = validators[deploy_index % 3]
            key = keys[deploy_index % 3]

            if deploy_index % 3 == 0:
                # Every 3rd: bridge.rho (complex contract with duplicate channel sends)
                contract = bridge_contract
                contract_type = "bridge"
                phlo = BRIDGE_PHLO
            else:
                factory = CONTRACT_FACTORIES[deploy_index % len(CONTRACT_FACTORIES)]
                contract = factory(deploy_index)
                contract_type = factory.__name__.replace("_contract", "")
                phlo = PHLO_LIMIT

            try:
                deploy_id = validator.deploy_string(
                    contract, key, phlo_limit=phlo, phlo_price=PHLO_PRICE,
                )
                deploy_records.append((deploy_id, time.time(), contract_type, validator.name))
                logging.info(
                    "  [%d/%d] Deployed %s via %s (id=%s)",
                    deploy_index + 1, TOTAL_DEPLOYS,
                    contract_type, validator.name, deploy_id[:16],
                )
            except Exception as e:
                deploy_failures.append((deploy_index, validator.name, str(e)))
                logging.warning(
                    "  [%d/%d] Deploy FAILED via %s: %s",
                    deploy_index + 1, TOTAL_DEPLOYS, validator.name, e,
                )

            time.sleep(DEPLOY_PAUSE_SECS)

        logging.info("  Waiting %ds for batch propagation...", BATCH_PROPAGATION_SECS)
        time.sleep(BATCH_PROPAGATION_SECS)

        current_lfbs = _get_lfb_numbers(all_nodes)
        current_timeouts = _count_finalizer_timeouts(all_nodes)
        api_latency = _measure_api_latency(validator1_node)
        max_api_latency = max(max_api_latency, api_latency)

        v1_lfb = current_lfbs.get(validator1_node.name, 0)
        v1_baseline = baseline_lfbs.get(validator1_node.name, 0)
        lfb_advance = v1_lfb - v1_baseline

        if v1_lfb == prev_v1_lfb:
            consecutive_stalls += 1
        else:
            consecutive_stalls = 0
        max_consecutive_stalls = max(max_consecutive_stalls, consecutive_stalls)
        prev_v1_lfb = v1_lfb

        lfb_values = [v for v in current_lfbs.values() if v >= 0]
        desync = max(lfb_values) - min(lfb_values) if lfb_values else 0
        max_desync_seen = max(max_desync_seen, desync)

        new_timeouts = {
            name: current_timeouts[name] - baseline_timeouts.get(name, 0)
            for name in current_timeouts
        }
        total_new_timeouts = sum(new_timeouts.values())

        elapsed_total = time.time() - test_start
        lfb_rate = (lfb_advance / elapsed_total * 60) if elapsed_total > 0 else 0

        if batch_num == 0 and lfb_rate > 0:
            initial_lfb_rate = lfb_rate

        batch_report = {
            "batch": batch_num + 1,
            "deploys_sent": (batch_num + 1) * BATCH_SIZE,
            "total_elapsed_s": round(elapsed_total, 1),
            "v1_lfb": v1_lfb,
            "lfb_advance": lfb_advance,
            "lfb_rate_per_min": round(lfb_rate, 2),
            "finalizer_timeouts": total_new_timeouts,
            "validator_desync": desync,
            "api_latency_ms": round(api_latency * 1000),
            "consecutive_stalls": consecutive_stalls,
        }
        batch_reports.append(batch_report)

        logging.info(
            "BATCH %d | deploys=%d | elapsed=%.0fs | LFB=#%d (+%d) | "
            "rate=%.1f blk/min | timeouts=%d | desync=%d | api=%dms | stalls=%d",
            batch_report["batch"],
            batch_report["deploys_sent"],
            batch_report["total_elapsed_s"],
            batch_report["v1_lfb"],
            batch_report["lfb_advance"],
            batch_report["lfb_rate_per_min"],
            batch_report["finalizer_timeouts"],
            batch_report["validator_desync"],
            batch_report["api_latency_ms"],
            batch_report["consecutive_stalls"],
        )

    # ---------------------------------------------------------------------------
    # Deploy lifecycle checks
    # ---------------------------------------------------------------------------
    lifecycle_results = []
    for idx, (deploy_id, deploy_time, contract_type, validator_name) in enumerate(deploy_records):
        if idx not in inclusion_indices:
            continue
        logging.info("Checking lifecycle for deploy #%d (%s via %s)...", idx + 1, contract_type, validator_name)
        inclusion_time, block_number, finalization_time = _check_deploy_lifecycle(
            validator1_node, deploy_id,
            MAX_DEPLOY_INCLUSION_SECS, MAX_DEPLOY_FINALIZATION_SECS,
        )
        if inclusion_time is not None and finalization_time is not None:
            logging.info("  Included in %.1fs (block #%d), finalized in %.1fs",
                         inclusion_time, block_number, finalization_time)
        elif inclusion_time is not None:
            logging.warning("  Included in %.1fs (block #%d), NOT finalized within %ds",
                            inclusion_time, block_number, MAX_DEPLOY_FINALIZATION_SECS)
        else:
            logging.warning("  NOT included within %ds", MAX_DEPLOY_INCLUSION_SECS)
        lifecycle_results.append((idx, deploy_id[:16], contract_type, block_number, inclusion_time, finalization_time))

    # ---------------------------------------------------------------------------
    # Report
    # ---------------------------------------------------------------------------
    final_lfbs = _get_lfb_numbers(all_nodes)
    final_timeouts = _count_finalizer_timeouts(all_nodes)
    total_final_timeouts = sum(
        final_timeouts[n] - baseline_timeouts.get(n, 0) for n in final_timeouts
    )
    final_rate = batch_reports[-1]["lfb_rate_per_min"] if batch_reports else 0

    logging.info("")
    logging.info("=" * 70)
    logging.info("RESULTS")
    logging.info("=" * 70)

    logging.info("%-6s %-8s %-8s %-12s %-10s %-8s %-8s %-8s %-8s",
                 "Batch", "Deploys", "Time", "LFB", "Rate", "Timeout", "Desync", "API ms", "Stalls")
    logging.info("-" * 78)
    for r in batch_reports:
        logging.info(
            "%-6d %-8d %-8.0f %-12s %-10.1f %-8d %-8d %-8d %-8d",
            r["batch"], r["deploys_sent"], r["total_elapsed_s"],
            f"#{r['v1_lfb']}(+{r['lfb_advance']})",
            r["lfb_rate_per_min"], r["finalizer_timeouts"],
            r["validator_desync"], r["api_latency_ms"], r["consecutive_stalls"],
        )

    logging.info("")
    logging.info("Deploy lifecycle (sampled %d deploys):", len(lifecycle_results))
    logging.info("  %-6s %-16s %-12s %-8s %-12s %-12s", "#", "Deploy ID", "Type", "Block", "Included", "Finalized")
    logging.info("  " + "-" * 66)
    for idx, did, ctype, bnum, itime, ftime in lifecycle_results:
        inc_str = f"{itime:.1f}s" if itime is not None else "TIMEOUT"
        fin_str = f"{ftime:.1f}s" if ftime is not None else "TIMEOUT"
        blk_str = f"#{bnum}" if bnum is not None else "-"
        logging.info("  %-6d %-16s %-12s %-8s %-12s %-12s", idx + 1, did, ctype, blk_str, inc_str, fin_str)

    logging.info("")
    logging.info("Final LFBs: %s", final_lfbs)
    logging.info("Max API latency: %dms", round(max_api_latency * 1000))
    logging.info("Max consecutive stalls: %d batches", max_consecutive_stalls)
    logging.info("Max validator desync: %d blocks", max_desync_seen)
    logging.info("Initial LFB rate: %.1f blk/min", initial_lfb_rate or 0)
    logging.info("Final LFB rate: %.1f blk/min", final_rate)
    logging.info("=" * 70)

    # ---------------------------------------------------------------------------
    # Assertions
    # ---------------------------------------------------------------------------
    failures = []

    if deploy_failures:
        failures.append(f"Deploy failures: {len(deploy_failures)} deploys failed to send")

    if total_final_timeouts > 0:
        failures.append(f"Finalizer timeouts: {total_final_timeouts} total")

    if initial_lfb_rate and initial_lfb_rate > 0:
        rate_ratio = final_rate / initial_lfb_rate
        if rate_ratio < MIN_RATE_RATIO:
            failures.append(
                f"LFB rate degraded: {final_rate:.1f} blk/min is {rate_ratio:.0%} of "
                f"initial {initial_lfb_rate:.1f} blk/min (threshold: {MIN_RATE_RATIO:.0%})"
            )

    if max_desync_seen > MAX_DESYNC_BLOCKS:
        failures.append(f"Validator desync: {max_desync_seen} blocks (max allowed: {MAX_DESYNC_BLOCKS})")

    if max_consecutive_stalls >= MAX_STALL_BATCHES:
        failures.append(
            f"LFB stalled: {max_consecutive_stalls} consecutive batches with no advancement "
            f"(max allowed: {MAX_STALL_BATCHES})"
        )

    not_included = [r for r in lifecycle_results if r[4] is None]
    if not_included:
        details = [f"#{r[0]+1} ({r[2]})" for r in not_included]
        failures.append(
            f"Deploy inclusion: {len(not_included)}/{len(lifecycle_results)} sampled deploys "
            f"not included within their timeout: {', '.join(details)}"
        )

    included_but_not_finalized = [r for r in lifecycle_results if r[4] is not None and r[5] is None]
    if included_but_not_finalized:
        details = [f"#{r[0]+1} ({r[2]}, block #{r[3]})" for r in included_but_not_finalized]
        failures.append(
            f"Deploy finalization: {len(included_but_not_finalized)}/{len(lifecycle_results)} sampled deploys "
            f"included but not finalized within their timeout: {', '.join(details)}"
        )

    if max_api_latency > MAX_API_LATENCY_SECS:
        failures.append(f"API latency: {max_api_latency*1000:.0f}ms exceeds {MAX_API_LATENCY_SECS*1000}ms threshold")

    if failures:
        failure_msg = "Production readiness FAILED:\n" + "\n".join(f"  - {f}" for f in failures)
        logging.error(failure_msg)
        pytest.fail(failure_msg)

    logging.info("All production readiness checks PASSED.")
