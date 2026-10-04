"""Unit tests for the parents-post-state metric refresh in infra/metrics.py.

The node renamed and extended the parents-post-state sub-stage metrics
(f1r3node-rust PR #362 and follow-ups): the old floor_compute / fs_seal /
scope_build / merge buckets no longer exist, so a printer still asking for
them shows only ensure_mergeable and hides the new attribution outside raw
CSVs. These tests pin the new bucket list to the node's metrics_constants.rs
names and prove the whole path — scrape, delta, and formatting — against a
synthetic /metrics payload, so a future rename fails here instead of
silently emptying the job-log "Node internals" dump again.

They also pin the counter-scrape mechanism: metrics-exporter-prometheus
renders `counter!` metrics as bare `name{labels} value` lines with no
`_sum`/`_count` pair, so a counter listed among the histograms scrapes
nothing — which is exactly how is_mergeable_channel_calls went dark.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from test.infra.metrics import (  # noqa: E402
    COUNTERS_TO_SCRAPE,
    METRICS_TO_SCRAPE,
    compute_metric_deltas,
    format_node_metrics,
    scrape_metrics,
)

_PPS = "block_processing_stage_parents_post_state"

_STALE_NAMES = [
    f"{_PPS}_floor_compute_time",
    f"{_PPS}_fs_seal_time",
    f"{_PPS}_scope_build_time",
    f"{_PPS}_merge_time",
    # Renamed node-side when the silent single-parent fallback became an Err
    # (successor: compute_parents_post_state_merge_scope_backstop_error).
    "compute_parents_post_state_fallback_merge_scope_too_large_fired",
    # Dead constants: declared in metrics_constants.rs, never emitted.
    "dag_merge_rejection_expansion_time",
    "dag_merge_rejection_expansion_fired",
]

_NEW_HISTOGRAMS = [
    f"{_PPS}_cache_lookup_time",
    f"{_PPS}_floor_derive_time",
    f"{_PPS}_base_holds_floor_time",
    f"{_PPS}_base_lineage_walk_time",
    f"{_PPS}_collect_ancestors_time",
    f"{_PPS}_ensure_mergeable_time",
    f"{_PPS}_prior_rejection_counts_time",
    f"{_PPS}_merge_call_time",
    f"{_PPS}_post_merge_time",
    f"{_PPS}_settled_index_build_time",
    f"{_PPS}_settled_index_blocks",
    f"{_PPS}_settled_floor_index_build_time",
    f"{_PPS}_settled_floor_index_blocks",
    "compute_parents_post_state_merge_scope_size",
]

_NEW_COUNTERS = [
    f"{_PPS}_settled_probe_wrapper_calls",
    f"{_PPS}_settled_probe_wrapper_time_ns",
    "compute_parents_post_state_merge_scope_backstop_error",
]


class _StubNode:
    """Just enough node to satisfy scrape_metrics."""

    class _Resp:
        def __init__(self, text: str):
            self.text = text

    def __init__(self, text: str):
        self._text = text

    def http_get(self, path: str, timeout: int = 10):
        return self._Resp(self._text)


def _histogram_lines(name: str, total: float, count: int, labels: str = "") -> str:
    return f"{name}_sum{labels} {total}\n{name}_count{labels} {count}\n"


# --- the scrape list itself --------------------------------------------------


def test_stale_bucket_names_are_gone_from_the_scrape_list():
    """The node no longer exports these; asking for them is dead weight that
    makes the printer look wired-up while it shows nothing."""
    for name in _STALE_NAMES:
        assert name not in METRICS_TO_SCRAPE, f"stale metric still scraped: {name}"


def test_new_bucket_names_are_scraped():
    for name in _NEW_HISTOGRAMS:
        assert name in METRICS_TO_SCRAPE, f"missing histogram: {name}"
    for name in _NEW_COUNTERS:
        assert name in COUNTERS_TO_SCRAPE, f"missing counter: {name}"


def test_counters_are_not_listed_as_histograms():
    """A counter in METRICS_TO_SCRAPE scrapes nothing (no _sum/_count lines
    exist for it) — the exact failure mode this split exists to prevent."""
    for name in COUNTERS_TO_SCRAPE:
        assert name not in METRICS_TO_SCRAPE, f"counter listed as histogram: {name}"


# --- scrape + delta ----------------------------------------------------------


def test_scrape_parses_histograms_and_bare_counter_lines():
    text = (
        _histogram_lines(f"{_PPS}_merge_call_time", 1.5, 3, '{source="casper"}')
        + f'{_PPS}_settled_probe_wrapper_calls{{source="casper"}} 400\n'
        + f"{_PPS}_settled_probe_wrapper_time_ns 80000000\n"
        + "is_mergeable_channel_calls 25\n"
    )
    result = scrape_metrics(_StubNode(text))

    assert result[f"{_PPS}_merge_call_time_sum"] == 1.5
    assert result[f"{_PPS}_merge_call_time_count"] == 3
    assert result[f"{_PPS}_settled_probe_wrapper_calls"] == 400
    assert result[f"{_PPS}_settled_probe_wrapper_time_ns"] == 80000000
    assert result["is_mergeable_channel_calls"] == 25


def test_counter_deltas_surface_as_count_keys():
    before = scrape_metrics(_StubNode(f"{_PPS}_settled_probe_wrapper_calls 100\n"))
    after = scrape_metrics(_StubNode(f"{_PPS}_settled_probe_wrapper_calls 400\n"))
    deltas = compute_metric_deltas(before, after)

    assert deltas[f"{_PPS}_settled_probe_wrapper_calls.count"] == 300


# --- presentation ------------------------------------------------------------


def _deltas_for(text: str) -> dict:
    empty = {}
    return compute_metric_deltas(empty, scrape_metrics(_StubNode(text)))


def test_formatter_shows_the_new_substages():
    text = (
        _histogram_lines(f"{_PPS}_cache_lookup_time", 0.010, 10)
        + _histogram_lines(f"{_PPS}_prior_rejection_counts_time", 0.050, 10)
        + _histogram_lines(f"{_PPS}_merge_call_time", 2.0, 10)
        + _histogram_lines(f"{_PPS}_post_merge_time", 0.020, 10)
    )
    out = format_node_metrics(_deltas_for(text))

    assert "parents_post_state sub-stages" in out
    for label in ("cache_lookup", "prior_rejection_counts", "merge_call", "post_merge"):
        assert label in out, f"sub-stage missing from output: {label}"


def test_probe_wrapper_and_index_builds_nest_under_merge_call():
    """The node attributes the settled-sig cost to closures invoked from
    inside the merge; the printout mirrors that with deeper indentation."""
    text = (
        _histogram_lines(f"{_PPS}_merge_call_time", 2.0, 10)
        + f"{_PPS}_settled_probe_wrapper_calls 400\n"
        + f"{_PPS}_settled_probe_wrapper_time_ns 80000000\n"
        + _histogram_lines(f"{_PPS}_settled_index_build_time", 0.5, 10)
        + _histogram_lines(f"{_PPS}_settled_index_blocks", 120, 10)
    )
    out = format_node_metrics(_deltas_for(text))
    lines = out.splitlines()

    merge_idx = next(i for i, ln in enumerate(lines) if "merge_call" in ln)
    probe_line = next(ln for ln in lines if "settled_probe wrapper" in ln)
    index_line = next(ln for ln in lines if "settled_index build" in ln)

    assert lines.index(probe_line) > merge_idx, "probe wrapper not under merge_call"
    assert len(probe_line) - len(probe_line.lstrip()) > len(lines[merge_idx]) - len(
        lines[merge_idx].lstrip()
    ), "probe wrapper not indented as a child"
    # 80_000_000 ns over 400 calls = 0.2ms per probe
    assert "0.200ms" in probe_line
    assert "(400 probes)" in probe_line
    assert "(10 builds, 12 blocks avg)" in index_line


def test_merge_scope_size_renders_as_a_count_not_a_time():
    text = _histogram_lines("compute_parents_post_state_merge_scope_size", 84, 12)
    out = format_node_metrics(_deltas_for(text))

    assert "merge scope size: 7.0 blocks avg (12 merges)" in out
    assert (
        "merge scope size: 7.0 blocks avg" in out
        and "ms" not in out.split("merge scope size")[1].splitlines()[0]
    ), "scope size must not be formatted as milliseconds"


def test_is_mergeable_channel_calls_prints_again():
    """It was listed as a histogram, so its row could never render."""
    out = format_node_metrics(_deltas_for("is_mergeable_channel_calls 25\n"))

    assert "is_mergeable_channel calls: 25" in out


def test_merge_scope_backstop_refusals_render():
    """The deterministic floor-distance backstop refusing merges is a
    soak-relevant anomaly signal; a counter, so it needs the bare-line path."""
    out = format_node_metrics(
        _deltas_for("compute_parents_post_state_merge_scope_backstop_error 3\n")
    )

    assert "merge_scope_backstop refused 3 merges" in out
