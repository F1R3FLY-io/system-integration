"""Typed translation of pyf1r3fly's empty-deployId-channel failure.

pyf1r3fly reports "deploy finalized but read back empty" only through the
message text of a generic ``DeployError``. The PR #88 review flagged the
substring match callers used on that text as its one major finding: reword
the upstream message and the retry in ``_query_bridge`` silently stops
firing, reintroducing the flake with no test pointing at the stale match.

``test.infra.polling.deploy_and_read`` now translates that one message into
``EmptyParListError`` at the wrapper boundary, and these tests pin both
halves of the contract:

- the upstream raise site still uses the marker the translation matches
  (the drift guard — this is the test that fails loudly on a reword), and
- the wrapper raises the typed error for matching messages and leaves every
  other ``DeployError`` untouched.
"""

import inspect
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

import f1r3fly.polling as upstream_polling  # noqa: E402
import test.infra.polling as infra_polling  # noqa: E402
from test.infra.polling import (  # noqa: E402
    _EMPTY_PAR_MARKER,
    DeployError,
    EmptyParListError,
    deploy_and_read,
)


class _StubNode:
    name = "stub-node"

    def _external_client(self):
        return None


def _deploy(monkeypatch, raises):
    def fake_client_deploy_and_read(**_kwargs):
        raise raises

    monkeypatch.setattr(infra_polling, "_client_deploy_and_read", fake_client_deploy_and_read)
    return deploy_and_read(
        _StubNode(), "Nil", private_key=None, inclusion_timeout=1, finalization_timeout=1
    )


def test_upstream_raise_site_still_uses_the_marker():
    """The drift guard: fails when pyf1r3fly rewords the message.

    The marker string is owned by pyf1r3fly's raise site; the wrapper's
    translation matches on it. If a pyf1r3fly bump rewords the message,
    this test — not a re-flaking bridge suite — is what breaks.

    Anchored to ``raise DeployError(...)`` statements inside the one
    upstream function the wrapper calls, not the whole module: the phrase
    surviving in a comment or docstring elsewhere must not keep this green
    (PR #90 review, major finding).
    """
    src = inspect.getsource(upstream_polling.deploy_and_read)
    raise_sites = re.findall(r"raise\s+DeployError\((?:[^()]|\([^()]*\))*\)", src)
    assert raise_sites, "f1r3fly.polling.deploy_and_read no longer raises DeployError directly"
    assert any(_EMPTY_PAR_MARKER in site for site in raise_sites), (
        f"no raise DeployError(...) in f1r3fly.polling.deploy_and_read mentions "
        f"{_EMPTY_PAR_MARKER!r}: the upstream wording drifted, so the "
        f"EmptyParListError translation in test/infra/polling.py is dead — "
        f"update _EMPTY_PAR_MARKER to match the new message"
    )


def test_empty_par_message_is_translated_to_the_typed_error(monkeypatch):
    upstream_err = DeployError(
        "Deploy 304402200c11aca2deadbeef returned empty par list from deployId channel"
    )
    with pytest.raises(EmptyParListError) as excinfo:
        _deploy(monkeypatch, upstream_err)
    assert excinfo.value.__cause__ is upstream_err


def test_typed_error_still_satisfies_a_plain_deploy_error_handler(monkeypatch):
    """Existing ``except DeployError`` call sites must keep catching it."""
    with pytest.raises(DeployError):
        _deploy(
            monkeypatch,
            DeployError("Deploy 3044 returned empty par list from deployId channel"),
        )


def test_other_deploy_errors_pass_through_untranslated(monkeypatch):
    original = DeployError("Deploy 3044 errored: out of phlogiston")
    with pytest.raises(DeployError) as excinfo:
        _deploy(monkeypatch, original)
    assert excinfo.value is original
    assert not isinstance(excinfo.value, EmptyParListError)


def test_timeouts_are_not_swallowed_by_the_translation(monkeypatch):
    original = TimeoutError("deploy inclusion timed out after 1s")
    with pytest.raises(TimeoutError) as excinfo:
        _deploy(monkeypatch, original)
    assert excinfo.value is original
