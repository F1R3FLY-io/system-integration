"""Resource-free catalogue compatibility commands."""

# Pyright in the agent harness does not attach to Poetry's environment.
# Runtime dependency imports are exercised by unit tests.
# pyright: reportMissingImports=false

import json
import sys
from pathlib import Path
from typing import Optional

import typer

from .catalog import (
    CatalogError,
    executor_capabilities,
    validate_catalog,
    validate_catalog_transition,
)

app = typer.Typer(
    name="soak",
    help="Validate and execute versioned randomized exercise soak epochs.",
    add_completion=False,
)


def _emit(document: dict) -> None:
    print(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


@app.command("capabilities")
def capabilities() -> None:
    """Print the stable executor capability document without starting resources."""
    _emit(executor_capabilities())


@app.command("validate")
def validate_command(
    catalog: Path = typer.Option(..., "--catalog", exists=True, dir_okay=False, readable=True),
    expected_schema: int = typer.Option(1, "--expected-schema", min=1),
    previous_catalog: Optional[Path] = typer.Option(
        None, "--previous-catalog", exists=True, dir_okay=False, readable=True
    ),
    epoch: Optional[str] = typer.Option(None, "--epoch"),
    revision: Optional[int] = typer.Option(None, "--revision", min=1),
    expected_digest: Optional[str] = typer.Option(None, "--definition-digest"),
) -> None:
    """Validate catalogue identity and digests without starting shard or OCI resources."""
    try:
        result = validate_catalog(
            catalog,
            expected_schema=expected_schema,
            expected_epoch=epoch,
            expected_revision=revision,
            expected_digest=expected_digest,
        )
        if previous_catalog is not None:
            transition = validate_catalog_transition(previous_catalog, catalog)
            result["transition_valid"] = transition["transition_valid"]
            result["previous_epoch_count"] = transition["previous_epoch_count"]
    except CatalogError as exc:
        print(f"catalog validation failed: {exc}", file=sys.stderr)
        raise typer.Exit(2) from exc
    _emit(result)
