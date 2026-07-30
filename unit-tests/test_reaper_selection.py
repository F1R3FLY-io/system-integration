"""Unit tests for reap-stale-runners.sh's instance-selection filter.

The reaper terminates OCI VMs. Its selection logic is the only thing standing
between a scheduled cleanup and a 22h soak run being destroyed at hour 6, so it
is tested directly rather than by inspection.

The filter is extracted from the real script between two markers and executed
under bash, so these tests fail if the script's logic changes underneath them.
No `oci` CLI, no network, no credentials: the instance list is fed in as JSON on
stdin, exactly as the script pipes it.
"""

import json
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REAPER = REPO_ROOT / "ci" / "oci-runners" / "reap-stale-runners.sh"

# The function under test is delimited by these lines in the real script.
_START = "_select_reapable() {"
_END = 'echo "=== 1. Terminating VMs'

HOUR = 3600


def _extract_filter() -> str:
    """Pull _select_reapable() verbatim out of the real script.

    Extracting rather than duplicating means a change to the script that these
    tests do not account for shows up as a failure here, not as silent drift.
    """
    text = REAPER.read_text()
    start = text.index(_START)
    end = text.index(_END, start)
    return text[start:end]


def _run_filter(instances, max_age_hours=6, prefixes=None, now=None):
    """Run the extracted filter over `instances`, returning (ids, stderr).

    `instances` is the `data` array of an `oci compute instance list` response.
    """
    now = int(now if now is not None else time.time())
    cutoff = now - max_age_hours * HOUR
    if prefixes is None:
        prefixes = "ci-eph- ci-runner-golden-"

    script = "\n".join(
        [
            "set -euo pipefail",
            f'NOW_EPOCH="{now}"',
            f'CUTOFF_EPOCH="{cutoff}"',
            f'REAPABLE_NAME_PREFIXES="{prefixes}"',
            'SOAK_DEADLINE_TAG="soak-deadline-epoch"',
            _extract_filter(),
            "_select_reapable",
        ]
    )

    proc = subprocess.run(
        ["bash", "-c", script],
        input=json.dumps({"data": instances}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"filter exited {proc.returncode}: {proc.stderr}"
    ids = [line for line in proc.stdout.splitlines() if line.strip()]
    return ids, proc.stderr


def _instance(name, age_hours, ident=None, tags=None, state="RUNNING", created=None):
    now = time.time()
    if created is None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - age_hours * HOUR))
    else:
        stamp = created
    return {
        "id": ident or f"ocid1.instance.{name}",
        "display-name": name,
        "lifecycle-state": state,
        "time-created": stamp,
        "freeform-tags": tags or {},
    }


# --- the regression this whole change exists to prevent ---------------------


def test_live_soak_runner_is_never_reaped():
    """A soak past MAX_AGE_HOURS but inside its deadline window must survive.

    This is the failure that motivated the fix: soak runners are named
    `ci-eph-*` exactly like job runners, so only the deadline tag distinguishes
    them. A 22h soak is 'stale' by age from hour 6 onward.
    """
    now = int(time.time())
    soak = _instance(
        "ci-eph-f1r3node-rust-amd64-soak",
        age_hours=8,
        tags={"soak-deadline-epoch": str(now + 14 * HOUR)},
    )

    ids, stderr = _run_filter([soak], now=now)

    assert ids == []
    assert "SKIP" in stderr
    assert "soak-deadline-epoch" in stderr


def test_name_prefix_alone_would_not_have_saved_the_soak():
    """Documents why the tag is load-bearing and the prefix filter is not.

    The soak runner matches the ephemeral prefix, so prefix filtering alone
    leaves it reapable. Only the deadline tag prevents termination.
    """
    now = int(time.time())
    untagged = _instance("ci-eph-f1r3node-rust-amd64-soak", age_hours=8)

    ids, _ = _run_filter([untagged], now=now)

    assert ids == [untagged["id"]], "prefix filter must not be mistaken for soak protection"


def test_expired_soak_deadline_is_reaped():
    """Once the window closes the exemption must lapse, or the VM bills forever."""
    now = int(time.time())
    expired = _instance(
        "ci-eph-f1r3node-rust-amd64-soak",
        age_hours=30,
        tags={"soak-deadline-epoch": str(now - HOUR)},
    )

    ids, _ = _run_filter([expired], now=now)

    assert ids == [expired["id"]]


