#!/usr/bin/env bash
# Safety net for ephemeral runners that didn't self-terminate.
#
# Ephemeral runners are supposed to self-terminate after their job (and, with
# the idle watchdog in cloud-init-runner.yml.tmpl, after a no-job timeout). This
# reaper catches anything that slips through: a cloud-init failure, a hung job,
# or a watchdog that never ran. Run it on a schedule (see reap-runners.yml).
#
# Age alone is NOT a safe liveness proxy. That assumption held while the only
# workload was the ~45-min pipeline, but the merge-recovery soak in f1r3node-rust
# runs 22h (daily) and 60h (weekend) on a runner this repo's launch-runner.sh
# creates. A bare age filter would kill a daily soak at hour 6, hours before its
# first checkpoint. Two guards therefore gate every termination:
#
#   * display-name must match REAPABLE_NAME_PREFIXES, so this can only ever
#     terminate VMs this system created; and
#   * a `soak-deadline-epoch` freeform tag in the future exempts the instance.
#     f1r3node-rust stamps this on soak runners (window end + grace).
#
# The name filter is blast-radius containment, NOT soak protection: soak runners
# are named `ci-eph-*` by launch-runner.sh, exactly like job runners, so the tag
# is the only thing distinguishing them. Do not drop the tag check on the theory
# that the prefix filter covers it.
#
#   1. OCI : terminate matching compartment instances older than MAX_AGE_HOURS.
#   2. GitHub: deregister offline ci-eph-* runners (dead VMs). Their VMs have
#      already been terminated by step 1 here or on a prior run, so they show
#      offline; this supersedes the old cleanup-orphan-runners.sh.
#
# Requires: oci CLI authenticated, gh authenticated (GH_TOKEN with repo-admin),
# and state.env alongside this script.

set -euo pipefail

MAX_AGE_HOURS="${MAX_AGE_HOURS:-6}"
# Validated before use: it is fed to $(( )) below, and bash evaluates variable
# contents there as an arithmetic expression, so an unchecked value is both an
# injection surface and a confusing crash on ordinary typos.
if ! [[ "$MAX_AGE_HOURS" =~ ^[0-9]+$ ]]; then
  echo "MAX_AGE_HOURS must be a non-negative integer, got: '$MAX_AGE_HOURS'" >&2
  exit 2
fi

# Space-separated display-name prefixes this reaper may terminate. `ci-eph-` is
# launch-runner.sh's job/soak runners; `ci-runner-golden-` is bake-image.sh's
# image-bake VM, which f1r3node-rust's reaper skips by design (it scopes to
# ci-eph-*), leaving this script as the only backstop for a bake that died
# before its own trap could fire.
#
# `:-` only substitutes when unset or empty, so a whitespace-only value such as
# REAPABLE_NAME_PREFIXES=' ' survives it and parses to zero prefixes. That must
# abort rather than match everything: an operator who mis-set this believes the
# name filter is protecting them, which is strictly more dangerous than the old
# reap-by-age-alone behaviour. Fail closed here; the deadline tag fails open, on
# purpose (see _select_reapable).
REAPABLE_NAME_PREFIXES="${REAPABLE_NAME_PREFIXES:-ci-eph- ci-runner-golden-}"
if [ -z "${REAPABLE_NAME_PREFIXES//[[:space:]]/}" ]; then
  echo "REAPABLE_NAME_PREFIXES parsed to no entries; refusing to run." >&2
  echo "Set it to a space-separated prefix list, or unset it for the default." >&2
  exit 2
fi

# Freeform tag exempting an instance until its deadline passes. OCI freeform
# tags are string->string, so the value is a decimal string holding Unix epoch
# **seconds** — matching what f1r3node-rust stamps on soak runners. Milliseconds
# would exempt effectively forever; a defined tag would never be seen here at
# all. Unparseable or absent => reapable.
SOAK_DEADLINE_TAG="${SOAK_DEADLINE_TAG:-soak-deadline-epoch}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/state.env"
export SUPPRESS_LABEL_WARNING=True

NOW_EPOCH="$(date -u +%s)"
CUTOFF_EPOCH=$(( NOW_EPOCH - MAX_AGE_HOURS * 3600 ))

