"""Exercise branch transitions without opening real GitHub PRs."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci/open-promotion-pr.sh"
WORKFLOWS = ROOT / ".github/workflows"
REPOSITORY = "example/harness"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def transition(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Example Maintainer")
    _git(repo, "config", "user.email", "maintainer@example.com")
    _git(repo, "commit", "--allow-empty", "-m", "Initial commit")
    initial = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "dev")
    origin = tmp_path / "origin.git"
    _git(repo, "clone", "--bare", str(repo), str(origin))
    _git(repo, "remote", "add", "origin", str(origin))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "entry = {'args': args, 'inherited_token': 'GITHUB_TOKEN' in os.environ}\n"
        "if '--body-file' in args:\n"
        "    entry['body'] = pathlib.Path(args[args.index('--body-file') + 1]).read_text()\n"
        "with open(os.environ['GH_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(entry) + '\\n')\n"
        "if args[:2] == ['pr', 'list']:\n"
        "    print(os.environ.get('OPEN_PRS', ''))\n"
        "    sys.exit(int(os.environ.get('LIST_STATUS', '0')))\n"
        "if args[0] == 'api':\n"
        "    sys.exit(int(os.environ.get('REF_STATUS', '0')))\n"
        "if args[:2] == ['pr', 'create']:\n"
        "    sys.exit(int(os.environ.get('CREATE_STATUS', '0')))\n"
        "sys.exit('Unexpected gh invocation')\n"
    )
    gh.chmod(0o755)
    log = tmp_path / "gh.jsonl"

    def advance(branch):
        _git(repo, "switch", branch)
        _git(repo, "commit", "--allow-empty", "-m", f"Advance {branch}")
        _git(repo, "push", "origin", branch)
        return _git(repo, "rev-parse", "HEAD")

    def run(operation="promote", **overrides):
        log.write_text("")
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GH_LOG": str(log),
            "GITHUB_TOKEN": "inherited-token-must-not-be-used",
            "GH_TOKEN": "promotion-app-token",
            "OPERATION": operation,
            "REPOSITORY": REPOSITORY,
            "TRIGGERED_BY": "example-maintainer",
            **overrides,
        }
        result = subprocess.run(
            ["bash", str(SCRIPT)], cwd=repo, env=env, text=True, capture_output=True
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result, calls

    return repo, initial, advance, run


@pytest.mark.parametrize(
    ("operation", "source", "target", "prefix"),
    [("promote", "dev", "main", "promote/"), ("backmerge", "main", "dev", "backmerge/")],
)
def test_transition_uses_temporary_branch_at_current_remote_source(
    transition, operation, source, target, prefix
):
    repo, initial, advance, run = transition
    sha = advance(source)
    # Run from an unrelated checkout: source selection comes from the operation.
    _git(repo, "switch", "--detach", initial)
    result, calls = run(operation)
    assert result.returncode == 0, result.stderr
    branch = f"{prefix}{source}-{sha[:12]}"
    listing, ref, pr = calls
    assert listing["args"][listing["args"].index("--base") + 1] == target
    assert ref["args"] == [
        "api",
        f"repos/{REPOSITORY}/git/refs",
        "-f",
        f"ref=refs/heads/{branch}",
        "-f",
        f"sha={sha}",
    ]
    assert pr["args"][pr["args"].index("--head") + 1] == branch
    assert pr["args"][pr["args"].index("--base") + 1] == target
    assert sha in pr["body"]
    assert "@example-maintainer" in pr["body"]
    assert "@Mergifyio queue" in pr["body"]
    assert "\n\n" in pr["body"] and "\\n" not in pr["body"]
    assert all(not call["inherited_token"] for call in calls)
    assert _git(repo, "rev-parse", "HEAD") == initial
    assert _git(repo, "rev-parse", target) == initial
    assert not Path(pr["args"][pr["args"].index("--body-file") + 1]).exists()


@pytest.mark.parametrize("operation", ["promote", "backmerge"])
def test_already_contained_source_needs_no_github_mutation(transition, operation):
    _, _, _, run = transition
    result, calls = run(operation)
    assert result.returncode == 0, result.stderr
    assert "already contains" in result.stdout
    assert calls == []


@pytest.mark.parametrize("operation,source", [("promote", "dev"), ("backmerge", "main")])
def test_existing_transition_pr_prevents_duplicate(transition, operation, source):
    _, _, advance, run = transition
    advance(source)
    result, calls = run(operation, OPEN_PRS="#42")
    assert result.returncode == 0, result.stderr
    assert "#42" in result.stdout
    assert len(calls) == 1 and calls[0]["args"][:2] == ["pr", "list"]


@pytest.mark.parametrize(
    "overrides,call_count",
    [({"LIST_STATUS": "1"}, 1), ({"REF_STATUS": "1"}, 2), ({"CREATE_STATUS": "1"}, 3)],
)
def test_github_failure_stops_without_merging_or_deleting(transition, overrides, call_count):
    _, _, advance, run = transition
    advance("dev")
    result, calls = run(**overrides)
    assert result.returncode != 0
    assert len(calls) == call_count
    assert not any("DELETE" in call["args"] or "merge" in call["args"] for call in calls)
    if "CREATE_STATUS" in overrides:
        assert "retained" in result.stderr


@pytest.mark.parametrize("operation,actor", [("other", "maintainer"), ("promote", "bad actor")])
def test_invalid_inputs_fail_before_any_github_call(transition, operation, actor):
    _, _, _, run = transition
    result, calls = run(operation, TRIGGERED_BY=actor)
    assert result.returncode == 2
    assert calls == []


def _workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


@pytest.mark.parametrize(
    "event,head,head_repo,accepted",
    [
        ("pull_request", "dev", REPOSITORY, False),
        ("pull_request", "main", REPOSITORY, False),
        ("pull_request", "promote/dev-123", REPOSITORY, True),
        ("pull_request", "backmerge/main-123", REPOSITORY, True),
        ("pull_request", "main", "example/fork", True),
        ("push", "dev", REPOSITORY, True),
    ],
)
def test_validate_job_rejects_protected_pr_heads(event, head, head_repo, accepted):
    steps = _workflow("smoke-test.yml")["jobs"]["validate"]["steps"]
    guard = next(step for step in steps if step.get("name") == "Validate promotion source")
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", guard["run"]],
        env={
            **os.environ,
            "EVENT_NAME": event,
            "HEAD_REF": head,
            "HEAD_REPO": head_repo,
            "REPOSITORY": REPOSITORY,
        },
        capture_output=True,
    )
    assert (result.returncode == 0) == accepted


def test_only_main_dispatch_can_reach_the_promotion_app_token():
    workflow = _workflow("promote-and-backmerge.yml")
    # PyYAML uses YAML 1.1, where the unquoted key 'on' parses as True.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["operation"]["options"] == [
        "promote",
        "backmerge",
    ]
    job = workflow["jobs"]["open"]
    assert job["environment"] == "release-credentials"
    steps = job["steps"]
    guard = steps[0]
    for ref, accepted in [("refs/heads/main", True), ("refs/heads/dev", False)]:
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", guard["run"]],
            env={**os.environ, "REF": ref},
            capture_output=True,
        )
        assert (result.returncode == 0) == accepted
    checkout = next(step for step in steps if step.get("name") == "Checkout")
    assert checkout["with"]["persist-credentials"] is False
    assert "ref" not in checkout["with"]
    token = next(step for step in steps if step.get("id") == "promotion-token")
    assert token["with"]["permission-contents"] == "write"
    assert token["with"]["permission-pull-requests"] == "write"
    assert steps[-1]["env"]["GH_TOKEN"] == "${{ steps.promotion-token.outputs.token }}"


def test_mergify_preserves_manual_requests_and_tests_the_updated_head():
    config = yaml.safe_load((ROOT / ".mergify.yml").read_text())
    assert config["merge_queue"] == {
        "mode": "serial",
        "max_parallel_checks": 1,
        "queue_controls_comment": True,
        "status_comments": "all",
    }
    assert not config.get("pull_request_rules")
    smoke = _workflow("smoke-test.yml")
    expected_checks = {f"check-success={job['name']}" for job in smoke["jobs"].values()}
    assert len(expected_checks) == 9
    queues = {rule["name"]: rule for rule in config["queue_rules"]}
    assert set(queues) == {"dev", "main"}
    for name, rule in queues.items():
        conditions = rule["queue_conditions"]
        assert f"base={name}" in conditions
        assert "#approved-reviews-by >= 1" in conditions
        assert "-draft" in conditions
        checks = {c for c in conditions if isinstance(c, str) and c.startswith("check-")}
        assert checks == expected_checks
        assert rule["merge_method"] == rule["update_method"] == "merge"
        assert rule["batch_size"] == 1
        assert rule["max_checks_retries"] == 0
        assert rule.get("merge_conditions", conditions) == conditions
    assert "head~=^(promote|hotfix)/" in queues["main"]["queue_conditions"]


def test_write_token_guard_never_checks_out_pr_code():
    job = _workflow("protected-head-guard.yml")["jobs"]["guard"]
    assert job["permissions"] == {"pull-requests": "write"}
    assert "github.event.pull_request.head.repo.full_name == github.repository" in job["if"]
    assert "github.head_ref == 'main'" in job["if"]
    assert "github.head_ref == 'dev'" in job["if"]
    assert all("uses" not in step for step in job["steps"])
