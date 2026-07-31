"""The RUNNER_LABELS override in launch-runner.sh.

A workflow that needs an exclusive runner has to register the VM under a label
nothing else requests. The alternative — register as shared CI capacity, then
relabel — leaves a window in which any queued job matching the shared label can
claim the VM. The merge-recovery soak lost run 30606130771 to exactly that.

These tests pin the override and, more importantly, the two ways it can fail
open: a blank value producing a runner with no labels, and an override that
drops a label every `runs-on` requires.
"""

import shlex
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "ci" / "oci-runners" / "launch-runner.sh"

_START = 'DEFAULT_LABELS="self-hosted,linux,'
_END = "# Render cloud-init from template"


def _extract_label_block() -> str:
    """Pull the label-resolution block out of the real launcher.

    Extracted rather than duplicated so the tests fail when the script changes,
    which is the only way they stay honest.
    """
    text = LAUNCHER.read_text()
    start = text.index(_START)
    end = text.index(_END, start)
    return text[start:end]


def _resolve_labels(runner_labels=None, label_arch="x64"):
    """Run the real block and report what it decided.

    Returns (exit_code, resolved_labels_or_None, combined_output).

    `unset RUNNER_LABELS` is unconditional: subprocess inherits the parent
    environment, so a developer or CI shell that exports RUNNER_LABELS would
    otherwise silently turn the default-path tests into override-path tests and
    they would still pass, for the wrong reason.
    """
    body = _extract_label_block()
    lines = [
        "set -euo pipefail",
        "unset RUNNER_LABELS",
        f"LABEL_ARCH={shlex.quote(label_arch)}",
    ]
    if runner_labels is not None:
        lines.append(f"export RUNNER_LABELS={shlex.quote(runner_labels)}")
    lines.append(body)
    lines.append('echo "RESOLVED:$LABELS"')

    proc = subprocess.run(
        ["bash", "-c", "\n".join(lines)], capture_output=True, text=True, timeout=30
    )
    out = proc.stdout + proc.stderr
    resolved = None
    for line in proc.stdout.splitlines():
        if line.startswith("RESOLVED:"):
            resolved = line[len("RESOLVED:") :]
    return proc.returncode, resolved, out


def test_unset_override_keeps_the_shared_ci_default():
    """The common path must not change: ordinary CI still gets shared capacity."""
    rc, labels, out = _resolve_labels(runner_labels=None)

    assert rc == 0, out
    assert labels == "self-hosted,linux,x64,f1r3fly-rust-ci-ephemeral,oracle-cloud"


def test_arch_flows_into_the_default():
    rc, labels, out = _resolve_labels(runner_labels=None, label_arch="arm64")

    assert rc == 0, out
    assert labels == "self-hosted,linux,arm64,f1r3fly-rust-ci-ephemeral,oracle-cloud"


def test_override_replaces_the_whole_set():
    """The point of the change: launch with the exclusive label, never the shared one."""
    exclusive = "self-hosted,linux,x64,f1r3fly-rust-soak,oracle-cloud"
    rc, labels, out = _resolve_labels(runner_labels=exclusive)

    assert rc == 0, out
    assert labels == exclusive


def test_override_does_not_carry_the_shared_label():
    """The race this closes: if the shared label survives, the window is still open."""
    exclusive = "self-hosted,linux,x64,f1r3fly-rust-soak,oracle-cloud"
    rc, labels, _ = _resolve_labels(runner_labels=exclusive)

    assert rc == 0
    assert "f1r3fly-rust-ci-ephemeral" not in labels


@pytest.mark.parametrize(
    "blank",
    ["", " ", "   ", "\t", "\n", " \t\n "],
    ids=["empty", "one-space", "spaces", "tab", "newline", "mixed"],
)
def test_blank_override_is_refused_not_silently_defaulted(blank):
    """`${VAR:-default}` does NOT substitute a whitespace-only value.

    Written as `LABELS="${RUNNER_LABELS:-$DEFAULT}"` this would hand cloud-init
    an empty label set for every case but the first. GitHub accepts the
    registration, so the failure is silent: a running, billed VM that no
    `runs-on` can ever match. Same fail-open shape as the whitespace-only
    REAPABLE_NAME_PREFIXES bug in reap-stale-runners.sh.
    """
    rc, _, out = _resolve_labels(runner_labels=blank)

    assert rc != 0, f"blank RUNNER_LABELS ({blank!r}) was accepted; it must abort"
    assert "RUNNER_LABELS is set but empty" in out


