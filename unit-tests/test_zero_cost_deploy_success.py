import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from f1r3fly.pb.DeployServiceCommon_pb2 import BlockInfo, DeployInfo  # noqa: E402
from test.infra.assertions import assert_deploy_succeeded  # noqa: E402


def test_zero_comm_deploy_is_successful_when_execution_did_not_error():
    deploy_id = bytes(range(32))
    block = BlockInfo(deploys=[DeployInfo(deployId=deploy_id, cost=0, errored=False)])

    assert_deploy_succeeded(block, deploy_id.hex())
