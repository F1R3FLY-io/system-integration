"""Full PoS validator lifecycle on a dedicated shard.

One comprehensive test walking three joiner validators (V4/V5/V6) through the
entire PoS lifecycle with INTERLEAVED concurrent bond and unbond — bonds and
unbonds in flight in the same blocks — to stress the multi-parent merge of PoS
state (the surface bug-d lived on). Runs on its own ``provider.create_shard``
shard (3 genesis validators + 3 joiners + readonly), destroyed at the end.

Coverage (see project-validator-lifecycle-test-design memory for the matrix):
  - bond: happy, already-bonded, below-minimum, above-maximum, Mode-B deposit
    failure (contract (false, msg) returns, not deploy failures)
  - concurrent multi-bond + cross-node bonds-cache consistency (grow)
  - withdraw: pending -> multi-element epoch-move -> multi-element quarantine
    payout; not-bonded; double-withdraw edge
  - concurrent shrink (move withdrawers out) + grow (activate a joiner) at one
    closeBlock; cross-node consistency on the shrink
  - post-unbond can't-propose; re-bond; everBonded re-bond guard
  - reward behavioral cases 1-5 (accrues / idle-frozen / withdrawn-frozen /
    paid-at-quarantine / directional proportionality by stake)
  - read sanity: getCoopVault / getInitialPosVault

All PoS state reads go through the readonly node: validators reject
exploratory deploy unless dev-mode (block_api.rs:1563), and the integration
shard runs dev-mode=false. Finalization waits use the retired bonding test's
per-wait load multipliers (finalization * 2/3/5, deploy_inclusion * 3/5) to
absorb the lumpy, batched finalization that always-on bg_load produces, plus
the epoch_transition budget for the inherently multi-block epoch-move and
quarantine polls.
"""
import logging
import threading
from typing import Dict, List, Optional, Tuple

import pytest
from f1r3fly.client import F1r3flyClientException
from f1r3fly.crypto import PrivateKey

from ...infra.assertions import (
    assert_block_finalized_on_all_nodes,
    assert_bonds_map_consistent_across_nodes,
)
from ...infra.config import ShardConfig
from ...infra.keys import (
    VALIDATOR1_ID,
    VALIDATOR2_ID,
    VALIDATOR3_ID,
    VALIDATOR4_ID,
    VALIDATOR5_ID,
    VALIDATOR6_ID,
)
from ...infra.polling import (
    poll_until,
    wait_for_block_visible,
    wait_for_block_visible_on_all_nodes,
    wait_for_deploy_included,
    wait_for_finalized,
)
from ...infra.shard import Shard

pytestmark = pytest.mark.xdist_group("custom")

# ── Shard parameters ───────────────────────────────────────────────────────
_GENESIS_STAKE = 100
# Distinct joiner stakes so reward weight (= bond / bond-minimum = bond / 100)
# differs: V4->2, V5->3, V6->4 vs genesis 1. Drives case 5 proportionality.
_JOINER_STAKE = {
    VALIDATOR4_ID.name: 200,
    VALIDATOR5_ID.name: 300,
    VALIDATOR6_ID.name: 400,
}
_BOND_MINIMUM = 100
_BOND_MAXIMUM = 1000
# These mirror conf/rust.conf (epoch-length=4, quarantine-length=10), which the
# shard already boots from — kept as constants for quarantine/epoch poll-budget
# math, NOT passed as CLI flags (that would be redundant with rust.conf).
_EPOCH_LENGTH = 4
_QUARANTINE_LENGTH = 10

_WALLET_BALANCE = 50_000_000_000_000_000  # joiners: cover phlo + stake
_BOND_PHLO_LIMIT = 100_000_000
_BOND_PHLO_PRICE = 1

# Only the bond bounds genuinely deviate from rust.conf / node defaults.
# epoch-length, quarantine-length, and synchrony-constraint-threshold=0 are
# already the effective values from conf/rust.conf + node defaults, so passing
# them as CLI flags would be redundant.
_GENESIS_CLI = {
    "--bond-minimum": str(_BOND_MINIMUM),
    "--bond-maximum": str(_BOND_MAXIMUM),
}

