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
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "ci" / "oci-runners" / "cloud-init-runner.yml.tmpl"

# The watchdog subshell is delimited by these markers in the real template.
# _START sits *inside* `while kill -0 ...; do`, so the extracted body is already
# one loop iteration and needs no loop-scaffolding surgery.
_START = "# Is a job running? Ask the process table, not the log."
_END = "WATCHDOG_PID=$!"
# One iteration ends at the sleep; everything before it is the two decisions.
_ITERATION_END = "sleep 15"


def _extract_watchdog() -> str:
    """Pull the watchdog's decision body out of the real template.

    cloud-init embeds the script indented inside YAML; dedent so bash sees it.
    """
    text = TEMPLATE.read_text()
    start = text.index(_START)
    end = text.index(_END, start)
    body = text[start:end]
    return "\n".join(line[6:] if line.startswith("      ") else line for line in body.splitlines())


def _one_iteration() -> str:
    """Both decision branches — job-detected and idle-timeout — and nothing else.

    An earlier version built this by splitting on the literal `"do"` to strip the
    `while ... do` header. `_START` is already inside the loop, so that split hit
    the first `"do"` in the *comment text* — inside the word "stdout" — and fed
    bash the prose fragment `ut is redirected to a FILE...`. It survived only
    because the harness ran without `set -e`, and it silently truncated the
    timeout branch out of every test. Slicing to a marker avoids the whole class.
    """
    body = _extract_watchdog()
    assert _ITERATION_END in body, "loop shape changed; the iteration marker is gone"
    return body.split(_ITERATION_END)[0]


class WatchdogRun(NamedTuple):
    killed: bool
    stood_down: bool
    pgrep_args: str
    out: str


def _run_watchdog(
    *,
    worker_running: bool,
    log_contents: str,
    elapsed: int = 0,
    timeout_secs: int = 1800,
    runner_user: str = "runner",
) -> WatchdogRun:
    """Drive one real watchdog iteration with the environment stubbed.

    `killed` reflects the actual `kill "$RUN_PID"` in the template, not a marker
    injected by this harness — the previous version deleted the timeout branch
    before running, so "would kill" only ever meant "did not stand down".
    """
    decision = _one_iteration()

    with tempfile.TemporaryDirectory() as tmp:
        runlog = Path(tmp) / "runner-run.log"
        runlog.write_text(log_contents)
        idle_flag = Path(tmp) / "runner-idle-timeout"
        pgrep_args = Path(tmp) / "pgrep-args"

        # Record the args even though the real call redirects stdout to
        # /dev/null: an explicit redirect inside the function wins.
        pgrep_stub = (
            f'pgrep() {{ printf "%s" "$*" > {shlex.quote(str(pgrep_args))}; '
            f"return {0 if worker_running else 1}; }}"
        )

        script = "\n".join(
            [
                "set -uo pipefail",
                'log() { echo "LOG: $*"; }',
                'kill() { echo "KILLED: $*"; }',
                # Phase 2 is exercised by its own tests below; here it must
                # only be observably engaged on the job-detected path.
                'wedge_watch() { echo "WEDGE-WATCH-ENGAGED"; }',
                pgrep_stub,
                f"RUNNER_USER={shlex.quote(runner_user)}",
                "RUN_PID=4242",
                f"IDLE_TIMEOUT_SECS={timeout_secs}",
                f"elapsed={elapsed}",
                decision.replace("/var/log/runner-run.log", shlex.quote(str(runlog))).replace(
                    "/run/runner-idle-timeout", shlex.quote(str(idle_flag))
                ),
                "echo DECISION:LOOP_CONTINUED",
            ]
        )

        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        out = proc.stdout + proc.stderr
        return WatchdogRun(
            killed="KILLED:" in out,
            stood_down="Job detected" in out,
            pgrep_args=pgrep_args.read_text() if pgrep_args.exists() else "",
            out=out,
        )


# --- the regression this change exists to prevent ---------------------------


def test_running_job_is_detected_when_the_log_has_not_flushed():
    """The exact failure: worker alive, but nothing in the log yet.

    run.sh's stdout is redirected to a file, so .NET block-buffers it (~4KB)
    while startup output is a few hundred bytes. "Running job:" can sit
    unflushed indefinitely. Before the fix the watchdog saw an empty log,
    concluded "idle", and killed a live job at the timeout.

    `elapsed` is past the timeout deliberately: standing down has to win even
    when the deadline has passed, which is the whole point.
    """
    run = _run_watchdog(worker_running=True, log_contents="", elapsed=1800, timeout_secs=1800)

    assert not run.killed, "watchdog would have killed a job that was actually running"
    assert run.stood_down


