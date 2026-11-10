"""Deploy load generation shared by the load and capacity tests.

Deploy kinds and mixes, the validAfterBlockNumber source every submitter
reads, and an open-loop submitter that holds a target rate regardless of how
slowly the node answers.
"""

from __future__ import annotations

import dataclasses
import hashlib
import heapq
import itertools
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import grpc
from f1r3fly.crypto import PrivateKey

from .metrics import DeployRecord
from .polling import parse_vabn_expiration

# How often the vabn source re-reads the chain height. Deploys are rejected
# once the DAG advances more than the 50-block deploy lifespan past their
# vabn, and a loaded shard can clear 50 blocks in well under 30 seconds.
VABN_REFRESH_INTERVAL = 5.0

# Total time one deploy may wait on deploy-queue refusals before the refusal
# counts as a submission failure.
REFUSAL_PATIENCE_SECONDS = 300.0
# The wait for a refusal that carries no retry-after hint.
DEFAULT_REFUSAL_WAIT_SECONDS = 1.0


class QueueRefusedError(Exception):
    """A deploy queue refused the deploy. A queue holds nothing it refused, so
    resubmitting the same deploy is safe."""

    def __init__(self, reason: str, retry_after: float):
        super().__init__(reason)
        self.retry_after = retry_after


def queue_refusal(exc: BaseException) -> Optional[QueueRefusedError]:
    """The refusal ``exc`` carries, or None when it is any other failure.

    A queue refuses with RESOURCE_EXHAUSTED (full) or UNAVAILABLE (not
    connected to its node), and names itself in the details; a bare
    UNAVAILABLE is a connection failure, whose outcome is unknown.
    """
    if not isinstance(exc, grpc.RpcError):
        return None
    if exc.code() not in (grpc.StatusCode.RESOURCE_EXHAUSTED, grpc.StatusCode.UNAVAILABLE):
        return None
    details = exc.details() or ""
    if not details.startswith("deploy queue"):
        return None
    hint = dict(exc.trailing_metadata() or ()).get("retry-after")
    retry_after = float(hint) if hint is not None else DEFAULT_REFUSAL_WAIT_SECONDS
    return QueueRefusedError(details, retry_after)


def loop_term(steps: int, index: int) -> str:
    return (
        "new loop, done in { contract loop(@i, @acc) = { if (i <= 0) { done!(acc) } "
        f"else {{ loop!(i - 1, acc + i) }} }} | loop!({steps}, {index}) }}"
    )


def map_term(entries: int, index: int) -> str:
    return (
        "new build, out in { contract build(@i, @m) = { if (i <= 0) { out!(m) } "
        f"else {{ build!(i - 1, m.set(i, i * {index})) }} }} | build!({entries}, {{}}) }}"
    )


# Deploy kinds by evaluation weight: (term for a deploy index, phlo limit).
# Limits sit 4-8x above the cost each kind measured on a dev node (115,
# ~65k, ~120k and ~637k phlo); map building grows faster than linearly, so
# the data kind stays small.
DEPLOY_KINDS = {
    "light": (lambda i: f"@{i}!({i})", 100_000),
    "compute": (lambda i: loop_term(200, i), 500_000),
    "data": (lambda i: map_term(100, i), 1_000_000),
    "heavy": (lambda i: loop_term(2000, i), 3_000_000),
}


def deploy_mix_from_env() -> List[str]:
    """The cycle of deploy kinds from LOAD_TEST_DEPLOY_MIX; unset means light only."""
    raw = os.environ.get("LOAD_TEST_DEPLOY_MIX", "").strip()
    return parse_deploy_mix(raw) if raw else ["light"]


def parse_deploy_mix(raw: str) -> List[str]:
    """The cycle of deploy kinds for weights like "light:5,heavy:1", interleaved
    as evenly as possible."""
    return interleave(parse_weights(raw, sorted(DEPLOY_KINDS)))


