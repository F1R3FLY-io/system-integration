"""Session-scoped cleanup and adoption find a session's shard resources.

Shard resources are named by the shard scope ``<session>-s<n>``
(``rnode.test.<session>-s1.validator1``, ``f1r3fly-test-<session>-s1``,
``test-<session>-s1_boot-data``); standalone ones by the bare session id.
Patterns anchored on ``<session>.`` missed every shard name, so
``shardctl test-reset --session-id`` reported success while the shard kept
running, and ``--skip-setup --session-id`` found no containers to adopt.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration-tests"))

from test.infra import cleanup  # noqa: E402
from test.infra.config import ResourcePaths  # noqa: E402
from test.infra.providers import docker as docker_provider  # noqa: E402

SESSION = "49e7cfbe"


def test_patterns_match_shard_and_standalone_resources():
    container, network, volume = cleanup.session_patterns(SESSION)
    for name in (
        "rnode.test.49e7cfbe-s1.validator1",
        "rnode.test.49e7cfbe-s12.readonly",
        "rnode.test.49e7cfbe.standalone2",
    ):
        assert container.match(name), name
    for name in (
        "f1r3fly-test-49e7cfbe-s1",
        "f1r3fly-test-49e7cfbe-standalone",
        "f1r3fly-test-49e7cfbe-standalone3",
    ):
        assert network.match(name), name
    for name in (
        "test-49e7cfbe-s1_boot-data",
        "test-49e7cfbe-standalone-data",
        "test-49e7cfbe-standalone3-data",
    ):
        assert volume.match(name), name


def test_patterns_ignore_other_sessions_sharing_a_prefix():
    container, network, volume = cleanup.session_patterns(SESSION)
    assert not container.match("rnode.test.49e7cfbe12-s1.validator1")
    assert not container.match("rnode.test.ab49e7cfbe-s1.validator1")
    assert not network.match("f1r3fly-test-49e7cfbe12-s1")
    assert not volume.match("test-49e7cfbe12-s1_boot-data")


def test_a_shard_scope_matches_that_shard_alone():
    container, _, volume = cleanup.session_patterns(f"{SESSION}-s2")
    assert container.match("rnode.test.49e7cfbe-s2.boot")
    assert not container.match("rnode.test.49e7cfbe-s1.boot")
    assert volume.match("test-49e7cfbe-s2_boot-data")
    assert not volume.match("test-49e7cfbe-s1_boot-data")


def _docker_listing(names, removed):
    def fake(*args, **_kwargs):
        if args[0] == "rm":
            removed.append(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] in ("network", "volume") and args[1] == "rm":
            removed.append(args[-1])
            return subprocess.CompletedProcess(args, 0, "", "")
        listing = {
            "ps": [n for n in names if n.startswith("rnode.")],
            "network": [n for n in names if n.startswith("f1r3fly-")],
            "volume": [n for n in names if n.startswith("test-")],
        }[args[0]]
        return subprocess.CompletedProcess(args, 0, "\n".join(listing) + "\n", "")

    return fake


def test_cleanup_session_removes_the_shard_and_nothing_else(monkeypatch):
    names = [
        "rnode.test.49e7cfbe-s1.validator1",
        "rnode.test.49e7cfbe-s1.boot",
        "rnode.test.49e7cfbe12-s1.boot",
        "f1r3fly-test-49e7cfbe-s1",
        "f1r3fly-test-49e7cfbe12-s1",
        "test-49e7cfbe-s1_boot-data",
        "test-49e7cfbe12-s1_boot-data",
    ]
    removed = []
    monkeypatch.setattr(cleanup, "_docker", _docker_listing(names, removed))
    cleanup.DockerCleanupRegistry.cleanup_session(SESSION)
    assert sorted(removed) == sorted(
        [
            "rnode.test.49e7cfbe-s1.validator1",
            "rnode.test.49e7cfbe-s1.boot",
            "f1r3fly-test-49e7cfbe-s1",
            "test-49e7cfbe-s1_boot-data",
        ]
    )


class _Registry:
    session_id = "cafe1234"
    keep_running = False
    keep_on_failure = False


def _provider(tmp_path):
    paths = ResourcePaths(
        rust_conf="unused",
        standalone_conf="unused",
        genesis_bonds="unused",
        genesis_wallets="unused",
        standalone_bonds="unused",
        standalone_wallets="unused",
        certs_dir="unused",
        repo_root=str(tmp_path),
        integration_tests=str(tmp_path / "integration-tests"),
    )
    return docker_provider.DockerProvider(
        port_allocator=None, registry=_Registry(), timeouts=None, paths=paths
    )


def _adoptable(monkeypatch, running):
    monkeypatch.setattr(
        docker_provider,
        "_docker",
        lambda *args, **_kwargs: subprocess.CompletedProcess(args, 0, "\n".join(running), ""),
    )
    monkeypatch.setattr(
        docker_provider.DockerNodeHandle,
        "from_container",
        classmethod(lambda _cls, name: SimpleNamespace(name=name)),
    )


def test_adopt_session_finds_the_shard_by_session_id(monkeypatch, tmp_path):
    _adoptable(
        monkeypatch,
        [
            "rnode.test.49e7cfbe-s1.readonly",
            "rnode.test.49e7cfbe-s1.validator2",
            "rnode.test.49e7cfbe-s1.boot",
            "rnode.test.49e7cfbe-s1.validator1",
            "rnode.test.49e7cfbe12-s1.boot",
        ],
    )
    handles = _provider(tmp_path).adopt_session(SESSION)
    assert [h.name.rsplit(".", 1)[-1] for h in handles] == [
        "boot",
        "validator1",
        "validator2",
        "readonly",
    ]


def test_adopt_session_refuses_to_guess_between_shards(monkeypatch, tmp_path):
    running = ["rnode.test.49e7cfbe-s1.boot", "rnode.test.49e7cfbe-s2.boot"]
    _adoptable(monkeypatch, running)
    with pytest.raises(ValueError, match="49e7cfbe-s1, 49e7cfbe-s2"):
        _provider(tmp_path).adopt_session(SESSION)
    _adoptable(monkeypatch, running)
    (handle,) = _provider(tmp_path).adopt_session(f"{SESSION}-s2")
    assert handle.name == "rnode.test.49e7cfbe-s2.boot"
