"""Timeout hierarchy — single source of truth for all timeouts.

Every polling function, fixture, and test derives its timeout from a
``TimeoutHierarchy`` instance. The hierarchy applies a scale factor
(``TimeoutConfig.scale``) so CI runners with slower hardware can
uniformly inflate all timeouts without per-test adjustments.
"""

from __future__ import annotations

from .config import TimeoutConfig


class TimeoutHierarchy:
    """Derives concrete timeout values from a ``TimeoutConfig``."""

    def __init__(self, config: TimeoutConfig) -> None:
        self._config = config

    @property
    def node_startup(self) -> int:
        """Max seconds for a container to reach Running state."""
        return self._scaled(self._config.node_startup)

    @property
    def deploy_inclusion(self) -> int:
        """Max seconds for a deploy to appear in a block."""
        return self._scaled(self._config.deploy_inclusion)

    @property
    def finalization(self) -> int:
        """Max seconds for LFB to advance past a deploy's block."""
        return self._scaled(self._config.finalization)

    @property
    def deploy_finalization_absolute(self) -> int:
        """Non-renewable total bound for exact deploy-terminal observation."""
        return self._scaled(self._config.deploy_finalization_absolute)

    @property
    def command(self) -> int:
        """Max seconds for a gRPC/HTTP call."""
        return self._scaled(self._config.command)

    @property
    def port_release(self) -> int:
        """Max seconds to wait for kernel TIME_WAIT on a port."""
        return self._scaled(self._config.port_release)

    @property
    def epoch_transition(self) -> int:
        """Max seconds for a multi-block consensus transition (epoch-boundary
        withdrawer move, quarantine payout) to complete."""
        return self._scaled(self._config.epoch_transition)

    @property
    def poll_interval(self) -> float:
        """Seconds between status checks in polling loops."""
        return self._config.poll_interval

    def custom(self, base_seconds: int) -> int:
        """Apply the scale factor to an arbitrary base timeout."""
        return self._scaled(base_seconds)

    def _scaled(self, base: int) -> int:
        return int(base * self._config.scale)
