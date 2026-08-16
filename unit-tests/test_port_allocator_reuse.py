"""Port-block reuse in the session PortAllocator.

Soak preflight 31919610258: the custom suite runs many sequential
create-and-destroy shard cycles inside one single-worker pytest session.
The allocator was strictly monotonic — freed blocks were never reused —
so gw0's 500-port range (83 six-port blocks) exhausted at 46% of the
suite: 14 setup errors and 2 failures, all
"test port range exhausted (41000-41500)".

These tests pin the fix: providers release a node's block on teardown,
released blocks are served (bind-probed) before fresh range, and a block
that is handed out but not yet bound by its node can never be handed out
a second time.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from test.infra.ports import _BLOCK_SIZE, PortAllocator  # noqa: E402

# A private base far from the real test range so these tests never fight
# live shards or TIME_WAIT from integration runs.
BASE = 45600

# Ports the fake probe reports as busy. Tests mutate this instead of
# binding real sockets.
BUSY_PORTS: set = set()


@pytest.fixture(autouse=True)
def deterministic_probe(monkeypatch):
    """Decouple the bind-probe from the host's real port state.

    Shared CI runners bind arbitrary ports: run 31923986341 had one of
    this file's two-block range occupied, so ``allocate()`` skipped the
    block and the exhaustion test raised one call early. With the probe
    faked, every test controls busyness exclusively via ``BUSY_PORTS``
    and the allocator logic is exercised deterministically everywhere.
    """
    BUSY_PORTS.clear()
    monkeypatch.setattr(
        PortAllocator,
        "_is_port_free",
        staticmethod(lambda port: port not in BUSY_PORTS),
    )
    yield
    BUSY_PORTS.clear()


def _allocator(blocks: int) -> PortAllocator:
    return PortAllocator(base=BASE, ceiling=BASE + blocks * _BLOCK_SIZE)


def test_released_block_is_reused_before_fresh_range():
    alloc = _allocator(blocks=4)
    first = alloc.allocate()
    alloc.release(first)
    second = alloc.allocate()
    assert second.protocol == first.protocol


def test_reuse_is_fifo_over_multiple_released_blocks():
    alloc = _allocator(blocks=4)
    a, b = alloc.allocate(), alloc.allocate()
    alloc.release(a)
    alloc.release(b)
    assert alloc.allocate().protocol == a.protocol
    assert alloc.allocate().protocol == b.protocol


def test_double_release_cannot_hand_out_a_block_twice():
    alloc = _allocator(blocks=4)
    a = alloc.allocate()
    alloc.release(a)
    alloc.release(a)  # second call must be a no-op
    reused = alloc.allocate()
    fresh = alloc.allocate()
    assert reused.protocol == a.protocol
    assert fresh.protocol != a.protocol


def test_release_of_foreign_block_is_ignored():
    """Adopted handles (e.g. --skip-setup session adoption) carry port
    mappings this allocator never issued; releasing them must not
    poison the free-list."""
    from test.infra.types import PortMapping

    alloc = _allocator(blocks=2)
    alloc.release(PortMapping.from_base(BASE + 10 * _BLOCK_SIZE))
    a = alloc.allocate()
    assert a.protocol == BASE  # fresh range, nothing served from free-list


def test_busy_released_block_is_skipped_and_requeued():
    """A released block still bound (TIME_WAIT, leftover process) must
    not be handed out; it stays queued for a later attempt."""
    alloc = _allocator(blocks=3)
    a = alloc.allocate()
    alloc.release(a)
    BUSY_PORTS.add(a.protocol)
    b = alloc.allocate()
    assert b.protocol != a.protocol  # served fresh instead
    # Port freed: the requeued block is served again.
    BUSY_PORTS.discard(a.protocol)
    c = alloc.allocate()
    assert c.protocol == a.protocol


def test_sequential_shard_cycles_never_exhaust_the_range():
    """The soak regression: far more create/destroy cycles than the
    range has blocks. 3 blocks of range, 5-node 'shards', 20 cycles =
    100 allocations — impossible without reuse."""
    alloc = _allocator(blocks=6)
    for _ in range(20):
        shard = [alloc.allocate() for _ in range(5)]
        for mapping in shard:
            alloc.release(mapping)


def test_exhaustion_still_raises_when_blocks_are_genuinely_held():
    alloc = _allocator(blocks=2)
    alloc.allocate()
    alloc.allocate()
    with pytest.raises(RuntimeError, match="port range exhausted"):
        alloc.allocate()


# ── Provider wiring is the real one ─────────────────────────────────

_PROVIDER_DIR = REPO_ROOT / "integration-tests/test/infra/providers"


@pytest.mark.parametrize("name", ["docker", "subprocess"])
def test_providers_release_ports_on_every_teardown_path(name):
    # destroy_shard + remove_node + destroy_standalone
    src = (_PROVIDER_DIR / f"{name}.py").read_text()
    assert src.count("._ports.release(") >= 3, (
        f"{name} provider must release port blocks on shard destroy, node "
        f"removal, and standalone destroy"
    )
