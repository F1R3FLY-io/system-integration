import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from test.infra.log_events import scan_for_forbidden  # noqa: E402


@pytest.mark.parametrize(
    "line, expected_key",
    [
        ("Proposal failed: BugError while creating a block", "ProposalFailedBugError"),
        ("Invalid block reason ContainsExpiredDeploy", "ContainsExpiredDeploy"),
    ],
)
def test_consensus_failure_signatures_are_forbidden(line, expected_key):
    errors = scan_for_forbidden(line, "validator")

    assert len(errors) == 1
    assert f"[{expected_key}]" in errors[0].message
