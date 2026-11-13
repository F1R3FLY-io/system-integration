"""The load module's deploy mix: parsing, interleaving and the kinds' terms."""

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

load = importlib.import_module("test.infra.load")


def test_unset_mix_is_light_only(monkeypatch):
    monkeypatch.delenv("LOAD_TEST_DEPLOY_MIX", raising=False)
    assert load.deploy_mix_from_env() == ["light"]


def test_mix_follows_the_weights_and_interleaves(monkeypatch):
    monkeypatch.setenv("LOAD_TEST_DEPLOY_MIX", "light:5, compute:3, data:1, heavy:1")
    mix = load.deploy_mix_from_env()
    assert len(mix) == 10
    assert {k: mix.count(k) for k in set(mix)} == {"light": 5, "compute": 3, "data": 1, "heavy": 1}
    assert all(a != b or a == "light" for a, b in zip(mix, mix[1:])), mix


@pytest.mark.parametrize(
    "raw",
    ["light", "light:0", "light:x", "giant:1", "light:2,light:1", "light:-1"],
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


def test_light_term_is_the_original_send():
    assert load.DEPLOY_KINDS["light"][0](7) == "@7!(7)"
    assert load.DEPLOY_KINDS["light"][1] == 100_000


def test_loop_and_map_terms_match_the_measured_shapes():
    assert load.loop_term(200, 9) == (
        "new loop, done in { contract loop(@i, @acc) = { if (i <= 0) { done!(acc) } "
        "else { loop!(i - 1, acc + i) } } | loop!(200, 9) }"
    )
    assert load.map_term(100, 9) == (
        "new build, out in { contract build(@i, @m) = { if (i <= 0) { out!(m) } "
        "else { build!(i - 1, m.set(i, i * 9)) } } | build!(100, {}) }"
    )
