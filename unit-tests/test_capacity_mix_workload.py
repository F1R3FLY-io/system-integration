"""The capacity test's mixed workload: weights, per-index kinds, and that each
part verifies only its own finalized deploys."""

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

workloads = importlib.import_module("test.infra.workloads")


class Part(workloads.Workload):
    def __init__(self, name):
        self.name = name
        self.prepared = False
        self.verified = None

    def prepare(self, setup):
        self.prepared = True

    def term(self, index, key):
        return f"{self.name}-{index}", 1000

    def verify(self, readonly, finalized):
        self.verified = dict(finalized)
        return [f"saw {len(finalized)}"]


def test_kinds_follow_the_weights_within_each_cycle():
    mix = workloads.MixWorkload({"a": Part("a"), "b": Part("b")}, {"a": 3, "b": 1})
    kinds = [mix.kind(i) for i in range(8)]
    assert kinds.count("a") == 6 and kinds.count("b") == 2
    assert kinds[:4] == kinds[4:]


def test_terms_come_from_the_part_and_verification_splits_by_part():
    a, b = Part("a"), Part("b")
    mix = workloads.MixWorkload({"a": a, "b": b}, {"a": 1, "b": 1})
    mix.prepare(setup=None)
    assert a.prepared and b.prepared
    terms = {i: mix.term(i, key=None)[0] for i in range(4)}
    assert terms[0].split("-")[0] == mix.kind(0)
    finalized = {f"d{i}": i for i in range(4)}
    problems = mix.verify(readonly=None, finalized=finalized)
    assert sorted(problems) == ["a: saw 2", "b: saw 2"]
    assert set(a.verified.values()) | set(b.verified.values()) == {0, 1, 2, 3}
    assert not set(a.verified) & set(b.verified)


def test_make_workload_builds_a_mix_of_known_kinds():
    mix = workloads.make_workload("mix", [], 8, 16, 1024, mix="light:2,heavy:1")
    assert isinstance(mix, workloads.MixWorkload)
    assert sorted({mix.kind(i) for i in range(3)}) == ["heavy", "light"]
    with pytest.raises(ValueError):
        workloads.make_workload("mix", [], 8, 16, 1024, mix="light:2,unknown:1")
