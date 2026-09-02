"""Unit coverage for unreleased-node capability gating."""

import ast
import importlib
import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "integration-tests/test/infra/node_capabilities.py"
SPEC = importlib.util.spec_from_file_location("node_capabilities", MODULE_PATH)
assert SPEC and SPEC.loader
node_capabilities = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(node_capabilities)

validate_node_capabilities = node_capabilities.validate_node_capabilities
required_node_capabilities = node_capabilities.required_node_capabilities
missing_node_capabilities = node_capabilities.missing_node_capabilities

_GATED_REGRESSIONS = {
    "integration-tests/test/tests/custom/test_concurrent_bridge_locks.py": (
        "concurrent-bridge-lock-accounting"
    ),
    "integration-tests/test/tests/custom/test_finality_stall_recovery.py": (
        "finality-stall-recovery"
    ),
    "integration-tests/test/tests/custom/test_observer_missing_block_retry.py": (
        "observer-missing-block-retry"
    ),
    "integration-tests/test/tests/custom/test_observer_overload.py": (
        "observer-exploratory-backpressure"
    ),
    "integration-tests/test/tests/custom/test_readonly_catchup_bounded.py": (
        "readonly-observer-api-catchup"
    ),
    "integration-tests/test/tests/custom/test_slow_peer_notification.py": (
        "slow-peer-notification-quorum"
    ),
    "integration-tests/test/tests/custom/test_transient_peer_liveness.py": (
        "transient-peer-liveness"
    ),
    "integration-tests/test/tests/standalone/test_duplicate_signed_deploy.py": (
        "duplicate-signed-deploy-race"
    ),
    "integration-tests/test/tests/standalone/test_expired_deploy_admission.py": (
        "expired-deploy-admission"
    ),
}


def _declared_capabilities(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    declared: set[str] = set()
    for call in ast.walk(tree):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "requires_node_capabilities"
        ):
            continue
        assert call.keywords == [], f"{path} uses keyword capability requirements"
        assert call.args, f"{path} has an empty capability requirement"
        for arg in call.args:
            assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
                f"{path} has a non-literal capability requirement"
            )
            declared.add(arg.value)
    return declared


def test_valid_capabilities_are_normalized_to_a_set():
    assert validate_node_capabilities(
        ["finality-stall-recovery", "expired-deploy-admission"], source="test"
    ) == frozenset({"finality-stall-recovery", "expired-deploy-admission"})


def test_malformed_capability_fails_closed():
    malformed = [
        "",
        "Finality-Stall-Recovery",
        "finality_stall_recovery",
        "-leading",
        "trailing-",
    ]
    for capability in malformed:
        with unittest.TestCase().assertRaisesRegex(ValueError, "invalid node capability"):
            validate_node_capabilities([capability], source="test")


def test_duplicate_capability_fails_closed():
    with unittest.TestCase().assertRaisesRegex(ValueError, "duplicate node capability"):
        validate_node_capabilities(
            ["finality-stall-recovery", "finality-stall-recovery"], source="test"
        )


def test_stacked_and_multi_argument_requirements_are_combined():
    assert required_node_capabilities(
        [
            ("finality-stall-recovery",),
            ("transient-peer-liveness", "slow-peer-notification-quorum"),
        ],
        source="test",
    ) == frozenset(
        {
            "finality-stall-recovery",
            "transient-peer-liveness",
            "slow-peer-notification-quorum",
        }
    )


def test_empty_requirement_fails_closed():
    with unittest.TestCase().assertRaisesRegex(ValueError, "empty node capability requirement"):
        required_node_capabilities([()], source="test")


def test_missing_capabilities_are_sorted_and_require_all():
    assert missing_node_capabilities(
        frozenset({"transient-peer-liveness", "finality-stall-recovery"}),
        frozenset({"transient-peer-liveness"}),
    ) == ("finality-stall-recovery",)


def test_each_unreleased_regression_declares_its_node_capability():
    for relative_path, capability in _GATED_REGRESSIONS.items():
        assert _declared_capabilities(REPO_ROOT / relative_path) == {capability}


def test_cold_start_readiness_remains_baseline_coverage():
    path = REPO_ROOT / "integration-tests/test/tests/custom/test_cold_start_readiness.py"
    assert _declared_capabilities(path) == set()


def test_collection_hook_combines_every_marker_and_uses_pytest_stash():
    conftest = (REPO_ROOT / "integration-tests/test/conftest.py").read_text()
    assert 'item.iter_markers("requires_node_capabilities")' in conftest
    assert "config.stash[_NODE_CAPABILITIES]" in conftest
    assert "get_closest_marker" not in conftest


def test_full_suite_option_controls_capability_gated_regressions(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "integration-tests"))
    integration_conftest = importlib.import_module("test.conftest")

    for run_all, expect_skip in ((False, True), (True, False)):

        class Config:
            stash = {integration_conftest._NODE_CAPABILITIES: frozenset()}

            @staticmethod
            def getoption(name):
                return {"--run-all-node-capability-tests": run_all}[name]

        class Item:
            nodeid = "test_capability_gate.py::test_regression"

            def __init__(self):
                self.added_markers = []

            @staticmethod
            def iter_markers(name):
                marker = integration_conftest.pytest.mark.requires_node_capabilities(
                    "missing-capability"
                ).mark
                return iter([marker] if name == "requires_node_capabilities" else ())

            def add_marker(self, marker):
                self.added_markers.append(marker)

        item = Item()
        integration_conftest.pytest_collection_modifyitems(Config(), [item])

        assert bool(item.added_markers) is expect_skip
        if expect_skip:
            assert item.added_markers[0].name == "skip"
