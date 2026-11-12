#!/usr/bin/env bash
set -euo pipefail

case "${OPERATION:-}" in
    promote) source_branch=dev; target_branch=main; prefix=promote/; title=Promote ;;
    backmerge) source_branch=main; target_branch=dev; prefix=backmerge/; title=Back-merge ;;
    *) echo "ERROR: OPERATION must be promote or backmerge." >&2; exit 2 ;;
esac

repo="${REPOSITORY:?REPOSITORY is required}"
actor="${TRIGGERED_BY:?TRIGGERED_BY is required}"
login="${actor%"[bot]"}"
if [[ ${#login} -gt 39 || ! "$actor" =~ ^[A-Za-z0-9](-?[A-Za-z0-9])*(\[bot\])?$ ]]; then
    echo "ERROR: TRIGGERED_BY must be a GitHub login." >&2
    exit 2
fi

git fetch --quiet --no-tags origin \
    "+refs/heads/$source_branch:refs/remotes/origin/$source_branch" \
    "+refs/heads/$target_branch:refs/remotes/origin/$target_branch"
sha="$(git rev-parse "origin/$source_branch")"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "ERROR: invalid source commit." >&2; exit 2; }

if git merge-base --is-ancestor "$sha" "origin/$target_branch"; then
    echo "Nothing to $OPERATION: $target_branch already contains $source_branch (${sha:0:12})."
    exit 0
else
    status=$?
    [ "$status" -eq 1 ] || exit "$status"
fi

open_filter="[.[] | select(.headRefName | startswith(\"$prefix\")) | \"#\\(.number)\"] | join(\" \")"
open_prs="$(env -u GITHUB_TOKEN gh pr list --repo "$repo" --base "$target_branch" \
    --state open --limit 1000 --json number,headRefName --jq "$open_filter")"
if [ -n "$open_prs" ]; then
    echo "A $OPERATION PR is already open: $open_prs. Merge or close it before running the workflow again."
    exit 0
fi

count="$(git rev-list --count "origin/$target_branch..$sha")"
branch="$prefix$source_branch-${sha:0:12}"
env -u GITHUB_TOKEN gh api "repos/$repo/git/refs" -f "ref=refs/heads/$branch" -f "sha=$sha" >/dev/null

body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT
cat >"$body_file" <<EOF
Merges \`$source_branch\` at \`$sha\` into \`$target_branch\` ($count commits).

The Promote and Back-merge workflow was started by @$actor. A different maintainer should approve this PR.

This PR uses the temporary branch \`$branch\`. Merge with a merge commit to preserve branch history. After CI and approval, tick Mergify's queue checkbox or comment \`@Mergifyio queue\`.
EOF

if ! env -u GITHUB_TOKEN gh pr create --repo "$repo" --base "$target_branch" --head "$branch" \
    --title "$title $source_branch into $target_branch (${sha:0:12})" --body-file "$body_file"; then
    echo "ERROR: PR creation failed. Branch $branch was retained; check for a PR before retrying or removing it." >&2
    exit 1
fi