# Reads `oci compute instance list` JSON on stdin, writes reapable instance ids
# on stdout and one human-readable line per exemption on stderr.
#
# The two failure directions are deliberately opposite:
#   * unparseable/absent deadline tag -> REAPABLE. A garbage tag must not buy
#     an unbounded exemption, or a typo becomes a permanent billing leak.
#   * unparseable/absent time-created -> SKIP. Unknown age must never authorise
#     a termination. Worst case we leak one VM, which the next run with a valid
#     timestamp collects; the opposite error destroys live work.
_select_reapable() {
  NOW_EPOCH="$NOW_EPOCH" CUTOFF_EPOCH="$CUTOFF_EPOCH" \
  PREFIXES="$REAPABLE_NAME_PREFIXES" DEADLINE_TAG="$SOAK_DEADLINE_TAG" \
  python3 -c '
import json, math, os, sys
from datetime import datetime, timezone

prefixes = tuple(p for p in os.environ.get("PREFIXES", "").split() if p)
deadline_tag = os.environ["DEADLINE_TAG"]
now = int(os.environ["NOW_EPOCH"])
cutoff = int(os.environ["CUTOFF_EPOCH"])

# Belt-and-braces: the caller already refuses to run with an empty prefix list.
# Repeated here because this function is extracted and executed directly by
# unit-tests/test_reaper_selection.py, where the caller guard is not present.
if not prefixes:
    sys.exit("refusing to select instances with an empty prefix list")

raw = sys.stdin.read().strip()
instances = (json.loads(raw).get("data") or []) if raw else []


def created_epoch(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


for inst in instances:
    name = inst.get("display-name") or "<unnamed>"
    if inst.get("lifecycle-state") != "RUNNING":
        continue
    if prefixes and not name.startswith(prefixes):
        print(f"  SKIP {name}: display-name outside REAPABLE_NAME_PREFIXES", file=sys.stderr)
        continue
    created = created_epoch(inst.get("time-created"))
    if created is None:
        print(f"  SKIP {name}: unreadable time-created, refusing to terminate", file=sys.stderr)
        continue
    if created >= cutoff:
        continue
    deadline = (inst.get("freeform-tags") or {}).get(deadline_tag)
    if deadline is not None:
        try:
            # float() accepts "Infinity", "inf" and overflowing exponents like
            # "1e309"; each compares greater than now and would grant permanent
            # immunity — the unbounded-exemption billing leak this branch exists
            # to prevent. Reject those explicitly rather than switching to
            # int(): f1r3node-rust parses the same tag with jq `tonumber`, which
            # accepts a fractional value, and a consumer stricter than the
            # producer would drop a *valid* exemption and kill a live soak.
            parsed_deadline = float(str(deadline).strip())
            if not math.isfinite(parsed_deadline):
                raise ValueError(f"non-finite deadline: {deadline!r}")
            if parsed_deadline > now:
                remaining = int((parsed_deadline - now) / 60)
                print(f"  SKIP {name}: {deadline_tag} {deadline} is {remaining}min in the future", file=sys.stderr)
                continue
        except (TypeError, ValueError):
            print(f"  WARN {name}: unparseable {deadline_tag}={deadline!r}; treating as reapable", file=sys.stderr)
    ident = inst.get("id")
    if ident:
        print(ident)
'
}

echo "=== 1. Terminating VMs >${MAX_AGE_HOURS}h old (prefixes: ${REAPABLE_NAME_PREFIXES}; ${SOAK_DEADLINE_TAG} exempts) ==="
IDS="$(oci compute instance list -c "$COMP" --all --output json 2>/dev/null \
  | _select_reapable || true)"

if [ -z "$IDS" ]; then
  echo "  No OCI instances older than ${MAX_AGE_HOURS}h."
else
  while IFS= read -r id; do
    [ -n "$id" ] || continue
    echo "  terminating $id"
    oci compute instance terminate --instance-id "$id" --force --preserve-boot-volume false >/dev/null 2>&1 &
  done <<< "$IDS"
  wait
  echo "  Terminate calls submitted."
fi

echo "=== 2. Deregistering offline ci-eph-* GitHub runners ==="
ORPHANS="$(gh api "repos/$GH_REPO/actions/runners" --paginate \
  --jq '.runners[] | select(.status == "offline" and (.name | startswith("ci-eph"))) | "\(.id) \(.name)"' \
  2>/dev/null || true)"

if [ -z "$ORPHANS" ]; then
  echo "  No offline ci-eph runners."
else
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    id="${line%% *}"
    name="${line#* }"
    echo "  deregistering $name (id=$id)"
    gh api --silent -X DELETE "repos/$GH_REPO/actions/runners/$id" || echo "    (delete failed)"
  done <<< "$ORPHANS"
fi

echo "Reap complete."
