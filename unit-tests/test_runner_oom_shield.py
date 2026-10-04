"""The runner-side kernel-OOM shield in cloud-init-runner.yml.tmpl.

Incident f1r3node-rust#365 (soak run 33136185540): on the first
green-preflight soak night the kernel OOM killer chose Runner.Worker
mid-soak. The wedge watch read "worker absent 600s", killed run.sh, the
bootstrap self-terminated the VM through the normal ephemeral exit path,
and the night's artifact died with it. The node-side guardian (f1r3node-rust
PR #364) biases the workload to oom_score_adj +1000; the shield tested here
is the complementary half — the runner tree pinned to -1000 so the kernel
can never prefer the runner over the workload.

Same harness shape as test_runner_watchdog.py: the real block is extracted
from the template and run under bash with `/proc` redirected to a tmpdir and
`pgrep`/`sleep` stubbed. No OCI, no VM, no root.
"""

import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "ci" / "oci-runners" / "cloud-init-runner.yml.tmpl"

_START = "# Kernel-OOM shield"
_END = "# Heartbeat:"


def _extract_shield() -> str:
    text = TEMPLATE.read_text()
    start = text.index(_START)
    end = text.index(_END, start)
    body = text[start:end]
    return "\n".join(line[6:] if line.startswith("      ") else line for line in body.splitlines())


def _run_shield(*, worker_pids=("111", "222"), precreate=True, pid_owner="runner"):
    """Run the real shield with /proc mapped to a tmpdir.

    The re-assert loop is unbounded by design; the stubbed `sleep` exits the
    background subshell after its first pass, and `wait` collects it.
    ``pid_owner`` is what the stubbed `stat -c %U` reports for every pid dir —
    the shield's write-time ownership re-check compares it to RUNNER_USER.
    """
    with tempfile.TemporaryDirectory() as tmp:
        proc_root = Path(tmp) / "proc"
        pgrep_args = Path(tmp) / "pgrep-args"
        pids_out = "; ".join(f'echo "{p}"' for p in worker_pids) or ":"
        script = "\n".join(
            [
                "set -uo pipefail",
                'log() { echo "LOG: $*"; }',
                "RUNNER_USER=runner",
                f'pgrep() {{ printf "%s" "$*" > "{pgrep_args}"; {pids_out}; }}',
                f'stat() {{ echo "{pid_owner}"; }}',
                "sleep() { exit 0; }",
                'echo "SELF:$$"',
                f'mkdir -p "{proc_root}/$$"' if precreate else ":",
                "\n".join(
                    f'mkdir -p "{proc_root}/{p}"' for p in (worker_pids if precreate else [])
                ),
                _extract_shield().replace("/proc", str(proc_root)),
                "wait",
                'echo "SHIELD-DONE"',
            ]
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        out = proc.stdout + proc.stderr
        self_pid = None
        for line in proc.stdout.splitlines():
            if line.startswith("SELF:"):
                self_pid = line.removeprefix("SELF:")

        # Materialize before the tmpdir vanishes with the `with` block.
        adjustments = {}
        for pid in (*worker_pids, self_pid):
            f = proc_root / str(pid) / "oom_score_adj"
            if pid is not None and f.exists():
                adjustments[str(pid)] = f.read_text()
        args_text = pgrep_args.read_text() if pgrep_args.exists() else ""

        return proc.returncode, out, adjustments.get, self_pid, args_text


def test_bootstrap_pins_itself_to_minus_1000():
    """Children inherit oom_score_adj, so the self-pin is what covers run.sh
    and every watchdog subshell without chasing their PIDs."""
    code, out, adj, self_pid, _ = _run_shield()

    assert code == 0
    assert "SHIELD-DONE" in out
    assert adj(self_pid) == "-1000\n", "bootstrap did not pin its own oom_score_adj"


def test_listener_and_worker_pids_are_reasserted():
    """The agent's self-update can re-exec Listener/Worker outside the
    inheritance chain; the loop pins whatever pgrep reports."""
    code, _, adj, _, _ = _run_shield(worker_pids=("111", "222"))

    assert code == 0
    assert adj("111") == "-1000\n"
    assert adj("222") == "-1000\n"


def test_pid_lookup_is_scoped_to_the_runner_user_and_runner_processes():
    """Unscoped pgrep -f would pin (or attempt to pin) arbitrary processes —
    including the workload the node guardian deliberately biases the other
    way."""
    _, _, _, _, args = _run_shield()

    assert "-u runner" in args, f"pgrep not scoped to the runner user: {args!r}"
    assert r"Runner\.(Listener|Worker)" in args, f"pattern too broad: {args!r}"


def test_reused_pid_owned_by_another_user_is_not_pinned():
    """A pid can be reaped and reused between the pgrep and the write; if the
    reused pid belongs to another user, pinning it would hand OOM immunity to
    an arbitrary process (multi-review PR #130, bedrock finding)."""
    code, out, adj, _, _ = _run_shield(worker_pids=("111",), pid_owner="root")

    assert code == 0
    assert adj("111") is None, "a foreign-owned reused pid was pinned to -1000"
    assert "SHIELD-DONE" in out


def test_shield_is_best_effort():
    """A missing /proc entry (process exited between pgrep and write) must
    not abort anything — the shield can never take down what it protects."""
    code, out, _, _, _ = _run_shield(worker_pids=("999",), precreate=False)

    assert code == 0
    assert "SHIELD-DONE" in out


def test_shield_engages_before_the_runner_starts():
    """The pin must precede `run.sh` (inheritance) and the watchdog subshells;
    a shield installed after the fork protects nothing."""
    text = TEMPLATE.read_text()

    assert text.index("protect_runner_from_oom\n") < text.index("heartbeat &"), (
        "shield must be invoked before the first background subshell"
    )
    assert text.index("protect_runner_from_oom\n") < text.index("./run.sh"), (
        "shield must be invoked before run.sh starts"
    )


def test_extracted_shield_is_the_real_one():
    body = _extract_shield()
    assert 'echo "-1000"' in body, "the -1000 pin is gone"
    assert "oom_score_adj" in body
    assert "pgrep" in body, "the Listener/Worker re-assert loop is gone"
    assert "stat -c %U" in body, "the write-time ownership re-check is gone"