def test_running_job_is_detected_from_the_log_when_pgrep_finds_nothing():
    """Secondary signal still works — e.g. worker exited, listener has not."""
    run = _run_watchdog(
        worker_running=False,
        log_contents="2026-01-01 Running job: soak\n",
        elapsed=1800,
        timeout_secs=1800,
    )

    assert not run.killed
    assert run.stood_down


def test_truly_idle_runner_is_killed_once_the_deadline_passes():
    """The watchdog's actual purpose, exercised through the real kill branch.

    No worker, nothing in the log, deadline reached: this VM never got a job and
    must be killed so it can self-terminate, or it leaks — which is how the idle
    fleet accumulated in the first place.

    Previously this asserted on a marker the harness printed after deleting the
    timeout branch, so it only proved the stand-down path was not taken. It now
    observes the template's own `kill "$RUN_PID"`.
    """
    run = _run_watchdog(worker_running=False, log_contents="", elapsed=1800, timeout_secs=1800)

    assert run.killed, "an idle runner must still be reaped, or VMs leak"
    assert "KILLED: 4242" in run.out, "the runner PID must be what gets killed"
    assert "killing runner to self-terminate" in run.out


def test_idle_runner_is_left_alone_before_the_deadline():
    """The other side of the branch: idle but early is not a kill.

    Without this, a mutation that made the timeout unconditional would still
    pass every other test here.
    """
    run = _run_watchdog(worker_running=False, log_contents="", elapsed=15, timeout_secs=1800)

    assert not run.killed, "killed an idle runner before its deadline"
    assert not run.stood_down
    assert "LOOP_CONTINUED" in run.out


def test_unrelated_log_noise_does_not_look_like_a_job():
    """Guard against the log check matching something that is not a job."""
    run = _run_watchdog(
        worker_running=False,
        log_contents="Listening for Jobs\nConnected to GitHub\n",
        elapsed=1800,
        timeout_secs=1800,
    )

    assert run.killed


# --- the process check must not be fooled by another user's processes --------


def test_worker_lookup_is_scoped_to_the_runner_user():
    """`pgrep -f` without -u matches any process on the box.

    A stray command line containing the pattern then reads as "a job is
    running", the watchdog stands down permanently, and the VM never
    self-terminates — the leak the watchdog exists to prevent. Reported on
    PR #72 and merged unfixed.
    """
    run = _run_watchdog(worker_running=True, log_contents="", runner_user="runner")

    assert "-u runner" in run.pgrep_args, (
        f"pgrep is not scoped to the runner user: {run.pgrep_args!r}"
    )


def test_worker_pattern_escapes_the_dot():
    """`-f` takes an ERE, so an unescaped `.` matches any character.

    Not a live exploit, but the pattern should mean what it looks like.
    """
    run = _run_watchdog(worker_running=True, log_contents="")

    assert r"Runner\.Worker" in run.pgrep_args, (
        f"worker pattern is not a literal: {run.pgrep_args!r}"
    )


# --- guard against the extraction silently breaking -------------------------


def test_extracted_watchdog_is_the_real_one():
    """If the markers stop matching, every test above would vacuously pass."""
    body = _extract_watchdog()
    assert "pgrep" in body, "process check gone — the buffered-log bug is back"
    assert re.search(r"Runner\\\.Worker", body), "worker process name gone"
    assert "Running job:" in body, "secondary log signal gone"
    assert "IDLE_TIMEOUT_SECS" in body, "timeout gone"


def test_extracted_iteration_contains_both_branches():
    """The slice must carry the kill path, not just the stand-down path.

    The bug this replaces: the old extraction split on the literal "do" and hit
    it inside the word "stdout", truncating the iteration mid-comment. Every
    "would kill" assertion was really "did not stand down".
    """
    iteration = _one_iteration()

    assert "pgrep" in iteration, "job-detection branch missing from the slice"
    assert 'if [ "$elapsed" -ge "$IDLE_TIMEOUT_SECS" ]' in iteration, "timeout branch missing"
    assert 'kill "$RUN_PID"' in iteration, "the actual kill is missing"
    assert "sleep" not in iteration, "slice ran past one iteration"


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


