import sys
from pathlib import Path

import pytest
from pyhocon import ConfigFactory

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "integration-tests"))

from test.infra.config import ShardConfig  # noqa: E402
from test.infra.genesis import (  # noqa: E402
    canonical_client_fuel_allocations,
    write_node_config,
)


def shard_config(allocations=None):
    return ShardConfig(bonds=[], client_fuel_allocations=allocations)


def test_empty_client_fuel_allocations_preserve_base_config_bytes(tmp_path):
    base = tmp_path / "base.conf"
    base.write_text("casper { genesis-block-data { epoch-length = 4 } }\n", encoding="utf-8")

    generated = write_node_config(shard_config(), str(base), tmp_path)

    assert generated.read_bytes() == base.read_bytes()


def test_client_fuel_allocations_are_canonicalized_and_parseable(tmp_path):
    base = tmp_path / "base.conf"
    base.write_text("casper { genesis-block-data { epoch-length = 4 } }\n", encoding="utf-8")
    config = shard_config([("0B", 2), ("0a", 3), ("0b", 5), ("0A", 0)])

    generated = write_node_config(config, str(base), tmp_path)
    parsed = ConfigFactory.parse_file(str(generated), resolve=False)
    allocations = parsed.get_list("casper.genesis-block-data.client-fuel-allocations")

    assert canonical_client_fuel_allocations(config) == [("0a", 3), ("0b", 7)]
    assert [dict(entry) for entry in allocations] == [
        {"public-key": "0a", "amount": 3},
        {"public-key": "0b", "amount": 7},
    ]


@pytest.mark.parametrize(
    "allocations, message",
    [
        ([("", 1)], "cannot be empty"),
        ([("not-hex", 1)], "not valid hexadecimal"),
        ([("01", -1)], "cannot be negative"),
        ([("01", True)], "must be an integer"),
        ([("01", 2**63)], "overflows i64"),
        ([("01", 2**63 - 1), ("01", 1)], "overflows i64"),
    ],
)
def test_invalid_client_fuel_allocations_fail_before_node_startup(allocations, message):
    with pytest.raises(ValueError, match=message):
        canonical_client_fuel_allocations(shard_config(allocations))
