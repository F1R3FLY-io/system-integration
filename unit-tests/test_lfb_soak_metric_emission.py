"""Structured LFB spread telemetry emitted by the load soak."""

import logging
import sys
from pathlib import Path

import pytest  # type: ignore[import-not-found]

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration-tests"))

from test.tests.custom.test_load import (  # type: ignore[import-not-found]  # noqa: E402
    _emit_lfb_spread_metric,
)


@pytest.mark.parametrize("drain_spread", [0, 7])
def test_emits_canonical_lfb_spread_metric_once(caplog, drain_spread):
    with caplog.at_level(logging.INFO):
        _emit_lfb_spread_metric(drain_spread)

    metric_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("SOAK_METRIC ")
    ]
    assert metric_lines == [f"SOAK_METRIC name=lfb_spread value={drain_spread} phase=drain"]
