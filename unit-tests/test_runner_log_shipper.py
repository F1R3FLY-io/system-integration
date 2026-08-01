"""The heartbeat and post-mortem console shipping in cloud-init-runner.yml.tmpl.

Console history only ever held the ~49s of boot output — serial writes stop
when the bootstrap goes quiet, so a runner failure at hour four of a soak left
nothing durable to read. The pgrep watchdog fix narrowed it further: the old
buffered-log grep loop kept echoing until ~226s, the fixed one goes silent the
moment a job starts. Finding by claude-session-9f68c6fa.

Two mechanisms fix it, both writing to /dev/console (captured by
`oci compute instance-console-history`, which survives termination):

- HEARTBEAT: one bounded line every 2 minutes for the VM's whole life.
- POST-MORTEM: bounded tails of runner-run.log / dmesg / docker state at the
  moment run.sh exits — where the unexplained runner losses live.

Both are extracted from the real template and run under bash with stubs, so a
template edit these tests do not account for fails here rather than silently
un-shipping the evidence again.
"""

import re
import shlex
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "ci" / "oci-runners" / "cloud-init-runner.yml.tmpl"

# The console-history buffer this all has to fit inside.
CONSOLE_BUFFER_BYTES = 1_000_000
SOAK_SECONDS = 60 * 3600  # the weekend run
MAX_HEARTBEAT_LINE = 256


def _extract(start_marker: str, end_marker: str) -> str:
    """Slice a function out of the template and dedent the 6-space YAML indent."""
    text = TEMPLATE.read_text()
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    body = text[start:end]
    return "\n".join(line[6:] if line.startswith("      ") else line for line in body.splitlines())


def _heartbeat_body() -> str:
    return _extract("heartbeat() {", "heartbeat &")


def _post_mortem_body() -> str:
    return _extract("post_mortem() {", "# Wedge escape.")


def _run(script: str) -> str:
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    return proc.stdout + proc.stderr


def _one_heartbeat(*, worker_running: bool, runlog_content=None) -> str:
    """Run exactly one heartbeat iteration with sleep and pgrep stubbed.

    The stub sleep returns once (letting the first iteration emit) and exits
    the shell on its second call, so the infinite loop terminates.
    """
    with tempfile.TemporaryDirectory() as tmp:
        runlog = Path(tmp) / "runner-run.log"
        if runlog_content is not None:
            runlog.write_text(runlog_content)

        body = _heartbeat_body().replace("/var/log/runner-run.log", shlex.quote(str(runlog)))
        script = "\n".join(
            [
                "set -euo pipefail",
                'log() { echo "LOG: $*"; }',
                "RUNNER_USER=runner",
                "SLEEPS=0",
                'sleep() { SLEEPS=$((SLEEPS+1)); [ "$SLEEPS" -ge 2 ] && exit 0; return 0; }',
                f"pgrep() {{ return {0 if worker_running else 1}; }}",
                body,
                "heartbeat",
            ]
        )
        return _run(script)


def test_heartbeat_emits_one_bounded_line():
    out = _one_heartbeat(worker_running=True, runlog_content="Running job: soak\n")
    lines = [ln for ln in out.splitlines() if "HEARTBEAT" in ln]

    assert len(lines) == 1, f"expected exactly one heartbeat per iteration, got {len(lines)}"
    assert len(lines[0]) <= MAX_HEARTBEAT_LINE, (
        f"heartbeat line is {len(lines[0])} bytes; the 60h console budget assumes "
        f"<= {MAX_HEARTBEAT_LINE}"
    )


def test_heartbeat_reports_worker_state_both_ways():
    assert "worker=up" in _one_heartbeat(worker_running=True, runlog_content="x\n")
    assert "worker=down" in _one_heartbeat(worker_running=False, runlog_content="x\n")


def test_heartbeat_survives_a_missing_run_log():
    """Before config.sh runs there is no runner-run.log; the heartbeat starts
    earlier than that and must not die to it (diagnostics are not load-bearing)."""
    out = _one_heartbeat(worker_running=False, runlog_content=None)

    assert "HEARTBEAT" in out, f"heartbeat died on a missing log file:\n{out}"