def parse_weights(raw: str, known: Sequence[str]) -> dict:
    """Weights like "light:5,heavy:1", each name one of ``known``."""
    weights = {}
    for part in raw.split(","):
        name, sep, weight = part.strip().partition(":")
        if name not in known or not sep or not weight.isdigit() or int(weight) < 1:
            raise ValueError(
                f"mix entry {part!r} must be <kind>:<positive weight> with kind one of {list(known)}"
            )
        if name in weights:
            raise ValueError(f"mix names {name!r} twice")
        weights[name] = int(weight)
    return weights


def interleave(weights: dict) -> List[str]:
    """One cycle of the weighted names, each spread as evenly as possible."""
    total = sum(weights.values())
    credit = dict.fromkeys(weights, 0)
    sequence = []
    for _ in range(total):
        for name, weight in weights.items():
            credit[name] += weight
        chosen = max(credit, key=credit.get)
        credit[chosen] -= total
        sequence.append(chosen)
    return sequence


def current_block_number(node, monitor=None) -> int:
    """The node's DAG-TIP height, with watchdog attribution on failure.

    ``Node.get_current_block_number()`` returns the LAST FINALIZED block
    number, the wrong height for validAfterBlockNumber freshness: deploy
    expiration compares against the latest DAG height, so with a deep
    un-finalized cone an LFB-derived vabn is expired on arrival.
    ``get_blocks(1)`` reports the actual tip; LFB is the fallback only when
    the tip read comes back empty.

    When the host-protection watchdog trips it SIGKILLs every node, so the
    next query dies with a bare connection error while the real cause, the
    monitor's breach line, sits thousands of log lines earlier. Attribute it
    here, at the point of failure.
    """
    try:
        blocks = node.get_blocks(1)
        if blocks:
            return max(b.blockNumber for b in blocks)
        return node.get_current_block_number()
    # Deliberately broad: a watchdog kill arrives as grpc.RpcError, but a dead
    # node also surfaces as connection resets and client-wrapper errors, and
    # all of them want the same attribution. The original exception is chained.
    except Exception as exc:
        breach = getattr(monitor, "breach", None) if monitor is not None else None
        if breach:
            raise AssertionError(
                f"Node {node.name} is unreachable because the host-protection "
                f"watchdog killed the nodes: {breach}. Raise --rss-ceiling-mb to "
                f"suit this host, or reduce the load profile — the nodes did not "
                f"crash on their own. Underlying error: {exc}"
            ) from exc
        if monitor is None:
            diagnosis = (
                "No resource monitor was attached to this call, so a "
                "host-protection kill cannot be ruled out here"
            )
        else:
            diagnosis = (
                "The resource monitor reports no breach, so this is a node-side "
                "failure rather than a watchdog kill — check that node's logs"
            )
        raise AssertionError(
            f"Node {node.name} became unreachable while querying the chain "
            f"height. {diagnosis}. Underlying error: {exc}"
        ) from exc


class VabnSource:
    """The validAfterBlockNumber every submitter thread signs with.

    Re-reads the tip at most every VABN_REFRESH_INTERVAL seconds, and takes
    the node's own height from an expiration rejection, which is fresher.
    """

    def __init__(self, node, monitor=None):
        self._node = node
        self._monitor = monitor
        self._lock = threading.Lock()
        self._value = max(0, current_block_number(node, monitor) - 1)
        self._read_at = time.time()

    def get(self) -> int:
        with self._lock:
            if time.time() - self._read_at <= VABN_REFRESH_INTERVAL:
                return self._value
        value = max(0, current_block_number(self._node, self._monitor) - 1)
        with self._lock:
            self._value = max(self._value, value)
            self._read_at = time.time()
            return self._value

    def advance_to(self, height: int) -> int:
        with self._lock:
            self._value = max(self._value, height - 1)
            self._read_at = time.time()
            return self._value