@pytest.mark.parametrize(
    "labels,missing",
    [
        ("linux,x64,f1r3fly-rust-soak,oracle-cloud", "self-hosted"),
        ("self-hosted,x64,f1r3fly-rust-soak,oracle-cloud", "linux"),
        ("self-hosted,linux,f1r3fly-rust-soak,oracle-cloud", "x64"),
        ("self-hosted,linux,arm64,f1r3fly-rust-soak,oracle-cloud", "x64"),
        ("self-hosted,linux,x64,f1r3fly-rust-soak", "oracle-cloud"),
        ("f1r3fly-rust-soak", "self-hosted"),
    ],
    ids=["no-self-hosted", "no-linux", "no-arch", "wrong-arch", "no-cloud", "bare-label"],
)
def test_override_missing_a_required_label_is_refused(labels, missing):
    """A complete-set override can omit a label every `runs-on` needs.

    That produces a healthy VM idling next to a job that will never route to it —
    a failure that shows up as a 15-minute timeout with no error anywhere.

    The `no-cloud` case is not hypothetical. An earlier draft of this change
    documented an example without `oracle-cloud`, which every `runs-on` in
    f1r3node-rust asks for; claude-session-9f68c6fa caught it before it shipped.
    Copying the example would have produced exactly the failure the validation
    exists to prevent, with the validation passing.
    """
    rc, _, out = _resolve_labels(runner_labels=labels)

    assert rc != 0, f"{labels!r} was accepted despite missing {missing!r}"
    assert missing in out


def test_every_default_label_but_the_pool_label_is_enforced():
    """The invariants are exactly the default set minus the pool label.

    If someone adds a label to the default and not to the required list, an
    override can drop it and route nothing — this is what catches that.
    """
    _, default, _ = _resolve_labels(runner_labels=None)
    invariants = [lbl for lbl in default.split(",") if lbl != "f1r3fly-rust-ci-ephemeral"]

    for label in invariants:
        dropped = ",".join(x for x in default.split(",") if x != label)
        # Swap in an exclusive pool label so only the dropped invariant differs.
        candidate = dropped.replace("f1r3fly-rust-ci-ephemeral", "f1r3fly-rust-soak")
        rc, _, out = _resolve_labels(runner_labels=candidate)

        assert rc != 0, f"dropping invariant {label!r} was accepted: {candidate!r}"
        assert label in out


def test_substring_match_does_not_count_as_the_required_label():
    """Guard the guard: `self-hosted-x` must not satisfy `self-hosted`.

    The check is comma-delimited on both sides for this reason. A substring test
    would let a typo through and reopen the exact routing failure being fixed.

    Every OTHER required label is present, so the only thing that can trigger a
    rejection here is the delimiter check. An earlier version of this test also
    omitted `oracle-cloud`, which meant it passed whether or not the delimiter
    guard worked — the loop rejected the input on the missing cloud label first.
    The assertion is on the exact error text for the same reason: `"self-hosted"
    in out` was satisfied by the echoed `self-hosted-x` value itself. Reported by
    the xai reviewer on PR #75.
    """
    rc, _, out = _resolve_labels(
        runner_labels="self-hosted-x,linux,x64,f1r3fly-rust-soak,oracle-cloud"
    )

    assert rc != 0, "a substring of the required label was accepted"
    assert "missing the required label 'self-hosted'" in out, (
        f"rejected, but not for the delimiter reason under test:\n{out}"
    )


def test_override_keeping_the_shared_pool_label_is_refused():
    """Presence of the invariants is not exclusivity, and exclusivity is the point.

    An override that adds `f1r3fly-rust-soak` but keeps `f1r3fly-rust-ci-ephemeral`
    passes every required-label check and is still claimable by ordinary CI — the
    race reopened, while the config reads as though it were closed. Reported by
    the openai reviewer on PR #75, against a version that accepted exactly this.
    """
    both = "self-hosted,linux,x64,f1r3fly-rust-ci-ephemeral,f1r3fly-rust-soak,oracle-cloud"
    rc, _, out = _resolve_labels(runner_labels=both)

    assert rc != 0, "an override carrying the shared pool label was accepted"
    assert "shared pool label" in out


def test_override_with_no_pool_label_is_refused():
    """The other half: invariants only, matching any `runs-on` that names no pool."""
    rc, _, out = _resolve_labels(runner_labels="self-hosted,linux,x64,oracle-cloud")

    assert rc != 0, "an override with no pool label was accepted"
    assert "no pool label" in out


def test_the_shared_label_guard_follows_the_default():
    """The guard derives the shared label from DEFAULT_LABELS, not a literal.

    If the default pool is renamed and the check is hard-coded, it silently stops
    guarding anything. This asserts the two agree.
    """
    _, default, _ = _resolve_labels(runner_labels=None)
    invariants = {"self-hosted", "linux", "x64", "oracle-cloud"}
    pool = [lbl for lbl in default.split(",") if lbl not in invariants]

    assert len(pool) == 1, f"expected exactly one pool label in the default, got {pool}"

    keeps_shared = f"self-hosted,linux,x64,{pool[0]},f1r3fly-rust-soak,oracle-cloud"
    rc, _, out = _resolve_labels(runner_labels=keeps_shared)

    assert rc != 0
    assert pool[0] in out


def test_exclusive_override_still_passes():
    """The guards must not block the case they exist to enable."""
    rc, labels, out = _resolve_labels(
        runner_labels="self-hosted,linux,x64,f1r3fly-rust-soak,oracle-cloud"
    )

    assert rc == 0, out
    assert labels == "self-hosted,linux,x64,f1r3fly-rust-soak,oracle-cloud"


def test_extracted_block_is_the_real_one():
    """If the launcher stops containing this block, these tests are theatre."""
    body = _extract_label_block()

    assert "RUNNER_LABELS" in body
    assert "f1r3fly-rust-ci-ephemeral" in body
    assert LAUNCHER.exists()
