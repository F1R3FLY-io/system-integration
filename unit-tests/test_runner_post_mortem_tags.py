"""Durable post-mortem persistence to OCI freeform tags.

Run 33136185540: `post_mortem` writes its evidence to the serial console of
a VM that self-terminates seconds later; the workflow-side capture job ran
10 minutes after the instance was deleted and recovered only metadata. The
guardian's `soak-health` tag proved freeform tags remain readable off the
dead VM's instance record, so `persist_post_mortem` now writes a bounded,
OOM-prioritized tail there.

The OCI constraints this must respect (each is a test below):
- `instance update --freeform-tags` REPLACES the whole map, and the reaper
  reads `soak-deadline-epoch` from it — existing tags must be merged in;
- a tag value caps at 256 chars and an instance at 10 freeform tags — the
  content is chunked into at most 4 `pm*` keys, fewer when slots are short.

Harness shape as in test_runner_watchdog.py: the real function is extracted
from the template and run under bash with `curl`, `oci`, and the log paths
stubbed; the embedded python3 runs for real.
"""

import json
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "ci" / "oci-runners" / "cloud-init-runner.yml.tmpl"

_START = "# Durable post-mortem"
_END = "# Wedge escape."

_BASE_TAGS = {"purpose": "soak", "series": "weekend", "soak-deadline-epoch": "1756500000"}


def _extract() -> str:
    text = TEMPLATE.read_text()
    start = text.index(_START)
    end = text.index(_END, start)
    body = text[start:end]
    return "\n".join(line[6:] if line.startswith("      ") else line for line in body.splitlines())


def _run(
    *,
    existing_tags=_BASE_TAGS,
    curl_ok=True,
    get_ok=True,
    runlog_line="Job completed with result: Failed",
    kern_lines="Out of memory: Killed process 4242 (Runner.Worker)\n",
):
    """Run persist_post_mortem with the OCI surface stubbed.

    Returns (exit_code, output, update_args_or_None) where update_args is the
    argv the stubbed `oci ... instance update` received, one per line.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        runlog = tmpdir / "runner-run.log"
        kernlog = tmpdir / "kern.log"
        update_args = tmpdir / "update-args"
        runlog.write_text(runlog_line + "\n")
        kernlog.write_text(kern_lines)

        curl_body = 'echo "ocid1.instance.oc1.test"' if curl_ok else "return 1"
        get_body = f"printf '%s' {json.dumps(json.dumps(existing_tags))}" if get_ok else "return 1"
        script = "\n".join(
            [
                "set -uo pipefail",
                'log() { echo "LOG: $*"; }',
                "attempt=1",
                f"curl() {{ {curl_body}; }}",
                'dmesg() { printf "irrelevant line\\nOut of memory: Killed process 4242\\n"; }',
                "oci_stub() {",
                '  case "$*" in',
                f'    *" get "*) {get_body};;',
                f'    *" update "*) printf "%s\\n" "$@" > "{update_args}";;',
                "  esac",
                "}",
                _extract()
                .replace("/usr/local/bin/oci", "oci_stub")
                .replace("/var/log/runner-run.log", str(runlog))
                .replace("/var/log/kern.log", str(kernlog)),
                "persist_post_mortem",
                'echo "PM-DONE:$?"',
            ]
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        out = proc.stdout + proc.stderr
        args = update_args.read_text().splitlines() if update_args.exists() else None
        return proc.returncode, out, args


def _merged_tags(args):
    assert args is not None, "no instance update was issued"
    return json.loads(args[args.index("--freeform-tags") + 1])


def test_existing_tags_survive_the_update():
    """update REPLACES the tag map; losing soak-deadline-epoch would hand the
    VM to the reaper's normal rule mid-post-mortem."""
    code, out, args = _run()
    tags = _merged_tags(args)

    assert code == 0
    for key, value in _BASE_TAGS.items():
        assert tags.get(key) == value, f"existing tag clobbered: {key}"
    assert "pm0" in tags
    assert "PM-DONE:0" in out


def test_evidence_carries_the_oom_line_and_exit_context():
    _, _, args = _run()
    tags = _merged_tags(args)
    evidence = "".join(tags[k] for k in sorted(tags) if k.startswith("pm"))

    assert "attempt=1" in evidence, "exit context missing"
    assert "Out of memory" in evidence, "the line that names the root cause is missing"
    assert "Job completed with result" in evidence, "runner-run tail missing"


def test_chunks_respect_the_tag_value_and_count_caps():
    """256 chars per value, 10 tags per instance: at most 4 pm keys of <=250."""
    _, _, args = _run(kern_lines=("Out of memory: Killed process 4242 xx\n" * 200))
    tags = _merged_tags(args)
    pm_keys = sorted(k for k in tags if k.startswith("pm"))

    assert 0 < len(pm_keys) <= 4
    assert pm_keys == [f"pm{i}" for i in range(len(pm_keys))]
    for k in pm_keys:
        assert len(tags[k]) <= 250, f"{k} exceeds the per-value cap"
    assert len(tags) <= 10, "tag count exceeds the OCI per-instance cap"


def test_pm_keys_yield_to_existing_tags_when_slots_are_short():
    """With 9 non-pm tags only one slot is free; the evidence truncates
    rather than evicting operational tags or breaching the cap."""
    crowded = {f"tag{i}": "x" for i in range(9)}
    _, _, args = _run(existing_tags=crowded)
    tags = _merged_tags(args)
    pm_keys = [k for k in tags if k.startswith("pm")]

    assert len(pm_keys) == 1
    assert len(tags) == 10
    for key in crowded:
        assert key in tags


def test_metadata_failure_skips_persistence_quietly():
    """No IMDS -> no instance id -> nothing to tag; termination must not wait."""
    code, out, args = _run(curl_ok=False)

    assert code == 0
    assert args is None, "an update was attempted with no instance id"
    assert "PM-DONE:0" in out


def test_tag_fetch_failure_still_persists_the_evidence():
    """On a VM seconds from deletion, evidence outranks tags nobody will read
    again: a failed get writes the pm keys alone rather than giving up."""
    code, _, args = _run(get_ok=False)
    tags = _merged_tags(args)

    assert code == 0
    assert "pm0" in tags


def test_persist_runs_on_the_ephemeral_exit_path():
    """It must follow post_mortem (same evidence window, console first) and
    precede self-termination, for every exit — job done, idle, or wedge."""
    text = TEMPLATE.read_text()

    assert "persist_post_mortem || true" in text
    assert (
        text.index("post_mortem || true")
        < text.index("persist_post_mortem || true")
        < (text.index("self-terminating instance via instance principal"))
    )
