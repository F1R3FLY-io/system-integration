"""test_load's per-node CPU counts: parsing into CPU sets."""

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

load = importlib.import_module("test.tests.custom.test_load")


def test_counts_take_the_lowest_available_cpus():
    assert load.parse_node_cpus("readonly:2, validator1:3", [7, 3, 5, 9]) == {
        "readonly": frozenset({3, 5}),
        "validator1": frozenset({3, 5, 7}),
    }


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("readonly", "must be <node>:<count>"),
        ("readonly:0", "must be <node>:<count>"),
        ("readonly:5", "count from 1 to 4"),
        ("readonly:two", "must be <node>:<count>"),
        (":2", "must be <node>:<count>"),
        ("readonly:1,readonly:2", "twice"),
    ],
)
def test_unusable_counts_are_rejected(raw, message):
    with pytest.raises(ValueError, match=message):
        load.parse_node_cpus(raw, [0, 1, 2, 3])