# --- phase 2: the wedge escape (live-VM evidence, c7fd9f 2026-08-01) ---------
#
# GitHub deregistered the runner and failed the job while Runner.Listener
# stayed alive on the VM — run.sh blocked `wait` forever, self-terminate never
# ran, and the VM leaked until the reaper. Every exit-path log sat after
# `wait`, so the console showed nothing. The wedge watch is the only path that
# turns that state back into the normal ephemeral exit.

_WEDGE_START = "WEDGE_TIMEOUT_SECS="
_WEDGE_END = 'log "=== Configure runner'


def _wedge_body() -> str:
    text = TEMPLATE.read_text()
    start = text.index(_WEDGE_START)
    end = text.index(_WEDGE_END, start)
    body = text[start:end]
    return "\n".join(line[6:] if line.startswith("      ") else line for line in body.splitlines())


def _run_wedge(*, worker_absent_iters: int, runpid_alive_checks: int = 30):
    """Drive wedge_watch with time compressed to zero.

    ``worker_absent_iters``: how many consecutive pgrep calls report no worker
    before the worker "appears" (a huge value = wedged forever).
    ``runpid_alive_checks``: how many `kill -0` liveness checks succeed before
    run.sh "exits on its own".
    """
    script = "\n".join(
        [
            "set -uo pipefail",
            'log() { echo "LOG: $*"; }',
            "RUN_PID=4242",
            "RUNNER_USER=runner",
            "CHECKS=0; PGREPS=0",
            "kill() {",
            '  if [ "$1" = "-0" ]; then',
            "    CHECKS=$((CHECKS+1));"
            f' [ "$CHECKS" -le {runpid_alive_checks} ] && return 0 || return 1',
            "  fi",
            '  echo "KILLED: $*"',
            "}",
            "pgrep() {",
            "  PGREPS=$((PGREPS+1));"
            f' [ "$PGREPS" -le {worker_absent_iters} ] && return 1 || return 0',
            "}",
            "sleep() { :; }",
            _wedge_body(),
            "wedge_watch",
            "echo WEDGE-RETURNED",
        ]
    )
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    return proc.stdout + proc.stderr


def test_wedged_listener_is_killed_after_the_timeout():
    """The c7fd9f case: worker never comes back, run.sh never exits."""
    out = _run_wedge(worker_absent_iters=10_000)

    assert "KILLED: 4242" in out, "a wedged listener was never killed — the VM leaks"
    assert "WEDGE:" in out, "the kill must announce itself on the console"
    assert "WEDGE-RETURNED" in out


def test_healthy_job_is_never_wedge_killed():
    """Worker present throughout: the watch waits for a natural exit."""
    out = _run_wedge(worker_absent_iters=0, runpid_alive_checks=15)

    assert "KILLED: 4242" not in out, "wedge watch killed a healthy job"
    assert "WEDGE:" not in out


def test_brief_worker_gaps_reset_the_wedge_clock():
    """Worker absent for 5 minutes then back (job phases, worker restart):
    the absence counter must reset rather than accumulate toward a kill."""
    out = _run_wedge(worker_absent_iters=5, runpid_alive_checks=20)

    assert "KILLED: 4242" not in out, "a transient worker gap was treated as a wedge"


def test_natural_run_exit_ends_the_watch_without_killing():
    out = _run_wedge(worker_absent_iters=3, runpid_alive_checks=4)

    assert "KILLED: 4242" not in out
    assert "WEDGE-RETURNED" in out, "the watch must end when run.sh exits"


def test_watchdog_engages_the_wedge_watch_on_job_detection():
    """Stand-down must hand off to phase 2, not just exit — a wedged listener
    with no one watching is exactly how c7fd9f leaked."""
    run = _run_watchdog(worker_running=True, log_contents="")

    assert run.stood_down
    assert "WEDGE-WATCH-ENGAGED" in run.out, "job detection exited without engaging the wedge watch"


def test_wedge_timeout_is_long_enough_for_real_worker_gaps():
    """10 minutes: long past any assignment->spawn or between-phase gap, short
    enough to stop a 16-OCPU leak well before the reaper's hours-later pass."""
    m = re.search(r"WEDGE_TIMEOUT_SECS=(\d+)", TEMPLATE.read_text())

    assert m, "WEDGE_TIMEOUT_SECS not found"
    assert 300 <= int(m.group(1)) <= 1800, "wedge timeout outside sane bounds"
