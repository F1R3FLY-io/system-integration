"""Unit tests for the ephemeral runner's idle watchdog.

The watchdog exists to kill a runner that never receives a job, so the VM can
self-terminate instead of leaking. Its one dangerous failure mode is the
inverse: judging a *busy* runner idle and killing a live job. Soak run
30590630059 lost its runner 19 minutes into a job with the VM still healthy and
no log uploaded, which is what that failure looks like from outside.

The loop is extracted from the real cloud-init template and run under bash with
a stubbed `pgrep`, so a change to the template that these tests do not account
for fails here rather than drifting silently. No OCI, no network, no VM.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "ci" / "oci-runners" / "cloud-init-runner.yml.tmpl"

# The watchdog subshell is delimited by these markers in the real template.
_START = "# Is a job running? Ask the process table, not the log."
_END = "WATCHDOG_PID=$!"


def _extract_watchdog() -> str:
    """Pull the watchdog's decision body out of the real template.

    cloud-init embeds the script indented inside YAML; dedent so bash sees it.
    """
    text = TEMPLATE.read_text()
    start = text.index(_START)
    end = text.index(_END, start)
    body = text[start:end]
    return "\n".join(line[6:] if line.startswith("      ") else line for line in body.splitlines())


def _run_watchdog(*, worker_running: bool, log_contents: str, timeout_secs: int = 1):
    """Drive one watchdog decision with `pgrep` and the log file stubbed.

    Returns (killed, stdout+stderr). `killed` is True when the watchdog decided
    the runner was idle and killed the job.
    """
    body = _extract_watchdog()
    # Keep only the decision logic; drop the outer `while kill -0 ...` loop so
    # the test exercises exactly one pass without needing a real child process.
    decision = body.split('if [ "$elapsed" -ge')[0]
    decision = decision.split("while kill -0", 1)[-1]
    decision = decision.split("do", 1)[-1] if "do" in decision else decision

    pgrep_stub = "pgrep() { return 0; }" if worker_running else "pgrep() { return 1; }"

    script = "\n".join(
        [
            "set -uo pipefail",
            'log() { echo "LOG: $*"; }',
            pgrep_stub,
            f"IDLE_TIMEOUT_SECS={timeout_secs}",
            'runlog="$(mktemp)"',
            f"printf '%s' {log_contents!r} > \"$runlog\"",
            # Point the extracted code at the temp log.
            "set -- ",
            decision.replace("/var/log/runner-run.log", '"$runlog"').replace(
                "exit 0", "echo DECISION:STAND_DOWN; exit 0"
            ),
            "echo DECISION:WOULD_KILL",
        ]
    )

    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    out = proc.stdout + proc.stderr
    return ("DECISION:WOULD_KILL" in out), out


# --- the regression this change exists to prevent ---------------------------


def test_running_job_is_detected_when_the_log_has_not_flushed():
    """The exact failure: worker alive, but nothing in the log yet.

    run.sh's stdout is redirected to a file, so .NET block-buffers it (~4KB)
    while startup output is a few hundred bytes. "Running job:" can sit
    unflushed indefinitely. Before the fix the watchdog saw an empty log,
    concluded "idle", and killed a live job at the timeout.
    """
    killed, out = _run_watchdog(worker_running=True, log_contents="")

    assert not killed, "watchdog would have killed a job that was actually running"
    assert "STAND_DOWN" in out


def test_running_job_is_detected_from_the_log_when_pgrep_finds_nothing():
    """Secondary signal still works — e.g. worker exited, listener has not."""
    killed, _ = _run_watchdog(worker_running=False, log_contents="2026-01-01 Running job: soak\n")

    assert not killed


def test_truly_idle_runner_is_still_killed():
    """The watchdog's actual purpose must survive the fix.

    No worker, nothing in the log: this VM never got a job and must be killed
    so it can self-terminate, or it leaks — which is how the idle fleet
    accumulated in the first place.
    """
    killed, _ = _run_watchdog(worker_running=False, log_contents="")

    assert killed, "an idle runner must still be reaped, or VMs leak"


def test_unrelated_log_noise_does_not_look_like_a_job():
    """Guard against the log check matching something that is not a job."""
    killed, _ = _run_watchdog(
        worker_running=False,
        log_contents="Listening for Jobs\nConnected to GitHub\n",
    )

    assert killed


# --- guard against the extraction silently breaking -------------------------


def test_extracted_watchdog_is_the_real_one():
    """If the markers stop matching, every test above would vacuously pass."""
    body = _extract_watchdog()
    assert "pgrep" in body, "process check gone — the buffered-log bug is back"
    assert re.search(r"Runner\.Worker", body), "worker process name gone"
    assert "Running job:" in body, "secondary log signal gone"
    assert "IDLE_TIMEOUT_SECS" in body, "timeout gone"


def test_log_function_reaches_the_serial_console():
    """Diagnostics must survive instance termination.

    Files on the VM die with it; serial console output is captured by
    `oci compute instance-console-history`, a separate resource with its own
    lifecycle. f1r3node-rust's capture_diagnostics job greps that history for
    the idle-watchdog kill line, so this tee is load-bearing for cross-repo
    diagnosis, not decoration.
    """
    text = TEMPLATE.read_text()
    log_def = text[text.index("log() {") : text.index("log() {") + 400]

    assert "/dev/console" in log_def, "log() no longer reaches the serial console"
    assert "|| true" in log_def, "console write must never abort the bootstrap"


def test_watchdog_timeout_is_long_enough_to_be_a_backstop_not_a_limit():
    """A short timeout would make this a job-duration cap rather than a leak guard."""
    text = TEMPLATE.read_text()
    m = re.search(r"IDLE_TIMEOUT_SECS=(\d+)", text)

    assert m, "IDLE_TIMEOUT_SECS not found"
    assert int(m.group(1)) >= 1800, "timeout too short to be a safe idle backstop"
