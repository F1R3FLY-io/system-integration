"""A shard config can start a node on given host CPUs.

Pins the config's validation of per-node CPU sets, the subprocess provider's
command prefix that applies them from the node's first instruction, and that a
node started that way sees only those CPUs.
"""

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

config_mod = importlib.import_module("test.infra.config")
keys = importlib.import_module("test.infra.keys")
provider = importlib.import_module("test.infra.providers.subprocess")

# CPU affinity is Linux-only: macOS has no os.sched_getaffinity and no taskset.
requires_affinity = pytest.mark.skipif(
    not hasattr(os, "sched_getaffinity"), reason="needs os.sched_getaffinity (Linux)"
)
requires_taskset = pytest.mark.skipif(
    shutil.which("taskset") is None, reason="needs taskset (util-linux)"
)


def _config(**overrides):
    params = dict(bonds=[(keys.VALIDATOR1_ID, 100)], include_readonly=True)
    params.update(overrides)
    return config_mod.ShardConfig(**params)


def test_config_rejects_cpus_for_a_node_the_shard_does_not_have():
    with pytest.raises(ValueError, match="does not have"):
        _config(per_node_cpus={"validator2": frozenset({0})})


@requires_affinity
@requires_taskset
def test_prefix_rejects_cpus_outside_this_process_affinity():
    with pytest.raises(ValueError, match="non-empty subset"):
        provider._cpu_affinity_prefix({10_000})


def test_prefix_refuses_to_pin_without_cpu_affinity(monkeypatch):
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    with pytest.raises(RuntimeError, match="needs Linux"):
        provider._cpu_affinity_prefix({0})


def test_prefix_refuses_to_pin_without_taskset(monkeypatch):
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0}, raising=False)
    monkeypatch.setattr(provider.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="needs taskset"):
        provider._cpu_affinity_prefix({0})


def test_prefix_is_empty_when_no_cpus_are_given(monkeypatch):
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    assert provider._cpu_affinity_prefix(None) == []


@requires_affinity
@requires_taskset
def test_a_process_started_with_the_prefix_sees_only_those_cpus():
    cpu = min(os.sched_getaffinity(0))
    seen = subprocess.run(
        [
            *provider._cpu_affinity_prefix({cpu}),
            sys.executable,
            "-c",
            "import os; print(sorted(os.sched_getaffinity(0)))",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert seen == f"[{cpu}]"
