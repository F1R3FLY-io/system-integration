"""Unit tests for ``metric_value`` in infra/metrics.py."""

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from test.infra.metrics import metric_value  # noqa: E402

_PAYLOAD = """\
# TYPE block_integrity_rejected counter
block_integrity_rejected{source="f1r3fly.casper.running",reason="hash-mismatch"} 2
block_integrity_rejected{source="f1r3fly.casper.running",reason="bad-signature"} 5
block_integrity_rejected_total{source="f1r3fly.casper.ingest",reason="hash-mismatch"} 1
block_ingest_held{source="f1r3fly.casper.block-ingest"} 3
block_ingest_held_bytes{source="f1r3fly.casper.block-ingest"} 4096
proposer_queue_pending 7
"""


def _node(payload: str = _PAYLOAD) -> SimpleNamespace:
    return SimpleNamespace(http_get=lambda path, timeout: SimpleNamespace(text=payload))


def test_samples_matching_the_labels_are_summed_across_name_forms() -> None:
    assert metric_value(_node(), "block_integrity_rejected", {"reason": "hash-mismatch"}) == 3


def test_without_labels_every_sample_of_the_name_counts() -> None:
    assert metric_value(_node(), "block_integrity_rejected") == 8


def test_a_name_that_prefixes_another_metric_reads_only_its_own_samples() -> None:
    assert metric_value(_node(), "block_ingest_held") == 3


def test_an_unlabelled_sample_is_read() -> None:
    assert metric_value(_node(), "proposer_queue_pending") == 7


def test_a_metric_not_yet_recorded_reads_zero() -> None:
    assert metric_value(_node(), "block_ingest_needs") == 0


def test_labels_no_sample_carries_read_zero() -> None:
    assert metric_value(_node(), "block_ingest_held", {"reason": "hash-mismatch"}) == 0