# ── Background load: same-vault transfer contention ──────────────────────────
# Repeatedly transfer a tiny amount from a single funded source vault to a
# single dest vault, round-robin across the genesis validators. Concurrent
# transfers on different proposers contend the SAME two balance channels
# (IntegerAdd merge — Check #4 / number_channels_data), the production-relevant
# merge surface, and produce netPhlo so rewards accrue.
_BG_SRC_KEY = PrivateKey.from_seed(70001)
_BG_DST_KEY = PrivateKey.from_seed(70002)
_BG_SRC_ADDR = _BG_SRC_KEY.get_public_key().get_vault_address()
_BG_DST_ADDR = _BG_DST_KEY.get_public_key().get_vault_address()
_BG_INTERVAL = 2.0  # retired-test load level; higher rates stall finalization
_BG_TRANSFER_AMOUNT = 1

# Throwaway deployer keys for the bond/withdraw rejection branches. They must be
# funded so the deploy precharges successfully and the contract reaches its
# amount/bond checks — an unfunded key would fail precharge (out-of-phlo)
# instead of returning the clean (false, reason) we assert.
_THROWAWAY_BOND_KEY = PrivateKey.from_seed(80001)
_THROWAWAY_WITHDRAW_KEY = PrivateKey.from_seed(80002)


class _BackgroundLoad:
    """Same-vault transfer generator, round-robin across producer nodes.

    Always-on by default; ``pause()``/``resume()`` quiet it at finalization
    gates and the epoch-move, where fork-choice contention would otherwise
    block cluster-wide finalization.
    """

    def __init__(self, producers: List, interval: float = _BG_INTERVAL) -> None:
        self._producers = producers
        self._interval = interval
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._counter = 0
        self._errors = 0
        self._deploy_ids: List[str] = []
        self._lock = threading.Lock()

    def deploy_ids(self) -> List[str]:
        """Snapshot of every transfer deploy id submitted so far."""
        with self._lock:
            return list(self._deploy_ids)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("BackgroundLoad already started")
        self._thread = threading.Thread(target=self._run, daemon=True, name="lifecycle-bg-load")
        self._thread.start()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def stop(self, join_timeout: float = 10.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=join_timeout)
        logging.info(
            "Background load stopped: %d transfers, %d errors", self._counter, self._errors
        )
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._paused.is_set():
                node = self._producers[self._counter % len(self._producers)]
                try:
                    did = node.get_vault().transfer(
                        _BG_SRC_ADDR,
                        _BG_DST_ADDR,
                        _BG_TRANSFER_AMOUNT,
                        _BG_SRC_KEY,
                        phlo_price=1,
                        phlo_limit=_BOND_PHLO_LIMIT,
                    )
                    with self._lock:
                        self._deploy_ids.append(did)
                except Exception as e:  # noqa: BLE001 — submit errors are surfaced via the
                    # finalization assertion (missing deploy), not swallowed silently.
                    self._errors += 1
                    logging.warning(
                        "bg-load transfer %d on %s failed: %s", self._counter, node.name, e
                    )
                self._counter += 1
            self._stop.wait(self._interval)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _vault_addr(identity) -> str:
    return identity.private_key().get_public_key().get_vault_address()


def _assert_bond_rejected(
    proposer, all_nodes, ro, key, amount: int, expected_reason: str, timeouts
) -> None:
    """A rejected bond is a SUCCESSFUL deploy whose contract returns
    ``(false, reason)`` — assert the reason and that the bonds map is
    unchanged, NOT that the deploy errored. Finalization is asserted across
    ALL nodes, with retired-test load budgets (deploy_inclusion * 3 /
    finalization * 3) since this runs under always-on bg_load.
    """
    bonds_before = ro.pos.get_bonds()
    deploy_id = proposer.pos.bond(key, amount)
    # Runs under bg_load; scale like retired Phase 4 (deploy_inclusion * 3 /
    # finalization * 3) so a lumpy under-load finalization doesn't false-fail.
    block = wait_for_deploy_included(proposer, deploy_id, timeouts.deploy_inclusion * 3)
    wait_for_finalized(proposer, block.blockNumber, timeouts.finalization * 3)
    assert_block_finalized_on_all_nodes(
        all_nodes, block.blockHash, timeout=timeouts.finalization * 3
    )
    result = proposer.pos.read_result(deploy_id, block.blockHash)
    assert not result.success, f"expected bond rejection, got success: {result}"
    assert (
        expected_reason in result.reason
    ), f"expected reason containing {expected_reason!r}, got {result.reason!r}"
    bonds_after = ro.pos.get_bonds()
    assert (
        bonds_after == bonds_before
    ), f"rejected bond changed bonds map: before={bonds_before} after={bonds_after}"
    logging.info("Bond correctly rejected (%s): %s", expected_reason, result.reason)


