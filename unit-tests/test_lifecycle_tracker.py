"""Unit coverage for LifecycleTracker's status sweep.

Pins the properties from the PR #120 review rounds:

- a sweep cycle serves MORE pending deploys than the worker pool has
  threads, with block-number enrichment collapsed to one ``get_block``
  per unique containing block (blocker: enrichment must not serialize
  the sweep at sustained-phase volume),
- finalization is recorded even when block enrichment is slow or broken
  (finalize-first ordering — enrichment is telemetry, never the verdict),
- ``clear()`` racing a sweep leaves no stale writes for the next phase,
- re-homing (``latestBlockHash`` changing between cycles) neither crashes
  nor rewrites the recorded inclusion, and
- a failed block-number lookup is re-resolved on a later cycle with the
  original inclusion timestamp preserved.
"""

import importlib.util
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from f1r3fly.pb.DeployServiceCommon_pb2 import (
    DEPLOY_STATE_EXPIRED,
    DEPLOY_STATE_FAILED,
    DEPLOY_STATE_FINALIZED,
    DEPLOY_STATE_PENDING,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "integration-tests/test/infra/metrics.py"
SPEC = importlib.util.spec_from_file_location("metrics", MODULE_PATH)
assert SPEC and SPEC.loader
metrics = importlib.util.module_from_spec(SPEC)
# Register before exec: the module's dataclasses resolve their postponed
# (string) annotations through sys.modules[__module__].
sys.modules["metrics"] = metrics
SPEC.loader.exec_module(metrics)

TERMINAL = {DEPLOY_STATE_FAILED: "FAILED", DEPLOY_STATE_EXPIRED: "EXPIRED"}
HASH_A = b"\xaa" * 32
HASH_B = b"\xbb" * 32


class _Status:
    def __init__(self, state, latest=b""):
        self.state = state
        self.latestBlockHash = latest


class _StubClient:
    """deploy_finalization_status backed by a dict; values may be
    callables to inject side effects (e.g. clear() mid-sweep)."""

    def __init__(self, statuses):
        self.statuses = statuses

    def deploy_finalization_status(self, deploy_id):
        value = self.statuses[deploy_id]
        return value() if callable(value) else value


class _StubNode:
    name = "stub-node"

    def __init__(self, client, block_number=7, fail_lookups=False):
        self._client = client
        self.block_number = block_number
        self.fail_lookups = fail_lookups
        self.get_block_calls = 0

    def _external_client(self):
        return self._client

    def get_block(self, block_hash):
        del block_hash
        self.get_block_calls += 1
        if self.fail_lookups:
            raise RuntimeError("block store unavailable")
        return SimpleNamespace(blockInfo=SimpleNamespace(blockNumber=self.block_number))


def _record(deploy_id):
    return metrics.DeployRecord(
        deploy_id=deploy_id,
        submit_time=time.time(),
        validator_name="v1",
        phase="unit",
        index=0,
    )


def _tracker_with(statuses, **node_kwargs):
    client = _StubClient(statuses)
    node = _StubNode(client, **node_kwargs)
    tracker = metrics.LifecycleTracker({"v1": node})
    for deploy_id in statuses:
        tracker.track_deploy(_record(deploy_id))
    return tracker, node, client


def _run_cycle(tracker, node, client):
    with ThreadPoolExecutor(max_workers=tracker._SWEEP_WORKERS) as pool:
        return tracker._sweep_cycle(pool, node, client, DEPLOY_STATE_FINALIZED, TERMINAL)


class SweepScalesPastWorkerCount(unittest.TestCase):
    def test_every_pending_deploy_is_visited_in_one_cycle(self):
        # 30 deploys > 8 workers; all finalized in the same block.
        statuses = {f"d{i:02d}": _Status(DEPLOY_STATE_FINALIZED, HASH_A) for i in range(30)}
        tracker, node, client = _tracker_with(statuses)
        pending, errors = _run_cycle(tracker, node, client)
        self.assertEqual((pending, errors), (30, 0))
        results = tracker.get_results()
        self.assertEqual(sum(1 for r in results if r.finalization_time is not None), 30)
        # Enrichment is per unique block, not per deploy.
        self.assertEqual(node.get_block_calls, 1)
        self.assertTrue(all(r.block_number == 7 for r in results))


class FinalizationNeverWaitsOnEnrichment(unittest.TestCase):
    def test_all_state_writes_land_before_the_first_block_lookup(self):
        """The sibling-review blocker on da55af67: a SLOW (not failing)
        get_block must not delay later deploys' finalization writes in
        the same sweep. Pinned structurally: when the FIRST enrichment
        RPC is issued, every finalization of the sweep is already
        recorded — so enrichment latency, however large, delays no
        verdict-relevant write."""
        statuses = {f"d{i:02d}": _Status(DEPLOY_STATE_FINALIZED, HASH_A) for i in range(12)}
        tracker, node, client = _tracker_with(statuses)
        finalized_at_first_lookup = []
        original_get_block = node.get_block

        def observing_get_block(block_hash):
            if not finalized_at_first_lookup:
                with tracker._lock:
                    finalized_at_first_lookup.append(len(tracker._finalization))
            return original_get_block(block_hash)

        node.get_block = observing_get_block
        _run_cycle(tracker, node, client)
        self.assertEqual(finalized_at_first_lookup, [12])

    def test_finalization_recorded_even_when_block_lookup_is_broken(self):
        statuses = {f"d{i}": _Status(DEPLOY_STATE_FINALIZED, HASH_A) for i in range(5)}
        tracker, node, client = _tracker_with(statuses, fail_lookups=True)
        _run_cycle(tracker, node, client)
        results = tracker.get_results()
        self.assertEqual(sum(1 for r in results if r.finalization_time is not None), 5)
        # Enrichment failed → number unknown, verdict unaffected.
        self.assertTrue(all(r.block_number == 0 for r in results))

    def test_failed_lookup_is_reresolved_with_timestamp_preserved(self):
        statuses = {"d0": _Status(DEPLOY_STATE_PENDING, HASH_A)}
        tracker, node, client = _tracker_with(statuses, fail_lookups=True)
        _run_cycle(tracker, node, client)
        first = tracker._inclusion["d0"]
        self.assertEqual(first[0], 0)
        node.fail_lookups = False
        _run_cycle(tracker, node, client)
        second = tracker._inclusion["d0"]
        self.assertEqual(second[0], 7)
        self.assertEqual(second[1], first[1])  # original inclusion time kept


class ClearDuringSweepLeavesNothingStale(unittest.TestCase):
    def test_probe_results_arriving_after_clear_are_dropped(self):
        tracker_ref = {}

        def _clearing_status():
            tracker_ref["t"].clear()
            return _Status(DEPLOY_STATE_FINALIZED, HASH_A)

        statuses = {"d0": _clearing_status}
        statuses.update({f"d{i}": _Status(DEPLOY_STATE_FINALIZED, HASH_A) for i in range(1, 12)})
        tracker, node, client = _tracker_with(statuses)
        tracker_ref["t"] = tracker
        _run_cycle(tracker, node, client)
        # Whatever landed before the clear was wiped by it; whatever
        # arrived after was gated out. Nothing may leak into the next
        # phase's bookkeeping.
        self.assertEqual(tracker._finalization, {})
        self.assertEqual(tracker._inclusion, {})
        self.assertEqual(tracker._terminal, {})
        self.assertEqual(tracker.get_results(), [])


class RehomingIsHarmless(unittest.TestCase):
    def test_latest_block_hash_change_keeps_first_resolution(self):
        statuses = {"d0": _Status(DEPLOY_STATE_PENDING, HASH_A)}
        tracker, node, client = _tracker_with(statuses)
        _run_cycle(tracker, node, client)
        first = tracker._inclusion["d0"]
        self.assertEqual(first[0], 7)
        # Re-homed to a different canonical block, now finalized.
        node.block_number = 11
        client.statuses["d0"] = _Status(DEPLOY_STATE_FINALIZED, HASH_B)
        _run_cycle(tracker, node, client)
        self.assertIn("d0", tracker._finalization)
        # Inclusion telemetry keeps the first observation.
        self.assertEqual(tracker._inclusion["d0"], first)


class TerminalStatesSettleWithoutFinalizing(unittest.TestCase):
    def test_failed_and_expired_stop_sweeping_but_count_unfinalized(self):
        statuses = {
            "dead": _Status(DEPLOY_STATE_FAILED),
            "old": _Status(DEPLOY_STATE_EXPIRED),
        }
        tracker, node, client = _tracker_with(statuses)
        _run_cycle(tracker, node, client)
        pending, _ = _run_cycle(tracker, node, client)
        self.assertEqual(pending, 0)  # no longer swept
        results = tracker.get_results()
        self.assertTrue(all(r.finalization_time is None for r in results))
        # Terminal diagnostics carry readable names, not enum ints.
        self.assertEqual(tracker._terminal, {"dead": "FAILED", "old": "EXPIRED"})
        tracker.wait_for_finalization(timeout=1)  # settles fast, no timeout burn


class EnrichmentIsPooledPerUniqueBlock(unittest.TestCase):
    def test_failed_block_lookup_costs_one_rpc_per_cycle_not_per_deploy(self):
        """Round-4 review: a hash whose pooled resolution FAILS must not
        fall back to one serial get_block per sharing deploy — the
        attempted-set makes per-deploy enrichment record 0 without
        another RPC, and the pooled path retries the hash next cycle."""
        statuses = {f"d{i:02d}": _Status(DEPLOY_STATE_PENDING, HASH_A) for i in range(25)}
        tracker, node, client = _tracker_with(statuses, fail_lookups=True)
        _run_cycle(tracker, node, client)
        self.assertEqual(node.get_block_calls, 1)  # one pooled attempt, no fallback
        # All 25 recorded as included with number-unknown (0).
        self.assertEqual(sum(1 for v in tracker._inclusion.values() if v[0] == 0), 25)
        # Next cycle retries the hash once via the pooled path; on
        # success every sharing deploy re-resolves from the cache.
        node.fail_lookups = False
        _run_cycle(tracker, node, client)
        self.assertEqual(node.get_block_calls, 2)
        self.assertTrue(all(v[0] == 7 for v in tracker._inclusion.values()))

    def test_many_unique_blocks_resolve_one_lookup_each(self):
        """Enrichment must not serialize across unique containing blocks:
        the sweep pre-resolves every uncached hash through the worker
        pool, then per-deploy enrichment is cache-hits only — one
        get_block per unique block regardless of deploy count."""
        statuses = {}
        for i in range(20):
            block_hash = bytes([i]) * 32
            statuses[f"a{i:02d}"] = _Status(DEPLOY_STATE_FINALIZED, block_hash)
            statuses[f"b{i:02d}"] = _Status(DEPLOY_STATE_FINALIZED, block_hash)
        tracker, node, client = _tracker_with(statuses)
        _run_cycle(tracker, node, client)
        self.assertEqual(node.get_block_calls, 20)  # one per unique block, not 40
        results = tracker.get_results()
        self.assertEqual(sum(1 for r in results if r.finalization_time is not None), 40)


if __name__ == "__main__":
    unittest.main()
