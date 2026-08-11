"""The RUNNER_MEM_GB_OVERRIDE knob in launch-runner.sh.

The merge-recovery soak's 6-node shard needs ~26GB (19-20GB node RSS plus
6-7GB host overhead) and breached the host free-floor guard on a 32GB VM
(f1r3node-rust run 31390673884). Per the FINAL decision entry in
docs/ToDos.md (2026-08-10), what shipped is BOTH: the amd64 fleet default
rose to 48GB — that default, not this override, is what covers the soak —
and RUNNER_MEM_GB_OVERRIDE survives as the generic per-launch escape hatch
for the next outlier workload.

These tests pin the knob and the two ways it can fail expensively: a
malformed value must die in the shell, not 90 seconds into an OCI launch
that then rejects the shape-config, and an absurdly large value (a
fat-fingered 999999) must never reach OCI as a real Flex memory request.
"""

import re
import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "ci" / "oci-runners" / "launch-runner.sh"
STATE_ENV = REPO_ROOT / "ci" / "oci-runners" / "state.env"

_START = "# RUNNER_MEM_GB_OVERRIDE replaces"
_END = "# Verify gh CLI is authenticated"


def _extract_override_block() -> str:
    """Pull the memory-override block out of the real launcher.

    Extracted rather than duplicated so the tests fail when the script
    changes, which is the only way they stay honest.
    """
    text = LAUNCHER.read_text()
    start = text.index(_START)
    end = text.index(_END, start)
    return text[start:end]


def _resolve_mem(override=None, preset_mem="32"):
    """Run the real block and report the MEM_GB it decided on.

    Returns (exit_code, resolved_mem_or_None, combined_output).

    ``preset_mem`` is the synthetic MEM_GB the arch-resolution block would
    have set BEFORE the override runs — it deliberately is not read from
    state.env (the live amd64 default is pinned separately by
    ``test_fleet_default_is_48``).

    `unset RUNNER_MEM_GB_OVERRIDE` is unconditional: subprocess inherits the
    parent environment, so a shell that exports the override would silently
    turn the default-path tests into override-path tests.
    """
    lines = [
        "set -euo pipefail",
        "unset RUNNER_MEM_GB_OVERRIDE",
        f"MEM_GB={shlex.quote(preset_mem)}",
    ]
    if override is not None:
        lines.append(f"export RUNNER_MEM_GB_OVERRIDE={shlex.quote(override)}")
    lines.append(_extract_override_block())
    lines.append('echo "RESOLVED:$MEM_GB"')

    proc = subprocess.run(
        ["bash", "-c", "\n".join(lines)], capture_output=True, text=True, timeout=30
    )
    output = proc.stdout + proc.stderr
    resolved = None
    for line in proc.stdout.splitlines():
        if line.startswith("RESOLVED:"):
            resolved = line.removeprefix("RESOLVED:")
    return proc.returncode, resolved, output


def test_unset_override_keeps_the_arch_default():
    code, mem, _ = _resolve_mem()
    assert code == 0
    assert mem == "32"


def test_override_replaces_the_default():
    code, mem, _ = _resolve_mem(override="48")
    assert code == 0
    assert mem == "48"


def test_ceiling_boundary_is_accepted():
    """1024 is the largest value the sanity ceiling lets through."""
    code, mem, _ = _resolve_mem(override="1024")
    assert code == 0
    assert mem == "1024"


@pytest.mark.parametrize(
    "bad",
    ["", " ", "abc", "-4", "0", "4.5", "48GB", "48 ", "$(id)", "1e3", "1025", "999999"],
    ids=[
        "empty",
        "space",
        "text",
        "negative",
        "zero",
        "fractional",
        "unit-suffix",
        "trailing-space",
        "cmd-subst",
        "exponent",
        "just-over-ceiling",
        "fat-fingered",
    ],
)
def test_malformed_override_is_refused(bad):
    """Fail closed in the shell, not later inside the OCI launch call."""
    code, mem, output = _resolve_mem(override=bad)
    assert code != 0
    assert mem is None
    assert "RUNNER_MEM_GB_OVERRIDE" in output


def test_fleet_default_is_48():
    """Pin the FINAL decision: the amd64 fleet default is 48GB.

    The override tests above run against a synthetic preset, so this is the
    only place the live state.env value is asserted. If the default moves,
    this fails and the soak sizing conversation reopens deliberately.
    """
    match = re.search(r"^AMD64_MEM_GB=(\d+)$", STATE_ENV.read_text(), re.M)
    assert match, "AMD64_MEM_GB not found in state.env"
    assert match.group(1) == "48"


def test_extracted_block_is_the_real_one():
    """The extraction markers still bound the live override logic."""
    block = _extract_override_block()
    assert 'MEM_GB="$RUNNER_MEM_GB_OVERRIDE"' in block
    assert "1024" in block
    assert "exit 1" in block