def _assert_withdraw_rejected(actor, all_nodes, ro, key, expected_reason: str, timeouts) -> None:
    bonds_before = ro.pos.get_bonds()
    deploy_id = actor.pos.withdraw(key)
    # Runs under bg_load; scale like retired Phase 4 (deploy_inclusion * 3 /
    # finalization * 3) so a lumpy under-load finalization doesn't false-fail.
    block = wait_for_deploy_included(actor, deploy_id, timeouts.deploy_inclusion * 3)
    wait_for_finalized(actor, block.blockNumber, timeouts.finalization * 3)
    assert_block_finalized_on_all_nodes(
        all_nodes, block.blockHash, timeout=timeouts.finalization * 3
    )
    result = actor.pos.read_result(deploy_id, block.blockHash)
    assert not result.success, f"expected withdraw rejection, got success: {result}"
    assert (
        expected_reason in result.reason
    ), f"expected reason containing {expected_reason!r}, got {result.reason!r}"
    assert ro.pos.get_bonds() == bonds_before, "rejected withdraw changed bonds map"
    logging.info("Withdraw correctly rejected (%s): %s", expected_reason, result.reason)


def _validators_on(ro) -> Dict[str, int]:
    """{publicKey_hex: stake} from the readonly node's /api/validators."""
    resp = ro.api_get("/validators")
    return {v["publicKey"]: v["stake"] for v in resp["validators"]}


_GENESIS_BY_NAME = {
    "validator1": VALIDATOR1_ID,
    "validator2": VALIDATOR2_ID,
    "validator3": VALIDATOR3_ID,
}


def _attach_prebond(shard, identity, timeouts):
    """Bring a joiner ONLINE (unbonded) and verify the pre-bond invariants.

    A bonded validator with no running, participating node is a silent
    participant that stalls finalization, so every joiner must be a live node
    before it is bonded. Sub-steps (retired ``_bond_lifecycle`` 1-2):
      1.  pre-bond: joiner not yet in bonds map
      1b. attach node + LFS-sync to current LFB
      2.  joiner cannot propose pre-bond

    Returns the attached joiner ``Node``.
    """
    pk = identity.public_hex
    current_lfb = shard.node("validator1").last_finalized_block()
    bonds_pre = {b.validator: b.stake for b in current_lfb.blockInfo.bonds}
    assert pk not in bonds_pre, f"{identity.name} already bonded pre-bond: {sorted(bonds_pre)}"

    joiner = shard.attach_joiner(identity)
    # LFS-sync budget scales with shard age (deep LFB + side-branch history under
    # bg_load); matches retired _bond_lifecycle (finalization * 3).
    wait_for_block_visible(
        joiner, current_lfb.blockInfo.blockHash, timeout=timeouts.finalization * 3
    )

    joiner.deploy_string(
        f'@"pre-bond-{identity.name}"!(0)',
        identity.private_key(),
        phlo_limit=_BOND_PHLO_LIMIT,
        phlo_price=_BOND_PHLO_PRICE,
    )
    with pytest.raises(F1r3flyClientException):
        joiner.propose()
    return joiner