def submit_deploy(
    node, key, term: str, phlo_limit: int, vabn: VabnSource, retry_refusals: bool = True
) -> str:
    """Submit one deploy and return its id.

    Retries ONLY failures that guarantee the deploy was not accepted: the
    vabn-expiration rejection, which carries the node's current height, and a
    deploy queue's refusal, after the wait it asks for (up to
    REFUSAL_PATIENCE_SECONDS in all; with ``retry_refusals`` off the refusal is
    raised as a QueueRefusedError at once). Any other failure (deadline, connection
    loss) is ambiguous, since the first submission may have landed, and a
    blind retry would double-submit untracked load, so it is raised.
    """
    waited = 0.0
    while True:
        try:
            return _submit_once(node, key, term, phlo_limit, vabn)
        except Exception as exc:
            refusal = queue_refusal(exc)
            if refusal is None:
                raise
            if not retry_refusals or waited + refusal.retry_after > REFUSAL_PATIENCE_SECONDS:
                raise refusal from exc
            time.sleep(refusal.retry_after)
            waited += refusal.retry_after


def _submit_once(node, key, term: str, phlo_limit: int, vabn: VabnSource) -> str:
    try:
        return node.deploy_string(
            term, key, phlo_limit=phlo_limit, phlo_price=1, valid_after_block_no=vabn.get()
        )
    except Exception as first_err:
        height = parse_vabn_expiration(str(first_err))
        if height is None:
            raise
        try:
            return node.deploy_string(
                term,
                key,
                phlo_limit=phlo_limit,
                phlo_price=1,
                valid_after_block_no=vabn.advance_to(height),
            )
        except Exception as retry_err:
            raise RuntimeError(
                f"{retry_err} (vabn-refresh retry after: {first_err})"
            ) from retry_err


@dataclass(frozen=True)
class PlannedDeploy:
    """What one scheduled slot submits."""

    node: object
    key: object
    term: str
    phlo_limit: int
    kind: str

    def record(
        self,
        deploy_id: str,
        submit_time: float,
        phase: str,
        index: int,
        refusals: int,
        first_attempt: Optional[float],
    ):
        return DeployRecord(
            deploy_id=deploy_id,
            submit_time=submit_time,
            validator_name=self.node.name,
            phase=phase,
            index=index,
            kind=self.kind,
            sender=self.key.get_public_key().get_vault_address() if self.key is not None else "",
            refusals=refusals,
            first_attempt=first_attempt,
        )


@dataclass
class _Refused:
    index: int
    planned: PlannedDeploy
    phase: str
    first_attempt: float
    refusals: int


