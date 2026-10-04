from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE = REPO_ROOT / "integration-tests/test/full-suite.txt"
EXPECTED_ROOTS = (
    "integration-tests/test/tests/shared/",
    "integration-tests/test/tests/custom/",
    "integration-tests/test/tests/standalone/",
)


def test_full_suite_profile_contains_all_test_roots_in_execution_order():
    assert tuple(PROFILE.read_text().splitlines()) == EXPECTED_ROOTS


def test_full_suite_profile_roots_exist_and_contain_tests():
    for root in EXPECTED_ROOTS:
        path = REPO_ROOT / root
        assert path.is_dir()
        assert any(path.glob("test_*.py"))