def _submit_bonds(submissions, timeouts):
    """Submit one or more bond deploys, then await each for inclusion + success.

    ``submissions``: list of ``(proposer, identity, stake)``. Deploys are
    submitted in order, then each is awaited for block inclusion and asserted to
    have returned contract success. With a single submission this is a plain
    sequential bond; with several it submits them back-to-back before awaiting,
    so the bonds can land in overlapping blocks (the concurrent-grow surface).

    Returns a list of dicts ``{proposer, identity, stake, deploy_id, bond_block}``
    in submission order.
    """
    pending = []
    for proposer, identity, stake in submissions:
        deploy_id = proposer.pos.bond(identity.private_key(), stake)
        pending.append((proposer, identity, stake, deploy_id))

    results = []
    for proposer, identity, stake, deploy_id in pending:
        # Bond inclusion under heartbeat-only config depends on the next
        # heartbeat round after attach; matches retired (deploy_inclusion * 3).
        bond_block = wait_for_deploy_included(proposer, deploy_id, timeouts.deploy_inclusion * 3)
        assert proposer.pos.read_result(
            deploy_id, bond_block.blockHash
        ).success, f"{identity.name} bond should succeed"
        results.append(
            {
                "proposer": proposer,
                "identity": identity,
                "stake": stake,
                "deploy_id": deploy_id,
                "bond_block": bond_block,
            }
        )
    return results


def _activate_and_verify_participation(shard, ro, proposer, joiner, identity, bond_block, timeouts):
    """Activate a bonded joiner and verify it participates (retired sub-phases 5-7).

    bg_load stays ON throughout (default-on policy). If the joiner's first
    post-activation block fails to finalize under load, that is a real signal to
    surface — not something to silently paper over by pausing the load.

      5. LFB advances past the epoch boundary (activation)
      6. joiner proposes a block that finalizes ON ALL NODES
      7. another validator justifies the joiner in a later block, finalized ON ALL NODES
    """
    pk = identity.public_hex
    # ``node.name`` is the full handle name (e.g. "rnode.test.<sid>.validator1");
    # the role key is its dotted suffix, matching how Shard derives _nodes keys.
    proposer_role = proposer.name.split(".")[-1]
    proposer_id = _GENESIS_BY_NAME[proposer_role]

    # 5. Advance past the epoch boundary (activation). Matches retired
    # (finalization * 2): the boundary is multiple blocks out under load.
    epoch_target = bond_block.blockNumber + _EPOCH_LENGTH
    poll_until(
        predicate=lambda: (
            proposer.last_finalized_block().blockInfo.blockNumber
            if proposer.last_finalized_block().blockInfo.blockNumber >= epoch_target
            else None
        ),
        timeout=timeouts.finalization * 2,
        interval=timeouts.poll_interval,
        description=f"LFB advances past epoch boundary #{epoch_target} for {identity.name}",
    )

    # 6. Joiner proposes a block that finalizes on all nodes.
    joiner.deploy_string(
        f'@"active-{identity.name}"!(1)',
        identity.private_key(),
        phlo_limit=_BOND_PHLO_LIMIT,
        phlo_price=_BOND_PHLO_PRICE,
    )

    def _joiner_proposed():
        for blk in joiner.get_blocks(50):
            if blk.sender == pk and blk.blockNumber > bond_block.blockNumber:
                return blk
        return None

    joiner_block = poll_until(
        predicate=_joiner_proposed,
        timeout=timeouts.finalization * 2,
        interval=timeouts.poll_interval,
        description=f"{identity.name} proposes a block post-activation",
    )
    # Widen the finalize budget vs base finalization to absorb load (finalization
    # under bg_load finalizes in lumpy batches); matches retired (finalization * 5).
    wait_for_finalized(joiner, joiner_block.blockNumber, timeouts.finalization * 5)
    assert_block_finalized_on_all_nodes(
        shard.all_nodes, joiner_block.blockHash, timeout=timeouts.finalization * 5
    )

    # 7. Another validator justifies the joiner in a later finalized block.
    proposer.deploy_string(
        f'@"after-{identity.name}"!(2)',
        proposer_id.private_key(),
        phlo_limit=_BOND_PHLO_LIMIT,
        phlo_price=_BOND_PHLO_PRICE,
    )

    def _proposer_justifies_joiner():
        for blk in proposer.get_blocks(50):
            if blk.blockNumber <= joiner_block.blockNumber:
                continue
            if blk.sender != proposer_id.public_hex:
                continue
            if any(j.validator == pk for j in blk.justifications):
                return blk
        return None

    just_block = poll_until(
        predicate=_proposer_justifies_joiner,
        timeout=timeouts.finalization * 2,
        interval=timeouts.poll_interval,
        description=f"{proposer.name} produces a block justifying {identity.name}",
    )
    wait_for_finalized(proposer, just_block.blockNumber, timeouts.finalization * 5)
    assert_block_finalized_on_all_nodes(
        shard.all_nodes, just_block.blockHash, timeout=timeouts.finalization * 5
    )
    logging.info("%s activated, proposes, and is justified (all nodes)", identity.name)


