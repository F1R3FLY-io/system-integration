"""The RUNNER_IDLE_TIMEOUT_SECS knob in launch-runner.sh.

Run 33208755550 (f1r3node-rust PR #366): the Heavy Pipeline launches its
arm64 runner pair up front, but `arm64-subprocess` queues only after
`arm64-docker` completes. The docker leg held one runner for ~61 minutes,
the idle second runner hit the 45-minute watchdog and self-terminated
~10 minutes before its leg queued, and the pipeline hung with no runner
alive. The launcher now substitutes a per-launch idle cap into the
cloud-init template so multi-leg callers can raise it without loosening
the leak guardrail for every other runner.

Same shape as test_runner_mem_override.py: the real block is extracted and
run under bash, so malformed values die in the shell instead of inside
cloud-init on a VM that then leaks.
"""

import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "ci" / "oci-runners" / "launch-runner.sh"
TEMPLATE = REPO_ROOT / "ci" / "oci-runners" / "cloud-init-runner.yml.tmpl"

_START = "# RUNNER_IDLE_TIMEOUT_SECS raises"
_END = "# Verify gh CLI is authenticated"


def _extract_block() -> str:
    text = LAUNCHER.read_text()
    start = text.index(_START)
    end = text.index(_END, start)
    return text[start:end]


def _resolve(override=None):
    """Run the real block; report (exit_code, resolved_value_or_None, output)."""
    lines = [
        "set -euo pipefail",
        "unset RUNNER_IDLE_TIMEOUT_SECS",
    ]
    if override is not None:
        lines.append(f"export RUNNER_IDLE_TIMEOUT_SECS={shlex.quote(override)}")
    lines.append(_extract_block())
    lines.append('echo "RESOLVED:$IDLE_TIMEOUT_SECS"')

    proc = subprocess.run(
        ["bash", "-c", "\n".join(lines)], capture_output=True, text=True, timeout=30
    )
    resolved = None
    for line in proc.stdout.splitlines():
        if line.startswith("RESOLVED:"):
            resolved = line.removeprefix("RESOLVED:")
    return proc.returncode, resolved, proc.stdout + proc.stderr


def test_unset_knob_keeps_the_default():
    code, secs, _ = _resolve()
    assert code == 0
    assert secs == "2700"


def test_pipeline_launch_can_raise_the_cap():
    """The remedy for 33208755550: ~7200s covers the observed docker leg."""
    code, secs, _ = _resolve(override="7200")
    assert code == 0
    assert secs == "7200"


def test_ceiling_boundary_is_accepted():
    code, secs, _ = _resolve(override="21600")
    assert code == 0
    assert secs == "21600"


def test_floor_boundary_is_accepted():
    code, secs, _ = _resolve(override="600")
    assert code == 0
    assert secs == "600"


@pytest.mark.parametrize(
    "bad",
    ["", " ", "abc", "-1", "0", "45.5", "45m", "$(id)", "599", "21601", "999999"],
    ids=[
        "empty",
        "space",
        "text",
        "negative",
        "zero",
        "fractional",
        "unit-suffix",
        "cmd-subst",
        "below-floor",
        "just-over-ceiling",
        "fat-fingered",
    ],
)
def test_malformed_or_out_of_range_is_refused(bad):
    """A too-short cap reintroduces the reap-before-assignment failure from
    the other side; a workday-long cap is a leak with a config entry."""
    code, secs, output = _resolve(override=bad)
    assert code != 0
    assert secs is None
    assert "RUNNER_IDLE_TIMEOUT_SECS" in output


def test_template_carries_the_placeholder_and_launcher_substitutes_it():
    """Half a knob is worse than none: a placeholder nothing substitutes
    reaches bash as a syntax error, and a substitution with no placeholder
    silently pins the old constant."""
    assert "IDLE_TIMEOUT_SECS=__RUNNER_IDLE_TIMEOUT_SECS__" in TEMPLATE.read_text()
    assert '__RUNNER_IDLE_TIMEOUT_SECS__|$(esc "$IDLE_TIMEOUT_SECS")' in LAUNCHER.read_text()


def test_extracted_block_is_the_real_one():
    block = _extract_block()
    assert 'IDLE_TIMEOUT_SECS="$RUNNER_IDLE_TIMEOUT_SECS"' in block
    assert "IDLE_TIMEOUT_SECS_DEFAULT=2700" in block
    assert "exit 1" in block
