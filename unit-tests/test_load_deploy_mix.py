"""The load module's deploy mix: parsing, interleaving and the kinds' terms."""

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

load = importlib.import_module("test.infra.load")


def test_mix_follows_the_weights_and_interleaves(monkeypatch):
    monkeypatch.setenv("LOAD_TEST_DEPLOY_MIX", "light:5, compute:3, data:1, heavy:1")
    mix = load.deploy_mix_from_env()
    assert len(mix) == 10
    assert {k: mix.count(k) for k in set(mix)} == {"light": 5, "compute": 3, "data": 1, "heavy": 1}
    assert all(a != b or a == "light" for a, b in zip(mix, mix[1:])), mix


@pytest.mark.parametrize(
    "raw",
    ["light:0", "giant:1", "light:2,light:1"],
)
def test_rejects_bad_mixes(monkeypatch, raw):
    monkeypatch.setenv("LOAD_TEST_DEPLOY_MIX", raw)
    with pytest.raises(ValueError):
        load.deploy_mix_from_env()


@pytest.mark.parametrize("kind", sorted(load.DEPLOY_KINDS))
def test_each_kind_builds_a_balanced_term_that_embeds_its_index(kind):
    term, phlo_limit = load.DEPLOY_KINDS[kind]
    text = term(123456)
    assert "123456" in text
    for opening, closing in ("{}", "()"):
        assert text.count(opening) == text.count(closing), text
    assert phlo_limit >= 100_000
