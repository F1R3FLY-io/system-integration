"""Unit tests for ``ComposeManager.pull_single_file``'s registry retry.

No Docker, no network: a scripted fake stands in for
``_run_single_file_command``. These exist because the retry's whole value is in
*which* failures it retries — retrying a missing image or a bad credential would
turn an honest instant failure into a slow one, so the negative cases below
matter at least as much as the positive ones.

Regression origin: `Rust: test_web_api (shard)` failed on main (run
30502597133) at ``shardctl pull`` with a Docker Hub TCP reset, skipping the test
step entirely and reporting as a test failure.
"""

import subprocess
from pathlib import Path

import pytest

from shardctl import compose as shardctl_compose

# The verbatim error from the run that motivated the retry.
REAL_CI_RESET = (
    'readonly Error Head "https://registry-1.docker.io/v2/f1r3flyindustries/'
    'f1r3fly-rust/manifests/staging": Get "https://auth.docker.io/token?..." : '
    "read tcp 10.1.0.166:39916->104.18.43.178:443: read: connection reset by peer"
)

TRANSIENT = [
    pytest.param(REAL_CI_RESET, id="real-ci-reset"),
    pytest.param("toomanyrequests: Too Many Requests", id="rate-limit"),
    pytest.param("received unexpected HTTP status: 502 Bad Gateway", id="502"),
    pytest.param("net/http: TLS handshake timeout", id="tls-timeout"),
    pytest.param("CONNECTION RESET BY PEER", id="case-insensitive"),
    pytest.param("context deadline exceeded", id="deadline"),
]

# Must never be retried: these do not fix themselves, so retrying only delays an
# honest failure by the full backoff.
PERMANENT = [
    pytest.param("manifest unknown: manifest unknown", id="manifest-unknown"),
    pytest.param("unauthorized: authentication required", id="unauthorized"),
    pytest.param("denied: requested access to the resource is denied", id="denied"),
    pytest.param("no such service: bogus", id="no-such-service"),
    pytest.param("", id="empty-output"),
]


@pytest.mark.parametrize("output", TRANSIENT)
def test_transient_errors_are_classified_retryable(output):
    assert shardctl_compose._is_transient_pull_error(output) is True


@pytest.mark.parametrize("output", PERMANENT)
def test_permanent_errors_are_not_retryable(output):
    assert shardctl_compose._is_transient_pull_error(output) is False


class _FakeManager:
    """Drives the real ``pull_single_file`` against scripted command outcomes.

    ``outcomes`` is a list of ``(returncode, stderr)``; the last entry repeats.
    """

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.profile = None

    def _run_single_file_command(self, compose_file, args, check=True, capture_output=True):
        assert args[0] == "pull", f"expected a pull command, got {args!r}"
        # The retry loop depends on this: with check=True the helper raises
        # SystemExit internally and no retry is possible.
        assert check is False, "retry path must invoke the helper with check=False"
        rc, err = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        return subprocess.CompletedProcess(args=["docker"], returncode=rc, stdout="", stderr=err)

    pull_single_file = shardctl_compose.ComposeManager.pull_single_file


COMPOSE_FILE = Path("compose/f1r3node-rust.yml")


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Keep the suite fast; backoff duration is not what these tests assert."""
    monkeypatch.setattr(shardctl_compose, "_PULL_BACKOFF_SECONDS", 0)


def test_success_on_first_attempt_does_not_retry():
    m = _FakeManager([(0, "")])
    result = m.pull_single_file(COMPOSE_FILE)
    assert result.returncode == 0
    assert m.calls == 1


def test_transient_failure_then_success_returns_cleanly():
    m = _FakeManager([(1, REAL_CI_RESET), (0, "")])
    result = m.pull_single_file(COMPOSE_FILE)
    assert result.returncode == 0
    assert m.calls == 2


def test_persistent_transient_failure_exhausts_attempts_then_exits():
    m = _FakeManager([(1, REAL_CI_RESET)])
    with pytest.raises(SystemExit):
        m.pull_single_file(COMPOSE_FILE)
    assert m.calls == shardctl_compose._PULL_ATTEMPTS_DEFAULT


def test_permanent_failure_fails_fast_without_retrying():
    """The point of the transient allowlist: no backoff for a missing image."""
    m = _FakeManager([(1, "manifest unknown: manifest unknown")])
    with pytest.raises(SystemExit):
        m.pull_single_file(COMPOSE_FILE)
    assert m.calls == 1


def test_attempts_honours_env_override(monkeypatch):
    monkeypatch.setenv("SHARDCTL_PULL_ATTEMPTS", "5")
    assert shardctl_compose._pull_attempts() == 5
    m = _FakeManager([(1, REAL_CI_RESET)])
    with pytest.raises(SystemExit):
        m.pull_single_file(COMPOSE_FILE)
    assert m.calls == 5


@pytest.mark.parametrize("raw", ["0", "-3"])
def test_attempts_clamps_to_one(monkeypatch, raw):
    """The override must never be able to disable pulling outright."""
    monkeypatch.setenv("SHARDCTL_PULL_ATTEMPTS", raw)
    assert shardctl_compose._pull_attempts() == 1


def test_attempts_falls_back_on_non_integer(monkeypatch):
    monkeypatch.setenv("SHARDCTL_PULL_ATTEMPTS", "garbage")
    assert shardctl_compose._pull_attempts() == shardctl_compose._PULL_ATTEMPTS_DEFAULT


def test_attempts_default_when_unset(monkeypatch):
    monkeypatch.delenv("SHARDCTL_PULL_ATTEMPTS", raising=False)
    assert shardctl_compose._pull_attempts() == shardctl_compose._PULL_ATTEMPTS_DEFAULT