def test_heartbeat_truncates_a_pathological_last_line():
    out = _one_heartbeat(worker_running=True, runlog_content="x" * 5000 + "\n")
    line = next(ln for ln in out.splitlines() if "HEARTBEAT" in ln)

    assert len(line) <= MAX_HEARTBEAT_LINE, (
        "a long runner-run.log line blew the heartbeat budget; the cut is missing"
    )


def _run_post_mortem(*, runlog_content=None, docker_defined=True, dmesg_lines=5) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        runlog = Path(tmp) / "runner-run.log"
        if runlog_content is not None:
            runlog.write_text(runlog_content)

        body = _post_mortem_body().replace("/var/log/runner-run.log", shlex.quote(str(runlog)))
        dmesg_stub = f'dmesg() {{ seq 1 {dmesg_lines} | sed "s/^/kernel line /"; }}'
        docker_stub = (
            'docker() { printf "rnode.bootstrap Up 4 hours\\nrnode.validator1 Exited (137)\\n"; }'
            if docker_defined
            else ""  # docker: command not found — the golden-image-drift case
        )
        script = "\n".join(
            [
                "set -euo pipefail",
                'log() { echo "LOG: $*"; }',
                "attempt=1",
                dmesg_stub,
                docker_stub,
                'PATH="/usr/bin:/bin"',  # keep real docker/dmesg out when undefined
                body,
                "post_mortem || true",
                "echo SCRIPT-REACHED-END",
            ]
        )
        return _run(script)


def test_post_mortem_ships_all_three_sources():
    out = _run_post_mortem(runlog_content="line-a\nline-b\n")

    assert "POST-MORTEM begin" in out
    assert "POST-MORTEM runner-run: line-b" in out
    assert "POST-MORTEM dmesg: kernel line" in out
    assert "POST-MORTEM docker: rnode.bootstrap" in out
    assert "POST-MORTEM end" in out, "dump did not run to completion"


def test_post_mortem_is_bounded():
    """One dump must not evict the heartbeat trail from the 1MB buffer."""
    out = _run_post_mortem(runlog_content="".join(f"l{i}\n" for i in range(500)), dmesg_lines=300)
    dump_lines = [ln for ln in out.splitlines() if "POST-MORTEM" in ln]

    assert len(dump_lines) <= 70, (
        f"post-mortem emitted {len(dump_lines)} lines; it must stay bounded"
    )


def test_post_mortem_survives_missing_docker_and_missing_log():
    """Every source is best-effort: a VM with no docker and no run log still
    produces begin/end and whatever it can — and does not abort the bootstrap."""
    out = _run_post_mortem(runlog_content=None, docker_defined=False)

    assert "POST-MORTEM begin" in out
    assert "POST-MORTEM end" in out
    assert "SCRIPT-REACHED-END" in out, "a missing source aborted the surrounding script"


def test_console_budget_holds_for_a_60h_soak():
    """The arithmetic the interval comment promises, enforced.

    If someone shrinks the sleep or widens the line, this fails instead of the
    heartbeat silently scrolling the buffer past the failure evidence.
    """
    body = _heartbeat_body()
    m = re.search(r"sleep (\d+)", body)
    assert m, "heartbeat interval not found"
    interval = int(m.group(1))

    heartbeat_bytes = (SOAK_SECONDS // interval) * MAX_HEARTBEAT_LINE
    post_mortem_bytes = 70 * 200
    assert heartbeat_bytes + post_mortem_bytes < CONSOLE_BUFFER_BYTES * 0.6, (
        f"60h at one {MAX_HEARTBEAT_LINE}B line per {interval}s is "
        f"{heartbeat_bytes + post_mortem_bytes} bytes — too close to the ~1MB "
        f"console-history buffer"
    )


def test_extracted_functions_are_the_real_ones():
    """If the markers stop matching, everything above passes vacuously."""
    text = TEMPLATE.read_text()

    assert "heartbeat() {" in text and "heartbeat &" in text, "heartbeat gone from template"
    assert "post_mortem() {" in text, "post_mortem gone from template"
    assert "post_mortem || true" in text, (
        "post_mortem is defined but never invoked before self-terminate"
    )
    assert text.index("post_mortem || true") < text.index("self-terminating instance"), (
        "post_mortem must run BEFORE the instance terminates, or the evidence dies with it"
    )
