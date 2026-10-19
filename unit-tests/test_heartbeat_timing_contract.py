"""The heartbeat timing harness's contract with the node summarizer.

test_heartbeat_timing feeds f1r3node-rust scripts/bench/summarize-issue24-phases.sh,
which parses its output with fixed patterns. These tests pin, without a node:
the phase line still matches the summarizer's awk gate and jq capture, the
repeated phase is test_load's high phase, N parses from the environment, and the
test stays out of every default selection unless HEARTBEAT_TIMING=1.
"""

import importlib
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

harness = importlib.import_module("test.tests.custom.test_heartbeat_timing")
load = importlib.import_module("test.tests.custom.test_load")

# Copied from summarize-issue24-phases.sh (f1r3node-rust dev): the awk line
# gate and the jq capture that turns the line into a record.
SUMMARIZER_GATE = re.compile(r"^  Phase [a-z_]+: inclusion p50=")
SUMMARIZER_CAPTURE = re.compile(
    r"inclusion p50=(?P<ip50>[0-9.]+)s p95=(?P<ip95>[0-9.]+)s, "
    r"finalization p50=(?P<fp50>[0-9.]+)s p95=(?P<fp95>[0-9.]+)s, "
    r"LFB #(?P<ls>[0-9]+)->#(?P<le>[0-9]+) \((?P<rate>[0-9.]+) blk/min\), "
    r"unfinalized=(?P<un>[0-9]+)"
)


def test_phase_line_matches_the_summarizer():
    line = harness.PHASE_LINE_FORMAT % ("high", 1.2, 4.0, 6.5, 12.0, 100, 142, 27.3, 0)
    assert SUMMARIZER_GATE.match(line)
    match = SUMMARIZER_CAPTURE.search(line)
    assert match
    fields = match.groupdict()
    assert fields == {
        "ip50": "1.2",
        "ip95": "4.0",
        "fp50": "6.5",
        "fp95": "12.0",
        "ls": "100",
        "le": "142",
        "rate": "27.3",
        "un": "0",
    }


def test_repeated_phase_is_the_load_test_high_phase():
    assert harness.HIGH_PHASE is next(p for p in load.PHASES if p["name"] == "high")
    assert {k: harness.HIGH_PHASE[k] for k in ("rate", "duration", "workers")} == {
        "rate": 10,
        "duration": 15,
        "workers": 3,
    }


def test_shard_is_the_load_test_shard():
    config = harness._load_shard_config()
    assert config.heartbeat and config.include_readonly
    assert len(config.bonds) == 4
    assert config.global_cli_options == {
        "--heartbeat-self-propose-cooldown": "3seconds",
        "--heartbeat-advanced-frontier-chase-max-lag": "20",
        "--max-user-deploys-per-block": "128",
    }


@pytest.mark.parametrize(("raw", "expected"), [(None, 20), ("", 20), ("3", 3), (" 40 ", 40)])
def test_repetitions_from_env(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("HEARTBEAT_TIMING_REPS", raising=False)
    else:
        monkeypatch.setenv("HEARTBEAT_TIMING_REPS", raw)
    assert harness.repetitions_from_env() == expected


@pytest.mark.parametrize(
    ("raw", "reason"), [("0", "at least 1"), ("-1", "at least 1"), ("twenty", "an integer")]
)
def test_repetitions_from_env_rejects_bad_values(monkeypatch, raw, reason):
    monkeypatch.setenv("HEARTBEAT_TIMING_REPS", raw)
    with pytest.raises(ValueError, match=f"HEARTBEAT_TIMING_REPS must be {reason}"):
        harness.repetitions_from_env()


def _skip_condition():
    (mark,) = [m for m in harness.pytestmark if m.name == "skipif"]
    return mark.args[0]


@pytest.mark.parametrize(("value", "skipped"), [(None, True), ("0", True), ("1", False)])
def test_opt_in_gate(monkeypatch, value, skipped):
    if value is None:
        monkeypatch.delenv("HEARTBEAT_TIMING", raising=False)
    else:
        monkeypatch.setenv("HEARTBEAT_TIMING", value)
    try:
        importlib.reload(harness)
        assert _skip_condition() is skipped
    finally:
        monkeypatch.undo()
        importlib.reload(harness)