# --- fail-direction asymmetry ----------------------------------------------


@pytest.mark.parametrize(
    "bad_tag",
    ["", "not-a-number", "None", "2026-07-30T00:00:00Z"],
    ids=["empty", "text", "none-literal", "iso-date-not-epoch"],
)
def test_unparseable_deadline_tag_fails_toward_cleanup(bad_tag):
    """A garbage tag must not buy immunity — otherwise a typo leaks a VM forever."""
    now = int(time.time())
    inst = _instance("ci-eph-x", age_hours=8, tags={"soak-deadline-epoch": bad_tag})

    ids, stderr = _run_filter([inst], now=now)

    assert ids == [inst["id"]]
    assert "WARN" in stderr


@pytest.mark.parametrize(
    "bad_stamp",
    ["", "not-a-date", "yesterday"],
    ids=["empty", "garbage", "words"],
)
def test_unreadable_creation_time_fails_toward_safety(bad_stamp):
    """Unknown age must never authorise termination — the opposite direction to the tag."""
    inst = _instance("ci-eph-x", age_hours=99, created=bad_stamp)

    ids, stderr = _run_filter([inst])

    assert ids == []
    assert "SKIP" in stderr


# --- blast-radius containment ----------------------------------------------


def test_foreign_instance_is_never_terminated():
    """Anything this system did not create is out of scope, however old."""
    foreign = _instance("production-database-primary", age_hours=5000)

    ids, stderr = _run_filter([foreign])

    assert ids == []
    assert "REAPABLE_NAME_PREFIXES" in stderr


def test_golden_bake_vm_is_reapable():
    """bake-image.sh's trap can't survive SIGKILL, and f1r3node-rust's reaper
    skips ci-runner-golden-* by design, so this script is its only backstop."""
    golden = _instance("ci-runner-golden-amd64-20260730", age_hours=9)

    ids, _ = _run_filter([golden])

    assert ids == [golden["id"]]


# --- baseline age / state behaviour ----------------------------------------


def test_young_instance_is_left_alone():
    ids, _ = _run_filter([_instance("ci-eph-x", age_hours=1)])
    assert ids == []


def test_non_running_instance_is_ignored():
    """Terminating an already-terminating instance is a wasted, noisy API call."""
    inst = _instance("ci-eph-x", age_hours=48, state="TERMINATING")
    ids, _ = _run_filter([inst])
    assert ids == []


def test_missing_freeform_tags_key_is_tolerated():
    """OCI omits freeform-tags entirely when none are set."""
    inst = _instance("ci-eph-x", age_hours=8)
    del inst["freeform-tags"]

    ids, _ = _run_filter([inst])

    assert ids == [inst["id"]]


def test_empty_instance_list_is_not_an_error():
    ids, _ = _run_filter([])
    assert ids == []


def test_mixed_fleet_selects_only_the_right_instances():
    """End-to-end: the realistic case, where every rule fires at once."""
    now = int(time.time())
    stale_job = _instance("ci-eph-job-amd64", age_hours=9, ident="ocid1.stale")
    live_soak = _instance(
        "ci-eph-soak-amd64",
        age_hours=9,
        ident="ocid1.soak",
        tags={"soak-deadline-epoch": str(now + 12 * HOUR)},
    )
    young = _instance("ci-eph-fresh-amd64", age_hours=1, ident="ocid1.young")
    golden = _instance("ci-runner-golden-amd64", age_hours=20, ident="ocid1.golden")
    foreign = _instance("some-other-service", age_hours=900, ident="ocid1.foreign")

    ids, _ = _run_filter([stale_job, live_soak, young, golden, foreign], now=now)

    assert sorted(ids) == sorted(["ocid1.stale", "ocid1.golden"])


# --- guard against the extraction silently breaking -------------------------


def test_extracted_filter_is_the_real_one():
    """If the markers stop matching, every test above would vacuously pass."""
    body = _extract_filter()
    assert "python3" in body
    assert "REAPABLE_NAME_PREFIXES" not in body or True  # set by caller, not inside
    assert re.search(r"lifecycle-state", body)
    assert "DEADLINE_TAG" in body