class Resubmitter:
    """A client's handling of deploy-queue refusals, beside an open-loop
    submitter: a refused deploy is signed again with a fresh
    validAfterBlockNumber and resubmitted once its retry-after has passed.
    Retries run on their own thread and pool, so new slots keep their
    schedule. A deploy is abandoned, as a client would give up, when a
    retry-after would carry it past REFUSAL_PATIENCE_SECONDS from its first
    attempt, or when the resubmitter closes before it is accepted.

    ``outcomes`` counts, per phase, refused deploys later ``accepted``,
    ``abandoned``, failed with an ``error``, and resubmission ``attempts``.
    """

    def __init__(
        self,
        workers: int,
        vabn: VabnSource,
        on_record: Callable[[DeployRecord], None],
        submit: Optional[Callable[..., str]] = None,
    ):
        self._vabn = vabn
        self._on_record = on_record
        self._submit = submit or submit_deploy
        self._cond = threading.Condition()
        self._due: List[Tuple[float, int, _Refused]] = []
        self._order = itertools.count()
        self._pending = 0
        self._closed = False
        self._pool = ThreadPoolExecutor(max_workers=workers)
        self.outcomes: Dict[str, Dict[str, int]] = {}
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def refused(
        self,
        index: int,
        planned: PlannedDeploy,
        phase: str,
        retry_after: float,
        first_attempt: float,
    ) -> None:
        """Take over deploy ``index``, first attempted at ``first_attempt``
        and refused once, for resubmission."""
        due = time.time() + retry_after
        if due - first_attempt > REFUSAL_PATIENCE_SECONDS:
            self._count(phase, "abandoned")
            return
        self._schedule(_Refused(index, planned, phase, first_attempt, 1), due)

    def _schedule(self, item: _Refused, due: float) -> None:
        with self._cond:
            if not self._closed:
                heapq.heappush(self._due, (due, next(self._order), item))
                self._pending += 1
                self._cond.notify_all()
                return
        self._count(item.phase, "abandoned")

    def _count(self, phase: str, outcome: str) -> None:
        with self._cond:
            counts = self.outcomes.setdefault(
                phase, {"accepted": 0, "abandoned": 0, "error": 0, "attempts": 0}
            )
            counts[outcome] += 1

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._closed and (not self._due or self._due[0][0] > time.time()):
                    timeout = self._due[0][0] - time.time() if self._due else None
                    self._cond.wait(timeout)
                if self._closed:
                    return
                _, _, item = heapq.heappop(self._due)
            self._pool.submit(self._attempt, item)

    def _attempt(self, item: _Refused) -> None:
        planned = item.planned
        self._count(item.phase, "attempts")
        began = time.time()
        try:
            deploy_id = self._submit(
                planned.node,
                planned.key,
                planned.term,
                planned.phlo_limit,
                self._vabn,
                retry_refusals=False,
            )
        except QueueRefusedError as refusal:
            if time.time() + refusal.retry_after - item.first_attempt > REFUSAL_PATIENCE_SECONDS:
                self._count(item.phase, "abandoned")
            else:
                item.refusals += 1
                self._schedule(item, time.time() + refusal.retry_after)
        except Exception as exc:
            logging.info("  resubmission of deploy %d failed: %s", item.index, exc)
            self._count(item.phase, "error")
        else:
            self._on_record(
                planned.record(
                    deploy_id, began, item.phase, item.index, item.refusals, item.first_attempt
                )
            )
            self._count(item.phase, "accepted")
        finally:
            with self._cond:
                self._pending -= 1
                self._cond.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        """Block until no refused deploy is waiting or being resubmitted."""
        with self._cond:
            return self._cond.wait_for(lambda: self._pending == 0, timeout)

    def close(self) -> None:
        """Stop resubmitting; deploys still waiting for their retry are abandoned."""
        with self._cond:
            self._closed = True
            waiting = [item for _, _, item in self._due]
            self._due.clear()
            self._pending -= len(waiting)
            self._cond.notify_all()
        for item in waiting:
            self._count(item.phase, "abandoned")
        self._thread.join(timeout=10)
        self._pool.shutdown(wait=True)


@dataclass
class SubmitStats:
    """How faithfully a run held its target rate."""

    target_rate: float
    duration: float
    submitted: int = 0
    errors: List[str] = field(default_factory=list)
    # Deploys a deploy queue refused and the run did not resubmit.
    refused: int = 0
    # Seconds between a slot's scheduled time and the moment a worker began
    # submitting it. A growing lag means the submitter, not the shard, set
    # the pace.
    max_start_lag: float = 0.0
    submit_seconds: List[float] = field(default_factory=list)

    @property
    def achieved_rate(self) -> float:
        """Deploys accepted per second."""
        return self.submitted / self.duration if self.duration > 0 else 0.0

    @property
    def offered_rate(self) -> float:
        """Deploys answered per second, accepted or refused: the load the
        submitter actually put on the shard."""
        return (self.submitted + self.refused) / self.duration if self.duration > 0 else 0.0


