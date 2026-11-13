"""Capacity workloads: what each deploy does, the state it needs first, and how
to confirm the shard executed it correctly.

A workload builds the term for deploy ``index`` signed by ``key``. Workloads
whose deploys touch shared state install it in ``prepare`` and confirm the
result in ``verify``, so a run that finalized every deploy but produced the
wrong state is not reported as capacity.
"""

from __future__ import annotations

import logging
import random
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from f1r3fly.deploy import DeployError, check_deploy_succeeded
from f1r3fly.par import par_value
from f1r3fly.vault import (
    TRANSFER_PHLO_LIMIT,
    TRANSFER_RHO_TPL,
    VaultAPI,
    render_contract_template,
)

from .load import DEPLOY_KINDS, interleave, parse_weights
from .polling import wait_for_deploy_finalized

# Deploys read back per check. Every read is one or two RPCs per deploy, so
# the checks sample instead of reading all of them.
_SAMPLE = 40


def _sample(finalized: Dict[str, int]) -> List[str]:
    return random.Random(0).sample(sorted(finalized), min(_SAMPLE, len(finalized)))


def check_execution(readonly, finalized: Dict[str, int]) -> Tuple[List[str], dict]:
    """Sampled proof that finalized deploys executed: each is in its block,
    not errored, with a positive cost. Returns (problems, cost summary)."""
    problems = []
    costs = []
    for deploy_id in _sample(finalized):
        block_hash = _canonical_block(readonly, deploy_id)
        try:
            block = readonly.get_block(block_hash)
            check_deploy_succeeded(block, deploy_id)
        except DeployError as exc:
            problems.append(str(exc))
            continue
        costs.append(next(d.cost for d in block.deploys if d.sig == deploy_id))
    cost = {
        "sampled": len(costs),
        "phlo_mean": round(statistics.fmean(costs)) if costs else None,
        "phlo_max": max(costs) if costs else None,
    }
    logging.info("  execution check: %d problems; cost %s", len(problems), cost)
    return problems, cost


def _canonical_block(node, deploy_id: str) -> str:
    """The block the node's canonical state holds the deploy in."""
    block_hash = node.deploy_status(deploy_id).latestBlockHash.hex()
    if not block_hash:
        raise AssertionError(f"{node.name} reports no block for deploy {deploy_id[:16]}")
    return block_hash


def _deploy_value(node, deploy_id: str):
    """What the deploy sent on its deploy-id channel, read at its own block."""
    data = node.get_deploy_data(deploy_id, block_hash=_canonical_block(node, deploy_id))
    if data is None or len(data.par) == 0:
        return None
    return par_value(data.par[0])


@dataclass(frozen=True)
class SetupContext:
    """Where a workload installs its state, and the nodes that must see it."""

    node: object
    key: object
    all_nodes: Sequence
    timeout: float

    def deploy_and_wait(self, term: str, phlo_limit: int) -> str:
        """Deploy ``term`` and wait until every node reports it Finalized."""
        (deploy_id,) = self.deploy_all_and_wait([term], phlo_limit)
        return deploy_id

    def deploy_all_and_wait(self, terms: Sequence[str], phlo_limit: int) -> List[str]:
        """Deploy every term, then wait until every node reports each Finalized."""
        deploy_ids = [
            self.node.deploy_string(term, self.key, phlo_limit=phlo_limit, phlo_price=1)
            for term in terms
        ]
        for deploy_id in deploy_ids:
            for node in self.all_nodes:
                wait_for_deploy_finalized(node, deploy_id, timeout=self.timeout)
        return deploy_ids


class Workload:
    name = ""

    def prepare(self, setup: SetupContext) -> None:
        """Install the state the deploys need. Stateless workloads have none."""

    def kind(self, index: int) -> str:
        """The deploy kind of deploy ``index``, for per-kind reporting."""
        return self.name

    def term(self, index: int, key) -> Tuple[str, int]:
        raise NotImplementedError

    def verify(self, readonly, finalized: Dict[str, int]) -> List[str]:
        """Problems found in the state the finalized deploys produced.

        ``finalized`` maps each deploy id every node finalized to its index.
        """
        return []


