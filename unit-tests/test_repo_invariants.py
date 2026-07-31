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
