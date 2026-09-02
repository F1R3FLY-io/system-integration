import ast
from pathlib import Path

from f1r3fly.pb.CasperMessage_pb2 import DeployDataProto

REPO_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_TEST_ROOT = REPO_ROOT / "integration-tests" / "test"
RETIRED_FIELDS = {"phlo_limit", "phlo_price", "phloLimit", "phloPrice"}


def _string_keys(node: ast.AST) -> set[str]:
    return {
        key.value
        for child in ast.walk(node)
        if isinstance(child, ast.Dict)
        for key in child.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def test_protocol_v6_protobuf_omits_retired_phlo_fields() -> None:
    fields = {field.name for field in DeployDataProto.DESCRIPTOR.fields}
    assert RETIRED_FIELDS.isdisjoint(fields)


def test_integration_calls_omit_retired_phlo_keywords() -> None:
    violations = []
    for path in INTEGRATION_TEST_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            for keyword in call.keywords:
                if keyword.arg in RETIRED_FIELDS:
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{keyword.value.lineno}")
    assert violations == []


def test_http_deploy_request_omits_retired_phlo_fields() -> None:
    path = INTEGRATION_TEST_ROOT / "tests" / "shared" / "test_web_api.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    deploy_requests = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "deploy_req" for target in node.targets
        )
    ]
    assert len(deploy_requests) == 1
    assert RETIRED_FIELDS.isdisjoint(_string_keys(deploy_requests[0]))