class KindWorkload(Workload):
    """One of the load test's stateless deploy kinds."""

    def __init__(self, kind: str):
        if kind not in DEPLOY_KINDS:
            raise ValueError(f"unknown deploy kind {kind!r}; known: {sorted(DEPLOY_KINDS)}")
        self.name = kind
        self._build, self._phlo_limit = DEPLOY_KINDS[kind]

    def term(self, index: int, key) -> Tuple[str, int]:
        return self._build(index), self._phlo_limit


class TransferWorkload(Workload):
    """Vault-to-vault transfers of 1 token, each signed by its sending vault.

    The deployer's key authorizes the debit, so a deploy's sender is its
    signing key, and the recipient is the next sender's vault, which exists
    from genesis. Every sender therefore needs genesis funds for both the
    transfers and the phlo.
    """

    name = "transfer"

    def __init__(self, sender_keys: Sequence):
        if len(sender_keys) < 2:
            raise ValueError("transfers need at least two funded sender keys")
        self._addresses = [k.get_public_key().get_vault_address() for k in sender_keys]
        self._index_of = {addr: i for i, addr in enumerate(self._addresses)}

    def term(self, index: int, key) -> Tuple[str, int]:
        sender = key.get_public_key().get_vault_address()
        recipient = self._addresses[(self._index_of[sender] + 1) % len(self._addresses)]
        term = render_contract_template(
            TRANSFER_RHO_TPL, {"from": sender, "to": recipient, "amount": "1"}
        )
        return term, TRANSFER_PHLO_LIMIT

    def verify(self, readonly, finalized: Dict[str, int]) -> List[str]:
        sample = _sample(finalized)
        vault = VaultAPI(readonly._external_client())
        problems = []
        for deploy_id in sample:
            result = vault.read_transfer_result(
                deploy_id, block_hash=_canonical_block(readonly, deploy_id)
            )
            if not result.success:
                problems.append(f"transfer {deploy_id[:16]} failed: {result.reason}")
        logging.info(
            "  transfer check: %d of %d sampled transfers failed",
            len(problems),
            len(sample),
        )
        return problems


class ContentionWorkload(Workload):
    """Increments of a few shared counters.

    Each deploy takes counter ``index % cells`` and puts back its successor,
    so concurrent deploys on one counter in sibling blocks conflict and the
    merge rejects all but one. Fewer cells mean more conflict. The counters
    sit on public names unique to the run, so a later run on the same shard
    starts from fresh cells, and the final values must add up to the number
    of finalized increments.
    """

    name = "contention"
    _PHLO_LIMIT = 100_000

    def __init__(self, cells: int):
        if cells < 1:
            raise ValueError("contention needs at least one cell")
        self._cells = cells
        self._prefix = f"capacity-cell-{int(time.time())}"

    def _channel(self, cell: int) -> str:
        return f"{self._prefix}-{cell}"

    def prepare(self, setup: SetupContext) -> None:
        sends = " | ".join(f'@"{self._channel(c)}"!(0)' for c in range(self._cells))
        setup.deploy_and_wait(sends, self._PHLO_LIMIT)
        logging.info("  contention: %d cells under %s", self._cells, self._prefix)

    def term(self, index: int, key) -> Tuple[str, int]:
        channel = self._channel(index % self._cells)
        return f'for (@n <- @"{channel}") {{ @"{channel}"!(n + 1) }}', self._PHLO_LIMIT

    def verify(self, readonly, finalized: Dict[str, int]) -> List[str]:
        expected = [0] * self._cells
        for index in finalized.values():
            expected[index % self._cells] += 1
        problems = []
        for cell, want in enumerate(expected):
            got = readonly.read_channel(self._channel(cell))
            if got != want:
                problems.append(f"cell {cell}: {got} after {want} finalized increments")
        logging.info(
            "  contention check: %d of %d cells disagree with the finalized increments",
            len(problems),
            self._cells,
        )
        return problems


