"""The RUNNER_MEM_GB_OVERRIDE knob in launch-runner.sh.

The merge-recovery soak's 6-node shard needs ~26GB (19-20GB node RSS plus
6-7GB host overhead) and dies on the host free-floor guard on the 32GB
default VM (f1r3node-rust run 31390673884). A global AMD64_MEM_GB raise was
rejected for cost — every PR CI launch would pay for headroom only the soak
uses — so the launcher honors a per-launch override instead.

These tests pin the override and the way it can fail expensively: a
malformed value must die in the shell, not 90 seconds into an OCI launch
that then rejects the shape-config.
"""

import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "ci" / "oci-runners" / "launch-runner.sh"

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


def _resolve_mem(override=None, default_mem="32"):
    """Run the real block and report the MEM_GB it decided on.

    Returns (exit_code, resolved_mem_or_None, combined_output).

    `unset RUNNER_MEM_GB_OVERRIDE` is unconditional: subprocess inherits the
    parent environment, so a shell that exports the override would silently
    turn the default-path tests into override-path tests.
    """
    lines = [
        "set -euo pipefail",
        "unset RUNNER_MEM_GB_OVERRIDE",
        f"MEM_GB={shlex.quote(default_mem)}",
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


@pytest.mark.parametrize(
    "bad",
    ["", " ", "abc", "-4", "0", "4.5", "48GB", "48 ", "$(id)", "1e3"],
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
    ],
)
def test_malformed_override_is_refused(bad):
    """Fail closed in the shell, not later inside the OCI launch call."""
    code, mem, output = _resolve_mem(override=bad)
    assert code != 0
    assert mem is None
    assert "RUNNER_MEM_GB_OVERRIDE" in output


def test_extracted_block_is_the_real_one():
    """The extraction markers still bound the live override logic."""
    block = _extract_override_block()
    assert 'MEM_GB="$RUNNER_MEM_GB_OVERRIDE"' in block
    assert "exit 1" in block
