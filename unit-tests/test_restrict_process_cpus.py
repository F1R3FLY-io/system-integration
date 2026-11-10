"""A shard config can start a node on given host CPUs.

Pins the config's validation of per-node CPU sets, the subprocess provider's
command prefix that applies them from the node's first instruction, and that a
node started that way sees only those CPUs.
"""

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

config_mod = importlib.import_module("test.infra.config")
keys = importlib.import_module("test.infra.keys")
provider = importlib.import_module("test.infra.providers.subprocess")


def _config(**overrides):
    params = dict(bonds=[(keys.VALIDATOR1_ID, 100)], include_readonly=True)
    params.update(overrides)
    return config_mod.ShardConfig(**params)


def test_config_accepts_cpus_for_its_own_nodes():
    config = _config(per_node_cpus={"readonly": frozenset({0}), "validator1": frozenset({0, 1})})
    assert config.node_keys == ["boot", "validator1", "readonly"]


@pytest.mark.parametrize(
    ("per_node_cpus", "message"),
    [
        ({"validator2": frozenset({0})}, "does not have"),
        ({"readonly": frozenset()}, "non-empty set"),
        ({"readonly": frozenset({-1})}, "non-empty set"),
    ],
)
def test_config_rejects_unusable_cpu_sets(per_node_cpus, message):
    with pytest.raises(ValueError, match=message):
        _config(per_node_cpus=per_node_cpus)


def test_no_cpus_means_no_prefix():
    assert provider._cpu_affinity_prefix(None) == []


def test_prefix_rejects_cpus_outside_this_process_affinity():
    with pytest.raises(ValueError, match="non-empty subset"):
        provider._cpu_affinity_prefix({10_000})


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
