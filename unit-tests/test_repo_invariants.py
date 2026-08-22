"""Repo-level invariants that are easy to break with an ordinary edit.

These assert things the code cannot enforce about itself: that CI validates
every compose file that exists, that no file points at a path that was deleted,
and that a removed CLI flag actually errors rather than silently doing
something. All were review findings on PR #74 — each one is a case where a
comment claimed a property that nothing checked.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "smoke-test.yml"
COMPOSE_DIR = REPO_ROOT / "compose"


def _ci_validated_compose_files() -> set:
    """The compose files the smoke-test workflow enumerates for validation."""
    text = WORKFLOW.read_text()
    m = re.search(r"for cf in \\\n(.*?)\n          do", text, re.S)
    assert m, "could not find the compose enumeration in smoke-test.yml"
    return set(re.findall(r"compose/[\w.-]+\.yml", m.group(1)))


def test_ci_validates_every_compose_file():
    """A compose file added without a CI entry must fail here, not slip through.

    The workflow enumerates files explicitly rather than globbing, which is the
    right call — a new topology should be classified deliberately. But the
    comment claimed that made omissions "visible" when nothing actually compared
    the list against the directory, so an omission was silent. This is the check
    that makes the claim true.
    """
    on_disk = {f"compose/{p.name}" for p in COMPOSE_DIR.glob("*.yml")}
    in_ci = _ci_validated_compose_files()

    assert on_disk - in_ci == set(), (
        f"compose files exist but CI never validates them: {sorted(on_disk - in_ci)}"
    )


def test_ci_does_not_validate_deleted_compose_files():
    """The mirror case: a stale CI entry fails the job with a confusing error."""
    on_disk = {f"compose/{p.name}" for p in COMPOSE_DIR.glob("*.yml")}
    in_ci = _ci_validated_compose_files()

    assert in_ci - on_disk == set(), (
        f"CI validates compose files that no longer exist: {sorted(in_ci - on_disk)}"
    )


def test_scala_flag_is_rejected_not_ignored():
    """`--scala` must be a hard error now that the Scala node is gone.

    Review finding on PR #74: the compatibility boundary was documented but not
    enforced. A stale script passing --scala would fail in whatever way the CLI
    framework happened to choose, at whatever point it noticed — rather than
    immediately and legibly. `--rust` is deliberately still accepted; this pins
    the asymmetry so neither half drifts.
    """
    proc = subprocess.run(
        ["poetry", "run", "shardctl", "test", "--scala"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode != 0, "--scala was accepted; the flag should be gone"
    combined = (proc.stdout + proc.stderr).lower()
    assert "no such option" in combined or "unexpected" in combined, (
        f"--scala failed, but not with a recognisable parse error:\n{combined[:400]}"
    )


def test_rust_flag_is_still_accepted():
    """The other half of the compatibility promise.

    Kept so existing callers work. If someone "tidies up" by removing it, this
    fails and they have to make that a deliberate, breaking decision.
    """
    proc = subprocess.run(
        ["poetry", "run", "shardctl", "test", "--rust", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    combined = (proc.stdout + proc.stderr).lower()
    assert "no such option" not in combined, "--rust was rejected; it must stay accepted"


@pytest.mark.parametrize(
    "deleted",
    [
        "compose/f1r3node.yml",
        "compose/f1r3node-standalone.yml",
        "compose/f1r3node-observer.yml",
        "compose/f1r3node-validator4.yml",
        "compose/f1r3node-shard-light.yml",
        "conf/scala.conf",
        "conf/standalone-scala.conf",
        "conf/logback.xml",
        "ci/setup-f1r3node-scala-runner.sh",
    ],
    ids=lambda p: Path(p).name,
)
def test_no_live_references_to_removed_files(deleted):
    """Deleted paths must not survive as references in code or operational docs.

    Historical records are exempt: migration-to-rust-node.md, ToDos.md and
    UserStories.md describe what the layout *used to be*, and scrubbing them
    would destroy the explanation of why things look the way they do.

    This file is exempt from itself — it has to name the paths it is checking
    for, and `git grep` searches tracked files, so once committed it matches
    every one of them.
    """
    historical = {
        "docs/migration-to-rust-node.md",
        "docs/ToDos.md",
        "docs/UserStories.md",
        "unit-tests/test_repo_invariants.py",
    }
    # A file may name its own predecessor in a provenance note. Allowed per
    # (deleted path, referring file) pair rather than by substring, so an
    # unrelated live reference in a similarly-named file still fails.
    provenance = {
        ("compose/f1r3node-shard-light.yml", "compose/f1r3node-rust-shard-light.yml"),
    }

    proc = subprocess.run(
        ["git", "grep", "-l", "--", deleted],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    hits = {line for line in proc.stdout.splitlines() if line.strip()}
    hits = {h for h in hits if h not in historical and (deleted, h) not in provenance}

    assert hits == set(), f"{deleted} was deleted but is still referenced by: {sorted(hits)}"


def test_workflow_has_no_scala_jobs():
    """The Scala CI jobs are gone and must not creep back via a merge."""
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    scala_jobs = [k for k in jobs if "scala" in k.lower()]

    assert scala_jobs == [], f"Scala jobs reappeared in smoke-test.yml: {scala_jobs}"


# ── Shard ports are reserved from the ephemeral range in CI ──────────────
#
# Compose publishes 40400-40455 and the test framework allocates 41000-49000,
# both inside Linux's ephemeral range. A fresh runner's outbound connections
# can land on one of them and make `docker compose up` fail with "address
# already in use" (observed: run 32571212745, "Error: Port conflict").
# Every job that brings up a shard must reserve the span first.

RESERVE_SCRIPT = REPO_ROOT / "scripts" / "ci" / "reserve-shard-ports.sh"
_SHARD_START = re.compile(r"shardctl (up|test)\b(?!-)")


def _starts_a_shard(step):
    run = str(step.get("run", ""))
    return bool(_SHARD_START.search(run)) and "--collect-only" not in run


def test_every_shard_job_reserves_ports_before_starting_one():
    offenders = []
    for name, job in yaml.safe_load(WORKFLOW.read_text())["jobs"].items():
        steps = job.get("steps", [])
        first_shard = next((i for i, s in enumerate(steps) if _starts_a_shard(s)), None)
        if first_shard is None:
            continue
        reserve = [i for i, s in enumerate(steps) if RESERVE_SCRIPT.name in str(s.get("run", ""))]
        if not reserve or reserve[0] > first_shard:
            offenders.append(name)
    assert not offenders, f"jobs start a shard without reserving its ports first: {offenders}"


def _published_host_ports(compose_text):
    """Host ports from every `ports:` mapping form compose accepts.

    Short syntax, quoted or not, with or without a bind address:
      - "40400:40400"  - 40400:40400  - "127.0.0.1:40400:40400"
    Long syntax: `published: 40400`.
    """
    short = re.compile(r"^\s*-\s*['\"]?(?:[\d.]+:)?(\d+):\d+(?:/\w+)?['\"]?\s*$", re.M)
    long = re.compile(r"^\s*published:\s*['\"]?(\d+)", re.M)
    return {int(m.group(1)) for m in short.finditer(compose_text)} | {
        int(m.group(1)) for m in long.finditer(compose_text)
    }


def _required_int(pattern, text, what):
    m = re.search(pattern, text, re.M)
    assert m, f"could not find {what} with {pattern!r}; update this invariant if it moved"
    return int(m.group(1))


def test_compose_port_parser_handles_every_mapping_form():
    sample = """
    ports:
      - "40400:40400"
      - 40401:40401
      - "127.0.0.1:40402:40402"
      - "40403:40403/udp"
      - target: 40404
        published: 40405
    """
    assert _published_host_ports(sample) == {40400, 40401, 40402, 40403, 40405}


def test_reserved_span_covers_compose_and_test_framework_ports():
    script = RESERVE_SCRIPT.read_text()
    default = r'RESERVED="\$\{SHARD_RESERVED_PORTS:-'
    lo = _required_int(default + r"(\d+)-\d+\}", script, "reserved span start")
    hi = _required_int(default + r"\d+-(\d+)\}", script, "reserved span end")

    host_ports = set()
    for compose in (REPO_ROOT / "compose").glob("f1r3node-rust*.yml"):
        host_ports |= _published_host_ports(compose.read_text())
    assert host_ports, "no published host ports found in compose/f1r3node-rust*.yml"

    ports_py = (REPO_ROOT / "integration-tests" / "test" / "infra" / "ports.py").read_text()
    const = r"(?:\s*:\s*int)?\s*=\s*(\d+)"
    base = _required_int(r"^_BASE" + const, ports_py, "PortAllocator _BASE")
    ceiling = _required_int(r"^_CEILING" + const, ports_py, "PortAllocator _CEILING")

    assert lo <= min(host_ports) and max(host_ports) <= hi, (lo, hi, sorted(host_ports))
    assert lo <= base and ceiling <= hi, (lo, hi, base, ceiling)


# ── Monitoring checks wait for readiness, not a fixed sleep ───────────────
#
# Grafana accepts connections before it can answer them; a fixed sleep after
# `shardctl up monitoring` let "Verify monitoring stack" hit that window
# (run 32572443142, curl exit 56). Every job that verifies monitoring must
# wait on the readiness script between bring-up and the first check.

WAIT_SCRIPT = REPO_ROOT / "scripts" / "ci" / "wait-for-monitoring.sh"


def test_every_monitoring_verification_waits_for_readiness():
    offenders = []
    for name, job in yaml.safe_load(WORKFLOW.read_text())["jobs"].items():
        steps = job.get("steps", [])
        runs = [str(s.get("run", "")) for s in steps]
        up = next((i for i, r in enumerate(runs) if "shardctl up monitoring" in r), None)
        if up is None:
            continue
        verify = next((i for i, r in enumerate(runs) if "localhost:3000" in r), None)
        waited = [i for i, r in enumerate(runs) if WAIT_SCRIPT.name in r]
        if verify is None or not waited or not (up <= waited[0] <= verify):
            offenders.append(name)
        if re.search(r"shardctl up monitoring\s*\n\s*sleep \d+", runs[up]):
            offenders.append(f"{name} (fixed sleep)")
    assert not offenders, f"monitoring verified without waiting for readiness: {offenders}"
