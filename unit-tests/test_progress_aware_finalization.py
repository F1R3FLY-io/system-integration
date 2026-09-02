import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from test.infra.config import TimeoutConfig  # noqa: E402
from test.infra.polling import deploy_and_read, wait_for_deploy_finalized  # noqa: E402
from test.infra.timeouts import TimeoutHierarchy  # noqa: E402


class StubNode:
    def __init__(self):
        self.client = object()

    def _external_client(self):
        return self.client


def test_timeout_hierarchy_keeps_absolute_bound_above_stall_budget():
    timeouts = TimeoutHierarchy(TimeoutConfig(scale=1.5))

    assert timeouts.deploy_finalization_absolute >= 3 * timeouts.finalization
    assert timeouts.finalization == 67
    assert timeouts.deploy_finalization_absolute == 202


def test_wait_wrapper_forwards_both_deadline_budgets():
    node = StubNode()
    expected = object()

    with patch(
        "test.infra.polling._client_wait_for_deploy_finalized",
        return_value=expected,
    ) as upstream:
        result = wait_for_deploy_finalized(
            node,
            "deploy",
            45,
            interval=2,
            absolute_timeout=135,
        )

    assert result is expected
    upstream.assert_called_once_with(
        node.client,
        "deploy",
        45,
        2,
        absolute_timeout=135,
    )


def test_deploy_wrapper_forwards_progress_and_absolute_bounds():
    node = StubNode()
    expected = ([], "block", 1)

    with patch(
        "test.infra.polling._client_deploy_and_read",
        return_value=expected,
    ) as upstream:
        result = deploy_and_read(
            node,
            "Nil",
            private_key=None,
            inclusion_timeout=30,
            finalization_timeout=45,
            finalization_absolute_timeout=135,
        )

    assert result == expected
    upstream.assert_called_once_with(
        client=node.client,
        term="Nil",
        private_key=None,
        inclusion_timeout=30,
        finalization_timeout=45,
        finalization_absolute_timeout=135,
        shard_id="root",
    )