class NonceWorkload(Workload):
    """Each deploy reads and advances its own sender's counter: one counter
    per account, the state a per-account nonce would keep.

    Deploys from one sender conflict with each other whenever two of them
    land in parallel blocks; deploys from different senders never do. The
    final counters must equal each sender's finalized deploys.
    """

    name = "nonce"
    _PHLO_LIMIT = 100_000

    def __init__(self, sender_keys: Sequence):
        self._senders = {
            k.get_public_key().get_vault_address(): i for i, k in enumerate(sender_keys)
        }
        self._sender_of_index: Dict[int, int] = {}
        self._prefix = f"capacity-nonce-{int(time.time())}"

    def _channel(self, sender: int) -> str:
        return f"{self._prefix}-{sender}"

    def prepare(self, setup: SetupContext) -> None:
        sends = " | ".join(f'@"{self._channel(s)}"!(0)' for s in range(len(self._senders)))
        setup.deploy_and_wait(sends, self._PHLO_LIMIT)
        logging.info("  nonce: %d per-sender counters under %s", len(self._senders), self._prefix)

    def term(self, index: int, key) -> Tuple[str, int]:
        sender = self._senders[key.get_public_key().get_vault_address()]
        self._sender_of_index[index] = sender
        channel = self._channel(sender)
        return f'for (@n <- @"{channel}") {{ @"{channel}"!(n + 1) }}', self._PHLO_LIMIT

    def verify(self, readonly, finalized: Dict[str, int]) -> List[str]:
        expected = [0] * len(self._senders)
        for index in finalized.values():
            expected[self._sender_of_index[index]] += 1
        problems = []
        for sender, want in enumerate(expected):
            got = readonly.read_channel(self._channel(sender))
            if got != want:
                problems.append(f"sender {sender}: counter {got} after {want} finalized deploys")
        logging.info(
            "  nonce check: %d of %d counters disagree with the finalized deploys",
            len(problems),
            len(expected),
        )
        return problems


_INSTALL_TERM = """new insertArbitrary(`rho:registry:insertArbitrary`), deployId(`rho:system:deployId`),
    counter, uriCh in {
  contract counter(@n, ret) = { ret!(n + 1) } |
  insertArbitrary!(bundle+{*counter}, *uriCh) |
  for (@uri <- uriCh) { deployId!(uri) }
}"""


class InstallWorkload(Workload):
    """Each deploy installs a new contract in the registry and reports its URI.

    Every install adds a persistent contract and a registry entry, so this is
    the workload whose state grows with every deploy.
    """

    name = "install"
    _PHLO_LIMIT = 1_000_000

    def term(self, index: int, key) -> Tuple[str, int]:
        return _INSTALL_TERM, self._PHLO_LIMIT

    def verify(self, readonly, finalized: Dict[str, int]) -> List[str]:
        problems = []
        for deploy_id in _sample(finalized):
            uri = _deploy_value(readonly, deploy_id)
            if not (isinstance(uri, str) and uri.startswith("rho:id:")):
                problems.append(f"install {deploy_id[:16]} reported {uri!r}, not a registry URI")
        logging.info("  install check: %d problems", len(problems))
        return problems


class CallWorkload(Workload):
    """Each deploy looks up one of a pool of installed contracts and calls it.

    ``prepare`` installs ``contracts`` contracts; deploy ``index`` calls
    contract ``index % contracts`` with ``index`` and reports the reply,
    which must be ``index + 1``.
    """

    name = "call"
    _PHLO_LIMIT = 1_000_000

    def __init__(self, contracts: int):
        if contracts < 1:
            raise ValueError("calls need at least one installed contract")
        self._contracts = contracts
        self._uris: List[str] = []

    def prepare(self, setup: SetupContext) -> None:
        deploy_ids = setup.deploy_all_and_wait(
            [_INSTALL_TERM] * self._contracts, InstallWorkload._PHLO_LIMIT
        )
        for deploy_id in deploy_ids:
            uri = _deploy_value(setup.node, deploy_id)
            if not (isinstance(uri, str) and uri.startswith("rho:id:")):
                raise AssertionError(f"contract install reported {uri!r}, not a registry URI")
            self._uris.append(uri)
        logging.info("  call: %d contracts installed", len(self._uris))

    def term(self, index: int, key) -> Tuple[str, int]:
        uri = self._uris[index % len(self._uris)]
        term = (
            "new lookup(`rho:registry:lookup`), deployId(`rho:system:deployId`), ch, ret in {\n"
            f"  lookup!(`{uri}`, *ch) |\n"
            f"  for (c <- ch) {{ c!({index}, *ret) | for (@v <- ret) {{ deployId!(v) }} }}\n"
            "}"
        )
        return term, self._PHLO_LIMIT

    def verify(self, readonly, finalized: Dict[str, int]) -> List[str]:
        problems = []
        for deploy_id in _sample(finalized):
            reply = _deploy_value(readonly, deploy_id)
            if reply != finalized[deploy_id] + 1:
                problems.append(
                    f"call {deploy_id[:16]} replied {reply!r}, not {finalized[deploy_id] + 1}"
                )
        logging.info("  call check: %d problems", len(problems))
        return problems


