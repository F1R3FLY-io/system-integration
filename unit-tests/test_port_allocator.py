import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from test.infra.ports import PortAllocator  # noqa: E402


def test_default_listener_range_starts_in_reserved_host_span(monkeypatch):
    monkeypatch.setattr(PortAllocator, "_is_port_free", staticmethod(lambda _port: True))

    allocation = PortAllocator(worker_id="gw0").allocate()

    assert allocation.protocol == 41000
    assert allocation.admin == 41005


def test_invalid_listener_range_is_rejected():
    with pytest.raises(RuntimeError, match="invalid test listener port range"):
        PortAllocator(base=33000, ceiling=33000)


def test_worker_partition_honors_custom_base_and_ceiling(monkeypatch):
    monkeypatch.setattr(PortAllocator, "_is_port_free", staticmethod(lambda _port: True))

    allocation = PortAllocator(base=20000, ceiling=22000, worker_id="gw2").allocate()

    assert allocation.protocol == 21000
    assert allocation.admin == 21005


def test_allocation_skips_an_entire_block_when_one_port_is_busy(monkeypatch):
    monkeypatch.setattr(
        PortAllocator,
        "_is_port_free",
        staticmethod(lambda port: port != 12002),
    )

    allocation = PortAllocator(base=12000, ceiling=12012).allocate()

    assert allocation.protocol == 12006
    assert allocation.admin == 12011


def test_worker_outside_configured_capacity_is_rejected():
    with pytest.raises(RuntimeError, match="invalid test listener port range"):
        PortAllocator(base=12000, ceiling=32000, worker_id="gw40")
