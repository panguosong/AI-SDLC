"""Integration tests for the ai-sdlc pr-review CLI."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from ai_sdlc.cli.loop_review_cmd import (
    resolve_review_input,
    validate_review_input_for_close,
)
from ai_sdlc.cli.main import app
from ai_sdlc.core import pr_review_service
from ai_sdlc.core.loop_review_models import LoopReviewOutcome
from ai_sdlc.core.pr_review_service import (
    PRReviewCommandStatus,
    commit_pr_review,
    verify_pr_review_command,
)

runner = CliRunner()


def _record_clean_local_review(root: Path, loop_id: str):
    review_input = resolve_review_input(
        root,
        loop_type="local-pr-review",
        loop_id=loop_id,
        review_round_number=1,
    )
    pointer = json.loads(
        (root / ".ai-sdlc" / "reviews" / "pr" / "current-review.json").read_text(
            encoding="utf-8"
        )
    )
    review_dir = root / ".ai-sdlc" / "reviews" / "pr" / pointer["review_id"]
    outcome = LoopReviewOutcome(
        loop_id=loop_id,
        loop_type="local-pr-review",
        round_number=1,
        input_digest=review_input.input_digest,
        status="completed",
        expert_roles=review_input.expert_roles,
        findings=[],
        recorded_at="2026-08-17T00:00:00Z",
    )
    (review_dir / "review-outcome-round-1.json").write_text(
        outcome.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return review_input


def _verify_review_and_commit(root: Path, loop_id: str):
    verified = verify_pr_review_command(
        root,
        cwd=".",
        argv=(sys.executable, "-c", "print('verified')"),
    )
    assert verified.status == PRReviewCommandStatus.READY
    review_input = _record_clean_local_review(root, loop_id)
    committed = commit_pr_review(root, message="deliver reviewed tree")
    assert committed.status == PRReviewCommandStatus.READY, committed.blocker
    return review_input


def _start_verified_local_review(root: Path, review_id: str):
    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=root):
        start = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--provider",
                "mock-reviewer",
                "--review-id",
                review_id,
                "--json",
            ],
        )
        verify = runner.invoke(
            app,
            [
                "pr-review",
                "verify",
                "--json",
                "--",
                sys.executable,
                "-c",
                "print('verified')",
            ],
        )

    assert start.exit_code == 0, start.output
    assert verify.exit_code == 0, verify.output
    payload = json.loads(start.output)
    review_input = _record_clean_local_review(root, payload["loop_id"])
    return payload, review_input


def test_pr_review_help_omits_removed_authority_commands() -> None:
    result = runner.invoke(app, ["pr-review", "--help"])

    assert result.exit_code == 0
    assert "attest" not in result.output
    assert "certificate" not in result.output


def test_pr_review_help_lists_p0_commands() -> None:
    result = runner.invoke(app, ["pr-review", "--help"])

    assert result.exit_code == 0
    assert "doctor" in result.output
    assert "start" in result.output
    assert "status" in result.output
    assert "fix" in result.output
    assert "rerun" in result.output
    assert "record-evidence" in result.output
    assert "verify" in result.output
    assert "commit" in result.output
    assert "close" in result.output
    assert "attest" not in result.output


@pytest.mark.parametrize("command", ["start", "rerun"])
@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "-inf"])
def test_pr_review_provider_timeout_rejects_invalid_without_artifacts(
    tmp_path: Path, command: str, timeout: str
) -> None:
    _init_repo(tmp_path)
    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(
            app,
            ["pr-review", command, "--provider-timeout-seconds", timeout, "--json"],
        )

    assert result.exit_code == 1, result.output
    assert "finite and positive" in json.loads(result.output)["blocker"]
    assert not (tmp_path / ".ai-sdlc" / "reviews").exists()


def test_pr_review_provider_timeout_limits_start_subprocess(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('reviewed')\n")
    _git(tmp_path, "add", "src/app.py")
    provider = f'"{sys.executable}" -c "import time; time.sleep(0.5)"'
    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--provider",
                "local-agent",
                "--current-model",
                "gpt-5",
                "--provider-command",
                provider,
                "--provider-timeout-seconds",
                "0.05",
                "--review-id",
                "review-cli-timeout",
                "--json",
            ],
        )
        def original_diagnostics():
            import tempfile

            from ai_sdlc.core.pr_review_models import ProviderRunnerInvocation
            from ai_sdlc.core.stable_file_read import read_stable_bytes

            # 失败时只收集本次调用的原件；诊断缺失或清理不完整不改变原拒绝断言。
            details = {"cli_output": start.output}
            try:
                payload = json.loads(start.output)
                review_dir = tmp_path / ".ai-sdlc/reviews/pr/review-cli-timeout"
                assert payload["review_id"] == "review-cli-timeout"
                assert (tmp_path / payload["review_dir"]).resolve() == review_dir.resolve()
                invocation_bytes = read_stable_bytes(
                    tmp_path, review_dir / "reviewer-invocation.json"
                )
                details["invocation"] = json.loads(invocation_bytes)
                details["invocation_sha256"] = hashlib.sha256(invocation_bytes).hexdigest()
                invocation = ProviderRunnerInvocation.model_validate_json(invocation_bytes)
                assert Path(invocation.cwd).resolve() == tmp_path.resolve()
                assert (tmp_path / invocation.input_path).resolve() == (tmp_path / payload["review_pack_path"]).resolve()
                proof = invocation.completion_proof
                assert proof is not None
                _, raw, cleanup = proof.verified_receipts()
                details["verified_raw"] = raw
                details["verified_cleanup"] = cleanup
                marker = "Provider originals retained at "
                if marker in payload["blocker"]:
                    retained = Path(payload["blocker"].rsplit(marker, 1)[1])
                    assert retained.is_absolute() and not retained.is_symlink()
                    assert retained.parent.resolve() == Path(tempfile.gettempdir()).resolve()
                    assert retained.name.startswith("ai-sdlc-provider-owned-")
                    actual_cleanup = read_stable_bytes(retained, retained / "cleanup.json")
                    assert actual_cleanup.decode("utf-8") == proof.originals["cleanup.json"]
                    assert json.loads(actual_cleanup)["ownership_nonce"] == proof.ownership_nonce
                    details["retained_directory"] = str(retained)
                    files = details["retained_files"] = {}
                    for name in ("cleanup.json", "process.json", "raw-result.json", "stderr"):
                        try:
                            content = read_stable_bytes(retained, retained / name)
                            digest = hashlib.sha256(content).hexdigest()
                            expected = raw["stderr_sha256"] if name == "stderr" else proof.sha256.get(name)
                            files[name] = {"sha256": digest, "expected_sha256": expected,
                                           "matches_bound_original": digest == expected,
                                           "content": content.decode("utf-8", errors="replace")}
                        except (OSError, ValueError) as exc:
                            files[name] = {"read_error": f"{type(exc).__name__}: {exc}"}
            except (AssertionError, KeyError, OSError, TypeError, ValueError) as exc:
                details["diagnostic_error"] = f"{type(exc).__name__}: {exc}"
            return json.dumps(details, ensure_ascii=False, indent=2)

        assert start.exit_code == 1, original_diagnostics()
        assert "timed out" in json.loads(start.output)["blocker"], original_diagnostics()


def test_pr_review_provider_timeout_reaches_rerun_service(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('reviewed')\n")
    _git(tmp_path, "add", "src/app.py")
    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            ["pr-review", "start", "--provider", "mock-reviewer", "--json"],
        )
        assert start.exit_code == 0, start.output
        with patch(
            "ai_sdlc.cli.pr_review_cmd.rerun_pr_review",
            wraps=pr_review_service.rerun_pr_review,
        ) as service:
            result = runner.invoke(
                app,
                ["pr-review", "rerun", "--provider-timeout-seconds", "200.5", "--json"],
            )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "started"
    assert service.call_args.kwargs["provider_timeout_seconds"] == 200.5


def test_local_staged_verify_commit_and_close_exact_tree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("print('reviewed')\n", encoding="utf-8")
    _git(tmp_path, "add", "src/app.py")

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--provider",
                "mock-reviewer",
                "--review-id",
                "review-exact-delivery",
                "--json",
            ],
        )
        verify = runner.invoke(
            app,
            [
                "pr-review",
                "verify",
                "--json",
                "--",
                sys.executable,
                "-c",
                "print('verified')",
            ],
        )

    assert start.exit_code == 0, start.output
    assert json.loads(start.output)["diff_source"]["source_kind"] == "local-staged"
    assert verify.exit_code == 0, verify.output
    review_input = _record_clean_local_review(
        tmp_path,
        json.loads(start.output)["loop_id"],
    )

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        commit = runner.invoke(
            app,
            [
                "pr-review",
                "commit",
                "--message",
                "deliver reviewed tree",
                "--json",
            ],
        )
        close = runner.invoke(
            app,
            [
                "pr-review",
                "close",
                "--review-id",
                "review-exact-delivery",
                "--loop-id",
                json.loads(start.output)["loop_id"],
                "--expect-review-digest",
                review_input.input_digest,
                "--json",
            ],
        )

    assert commit.exit_code == 0, commit.output
    commit_payload = json.loads(commit.output)
    pack = json.loads(
        (
            tmp_path / ".ai-sdlc/reviews/pr/review-exact-delivery/review-pack.json"
        ).read_text(encoding="utf-8")
    )
    assert commit_payload["tree_oid"] == pack["staged_tree_oid"]
    assert _git(tmp_path, "rev-parse", "HEAD^{tree}") == pack["staged_tree_oid"]
    assert close.exit_code == 0, close.output
    assert json.loads(close.output)["status"] == "closed"


def test_local_staged_manual_exact_commit_can_close(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('reviewed')\n")
    _git(tmp_path, "add", "src/app.py")
    started, review_input = _start_verified_local_review(
        tmp_path,
        "review-manual-exact-delivery",
    )

    _git(tmp_path, "commit", "-m", "manual exact delivery")
    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        close = runner.invoke(
            app,
            [
                "pr-review",
                "close",
                "--review-id",
                started["review_id"],
                "--loop-id",
                started["loop_id"],
                "--expect-review-digest",
                review_input.input_digest,
                "--json",
            ],
        )

    assert close.exit_code == 0, close.output
    assert json.loads(close.output)["status"] == "closed"


def test_local_staged_close_blocks_commit_with_extra_tree_change(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('reviewed')\n")
    _git(tmp_path, "add", "src/app.py")
    started, review_input = _start_verified_local_review(
        tmp_path,
        "review-extra-tree-delivery",
    )
    _write_file(tmp_path, "src/extra.py", "print('unreviewed')\n")
    _git(tmp_path, "add", "src/extra.py")
    _git(tmp_path, "commit", "-m", "delivery with extra change")

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        close = runner.invoke(
            app,
            [
                "pr-review",
                "close",
                "--review-id",
                started["review_id"],
                "--loop-id",
                started["loop_id"],
                "--expect-review-digest",
                review_input.input_digest,
                "--json",
            ],
        )

    payload = json.loads(close.output)
    assert close.exit_code == 1
    assert payload["status"] == "blocked"
    assert "tree does not match" in payload["blocker"]


def test_local_staged_close_blocks_commit_with_wrong_parent(tmp_path: Path) -> None:
    reviewed_parent = _init_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('reviewed')\n")
    _git(tmp_path, "add", "src/app.py")
    started, review_input = _start_verified_local_review(
        tmp_path,
        "review-wrong-parent-delivery",
    )
    base_tree = _git(tmp_path, "rev-parse", f"{reviewed_parent}^{{tree}}")
    intervening = _git_commit_tree(
        tmp_path,
        base_tree,
        parents=(reviewed_parent,),
        message="intervening commit",
    )
    _git(tmp_path, "update-ref", "HEAD", intervening, reviewed_parent)
    _git(tmp_path, "commit", "-m", "delivery after intervening commit")

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        close = runner.invoke(
            app,
            [
                "pr-review",
                "close",
                "--review-id",
                started["review_id"],
                "--loop-id",
                started["loop_id"],
                "--expect-review-digest",
                review_input.input_digest,
                "--json",
            ],
        )

    payload = json.loads(close.output)
    assert close.exit_code == 1
    assert payload["status"] == "blocked"
    assert "parent does not match" in payload["blocker"]


def test_local_staged_close_blocks_merge_commit(tmp_path: Path) -> None:
    reviewed_parent = _init_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('reviewed')\n")
    _git(tmp_path, "add", "src/app.py")
    started, review_input = _start_verified_local_review(
        tmp_path,
        "review-merge-delivery",
    )
    base_tree = _git(tmp_path, "rev-parse", f"{reviewed_parent}^{{tree}}")
    other_parent = _git_commit_tree(
        tmp_path,
        base_tree,
        parents=(reviewed_parent,),
        message="other parent",
    )
    reviewed_tree = _git(tmp_path, "write-tree")
    merge_commit = _git_commit_tree(
        tmp_path,
        reviewed_tree,
        parents=(reviewed_parent, other_parent),
        message="merge reviewed tree",
    )
    _git(tmp_path, "update-ref", "HEAD", merge_commit, reviewed_parent)

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        close = runner.invoke(
            app,
            [
                "pr-review",
                "close",
                "--review-id",
                started["review_id"],
                "--loop-id",
                started["loop_id"],
                "--expect-review-digest",
                review_input.input_digest,
                "--json",
            ],
        )

    payload = json.loads(close.output)
    assert close.exit_code == 1
    assert payload["status"] == "blocked"
    assert payload["blocker"] == "Delivered commit must have exactly one parent."


@pytest.mark.skipif(sys.platform == "win32", reason="requires executable Git hook")
def test_local_staged_commit_blocks_hook_modified_tree_without_rollback(
    tmp_path: Path,
) -> None:
    reviewed_parent = _init_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('reviewed')\n")
    _git(tmp_path, "add", "src/app.py")
    started, _review_input = _start_verified_local_review(
        tmp_path,
        "review-hook-modified-delivery",
    )
    hook_path = tmp_path / ".git" / "hooks" / "pre-commit"
    hook_path.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import subprocess\n"
        "Path('src/hook-added.py').write_text(\"print('hook')\\n\", encoding='utf-8')\n"
        "subprocess.run(['git', 'add', 'src/hook-added.py'], check=True)\n",
        encoding="utf-8",
    )
    hook_path.chmod(0o755)

    committed = commit_pr_review(tmp_path, message="hook modified delivery")

    assert committed.status == PRReviewCommandStatus.BLOCKED
    assert "tree does not match" in committed.blocker
    assert committed.commit == _git(tmp_path, "rev-parse", "HEAD")
    assert committed.commit != reviewed_parent
    assert _git(tmp_path, "show", f"{committed.commit}:src/hook-added.py") == (
        "print('hook')"
    )
    assert started["review_id"] == committed.review_id


def test_local_staged_commit_never_auto_adds_unreviewed_path(tmp_path: Path) -> None:
    reviewed_parent = _init_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('reviewed')\n")
    _git(tmp_path, "add", "src/app.py")
    _start_verified_local_review(tmp_path, "review-no-auto-add")
    _write_file(tmp_path, "src/unreviewed.py", "print('unreviewed')\n")

    committed = commit_pr_review(tmp_path, message="must not auto add")

    assert committed.status == PRReviewCommandStatus.BLOCKED
    assert "uncommitted changes" in committed.blocker
    assert _git(tmp_path, "rev-parse", "HEAD") == reviewed_parent
    status = _git(tmp_path, "status", "--short")
    assert "A  src/app.py" in status
    assert "?? src/unreviewed.py" in status


def test_local_staged_commit_blocks_missing_expert_outcome(tmp_path: Path) -> None:
    reviewed_parent = _init_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('reviewed')\n")
    _git(tmp_path, "add", "src/app.py")
    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--provider",
                "mock-reviewer",
                "--review-id",
                "review-missing-expert-outcome",
                "--json",
            ],
        )
        verify = runner.invoke(
            app,
            [
                "pr-review",
                "verify",
                "--json",
                "--",
                sys.executable,
                "-c",
                "print('verified')",
            ],
        )

    assert start.exit_code == 0, start.output
    assert verify.exit_code == 0, verify.output
    committed = commit_pr_review(tmp_path, message="missing expert outcome")

    assert committed.status == PRReviewCommandStatus.BLOCKED
    assert "expert review is not current and clean" in committed.blocker
    assert _git(tmp_path, "rev-parse", "HEAD") == reviewed_parent


def test_pr_review_start_dry_run_json_is_read_only(tmp_path: Path) -> None:
    base_commit = _init_repo(tmp_path)
    _commit_file(tmp_path, "src/app.py", "print('hello')\n", "add app")

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--diff-source",
                "local-git-range",
                "--base",
                base_commit,
                "--provider",
                "mock-reviewer",
                "--dry-run",
                "--review-id",
                "review-dry",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "dry_run"
    assert payload["provider_id"] == "mock-reviewer"
    assert payload["resolved_model"] == "mock-reviewer"
    assert payload["source_adapter"] == "local-git-range"
    assert payload["source_access_status"] == "resolved"
    assert payload["diff_source"]["source_kind"] == "local-git-range"
    assert not (tmp_path / ".ai-sdlc" / "reviews").exists()


def test_pr_review_start_uses_policy_default_provider_when_option_omitted(
    tmp_path: Path,
) -> None:
    base_commit = _init_repo(tmp_path)
    policy_path = tmp_path / ".ai-sdlc" / "project" / "config" / "loop-policy.yaml"
    policy_path.parent.mkdir(parents=True)
    policy_path.write_text("default_provider: mock-reviewer\n", encoding="utf-8")
    _commit_file(tmp_path, "src/app.py", "print('hello')\n", "add app")

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--diff-source",
                "local-git-range",
                "--base",
                base_commit,
                "--dry-run",
                "--review-id",
                "review-policy-default-cli",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "dry_run"
    assert payload["provider_id"] == "mock-reviewer"
    assert payload["resolved_model"] == "mock-reviewer"


def test_pr_review_start_mock_and_status_json(tmp_path: Path) -> None:
    base_commit = _init_repo(tmp_path)
    _commit_file(tmp_path, "src/app.py", "print('hello')\n", "add app")

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--diff-source",
                "local-git-range",
                "--base",
                base_commit,
                "--provider",
                "mock-reviewer",
                "--review-id",
                "review-cli",
                "--json",
            ],
        )
        status = runner.invoke(app, ["pr-review", "status", "--json"])

    assert start.exit_code == 0
    start_payload = json.loads(start.output)
    assert start_payload["status"] == "started"
    assert start_payload["verdict"] == "clean"
    assert Path(start_payload["review_pack_path"]).is_file()
    assert Path(start_payload["findings_path"]).is_file()

    assert status.exit_code == 0
    status_payload = json.loads(status.output)
    assert status_payload["status"] == "started"
    assert status_payload["review_id"] == "review-cli"
    assert status_payload["verdict"] == "clean"


def test_pr_review_start_without_base_uses_default_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path, branch="master")
    _commit_file(tmp_path, "src/app.py", "print('hello')\n", "add app")

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--diff-source",
                "local-git-range",
                "--provider",
                "mock-reviewer",
                "--review-id",
                "review-auto-base",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "started"
    pack = json.loads(Path(payload["review_pack_path"]).read_text(encoding="utf-8"))
    assert pack["base_ref"] == "master"
    assert pack["source_adapter"] == "local-git-range"


def test_pr_review_start_patch_source_missing_reports_source_blocker(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--diff-source",
                "patch",
                "--patch-file",
                "missing.patch",
                "--provider",
                "mock-reviewer",
                "--dry-run",
                "--review-id",
                "review-missing-patch-cli",
                "--json",
            ],
        )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "blocked"
    assert payload["source_adapter"] == "patch"
    assert payload["source_access_status"] == "blocked"
    assert "missing.patch" in payload["blocker"]


def test_pr_review_start_patch_source_runs_mock_reviewer(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('from patch')\n")
    (tmp_path / "change.patch").write_text(
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -0,0 +1 @@\n"
        "+print('from patch')\n",
        encoding="utf-8",
    )

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        result = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--diff-source",
                "patch",
                "--patch-file",
                "change.patch",
                "--provider",
                "mock-reviewer",
                "--review-id",
                "review-patch-cli",
                "--json",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "started"
    assert payload["source_adapter"] == "patch"
    pack = json.loads(Path(payload["review_pack_path"]).read_text(encoding="utf-8"))
    assert pack["diff_source"]["source_kind"] == "patch"
    assert pack["changed_files"] == ["src/app.py"]


def test_pr_review_doctor_json_reports_missing_project() -> None:
    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=None):
        result = runner.invoke(app, ["pr-review", "doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "blocked"
    assert ".ai-sdlc is missing" in payload["blocker"]
    assert "ai-sdlc init" in payload["next_action"]


def test_pr_review_fix_and_close_require_no_blockers_json(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('hello')\n")
    _git(tmp_path, "add", "src/app.py")

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--provider",
                "mock-reviewer",
                "--mock-fixture",
                "changes_required",
                "--review-id",
                "review-fix-cli",
                "--json",
            ],
        )
        fix = runner.invoke(app, ["pr-review", "fix", "--json"])
        started = json.loads(start.output)
        reviewed = _verify_review_and_commit(tmp_path, started["loop_id"])
        close = runner.invoke(
            app,
            [
                "pr-review",
                "close",
                "--review-id",
                started["review_id"],
                "--loop-id",
                started["loop_id"],
                "--expect-review-digest",
                reviewed.input_digest,
                "--require-no-blockers",
                "--json",
            ],
        )

    assert start.exit_code == 10
    fix_payload = json.loads(fix.output)
    assert fix.exit_code == 0
    assert fix_payload["status"] == "ready"
    assert Path(fix_payload["fix_plan_path"]).is_file()
    assert Path(fix_payload["resolution_path"]).is_file()

    close_payload = json.loads(close.output)
    assert close.exit_code == 0
    assert close_payload["status"] == "closed"
    assert close_payload["verdict"] == "risk_accepted"
    assert Path(close_payload["final_report_path"]).is_file()


def test_pr_review_close_revalidates_resolution_at_transition(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('hello')\n")
    _git(tmp_path, "add", "src/app.py")

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--provider",
                "mock-reviewer",
                "--mock-fixture",
                "changes_required",
                "--review-id",
                "review-resolution-transition-cli",
                "--json",
            ],
        )
        fix = runner.invoke(app, ["pr-review", "fix", "--json"])
        started = json.loads(start.output)
        fixed = json.loads(fix.output)
        reviewed = _verify_review_and_commit(tmp_path, started["loop_id"])
        resolution_path = Path(fixed["resolution_path"])
        validation_count = 0

        def validate_then_replace_resolution(*args, **kwargs):
            nonlocal validation_count
            validation_count += 1
            result = validate_review_input_for_close(*args, **kwargs)
            if validation_count == 1:
                resolution = yaml.safe_load(resolution_path.read_text(encoding="utf-8"))
                resolution["finding_resolutions"][0].update(
                    {
                        "status": "waived",
                        "reason": "Concurrent unreviewed waiver.",
                        "operator": "other-process",
                        "resolved_at": "2026-08-16T00:00:00Z",
                    }
                )
                resolution_path.write_text(
                    yaml.safe_dump(resolution),
                    encoding="utf-8",
                )
            return result

        with patch(
            "ai_sdlc.cli.pr_review_cmd.validate_review_input_for_close",
            side_effect=validate_then_replace_resolution,
        ):
            close = runner.invoke(
                app,
                [
                    "pr-review",
                    "close",
                    "--review-id",
                    started["review_id"],
                    "--loop-id",
                    started["loop_id"],
                    "--expect-review-digest",
                    reviewed.input_digest,
                    "--json",
                ],
            )

    payload = json.loads(close.output)
    assert validation_count == 2
    assert close.exit_code == 1
    assert payload["status"] == "blocked"
    assert payload["reason"] == "review-input-drift"
    assert not (
        tmp_path
        / ".ai-sdlc"
        / "reviews"
        / "pr"
        / started["review_id"]
        / "final-report.md"
    ).exists()


@pytest.mark.parametrize("reviewed_resolution_exists", [False, True])
def test_pr_review_close_uses_validated_resolution_across_aba(
    tmp_path: Path,
    reviewed_resolution_exists: bool,
) -> None:
    _init_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('hello')\n")
    _git(tmp_path, "add", "src/app.py")

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--provider",
                "mock-reviewer",
                "--mock-fixture",
                "changes_required",
                "--review-id",
                "review-resolution-aba-cli",
                "--json",
            ],
        )
        fix = runner.invoke(app, ["pr-review", "fix", "--json"])
        started = json.loads(start.output)
        fixed = json.loads(fix.output)
        resolution_path = Path(fixed["resolution_path"])
        reviewed_bytes = resolution_path.read_bytes()
        if not reviewed_resolution_exists:
            resolution_path.unlink()
        reviewed = _verify_review_and_commit(tmp_path, started["loop_id"])
        validation_count = 0

        def validate_then_replace_resolution(*args, **kwargs):
            nonlocal validation_count
            validation_count += 1
            result = validate_review_input_for_close(*args, **kwargs)
            if validation_count == 1:
                resolution = yaml.safe_load(reviewed_bytes)
                resolution["finding_resolutions"][0].update(
                    {
                        "status": "fixed",
                        "evidence_refs": ["transient unreviewed evidence"],
                        "operator": "other-process",
                        "resolved_at": "2026-08-16T00:00:00Z",
                    }
                )
                resolution_path.write_bytes(yaml.safe_dump(resolution).encode("utf-8"))
            return result

        original_revalidate = pr_review_service.revalidate_review_input_at_transition

        def restore_then_revalidate(*args, **kwargs):
            if reviewed_resolution_exists:
                resolution_path.write_bytes(reviewed_bytes)
            else:
                resolution_path.unlink()
            return original_revalidate(*args, **kwargs)

        with (
            patch(
                "ai_sdlc.cli.pr_review_cmd.validate_review_input_for_close",
                side_effect=validate_then_replace_resolution,
            ),
            patch(
                "ai_sdlc.core.pr_review_service.revalidate_review_input_at_transition",
                side_effect=restore_then_revalidate,
            ),
        ):
            close = runner.invoke(
                app,
                [
                    "pr-review",
                    "close",
                    "--review-id",
                    started["review_id"],
                    "--loop-id",
                    started["loop_id"],
                    "--expect-review-digest",
                    reviewed.input_digest,
                    "--json",
                ],
            )

    payload = json.loads(close.output)
    assert validation_count == 2
    assert close.exit_code == 1
    assert payload["status"] == "blocked"
    assert payload["verdict"] == "blocked"
    assert payload["blocker"] == "Unresolved REQUIRED findings remain."
    assert resolution_path.exists() is reviewed_resolution_exists
    if reviewed_resolution_exists:
        resolution = yaml.safe_load(resolution_path.read_text(encoding="utf-8"))
        assert resolution["finding_resolutions"][0]["status"] == "unresolved"


@pytest.mark.parametrize("restore_originals", [True, False])
def test_pr_review_close_uses_all_validated_artifacts_across_aba(
    tmp_path: Path,
    restore_originals: bool,
) -> None:
    _init_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('hello')\n")
    _git(tmp_path, "add", "src/app.py")

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--provider",
                "mock-reviewer",
                "--mock-fixture",
                "changes_required",
                "--review-id",
                "review-artifact-aba-cli",
                "--json",
            ],
        )
        started = json.loads(start.output)
        review_run_path = Path(started["review_pack_path"]).with_name("review-run.json")
        findings_path = Path(started["findings_path"])
        reviewed = _verify_review_and_commit(tmp_path, started["loop_id"])
        reviewed_run = review_run_path.read_bytes()
        reviewed_findings = findings_path.read_bytes()
        validation_count = 0

        def validate_then_replace_outputs(*args, **kwargs):
            nonlocal validation_count
            validation_count += 1
            result = validate_review_input_for_close(*args, **kwargs)
            if validation_count == 1:
                findings = json.loads(reviewed_findings)
                findings["verdict"] = "clean"
                findings["findings"] = []
                transient_findings = json.dumps(findings).encode("utf-8")
                review_run = json.loads(reviewed_run)
                review_run["findings_digest"] = hashlib.sha256(
                    transient_findings
                ).hexdigest()
                findings_path.write_bytes(transient_findings)
                review_run_path.write_text(json.dumps(review_run), encoding="utf-8")
            return result

        original_revalidate = pr_review_service.revalidate_review_input_at_transition

        def restore_then_revalidate(*args, **kwargs):
            if restore_originals:
                review_run_path.write_bytes(reviewed_run)
                findings_path.write_bytes(reviewed_findings)
            return original_revalidate(*args, **kwargs)

        with (
            patch(
                "ai_sdlc.cli.pr_review_cmd.validate_review_input_for_close",
                side_effect=validate_then_replace_outputs,
            ),
            patch(
                "ai_sdlc.core.pr_review_service.revalidate_review_input_at_transition",
                side_effect=restore_then_revalidate,
            ),
        ):
            close = runner.invoke(
                app,
                [
                    "pr-review",
                    "close",
                    "--review-id",
                    started["review_id"],
                    "--loop-id",
                    started["loop_id"],
                    "--expect-review-digest",
                    reviewed.input_digest,
                    "--json",
                ],
            )

    payload = json.loads(close.output)
    assert validation_count == 2
    assert close.exit_code == 1
    assert payload["status"] == "blocked"
    if restore_originals:
        assert payload["verdict"] == "blocked"
        assert payload["blocker"] == "Unresolved REQUIRED findings remain."
        assert findings_path.read_bytes() == reviewed_findings
    else:
        assert payload["reason"] == "review-input-drift"
        assert not review_run_path.with_name("final-report.md").exists()
        assert json.loads(findings_path.read_bytes())["findings"] == []


def test_pr_review_fix_dry_run_json_does_not_write_artifacts(tmp_path: Path) -> None:
    base_commit = _init_repo(tmp_path)
    _commit_file(tmp_path, "src/app.py", "print('hello')\n", "add app")

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--diff-source",
                "local-git-range",
                "--base",
                base_commit,
                "--provider",
                "mock-reviewer",
                "--mock-fixture",
                "changes_required",
                "--review-id",
                "review-fix-dry-run-cli",
                "--json",
            ],
        )
        fix = runner.invoke(app, ["pr-review", "fix", "--dry-run", "--json"])

    assert start.exit_code == 10
    payload = json.loads(fix.output)
    assert fix.exit_code == 0
    assert payload["status"] == "ready"
    assert payload["dry_run"] is True
    assert payload["selected_findings_count"] == 1
    assert not Path(payload["fix_plan_path"]).exists()
    assert not Path(payload["resolution_path"]).exists()


def test_pr_review_rerun_json_regenerates_current_review(tmp_path: Path) -> None:
    base_commit = _init_repo(tmp_path)
    _commit_file(tmp_path, "src/app.py", "print('hello')\n", "add app")

    with patch("ai_sdlc.cli.pr_review_cmd.find_project_root", return_value=tmp_path):
        start = runner.invoke(
            app,
            [
                "pr-review",
                "start",
                "--diff-source",
                "local-git-range",
                "--base",
                base_commit,
                "--provider",
                "mock-reviewer",
                "--review-id",
                "review-rerun-cli",
                "--json",
            ],
        )
        start_payload = json.loads(start.output)
        old_head = json.loads(
            Path(start_payload["review_pack_path"]).read_text(encoding="utf-8")
        )["head_commit"]
        _commit_file(tmp_path, "src/app.py", "print('updated')\n", "update app")
        rerun = runner.invoke(app, ["pr-review", "rerun", "--json"])

    assert rerun.exit_code == 0
    rerun_payload = json.loads(rerun.output)
    new_head = json.loads(
        Path(rerun_payload["review_pack_path"]).read_text(encoding="utf-8")
    )["head_commit"]
    assert rerun_payload["status"] == "started"
    assert new_head != old_head


def test_python_module_help_fallback_lists_pr_review() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ai_sdlc", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0
    assert "pr-review" in result.stdout


def _init_repo(path: Path, *, branch: str = "main") -> str:
    (path / ".ai-sdlc").mkdir()
    _git(path, "init", f"--initial-branch={branch}")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    _commit_file(path, "README.md", "# Test\n", "initial")
    return _git(path, "rev-parse", "HEAD")


def _commit_file(path: Path, file_path: str, content: str, message: str) -> None:
    _write_file(path, file_path, content)
    _git(path, "add", file_path)
    _git(path, "commit", "-m", message)


def _write_file(path: Path, file_path: str, content: str) -> None:
    target = path / file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _git_commit_tree(
    path: Path,
    tree: str,
    *,
    parents: tuple[str, ...],
    message: str,
) -> str:
    command = ["git", "commit-tree", tree]
    for parent in parents:
        command.extend(["-p", parent])
    result = subprocess.run(
        command,
        cwd=path,
        input=message + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git commit-tree failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()