class PayloadWorkload(Workload):
    """Each deploy stores a string of ``payload_bytes`` on a fresh name.

    The data stays in the tuple space, so this measures both block size and
    stored-state growth. The proposer's per-block deploy budget is 2 MiB
    (512 KiB under backpressure), and a deploy larger than the budget is
    admitted alone, so sizes near those bounds change how many deploys a
    block carries.
    """

    name = "payload"

    def __init__(self, payload_bytes: int):
        if payload_bytes < 1:
            raise ValueError("a payload needs at least one byte")
        self._bytes = payload_bytes
        # Storage is charged per byte; the limit leaves room for that and the
        # term's evaluation.
        self._phlo_limit = 1_000_000 + 100 * payload_bytes

    def term(self, index: int, key) -> Tuple[str, int]:
        filler = "x" * (self._bytes - len(str(index)))
        return f'new store in {{ store!("{index}{filler}") }}', self._phlo_limit


class MixWorkload(Workload):
    """Several workloads interleaved by weight, so every rate carries the same
    blend of deploy kinds. Each part prepares its own state, and checks the
    state its own finalized deploys produced."""

    name = "mix"

    def __init__(self, parts: Dict[str, Workload], weights: Dict[str, int]):
        if set(parts) != set(weights):
            raise ValueError("every weighted kind needs exactly one workload")
        self._parts = parts
        self._cycle = interleave(weights)
        self._kind_of_index: Dict[int, str] = {}

    def kind(self, index: int) -> str:
        return self._cycle[index % len(self._cycle)]

    def prepare(self, setup: SetupContext) -> None:
        with ThreadPoolExecutor(max_workers=len(self._parts)) as pool:
            for done in [pool.submit(part.prepare, setup) for part in self._parts.values()]:
                done.result()
        logging.info("  mix: %s", ",".join(f"{k}:{self._cycle.count(k)}" for k in self._parts))

    def term(self, index: int, key) -> Tuple[str, int]:
        kind = self.kind(index)
        self._kind_of_index[index] = kind
        return self._parts[kind].term(index, key)

    def verify(self, readonly, finalized: Dict[str, int]) -> List[str]:
        problems = []
        for kind, part in self._parts.items():
            own = {d: i for d, i in finalized.items() if self._kind_of_index[i] == kind}
            problems.extend(f"{kind}: {p}" for p in part.verify(readonly, own))
        return problems


MIX_KINDS = sorted(DEPLOY_KINDS) + ["transfer", "contention", "nonce", "install", "call", "payload"]

# Every stateless kind, the account-state kinds and the contract kinds, weighted
# toward the cheap ones a real shard sees most.
DEFAULT_MIX = "light:4,compute:2,data:2,heavy:1,transfer:2,install:1,call:2,contention:1,nonce:1"


def make_workload(
    name: str,
    sender_keys: Sequence,
    cells: int,
    contracts: int,
    payload_bytes: int,
    mix: str = "",
) -> Workload:
    if name == "mix":
        weights = parse_weights(mix, MIX_KINDS)
        parts = {
            kind: make_workload(kind, sender_keys, cells, contracts, payload_bytes)
            for kind in weights
        }
        return MixWorkload(parts, weights)
    if name == "transfer":
        return TransferWorkload(sender_keys)
    if name == "contention":
        return ContentionWorkload(cells)
    if name == "nonce":
        return NonceWorkload(sender_keys)
    if name == "install":
        return InstallWorkload()
    if name == "call":
        return CallWorkload(contracts)
    if name == "payload":
        return PayloadWorkload(payload_bytes)
    return KindWorkload(name)