def _assert_full_liveness(shard, active, timeouts):
    """Every currently-active validator (incl. joiners) produces a block that
    finalizes and is visible ON ALL NODES.

    ``active``: list of ``(node, identity)``. Stricter than per-joiner liveness:
    asserted over the full active set after all joiners are participating, so a
    validator that silently stops contributing post-activation is caught.
    """
    # Post-bond liveness budgets match retired (deploy_inclusion * 5,
    # finalization * 5) to absorb lumpy under-load finalization.
    for node, node_id in active:
        live_id = node.deploy_string(
            f'@"liveness-{node.name}"!(1)',
            node_id.private_key(),
            phlo_limit=_BOND_PHLO_LIMIT,
            phlo_price=_BOND_PHLO_PRICE,
        )
        live_block = wait_for_deploy_included(node, live_id, timeouts.deploy_inclusion * 5)
        wait_for_finalized(node, live_block.blockNumber, timeouts.finalization * 5)
        wait_for_block_visible_on_all_nodes(
            shard.all_nodes, live_block.blockHash, timeout=timeouts.finalization * 5
        )
        assert_block_finalized_on_all_nodes(
            shard.all_nodes, live_block.blockHash, timeout=timeouts.finalization * 5
        )


def _wait_for_active(ro, pubkey_hex: str, present: bool, timeouts, label: str):
    """Poll /validators until pubkey is present (or absent) in the active set."""
    poll_until(
        predicate=lambda: True if (pubkey_hex in _validators_on(ro)) == present else None,
        timeout=timeouts.epoch_transition,
        interval=timeouts.poll_interval,
        description=label,
    )


def _wait_for_payout(ro, pubkey_hex: str, timeouts, label: str):
    """Poll get_withdrawers until pubkey is gone (quarantine elapsed, paid)."""
    poll_until(
        predicate=lambda: True if pubkey_hex not in ro.pos.get_withdrawers() else None,
        timeout=timeouts.epoch_transition,
        interval=timeouts.poll_interval,
        description=label,
    )


def _assert_bg_load_deploys_finalized(
    producers, all_nodes, deploy_ids: List[str], timeouts, label: str = "bg-load"
) -> None:
    """Every background-load transfer must land in a block that finalizes ON ALL
    NODES.

    Strict regression detector for the fork-choice + multi-parent-merge orphan
    path: a transfer that lands in a losing-fork block and is never merged into
    a finalized descendant is silently dropped work. Same-vault transfers from a
    well-funded source never fail for balance reasons, so any missing or
    unfinalized deploy here is a real merge/orphan regression — zero tolerance.

    Finalization is verified on EVERY node (not just the proposer): a block can
    finalize on its proposer but be rejected/never-finalized on a peer.
    """
    if not deploy_ids:
        return
    missing: List[str] = []
    unfinalized: List[Tuple[str, int]] = []
    for sig in deploy_ids:
        light_block = None
        for node in producers:
            try:
                light_block = node.find_deploy(sig)
                if light_block is not None:
                    break
            except Exception:  # noqa: BLE001 — try the next producer
                continue
        if light_block is None:
            missing.append(sig)
            continue
        try:
            assert_block_finalized_on_all_nodes(
                all_nodes, light_block.blockHash, timeout=timeouts.finalization * 2
            )
        except Exception:  # noqa: BLE001 — any node not finalized counts as unfinalized
            unfinalized.append((sig, light_block.blockNumber))
    assert not missing and not unfinalized, (
        f"[{label}] {len(missing)} bg-load deploys never included, "
        f"{len(unfinalized)} included but not finalized on all nodes (of {len(deploy_ids)} total). "
        f"missing(first 3)={[s[:16] for s in missing[:3]]} "
        f"unfinalized(first 3)={[(s[:16], n) for s, n in unfinalized[:3]]}"
    )


