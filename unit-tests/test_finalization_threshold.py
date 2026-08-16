import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from test.infra.polling import wait_for_lfb_with_ft  # noqa: E402


class _Node:
    name = "validator"

    def __init__(self, fault_tolerances):
        self.fault_tolerances = iter(fault_tolerances)
        self.calls = 0

    def last_finalized_block(self):
        self.calls += 1
        return SimpleNamespace(
            blockInfo=SimpleNamespace(
                blockNumber=7,
                faultTolerance=next(self.fault_tolerances),
            )
        )


def test_finalization_certificate_requires_strictly_greater_fault_tolerance():
    node = _Node([0.5, 0.5001])

    result = wait_for_lfb_with_ft(
        node,
        target_number=7,
        ftt=0.5,
        timeout=1,
        interval=0,
    )

    assert node.calls == 2
    assert result.faultTolerance == 0.5001