class OpenLoopSubmitter:
    """Submits deploys on a fixed schedule from a worker pool.

    Slot ``i`` is due at ``start + i / rate``, whatever happened to earlier
    slots. A serial loop instead waits for each submission to return, so a
    slow node silently lowers the offered rate and the measurement reports
    the node's own pace back as the load.
    """

    def __init__(self, workers: int):
        if workers < 1:
            raise ValueError("an open-loop submitter needs at least one worker")
        self._workers = workers

    @staticmethod
    def workers_for(rate: float, expected_submit_seconds: float = 0.5) -> int:
        """A pool large enough that the target rate never waits on a worker."""
        return max(4, math.ceil(rate * expected_submit_seconds * 4))

    def run(
        self,
        rate: float,
        duration: float,
        first_index: int,
        plan: Callable[[int], PlannedDeploy],
        vabn: VabnSource,
        phase: str,
        on_record: Callable[[DeployRecord], None],
        stop: Optional[threading.Event] = None,
        retry_refusals: bool = True,
        on_refused: Optional[Callable[[int, PlannedDeploy, str, float, float], None]] = None,
    ) -> SubmitStats:
        """Run the schedule. A refused deploy is counted, then handed to
        ``on_refused`` (index, deploy, phase, retry-after, attempt start) when
        given."""
        if rate <= 0 or duration <= 0:
            raise ValueError("an open-loop run needs a positive rate and duration")
        total = int(rate * duration)
        stats = SubmitStats(target_rate=rate, duration=duration)
        lock = threading.Lock()
        start = time.time()

        def submit(index: int, due: float) -> None:
            began = time.time()
            planned = plan(index)
            try:
                deploy_id = submit_deploy(
                    planned.node,
                    planned.key,
                    planned.term,
                    planned.phlo_limit,
                    vabn,
                    retry_refusals=retry_refusals,
                )
            except QueueRefusedError as refusal:
                with lock:
                    stats.refused += 1
                if on_refused is not None:
                    on_refused(index, planned, phase, refusal.retry_after, began)
                return
            except Exception as exc:
                with lock:
                    stats.errors.append(f"deploy {index}: {exc}")
                return
            ended = time.time()
            on_record(planned.record(deploy_id, began, phase, index, 0, None))
            with lock:
                stats.submitted += 1
                stats.max_start_lag = max(stats.max_start_lag, began - due)
                stats.submit_seconds.append(ended - began)

        stopped_at = None
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            for slot in range(total):
                if stop is not None and stop.is_set():
                    stopped_at = time.time()
                    break
                due = start + slot / rate
                delay = due - time.time()
                if delay > 0:
                    time.sleep(delay)
                pool.submit(submit, first_index + slot, due)
        if stopped_at is not None:
            stats.duration = stopped_at - start
        logging.info(
            "  %s: %d submitted at %.2f/s (target %.2f/s), %d refused, %d errors, "
            "max start lag %.2fs",
            phase,
            stats.submitted,
            stats.achieved_rate,
            rate,
            stats.refused,
            len(stats.errors),
            stats.max_start_lag,
        )
        return stats


# Genesis funds for each generated sender: far beyond what a run spends on
# transfers and phlo.
SENDER_FUNDS = 10**15


def sender_key(index: int) -> PrivateKey:
    """A deterministic key for generated sender ``index``."""
    return PrivateKey.from_bytes(
        hashlib.blake2b(f"capacity-sender-{index}".encode(), digest_size=32).digest()
    )


def with_funded_senders(config, keys: Sequence):
    """``config`` with every key's vault funded at genesis."""
    return dataclasses.replace(
        config,
        extra_wallets=(config.extra_wallets or [])
        + [(k.get_public_key().get_vault_address(), SENDER_FUNDS) for k in keys],
    )


def node_key_pairs(nodes: Sequence, keys: Sequence) -> List[Tuple[object, object]]:
    """Pair every key with a node, cycling nodes, so each key always submits
    to the same node."""
    return [(nodes[i % len(nodes)], key) for i, key in enumerate(keys)]
