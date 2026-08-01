"""Version 1 soak catalogue loading, validation, and canonical digest support.

All functions in this module are resource-free. They read catalogue files only;
they never invoke Docker, start node subprocesses, or reserve ports.
"""

# Pyright in the agent harness does not attach to Poetry's environment.
# Runtime dependency imports are exercised by the catalogue unit tests.
# pyright: reportMissingImports=false, reportMissingModuleSource=false

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

CATALOG_SCHEMA_VERSION = 1
EXECUTOR_PROTOCOL_VERSION = 1
RESULT_SCHEMA_VERSION = 1
LIMIT_SCHEMA_VERSION = 1
JSON_SAFE_INTEGER_MAX = (1 << 53) - 1

EPOCH_ID_RE = re.compile(r"^SOAK-EPOCH-[0-9]{3}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WORKLOAD_KIND_RE = re.compile(r"^[a-z][a-z0-9_]*$")

PROVIDERS = ("docker", "subprocess")
TOPOLOGIES = ("six-node-single-shard",)
FAILURE_CLASSES = (
    "none",
    "workload",
    "assertion",
    "safety",
    "host",
    "reset",
    "infrastructure",
)
LIMIT_KEYS = (
    "max_operations",
    "max_concurrency",
    "max_payload_bytes",
    "max_phlo_limit",
    "max_submit_rate_per_second",
    "max_active_phase_seconds",
    "max_drain_seconds",
    "max_node_rss_bytes",
    "min_host_available_bytes",
)


class CatalogError(ValueError):
    """Raised when catalogue input violates the fail-closed version 1 contract."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise CatalogError("mapping keys must be scalar JSON strings") from exc
        if duplicate:
            raise CatalogError(f"duplicate mapping key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CatalogError(f"cannot read {path}: {exc}") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise CatalogError(f"{path}: UTF-8 byte-order marks are not allowed")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CatalogError(f"{path}: input must be UTF-8") from exc
    loader = _UniqueKeyLoader(text)
    try:
        return loader.get_single_data()
    except CatalogError:
        raise
    except yaml.YAMLError as exc:
        raise CatalogError(f"{path}: malformed YAML: {exc}") from exc
    finally:
        loader.dispose()


@lru_cache(maxsize=2)
def _schema_validator(schema_path: str) -> Draft202012Validator:
    path = Path(schema_path)
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, SchemaError) as exc:
        raise CatalogError(f"cannot load catalogue schema {path}: {exc}") from exc
    return Draft202012Validator(schema)


def _validate_schema(value: Any, schema_path: Path, location: str) -> None:
    errors = sorted(_schema_validator(str(schema_path)).iter_errors(value), key=str)
    if errors:
        error = errors[0]
        member = ".".join(str(part) for part in error.absolute_path)
        suffix = f".{member}" if member else ""
        raise CatalogError(f"{location}{suffix}: {error.message}")


def _require_mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise CatalogError(f"{location} must be an object with string keys")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], *, required: set[str], optional: set[str], location: str
) -> None:
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise CatalogError(f"{location} is missing keys: {', '.join(sorted(missing))}")
    if unknown:
        raise CatalogError(f"{location} has unknown keys: {', '.join(sorted(unknown))}")


def _require_positive_int(value: Any, location: str) -> int:
    if type(value) is not int or value < 1 or value > JSON_SAFE_INTEGER_MAX:
        raise CatalogError(f"{location} must be a positive I-JSON safe integer")
    return value


def _validate_json_value(value: Any, location: str) -> None:
    if value is None or type(value) in (str, int, bool):
        if type(value) is int and not (-JSON_SAFE_INTEGER_MAX <= value <= JSON_SAFE_INTEGER_MAX):
            raise CatalogError(f"{location} integer is outside the I-JSON safe range")
        return
    if isinstance(value, float):
        raise CatalogError(f"{location} floating-point values are not allowed")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{location}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CatalogError(f"{location} has a non-string mapping key")
            if not key.isascii():
                raise CatalogError(f"{location} has a non-ASCII object key")
            _validate_json_value(item, f"{location}.{key}")
        return
    raise CatalogError(f"{location} contains unsupported value type {type(value).__name__}")


def _require_string_list(
    value: Any, location: str, *, allowed: Sequence[str] | None = None
) -> list[str]:
    if not isinstance(value, list) or not value:
        raise CatalogError(f"{location} must be a non-empty array")
    if any(not isinstance(item, str) or not item for item in value):
        raise CatalogError(f"{location} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise CatalogError(f"{location} must not contain duplicates")
    if allowed is not None:
        unknown = set(value) - set(allowed)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise CatalogError(f"{location} contains unsupported values: {names}")
    return value


def _safe_relative_path(root: Path, value: Any, location: str) -> tuple[str, Path]:
    if not isinstance(value, str) or not value or "\\" in value:
        raise CatalogError(f"{location} must be a repository-relative POSIX path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise CatalogError(f"{location} must not be absolute or traverse parent directories")
    normalized = pure.as_posix()
    candidate = root.joinpath(*pure.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise CatalogError(f"{location} does not resolve to a readable file: {value}") from exc
    resolved_root = root.resolve()
    if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
        raise CatalogError(f"{location} escapes the catalogue root or is not a file: {value}")
    return normalized, resolved


def _validate_epoch_definition(data: Any, path: Path) -> Mapping[str, Any]:
    schema_path = path.parent.parent / "schemas" / "epoch-v1.schema.json"
    _validate_schema(data, schema_path, str(path))
    epoch = _require_mapping(data, str(path))
    _require_exact_keys(
        epoch,
        required={
            "catalog_schema_version",
            "epoch_id",
            "epoch_revision",
            "definition",
            "fixtures",
        },
        optional={"annotations"},
        location=str(path),
    )
    if epoch["catalog_schema_version"] != CATALOG_SCHEMA_VERSION:
        raise CatalogError(f"{path}: unsupported catalog_schema_version")
    epoch_id = epoch["epoch_id"]
    if not isinstance(epoch_id, str) or not EPOCH_ID_RE.fullmatch(epoch_id):
        raise CatalogError(f"{path}: epoch_id must match SOAK-EPOCH-NNN")
    revision = _require_positive_int(epoch["epoch_revision"], f"{path}.epoch_revision")
    if path.name != f"{epoch_id}.v{revision}.yml":
        raise CatalogError(f"{path}: filename must match epoch identity and revision")

    definition = _require_mapping(epoch["definition"], f"{path}.definition")
    _require_exact_keys(
        definition,
        required={"providers", "topologies", "workload", "safety_limits", "invariants"},
        optional=set(),
        location=f"{path}.definition",
    )
    _require_string_list(definition["providers"], f"{path}.definition.providers", allowed=PROVIDERS)
    _require_string_list(
        definition["topologies"], f"{path}.definition.topologies", allowed=TOPOLOGIES
    )

    workload = _require_mapping(definition["workload"], f"{path}.definition.workload")
    _require_exact_keys(
        workload,
        required={"kind", "parameters"},
        optional=set(),
        location=f"{path}.definition.workload",
    )
    kind = workload["kind"]
    if not isinstance(kind, str) or not WORKLOAD_KIND_RE.fullmatch(kind):
        raise CatalogError(f"{path}.definition.workload.kind has invalid syntax")
    _require_mapping(workload["parameters"], f"{path}.definition.workload.parameters")
    _validate_json_value(workload["parameters"], f"{path}.definition.workload.parameters")

    limits = _require_mapping(definition["safety_limits"], f"{path}.definition.safety_limits")
    unknown_limits = limits.keys() - set(LIMIT_KEYS)
    if unknown_limits:
        raise CatalogError(
            f"{path}.definition.safety_limits has unknown keys: {', '.join(sorted(unknown_limits))}"
        )
    for key, value in limits.items():
        _require_positive_int(value, f"{path}.definition.safety_limits.{key}")

    invariants = _require_string_list(definition["invariants"], f"{path}.definition.invariants")
    for invariant in invariants:
        if not WORKLOAD_KIND_RE.fullmatch(invariant):
            raise CatalogError(f"{path}.definition.invariants contains invalid identifier")

    fixtures = epoch["fixtures"]
    if not isinstance(fixtures, list):
        raise CatalogError(f"{path}.fixtures must be an array")
    normalized_fixtures: set[str] = set()
    for index, fixture in enumerate(fixtures):
        normalized, _ = _safe_relative_path(path.parent, fixture, f"{path}.fixtures[{index}]")
        if normalized in normalized_fixtures:
            raise CatalogError(f"{path}.fixtures must not contain normalized duplicates")
        normalized_fixtures.add(normalized)

    if "annotations" in epoch:
        _require_mapping(epoch["annotations"], f"{path}.annotations")
        _validate_json_value(epoch["annotations"], f"{path}.annotations")
    return epoch


def canonical_definition_bytes(path: Path) -> bytes:
    """Return the contract's canonical UTF-8 digest payload for one definition."""
    definition_path = Path(path).resolve()
    epoch = _validate_epoch_definition(_load_yaml(definition_path), definition_path)
    fixture_records = []
    for index, fixture in enumerate(epoch["fixtures"]):
        normalized, resolved = _safe_relative_path(
            definition_path.parent, fixture, f"{definition_path}.fixtures[{index}]"
        )
        fixture_records.append(
            {"path": normalized, "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest()}
        )
    fixture_records.sort(key=lambda item: item["path"])
    payload = {
        "catalog_schema_version": epoch["catalog_schema_version"],
        "epoch_id": epoch["epoch_id"],
        "epoch_revision": epoch["epoch_revision"],
        "definition": epoch["definition"],
        "fixtures": fixture_records,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def definition_digest(path: Path) -> str:
    """Calculate the lowercase SHA-256 digest for one epoch definition."""
    return hashlib.sha256(canonical_definition_bytes(path)).hexdigest()


def effective_limits(
    orchestrator_limits: Mapping[str, Any],
    epoch_limits: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Apply version 1 epoch overrides without weakening host protections."""
    supplied = _require_mapping(orchestrator_limits, "orchestrator_limits")
    _require_exact_keys(
        supplied,
        required={"limit_schema_version", *LIMIT_KEYS},
        optional=set(),
        location="orchestrator_limits",
    )
    if supplied["limit_schema_version"] != LIMIT_SCHEMA_VERSION:
        raise CatalogError("unsupported orchestrator limit_schema_version")
    result = {"limit_schema_version": LIMIT_SCHEMA_VERSION}
    for key in LIMIT_KEYS:
        result[key] = _require_positive_int(supplied[key], f"orchestrator_limits.{key}")

    overrides = _require_mapping(epoch_limits or {}, "epoch_limits")
    unknown = overrides.keys() - set(LIMIT_KEYS)
    if unknown:
        raise CatalogError(f"epoch_limits has unknown keys: {', '.join(sorted(unknown))}")
    for key, raw_value in overrides.items():
        value = _require_positive_int(raw_value, f"epoch_limits.{key}")
        supplied_value = result[key]
        if key == "min_host_available_bytes":
            if value < supplied_value:
                raise CatalogError(f"epoch_limits.{key} relaxes the orchestrator floor")
        elif value > supplied_value:
            raise CatalogError(f"epoch_limits.{key} relaxes the orchestrator ceiling")
        result[key] = value
    return result


def executor_capabilities() -> dict[str, Any]:
    """Return the resource-free stable protocol capability document."""
    return {
        "executor_protocol_version": EXECUTOR_PROTOCOL_VERSION,
        "catalog_schema_versions": [CATALOG_SCHEMA_VERSION],
        "result_schema_versions": [RESULT_SCHEMA_VERSION],
        "providers": list(PROVIDERS),
        "topologies": list(TOPOLOGIES),
        "commands": ["capabilities", "validate", "run", "replay"],
        "failure_classes": list(FAILURE_CLASSES),
        "limit_schema_version": LIMIT_SCHEMA_VERSION,
    }


def validate_catalog(
    catalog_path: Path,
    *,
    expected_schema: int = CATALOG_SCHEMA_VERSION,
    expected_epoch: str | None = None,
    expected_revision: int | None = None,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    """Validate a complete catalogue and optional pinned epoch identity."""
    path = Path(catalog_path).resolve()
    catalog_data = _load_yaml(path)
    _validate_schema(catalog_data, path.parent / "schemas" / "catalog-v1.schema.json", str(path))
    catalog = _require_mapping(catalog_data, str(path))
    _require_exact_keys(
        catalog,
        required={"catalog_schema_version", "epochs"},
        optional=set(),
        location=str(path),
    )
    if type(expected_schema) is not int or expected_schema != CATALOG_SCHEMA_VERSION:
        raise CatalogError(f"unsupported expected schema: {expected_schema}")
    if catalog["catalog_schema_version"] != expected_schema:
        raise CatalogError(
            f"catalog schema mismatch: expected {expected_schema}, "
            f"got {catalog['catalog_schema_version']!r}"
        )
    if expected_revision is not None and expected_epoch is None:
        raise CatalogError("--revision requires --epoch")
    if expected_digest is not None and expected_epoch is None:
        raise CatalogError("--definition-digest requires --epoch")
    if expected_epoch is not None and not EPOCH_ID_RE.fullmatch(expected_epoch):
        raise CatalogError("expected epoch must match SOAK-EPOCH-NNN")
    if expected_revision is not None:
        _require_positive_int(expected_revision, "expected revision")
    if expected_digest is not None and not SHA256_RE.fullmatch(expected_digest):
        raise CatalogError("expected definition digest must be lowercase SHA-256")

    entries = catalog["epochs"]
    if not isinstance(entries, list) or not entries:
        raise CatalogError(f"{path}.epochs must be a non-empty array")

    seen_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, raw_entry in enumerate(entries):
        location = f"{path}.epochs[{index}]"
        entry = _require_mapping(raw_entry, location)
        _require_exact_keys(
            entry,
            required={
                "epoch_id",
                "epoch_revision",
                "path",
                "definition_digest",
                "implementation_status",
            },
            optional=set(),
            location=location,
        )
        epoch_id = entry["epoch_id"]
        if not isinstance(epoch_id, str) or not EPOCH_ID_RE.fullmatch(epoch_id):
            raise CatalogError(f"{location}.epoch_id must match SOAK-EPOCH-NNN")
        if epoch_id in seen_ids:
            raise CatalogError(f"duplicate epoch_id in catalogue: {epoch_id}")
        seen_ids.add(epoch_id)
        revision = _require_positive_int(entry["epoch_revision"], f"{location}.epoch_revision")
        recorded_digest = entry["definition_digest"]
        if not isinstance(recorded_digest, str) or not SHA256_RE.fullmatch(recorded_digest):
            raise CatalogError(f"{location}.definition_digest must be lowercase SHA-256")
        status = entry["implementation_status"]
        if status not in ("planned", "implemented"):
            raise CatalogError(f"{location}.implementation_status is unsupported")

        normalized_path, definition_path = _safe_relative_path(
            path.parent, entry["path"], f"{location}.path"
        )
        epoch = _validate_epoch_definition(_load_yaml(definition_path), definition_path)
        if epoch["epoch_id"] != epoch_id or epoch["epoch_revision"] != revision:
            raise CatalogError(f"{location} identity does not match {normalized_path}")
        calculated_digest = definition_digest(definition_path)
        if calculated_digest != recorded_digest:
            raise CatalogError(
                f"{location} digest mismatch: recorded {recorded_digest}, "
                f"calculated {calculated_digest}"
            )
        validated.append(
            {
                "epoch_id": epoch_id,
                "epoch_revision": revision,
                "definition_digest": calculated_digest,
                "implementation_status": status,
                "path": normalized_path,
            }
        )

    if expected_epoch is not None:
        selected = next((entry for entry in validated if entry["epoch_id"] == expected_epoch), None)
        if selected is None:
            raise CatalogError(f"unknown epoch: {expected_epoch}")
        if expected_revision is not None and selected["epoch_revision"] != expected_revision:
            raise CatalogError(
                f"epoch revision mismatch: expected {expected_revision}, "
                f"got {selected['epoch_revision']}"
            )
        if expected_digest is not None and selected["definition_digest"] != expected_digest:
            raise CatalogError("definition digest mismatch for selected epoch")

    return {
        "valid": True,
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "epoch_count": len(validated),
        "epochs": validated,
    }


def validate_catalog_transition(previous_path: Path, candidate_path: Path) -> dict[str, Any]:
    """Reject revision reuse, regression, removal, or revision-only churn."""
    previous = validate_catalog(previous_path)
    candidate = validate_catalog(candidate_path)
    previous_by_id = {entry["epoch_id"]: entry for entry in previous["epochs"]}
    candidate_by_id = {entry["epoch_id"]: entry for entry in candidate["epochs"]}

    removed = previous_by_id.keys() - candidate_by_id.keys()
    if removed:
        raise CatalogError(f"candidate removes permanent epoch IDs: {', '.join(sorted(removed))}")

    for epoch_id, old in previous_by_id.items():
        new = candidate_by_id[epoch_id]
        old_revision = old["epoch_revision"]
        new_revision = new["epoch_revision"]
        digest_changed = old["definition_digest"] != new["definition_digest"]
        if new_revision < old_revision:
            raise CatalogError(
                f"{epoch_id} revision regressed from {old_revision} to {new_revision}"
            )
        if new_revision == old_revision and digest_changed:
            raise CatalogError(f"{epoch_id} semantic digest changed without a revision increment")
        if new_revision > old_revision + 1:
            raise CatalogError(f"{epoch_id} revision must increment by exactly one")
        if new_revision > old_revision and not digest_changed:
            raise CatalogError(f"{epoch_id} revision changed without a semantic digest change")

    return {
        **candidate,
        "transition_valid": True,
        "previous_epoch_count": previous["epoch_count"],
    }
