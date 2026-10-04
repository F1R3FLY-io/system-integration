"""DockerProvider.monitor_output_dir is a host-visible per-session dir.

Cross-repo contract: f1r3node-rust's run-merge-recovery-soak.sh joins
per-node RSS against the monitor's resource-timeseries.csv and fails the
soak closed on host-protection-breach.txt — both written to this dir. It
returned None for the docker provider, which silently disabled the CSV
time-series AND the breach-marker channel on every docker iteration (the
2026-08-04 soak breach had to be attributed from the teardown table alone).
The dir must be the same per-session archive location log archival uses,
so CI's integration-tests tree capture includes it with no extra wiring.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration-tests"))

from test.infra.config import ResourcePaths  # noqa: E402
from test.infra.providers.base import archive_root_for  # noqa: E402
from test.infra.providers.docker import DockerProvider  # noqa: E402


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
    return DockerProvider(port_allocator=None, registry=_Registry(), timeouts=None, paths=paths)


def test_monitor_output_dir_is_the_session_archive_dir(tmp_path):
    provider = _provider(tmp_path)
    assert provider.monitor_output_dir is not None
    assert provider.monitor_output_dir == archive_root_for(
        str(tmp_path / "integration-tests"), "cafe1234"
    )


def test_monitor_output_dir_is_under_the_integration_tests_tree(tmp_path):
    provider = _provider(tmp_path)
    integration_tests = (tmp_path / "integration-tests").resolve()
    assert integration_tests in provider.monitor_output_dir.resolve().parents
