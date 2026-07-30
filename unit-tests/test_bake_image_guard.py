"""Unit tests for the ``bake-image.sh`` golden-instance cost guard.

Nothing here touches OCI: the guard is extracted from the real script and run
under bash with a stubbed ``oci`` function, so these tests exercise shipped code
rather than a copy of it.

Why this file exists: a leaked golden VM bills indefinitely, and f1r3node-rust
suffered a ~$1000/day runner leak. The guard is the only thing standing between
an interrupted bake and that outcome, because no scheduled reaper matches the
``ci-runner-golden-*`` name pattern (see docs/ToDos.md TASK-008).

Two of these are regression tests for bugs found by the PR #68 multi-review,
both confirmed reproducible against commit 8995d10:
  * a clean run whose terminate failed exited 0 — reporting success over a live VM
  * SIGINT ran the handler twice and masked the signal status as 0
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BAKE_SCRIPT = REPO_ROOT / "ci" / "oci-runners" / "bake-image.sh"

GUARD_START = 'GOLDEN_INSTANCE_OCID=""'
GUARD_END = "trap 'exit 143' TERM"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def _extract_guard() -> str:
    """Return the cost-guard block verbatim from the real script."""
    lines = BAKE_SCRIPT.read_text().splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == GUARD_START)
        end = next(i for i, ln in enumerate(lines) if ln.strip() == GUARD_END)
    except StopIteration:  # pragma: no cover - only if the script is restructured
        pytest.fail(
            f"Could not locate the cost guard in {BAKE_SCRIPT}. If the guard was "
            f"intentionally restructured, update GUARD_START/GUARD_END here — do "
            f"not delete these tests, they protect against a billing leak."
        )
    guard = "\n".join(lines[start : end + 1])
    assert "_reap_golden_on_exit()" in guard, "extraction missed the handler body"
    assert "trap _reap_golden_on_exit EXIT" in guard, "extraction missed the EXIT trap"
    return guard


@pytest.fixture(scope="module")
def guard() -> str:
    return _extract_guard()


def _run(guard: str, body: str, oci_rc: int, tmp_path: Path):
    """Run the guard with a stubbed ``oci``; return (exit_status, stdout, calls)."""
    calls_file = tmp_path / "calls"
    calls_file.write_text("")
    script = tmp_path / "case.sh"
    # printf, never echo: the guard contains a literal backslash and some shells'
    # echo would mangle it into a broken script.
    script.write_text(
        "set -euo pipefail\n"
        f'oci() {{ printf "call\\n" >> "{calls_file}"; return {oci_rc}; }}\n'
        f"{guard}\n"
        'GOLDEN_INSTANCE_OCID="ocid1.instance.TEST"\n'
        f"{body}\n"
    )
    proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=30)
    calls = len([ln for ln in calls_file.read_text().splitlines() if ln.strip()])
    return proc.returncode, proc.stdout + proc.stderr, calls


def test_unclean_exit_terminates_instance_exactly_once(guard, tmp_path):
    rc, out, calls = _run(guard, "false", 0, tmp_path)
    assert "Terminating to avoid a billing leak" in out
    assert calls == 1


def test_failed_terminate_prints_manual_recovery_command(guard, tmp_path):
    rc, out, calls = _run(guard, "false", 1, tmp_path)
    assert "TERMINATE FAILED" in out
    assert "ocid1.instance.TEST" in out


def test_clean_run_with_failed_terminate_exits_nonzero(guard, tmp_path):
    """Regression (PR #68 review, openai+xai): must not report success.

    Confirmed against 8995d10: the handler did ``exit "$rc"`` where rc was 0 on
    an otherwise-clean run, so a bake whose terminate failed exited 0 with the
    instance still live — defeating the guard's entire purpose.
    """
    rc, out, calls = _run(guard, "exit 0", 1, tmp_path)
    assert rc != 0, "a bake must never exit 0 while the golden VM may still be live"


def test_clean_run_with_successful_terminate_stays_zero(guard, tmp_path):
    """The guard must not manufacture failures on the happy path."""
    rc, out, calls = _run(guard, "exit 0", 0, tmp_path)
    assert rc == 0


def test_original_failure_status_is_preserved(guard, tmp_path):
    rc, out, calls = _run(guard, "exit 42", 0, tmp_path)
    assert rc == 42


def test_disarmed_guard_makes_no_terminate_call(guard, tmp_path):
    rc, out, calls = _run(guard, 'GOLDEN_INSTANCE_OCID=""; exit 0', 0, tmp_path)
    assert calls == 0
    assert rc == 0


def test_sigint_reaps_once_and_preserves_signal_status(guard, tmp_path):
    """Regression (PR #68 review): handler ran twice on SIGINT.

    Confirmed against 8995d10: registering one handler for EXIT/INT/TERM meant
    the INT path's ``exit`` re-entered via EXIT, issuing a second terminate that
    failed against an already-TERMINATING instance and printed a bogus
    TERMINATE FAILED — poisoning the one warning an operator must trust. It also
    reported status 0 for an interrupted bake.
    """
    rc, out, calls = _run(guard, "kill -INT $$\nsleep 5", 0, tmp_path)
    assert "Terminating to avoid a billing leak" in out
    assert calls == 1, f"handler double-fired ({calls} terminate calls)"
    assert rc == 130, f"SIGINT should surface as 130, got {rc}"