# ── Fixture: dedicated lifecycle shard ───────────────────────────────────────


@pytest.fixture(scope="module")
def lifecycle_shard(provider, timeouts):
    extra_wallets = [
        (_vault_addr(ident), _WALLET_BALANCE)
        for ident in (VALIDATOR4_ID, VALIDATOR5_ID, VALIDATOR6_ID)
    ]
    extra_wallets.append((_BG_SRC_ADDR, _WALLET_BALANCE))
    extra_wallets.append((_BG_DST_ADDR, _WALLET_BALANCE))
    extra_wallets.append(
        (_THROWAWAY_BOND_KEY.get_public_key().get_vault_address(), _WALLET_BALANCE)
    )
    extra_wallets.append(
        (_THROWAWAY_WITHDRAW_KEY.get_public_key().get_vault_address(), _WALLET_BALANCE)
    )

    config = ShardConfig(
        bonds=[
            (VALIDATOR1_ID, _GENESIS_STAKE),
            (VALIDATOR2_ID, _GENESIS_STAKE),
            (VALIDATOR3_ID, _GENESIS_STAKE),
        ],
        ftt=0.1,
        heartbeat=True,
        include_readonly=True,
        global_cli_options=_GENESIS_CLI,
        extra_wallets=extra_wallets,
    )
    shard = Shard.create(provider, config, timeouts)
    try:
        yield shard
    finally:
        shard.destroy()


def test_validator_lifecycle(lifecycle_shard, timeouts) -> None:
    shard = lifecycle_shard
    v1, v2, v3 = (shard.node("validator1"), shard.node("validator2"), shard.node("validator3"))
    ro = shard.readonly

    v4_pk, v5_pk, v6_pk = (
        VALIDATOR4_ID.public_hex,
        VALIDATOR5_ID.public_hex,
        VALIDATOR6_ID.public_hex,
    )

    bg = _BackgroundLoad([v1, v2, v3])
    bg.start()
    try:
        _run_lifecycle(shard, v1, v2, v3, ro, v4_pk, v5_pk, v6_pk, bg, timeouts)
    finally:
        bg.stop()
    # Strict: every background-load transfer submitted during the lifecycle
    # must have finalized on ALL nodes (no orphaned/dropped work under the merge
    # contention). Producers [v1,v2,v3] are searched to locate each deploy's
    # block; finalization is asserted across the full node set.
    _assert_bg_load_deploys_finalized([v1, v2, v3], shard.all_nodes, bg.deploy_ids(), timeouts)


