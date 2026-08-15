"""Fail-closed tests for the randomized exercise soak catalogue contract."""

# Pyright in the agent harness does not attach to Poetry's environment.
# The complete suite is executed through `poetry run pytest`.
# pyright: reportMissingImports=false

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from shardctl.cli import app
from shardctl.soak.catalog import (
    CatalogError,
    canonical_definition_bytes,
    definition_digest,
    effective_limits,
    executor_capabilities,
    validate_catalog,
    validate_catalog_transition,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG = REPO_ROOT / "soak" / "catalog-v1.yml"
EPOCH_001 = REPO_ROOT / "soak" / "epochs" / "SOAK-EPOCH-001.v1.yml"
CANONICAL_001 = REPO_ROOT / "soak" / "fixtures" / "digest" / "SOAK-EPOCH-001.v1.canonical.json"
DIGEST_001 = REPO_ROOT / "soak" / "fixtures" / "digest" / "SOAK-EPOCH-001.v1.sha256"


def _copy_catalog(tmp_path: Path) -> Path:
    destination = tmp_path / "soak"
    shutil.copytree(REPO_ROOT / "soak", destination)
    return destination / "catalog-v1.yml"


def test_complete_catalogue_validates_without_resources():
    result = validate_catalog(CATALOG)

    assert result["valid"] is True
    assert result["catalog_schema_version"] == 1
    assert result["epoch_count"] == 6
    assert {entry["implementation_status"] for entry in result["epochs"]} == {"planned"}


def test_normative_canonical_bytes_and_digest_are_stable():
    expected_bytes = CANONICAL_001.read_bytes()
    expected_digest = DIGEST_001.read_text().strip()

    assert canonical_definition_bytes(EPOCH_001) == expected_bytes
    assert definition_digest(EPOCH_001) == expected_digest


def test_annotations_do_not_change_semantic_digest(tmp_path):
    catalog = _copy_catalog(tmp_path)
    definition = catalog.parent / "epochs" / EPOCH_001.name
    original_digest = definition_digest(definition)
    definition.write_text(definition.read_text().replace("planned", "implemented"))

    assert definition_digest(definition) == original_digest


def test_definition_digest_mismatch_fails_closed(tmp_path):
    catalog = _copy_catalog(tmp_path)
    definition = catalog.parent / "epochs" / EPOCH_001.name
    definition.write_text(definition.read_text().replace("parameters: {}", "parameters: {rate: 1}"))

    with pytest.raises(CatalogError, match="digest mismatch"):
        validate_catalog(catalog)


def test_semantic_change_requires_revision_increment(tmp_path):
    catalog = _copy_catalog(tmp_path)
    definition = catalog.parent / "epochs" / EPOCH_001.name
    old_digest = definition_digest(definition)
    definition.write_text(definition.read_text().replace("parameters: {}", "parameters: {rate: 1}"))
    new_digest = definition_digest(definition)
    catalog.write_text(catalog.read_text().replace(old_digest, new_digest))

    assert validate_catalog(catalog)["valid"] is True
    with pytest.raises(CatalogError, match="without a revision increment"):
        validate_catalog_transition(CATALOG, catalog)


def test_single_revision_increment_with_semantic_change_is_valid(tmp_path):
    catalog = _copy_catalog(tmp_path)
    old_definition = catalog.parent / "epochs" / EPOCH_001.name
    old_digest = definition_digest(old_definition)
    new_definition = old_definition.with_name("SOAK-EPOCH-001.v2.yml")
    new_definition.write_text(
        old_definition.read_text()
        .replace("epoch_revision: 1", "epoch_revision: 2")
        .replace("parameters: {}", "parameters: {rate: 1}")
    )
    old_definition.unlink()
    new_digest = definition_digest(new_definition)
    old_entry = (
        "epoch_id: SOAK-EPOCH-001\n"
        "    epoch_revision: 1\n"
        "    path: epochs/SOAK-EPOCH-001.v1.yml\n"
        f"    definition_digest: {old_digest}"
    )
    new_entry = (
        "epoch_id: SOAK-EPOCH-001\n"
        "    epoch_revision: 2\n"
        "    path: epochs/SOAK-EPOCH-001.v2.yml\n"
        f"    definition_digest: {new_digest}"
    )
    catalog.write_text(catalog.read_text().replace(old_entry, new_entry))

    result = validate_catalog_transition(CATALOG, catalog)
    assert result["transition_valid"] is True


def test_duplicate_yaml_keys_fail_closed(tmp_path):
    catalog = _copy_catalog(tmp_path)
    definition = catalog.parent / "epochs" / EPOCH_001.name
    definition.write_text(definition.read_text() + "epoch_revision: 1\n")

    with pytest.raises(CatalogError, match="duplicate mapping key"):
        validate_catalog(catalog)


def test_floating_point_semantics_fail_closed(tmp_path):
    catalog = _copy_catalog(tmp_path)
    definition = catalog.parent / "epochs" / EPOCH_001.name
    definition.write_text(
        definition.read_text().replace("parameters: {}", "parameters: {rate: 1.5}")
    )

    with pytest.raises(CatalogError):
        validate_catalog(catalog)


def test_non_ascii_semantic_object_keys_fail_closed(tmp_path):
    catalog = _copy_catalog(tmp_path)
    definition = catalog.parent / "epochs" / EPOCH_001.name
    definition.write_text(
        definition.read_text().replace("parameters: {}", "parameters: {\u03c0: 3}")
    )

    with pytest.raises(CatalogError):
        validate_catalog(catalog)


def test_integers_outside_the_i_json_safe_range_fail_closed(tmp_path):
    catalog = _copy_catalog(tmp_path)
    definition = catalog.parent / "epochs" / EPOCH_001.name
    definition.write_text(
        definition.read_text().replace("parameters: {}", "parameters: {count: 9007199254740992}")
    )

    with pytest.raises(CatalogError):
        validate_catalog(catalog)


def test_non_string_and_normalized_duplicate_fixtures_fail_closed(tmp_path):
    catalog = _copy_catalog(tmp_path)
    definition = catalog.parent / "epochs" / EPOCH_001.name
    definition.write_text(definition.read_text().replace("fixtures: []", "fixtures: [{path: x}]"))
    with pytest.raises(CatalogError):
        validate_catalog(catalog)

    definition.write_text(
        EPOCH_001.read_text().replace("fixtures: []", "fixtures: [payload.bin, ./payload.bin]")
    )
    (definition.parent / "payload.bin").write_bytes(b"fixture")
    with pytest.raises(CatalogError, match="normalized duplicates"):
        validate_catalog(catalog)


def test_machine_readable_schemas_are_runtime_inputs(tmp_path):
    catalog = _copy_catalog(tmp_path)
    (catalog.parent / "schemas" / "epoch-v1.schema.json").unlink()

    with pytest.raises(CatalogError, match="cannot load catalogue schema"):
        validate_catalog(catalog)


def test_catalogue_rejects_unknown_or_mismatched_pins():
    digest = DIGEST_001.read_text().strip()
    selected = validate_catalog(
        CATALOG,
        expected_epoch="SOAK-EPOCH-001",
        expected_revision=1,
        expected_digest=digest,
    )
    assert selected["valid"] is True

    with pytest.raises(CatalogError, match="unknown epoch"):
        validate_catalog(CATALOG, expected_epoch="SOAK-EPOCH-999")
    with pytest.raises(CatalogError, match="revision mismatch"):
        validate_catalog(CATALOG, expected_epoch="SOAK-EPOCH-001", expected_revision=2)
    with pytest.raises(CatalogError, match="definition digest mismatch"):
        validate_catalog(CATALOG, expected_epoch="SOAK-EPOCH-001", expected_digest="0" * 64)


def test_effective_limits_only_tighten_orchestrator_protections():
    orchestrator = {
        "limit_schema_version": 1,
        "max_operations": 1000,
        "max_concurrency": 8,
        "max_payload_bytes": 1048576,
        "max_phlo_limit": 10000000,
        "max_submit_rate_per_second": 10,
        "max_active_phase_seconds": 600,
        "max_drain_seconds": 300,
        "max_node_rss_bytes": 4294967296,
        "min_host_available_bytes": 2147483648,
    }

    tightened = effective_limits(
        orchestrator,
        {"max_operations": 500, "min_host_available_bytes": 3221225472},
    )
    assert tightened["max_operations"] == 500
    assert tightened["max_concurrency"] == 8
    assert tightened["min_host_available_bytes"] == 3221225472

    with pytest.raises(CatalogError, match="relaxes the orchestrator ceiling"):
        effective_limits(orchestrator, {"max_concurrency": 9})
    with pytest.raises(CatalogError, match="relaxes the orchestrator floor"):
        effective_limits(orchestrator, {"min_host_available_bytes": 1})
    with pytest.raises(CatalogError, match="I-JSON safe integer"):
        effective_limits(orchestrator, {"max_operations": 1 << 53})


def test_capabilities_match_the_accepted_v1_contract():
    capabilities = executor_capabilities()

    assert capabilities["executor_protocol_version"] == 1
    assert capabilities["catalog_schema_versions"] == [1]
    assert capabilities["providers"] == ["docker", "subprocess"]
    assert capabilities["commands"] == ["capabilities", "validate", "run", "replay"]
    assert capabilities["failure_classes"] == [
        "none",
        "workload",
        "assertion",
        "safety",
        "host",
        "reset",
        "infrastructure",
    ]


def test_cli_capabilities_and_validation_emit_json():
    runner = CliRunner()

    capability_result = runner.invoke(app, ["soak", "capabilities"])
    assert capability_result.exit_code == 0, capability_result.output
    assert json.loads(capability_result.stdout)["executor_protocol_version"] == 1

    validation_result = runner.invoke(
        app,
        ["soak", "validate", "--catalog", str(CATALOG), "--expected-schema", "1"],
    )
    assert validation_result.exit_code == 0, validation_result.output
    assert json.loads(validation_result.stdout)["epoch_count"] == 6


def test_cli_incompatibility_exits_two_without_resource_launch():
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["soak", "validate", "--catalog", str(CATALOG), "--expected-schema", "2"],
    )

    assert result.exit_code == 2
    assert "unsupported expected schema" in result.output
