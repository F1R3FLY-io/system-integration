"""Structured telemetry emitted by soak tests."""

import logging


def emit_lfb_spread_metric(drain_spread: int) -> None:
    """Emit the canonical LFB drain-spread line consumed by soak dashboards."""
    logging.info("SOAK_METRIC name=lfb_spread value=%d phase=drain", drain_spread)