def _run_lifecycle(shard, v1, v2, v3, ro, v4_pk, v5_pk, v6_pk, bg, timeouts) -> None:
    # ── Phase 1: SEQUENTIAL bond+activate V4, then V5 ────────────────────────
    # Each joiner is brought online (LFS-synced, can't-propose verified), bonded
    # alone, finalized + bonds-cache-consistent across all nodes, then activated
    # and verified to propose + be justified before the next joiner starts. This
    # matches the green-baseline ordering. The ONLY behavioral delta from that
    # baseline is bg_load policy: bg_load runs throughout (always-on) instead of
    # being stopped after V4 activation. Finalization budgets match the retired
    # test's per-wait multipliers (finalization * 3/5, etc.) to absorb lumpy
    # under-load finalization, asserted over ALL nodes read fresh.
    bonds_pre = {b.validator: b.stake for b in v1.last_finalized_block().blockInfo.bonds}
    assert len(bonds_pre) == 3, f"expected 3 genesis bonds pre-Phase-1: {sorted(bonds_pre)}"

    joiners = {}
    for proposer, identity, expected_after in (
        (v1, VALIDATOR4_ID, 4),
        (v2, VALIDATOR5_ID, 5),
    ):
        pk = identity.public_hex
        stake = _JOINER_STAKE[identity.name]
        joiner = _attach_prebond(shard, identity, timeouts)
        joiners[pk] = joiner

        [r] = _submit_bonds([(proposer, identity, stake)], timeouts)
        bond_block = r["bond_block"]

        # Bond block finalizes on all nodes; bonds map consistent on all nodes.
        # Matches retired Phase 4 (finalization * 3).
        wait_for_finalized(proposer, bond_block.blockNumber, timeouts.finalization * 3)
        assert_block_finalized_on_all_nodes(
            shard.all_nodes, bond_block.blockHash, timeout=timeouts.finalization * 3
        )
        bonds_now = {
            b.validator: b.stake for b in proposer.get_block(bond_block.blockHash).blockInfo.bonds
        }
        assert (
            pk in bonds_now and bonds_now[pk] == stake
        ), f"{identity.name} not bonded at its bond block: {sorted(bonds_now)}"
        assert len(bonds_now) == expected_after, (
            f"expected {expected_after} bonds after {identity.name}, got {len(bonds_now)}: "
            f"{sorted(bonds_now)}"
        )
        assert_bonds_map_consistent_across_nodes(
            shard.all_nodes, bond_block.blockHash, bonds_now, timeout=timeouts.finalization * 3
        )
        _wait_for_active(ro, pk, True, timeouts, f"{identity.name} in /validators")

        # Activate + verify it proposes and is justified before moving on.
        _activate_and_verify_participation(
            shard, ro, proposer, joiner, identity, bond_block, timeouts
        )

    j4, j5 = joiners[v4_pk], joiners[v5_pk]

    # Full-set liveness once both joiners are active: every validator in the
    # 5-node active set produces a block finalized + visible on all nodes.
    _assert_full_liveness(
        shard,
        [
            (v1, VALIDATOR1_ID),
            (v2, VALIDATOR2_ID),
            (v3, VALIDATOR3_ID),
            (j4, VALIDATOR4_ID),
            (j5, VALIDATOR5_ID),
        ],
        timeouts,
    )

    validators = _validators_on(ro)
    assert validators.get(v4_pk) == _JOINER_STAKE["validator4"], f"V4 stake: {validators}"
    assert validators.get(v5_pk) == _JOINER_STAKE["validator5"], f"V5 stake: {validators}"
    logging.info(
        "Phase 1: V4+V5 bonded SEQUENTIALLY, activated, and participating "
        "(all nodes; bg_load always-on)"
    )

    # ── Phase 2: negatives (woven, non-mutating) ─────────────────────────────
    _assert_bond_rejected(
        v1,
        shard.all_nodes,
        ro,
        VALIDATOR4_ID.private_key(),
        _JOINER_STAKE["validator4"],
        "already bonded",
        timeouts,
    )
    _assert_bond_rejected(
        v1,
        shard.all_nodes,
        ro,
        _THROWAWAY_BOND_KEY,
        _BOND_MINIMUM - 50,
        "less than minimum",
        timeouts,
    )
    _assert_bond_rejected(
        v1,
        shard.all_nodes,
        ro,
        _THROWAWAY_BOND_KEY,
        _BOND_MAXIMUM + 1000,
        "greater than maximum",
        timeouts,
    )
    _assert_withdraw_rejected(
        v1, shard.all_nodes, ro, _THROWAWAY_WITHDRAW_KEY, "not bonded", timeouts
    )
    logging.info("Phase 2: bond/withdraw rejection branches verified")

    # NOTE: Mode-B deposit-fail + double-withdraw edge + reward windows + the
    # concurrent bond+unbond stages + payout + re-bond follow here. Built
    # incrementally and tuned against a live run (Mode-B phlo_limit/balance
    # tuning, poll budgets). This first slice establishes the shard, bonding,
    # cross-node consistency, the rejection branches, and the strict bg-load
    # finalization guard end-to-end. bg_load runs throughout (paused only at the
    # joiner-activation and epoch-move windows once those phases exist).
