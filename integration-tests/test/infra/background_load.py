"""Background deploy load for a shard.

``BackgroundLoad`` submits one deploy every ``interval`` seconds, round-robin
across the given nodes, until stopped. What each deploy is comes from a
``submit`` callable; ``own_key_sends`` and ``vault_transfers`` build the two
kinds the suites use. A failed submission is logged and counted, and the load
carries on: callers decide what the load must have achieved.
``assert_vault_transfers_reconciled`` is the end check for ``vault_transfers``.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, List, Optional, Sequence, Tuple

from f1r3fly.crypto import PrivateKey

from .assertions import (
    assert_balance_consistent_across_nodes,
    await_balance_converges_on_all_nodes,
    common_finalized_anchor,
    resolve_deploy_verdicts,
)

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 2.0
DEFAULT_PHLO_LIMIT = 100_000_000

# Submits deploy number ``index`` to ``node`` and returns its deploy id.
Submit = Callable[[object, int], str]


def own_key_sends(identities: Sequence, phlo_limit: int = DEFAULT_PHLO_LIMIT) -> Submit:
    """Each deploy sends one integer on a fresh channel, signed by the
    validator whose node receives it: ``identities[i]`` for ``nodes[i]``."""
    keys = [identity.private_key() for identity in identities]

    def submit(node, index: int) -> str:
        return node.deploy_string(
            f'@"background-load-{index}"!({index})',
            keys[index % len(keys)],
            phlo_limit=phlo_limit,
            phlo_price=1,
        )

    return submit


def vault_transfers(
    key: PrivateKey,
    destination: str,
    amount: int = 1,
    phlo_limit: int = DEFAULT_PHLO_LIMIT,
) -> Submit:
    """Each deploy moves ``amount`` from ``key``'s vault to ``destination``.
    Every deploy contends on the same two balance channels, so concurrent
    blocks make the merge do real work. Both vaults must exist at genesis."""
    source = key.get_public_key().get_vault_address()

    def submit(node, index: int) -> str:
        return node.get_vault().transfer(
            source, destination, amount, key, phlo_price=1, phlo_limit=phlo_limit
        )

    return submit


def assert_vault_transfers_reconciled(
    load: "BackgroundLoad",
    nodes: Sequence,
    key: PrivateKey,
    destination: str,
    amount: int,
    source_before: int,
    destination_before: int,
    timeouts,
    label: str,
) -> int:
    """Reconcile a stopped ``vault_transfers`` load exactly, on every node.
    Returns the number of transfers that finalized.

    Every transfer must reach the same terminal verdict on every node; one
    the shard judged Expired moved nothing and is not counted. The
    destination must converge to ``destination_before`` plus ``amount`` per
    finalized transfer, never fall on the way, and never exceed it: an
    expired transfer whose credit landed anyway shows as an over-apply. The
    source pays gas too, so it must be identical on every node at a common
    finalized block and have fallen by at least the transferred total.
    """
    verdicts = resolve_deploy_verdicts(
        nodes, load.deploy_ids(), timeouts.finalization * 3, label=label
    )
    logger.info("[%s] verdicts: %s", label, verdicts.summary())
    finalized = len(verdicts.finalized)
    expected = destination_before + finalized * amount
    await_balance_converges_on_all_nodes(
        nodes,
        destination,
        expected,
        timeouts.finalization * 3,
        f"{label}-destination",
        non_regression="up",
        upper_bound=expected,
    )
    anchor = common_finalized_anchor(nodes, timeouts.finalization)
    source = key.get_public_key().get_vault_address()
    source_after = assert_balance_consistent_across_nodes(nodes, source, anchor)
    assert source_before - source_after >= finalized * amount, (
        f"[{label}] source fell by {source_before - source_after}, less than the "
        f"{finalized * amount} transferred: a finalized debit was lost"
    )
    logger.info(
        "[%s] reconciled %d transfers on %d nodes: destination %d, source %d",
        label,
        finalized,
        len(nodes),
        expected,
        source_after,
    )
    return finalized


class BackgroundLoad:
    """Usable with ``start``/``stop`` or as a context manager."""

    def __init__(
        self,
        nodes: Sequence,
        submit: Submit,
        interval: float = DEFAULT_INTERVAL,
        name: str = "background-load",
    ) -> None:
        if not nodes:
            raise ValueError("background load needs at least one node")
        self._nodes = list(nodes)
        self._submit = submit
        self._interval = interval
        self._name = name
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._attempts = 0
        self._submitted: List[Tuple[object, str]] = []
        self._errors: List[str] = []

    @property
    def attempts(self) -> int:
        """Deploys tried, failed submissions included."""
        return self._attempts

    @property
    def errors(self) -> List[str]:
        with self._lock:
            return list(self._errors)

    def deploy_ids(self) -> List[str]:
        """Every deploy id submitted so far, in order."""
        with self._lock:
            return [deploy_id for _, deploy_id in self._submitted]

    def last_submitted(self) -> Optional[Tuple[object, str]]:
        """The node and deploy id of the latest submission."""
        with self._lock:
            return self._submitted[-1] if self._submitted else None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("background load already started")
        self._thread = threading.Thread(target=self._run, daemon=True, name=self._name)
        self._thread.start()

    def stop(self, join_timeout: float = 10.0) -> None:
        """Idempotent."""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=join_timeout)
        self._thread = None
        logger.info(
            "%s stopped: %d deploys tried, %d failed",
            self._name,
            self._attempts,
            len(self._errors),
        )

    def __enter__(self) -> "BackgroundLoad":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def _run(self) -> None:
        while not self._stop.is_set():
            index = self._attempts
            node = self._nodes[index % len(self._nodes)]
            try:
                deploy_id = self._submit(node, index)
                with self._lock:
                    self._submitted.append((node, deploy_id))
            except Exception as error:  # noqa: BLE001 — counted; callers judge the load
                with self._lock:
                    self._errors.append(f"{node.name}: {error}")
                logger.warning("%s deploy %d on %s failed: %s", self._name, index, node.name, error)
            self._attempts += 1
            self._stop.wait(self._interval)
