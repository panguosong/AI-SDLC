"""Tests for local PR review provider runners."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ai_sdlc.core import pr_review_provider
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.pr_review_models import (
    DiffSourceDescriptor,
    DiffSourceKind,
    ModelResolutionSource,
    ModelResolutionStatus,
    ProviderMode,
    ProviderRunnerInvocation,
    ReviewPack,
)
from ai_sdlc.core.pr_review_provider import (
    MockReviewerFixture,
    ProviderCommandOptions,
    ProviderRunStatus,
    run_mock_reviewer,
    run_provider_command,
)


def test_local_agent_without_configured_command_needs_user(tmp_path) -> None:
    review_pack_path = _write_review_pack(tmp_path)

    result = run_provider_command(
        ProviderCommandOptions(root=tmp_path, review_pack_path=review_pack_path)
    )

    assert result.status == ProviderRunStatus.NEEDS_USER
    assert "not configured" in result.blocker
    assert result.invocation_path == ""


def test_local_agent_configured_command_writes_findings_and_invocation(
    tmp_path,
) -> None:
    review_pack_path = _write_review_pack(
        tmp_path,
        model_selector="claude-sonnet-4",
        resolved_model="claude-sonnet-4",
        source=ModelResolutionSource.EXPLICIT_CLI,
    )
    script = _write_reviewer_script(tmp_path, exit_code=10)

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.CHANGES_REQUIRED
    assert result.exit_code == 10
    assert result.findings is not None
    assert result.findings.verdict == "changes_required"

    invocation_payload = json.loads(
        Path(result.invocation_path).read_text(encoding="utf-8")
    )
    assert invocation_payload["provider_id"] == "local-agent"
    assert invocation_payload["provider_mode"] == "local_agent"
    assert invocation_payload["model_selector"] == "claude-sonnet-4"
    assert invocation_payload["resolved_model"] == "claude-sonnet-4"
    assert invocation_payload["model_resolution_source"] == "explicit_cli"
    assert invocation_payload["code_egress"] is False
    assert invocation_payload["allowlist"] == ["src/app.py"]
    assert invocation_payload["isolation_status"] == "isolated_process"
    assert invocation_payload["exit_code"] == 10
    assert "execution_failure" not in invocation_payload


def test_local_agent_expands_diff_path_placeholder(tmp_path) -> None:
    review_pack_path = _write_review_pack(tmp_path)
    script = tmp_path / "diff_placeholder_reviewer.py"
    script.write_text(
        "\n".join(
            [
                "import json, pathlib, sys",
                "review_pack_path, output_path, diff_path = sys.argv[1:4]",
                "pack = json.load(open(review_pack_path, encoding='utf-8'))",
                "assert pathlib.Path(diff_path).is_file()",
                "assert pathlib.Path(diff_path).name == 'diff.patch'",
                "payload = {",
                "  'schema_version': '1',",
                "  'artifact_kind': 'review-findings',",
                "  'review_id': pack['review_id'],",
                "  'loop_id': pack['loop_id'],",
                "  'review_pack_path': review_pack_path,",
                "  'provider_id': 'local-agent',",
                "  'model_selector': 'current',",
                "  'resolved_model': 'gpt-5',",
                "  'verdict': 'clean',",
                "  'findings': []",
                "}",
                "json.dump(payload, open(output_path, 'w', encoding='utf-8'))",
            ]
        ),
        encoding="utf-8",
    )

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[
                sys.executable,
                str(script),
                "{review_pack}",
                "{findings}",
                "{diff_path}",
            ],
        )
    )

    invocation_payload = json.loads(
        Path(result.invocation_path).read_text(encoding="utf-8")
    )
    assert result.status == ProviderRunStatus.SUCCESS
    assert invocation_payload["argv"][-1].endswith("diff.patch")


def test_local_agent_blocks_when_reviewed_head_is_not_checked_out(tmp_path) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('reviewed')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "reviewed head")
    reviewed_head = _git(tmp_path, "rev-parse", "HEAD")
    _write_file(tmp_path, "src/app.py", "print('current')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "current head")
    review_pack_path = _write_review_pack(tmp_path, head_commit=reviewed_head)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "current worktree HEAD" in result.blocker
    invocation = ProviderRunnerInvocation.model_validate_json(Path(result.invocation_path).read_bytes())
    assert invocation.launch_status == "never_started"
    assert invocation.preflight_incomplete is True
    assert invocation.workspace_check.status == "unproven"
    assert invocation.workspace_check.review_pack_digest == hashlib.sha256(review_pack_path.read_bytes()).hexdigest()


def test_local_agent_blocks_preexisting_dirty_worktree(tmp_path) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('reviewed')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "reviewed head")
    review_pack_path = _write_review_pack(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('unreviewed')\n")
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "pre-existing unreviewed worktree changes" in result.blocker
    assert "src/app.py" in result.blocker
    invocation = ProviderRunnerInvocation.model_validate_json(Path(result.invocation_path).read_bytes())
    assert invocation.launch_status == "never_started"
    assert invocation.preflight_incomplete is True
    assert invocation.workspace_check.status == "unproven"
    assert invocation.workspace_check.review_pack_digest == hashlib.sha256(review_pack_path.read_bytes()).hexdigest()


def test_local_agent_allows_reviewed_local_staged_dirty_paths(tmp_path) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('base')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    _write_file(tmp_path, "src/app.py", "print('staged')\n")
    _git(tmp_path, "add", "src/app.py")
    reviewed_hash = hashlib.sha256(
        _git_raw(tmp_path, "diff", "--cached").encode("utf-8")
    ).hexdigest()
    review_pack_path = _write_review_pack(
        tmp_path,
        diff_source_kind=DiffSourceKind.LOCAL_STAGED,
        base_ref="HEAD",
        head_ref="INDEX",
        changed_files=["src/app.py"],
        reviewer_allowlist=["src/app.py"],
        patch_hash=reviewed_hash,
    )
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.SUCCESS
    assert Path(result.invocation_path).is_file()


def test_local_agent_blocks_unstaged_edit_on_reviewed_local_staged_path(
    tmp_path,
) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('base')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    _write_file(tmp_path, "src/app.py", "print('staged')\n")
    _git(tmp_path, "add", "src/app.py")
    reviewed_hash = hashlib.sha256(
        _git_raw(tmp_path, "diff", "--cached").encode("utf-8")
    ).hexdigest()
    review_pack_path = _write_review_pack(
        tmp_path,
        diff_source_kind=DiffSourceKind.LOCAL_STAGED,
        base_ref="HEAD",
        head_ref="INDEX",
        changed_files=["src/app.py"],
        reviewer_allowlist=["src/app.py"],
        patch_hash=reviewed_hash,
    )
    _write_file(tmp_path, "src/app.py", "print('unstaged')\n")
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "pre-existing unreviewed worktree changes" in result.blocker
    assert "src/app.py" in result.blocker
    invocation = ProviderRunnerInvocation.model_validate_json(Path(result.invocation_path).read_bytes())
    assert invocation.launch_status == "never_started"
    assert invocation.preflight_incomplete is True
    assert invocation.workspace_check.status == "unproven"
    assert invocation.workspace_check.review_pack_digest == hashlib.sha256(review_pack_path.read_bytes()).hexdigest()


def test_local_agent_allows_reviewed_patch_file_dirty_input(tmp_path) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('base')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    patch_text = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-print('base')\n"
        "+print('from patch')\n"
    )
    _write_file(tmp_path, "change.patch", patch_text)
    patch_hash = hashlib.sha256((tmp_path / "change.patch").read_bytes()).hexdigest()
    review_pack_path = _write_review_pack(
        tmp_path,
        diff_source_kind=DiffSourceKind.PATCH,
        base_ref="patch-file",
        head_ref="HEAD",
        changed_files=["src/app.py"],
        reviewer_allowlist=["src/app.py"],
        patch_file="change.patch",
        patch_hash=patch_hash,
    )
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.SUCCESS
    assert Path(result.invocation_path).is_file()


def test_local_agent_blocks_changed_patch_file_before_launch(tmp_path) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('base')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    patch_text = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-print('base')\n"
        "+print('from patch')\n"
    )
    _write_file(tmp_path, "change.patch", patch_text)
    patch_hash = hashlib.sha256((tmp_path / "change.patch").read_bytes()).hexdigest()
    review_pack_path = _write_review_pack(
        tmp_path,
        diff_source_kind=DiffSourceKind.PATCH,
        base_ref="patch-file",
        head_ref="HEAD",
        changed_files=["src/app.py"],
        reviewer_allowlist=["src/app.py"],
        patch_file="change.patch",
        patch_hash=patch_hash,
    )
    _write_file(tmp_path, "change.patch", patch_text + "+print('changed')\n")
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "patch file hash does not match" in result.blocker
    invocation = ProviderRunnerInvocation.model_validate_json(Path(result.invocation_path).read_bytes())
    assert invocation.launch_status == "never_started"
    assert invocation.preflight_incomplete is True
    assert invocation.workspace_check.status == "unproven"
    assert invocation.workspace_check.review_pack_digest == hashlib.sha256(review_pack_path.read_bytes()).hexdigest()


def test_local_agent_blocks_changed_local_staged_diff_before_launch(
    tmp_path,
) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('base')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    _write_file(tmp_path, "src/app.py", "print('reviewed staged')\n")
    _git(tmp_path, "add", "src/app.py")
    reviewed_hash = hashlib.sha256(
        _git_raw(tmp_path, "diff", "--cached").encode("utf-8")
    ).hexdigest()
    review_pack_path = _write_review_pack(
        tmp_path,
        diff_source_kind=DiffSourceKind.LOCAL_STAGED,
        base_ref="HEAD",
        head_ref="INDEX",
        changed_files=["src/app.py"],
        reviewer_allowlist=["src/app.py"],
        patch_hash=reviewed_hash,
    )
    _write_file(tmp_path, "src/app.py", "print('changed staged')\n")
    _git(tmp_path, "add", "src/app.py")
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "staged tree does not match" in result.blocker
    invocation = ProviderRunnerInvocation.model_validate_json(Path(result.invocation_path).read_bytes())
    assert invocation.launch_status == "never_started"
    assert invocation.preflight_incomplete is True
    assert invocation.workspace_check.status == "unproven"
    assert invocation.workspace_check.review_pack_digest == hashlib.sha256(review_pack_path.read_bytes()).hexdigest()


def test_local_agent_blocks_when_findings_output_is_missing(tmp_path) -> None:
    review_pack_path = _write_review_pack(tmp_path)
    script = tmp_path / "no_output.py"
    script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "did not write findings.json" in result.blocker
    assert Path(result.invocation_path).is_file()


def test_local_agent_blocks_when_command_cannot_start(
    tmp_path,
    monkeypatch,
) -> None:
    review_pack_path = _write_review_pack(tmp_path)
    from ai_sdlc.core.quality_command import _CONTROLLED_LAUNCHER

    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    original_popen = subprocess.Popen

    def fail_creation(args, *pargs, **kwargs):
        if _CONTROLLED_LAUNCHER in args:
            raise PermissionError("permission denied")
        return original_popen(args, *pargs, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fail_creation)

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "could not be started" in result.blocker
    assert "permission denied" in result.blocker
    assert Path(result.invocation_path).is_file()


def test_local_agent_refuses_incomplete_allowlist_before_launch(tmp_path) -> None:
    review_pack_path = _write_review_pack(
        tmp_path,
        changed_files=["src/app.py", "src/secret.py"],
        reviewer_allowlist=["src/app.py"],
        diff_coverage={"redacted_files": 1, "omitted_files": 0},
    )
    script = tmp_path / "should_not_run.py"
    script.write_text(
        "from pathlib import Path\nPath('side-effect.txt').write_text('ran')\n",
        encoding="utf-8",
    )

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "allowlist is incomplete" in result.blocker
    assert not (tmp_path / "side-effect.txt").exists()


def test_local_agent_allows_explicit_omitted_file_policy_waiver(tmp_path) -> None:
    review_pack_path = _write_review_pack(
        tmp_path,
        changed_files=["src/app.py", "dist/app.generated.ts"],
        reviewer_allowlist=["src/app.py"],
        diff_coverage={"redacted_files": 0, "omitted_files": 1},
        policy_decisions={"incomplete_review_waiver": True},
    )
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    # 仅失败时输出本次真实 blocker、调用与收尾原件，供 JUnit 保留失败原因。
    assert result.status == ProviderRunStatus.SUCCESS, json.dumps(
        result.model_dump(mode="json"), ensure_ascii=False, indent=2
    )
    assert result.findings is not None
    assert result.findings.verdict == "clean"


def test_local_agent_allows_literal_braces_in_provider_command(tmp_path) -> None:
    review_pack_path = _write_review_pack(tmp_path)

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, "-c", "print('{}')"],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "did not write findings.json" in result.blocker
    assert Path(result.invocation_path).is_file()


def test_local_agent_does_not_reuse_stale_findings_output(tmp_path) -> None:
    review_pack_path = _write_review_pack(tmp_path)
    stale_findings = review_pack_path.with_name("findings.json")
    stale_findings.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "artifact_kind": "review-findings",
                "review_id": "review-001",
                "loop_id": "review-001-loop",
                "review_pack_path": str(review_pack_path),
                "provider_id": "local-agent",
                "model_selector": "current",
                "resolved_model": "gpt-5",
                "verdict": "clean",
            }
        ),
        encoding="utf-8",
    )
    script = tmp_path / "no_output.py"
    script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "did not write findings.json" in result.blocker
    assert not stale_findings.exists()


def test_local_agent_blocks_when_findings_schema_is_invalid(tmp_path) -> None:
    review_pack_path = _write_review_pack(tmp_path)
    script = tmp_path / "malformed.py"
    script.write_text(
        "\n".join(
            [
                "import argparse",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--review-pack')",
                "parser.add_argument('--output')",
                "parser.add_argument('--model')",
                "parser.add_argument('--resolved-model')",
                "parser.add_argument('--allowlist', nargs='*')",
                "args = parser.parse_args()",
                "open(args.output, 'w', encoding='utf-8').write('{not-json')",
            ]
        ),
        encoding="utf-8",
    )

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "schema validation failed" in result.blocker
    assert Path(result.schema_validation_path).is_file()


def test_local_agent_blocks_non_json_findings_even_when_yaml_schema_is_valid(
    tmp_path,
) -> None:
    review_pack_path = _write_review_pack(tmp_path)
    script = tmp_path / "yaml_findings.py"
    script.write_text(
        "\n".join(
            [
                "import argparse, json",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--review-pack')",
                "parser.add_argument('--output')",
                "parser.add_argument('--model')",
                "parser.add_argument('--resolved-model')",
                "parser.add_argument('--allowlist', nargs='*')",
                "args = parser.parse_args()",
                "pack = json.load(open(args.review_pack, encoding='utf-8'))",
                "open(args.output, 'w', encoding='utf-8').write(",
                "  \"schema_version: '1'\\n\"",
                '  "artifact_kind: review-findings\\n"',
                "  f\"review_id: {pack['review_id']}\\n\"",
                "  f\"loop_id: {pack['loop_id']}\\n\"",
                "  f\"review_pack_path: '{args.review_pack}'\\n\"",
                '  "provider_id: local-agent\\n"',
                '  "model_selector: current\\n"',
                '  "resolved_model: gpt-5\\n"',
                '  "verdict: clean\\n"',
                '  "findings: []\\n"',
                ")",
            ]
        ),
        encoding="utf-8",
    )

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "strict JSON" in result.blocker
    assert Path(result.schema_validation_path).is_file()


def test_local_agent_blocks_unexpected_exit_code(tmp_path) -> None:
    review_pack_path = _write_review_pack(tmp_path)
    script = tmp_path / "failure.py"
    script.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert result.exit_code == 2
    assert "exit code 2" in result.blocker


def test_local_agent_blocks_mismatched_exit_code_and_verdict(tmp_path) -> None:
    review_pack_path = _write_review_pack(tmp_path)
    script = tmp_path / "mismatched.py"
    script.write_text(
        "\n".join(
            [
                "import argparse, json, sys",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--review-pack', required=True)",
                "parser.add_argument('--output', required=True)",
                "parser.add_argument('--model')",
                "parser.add_argument('--resolved-model')",
                "parser.add_argument('--allowlist', nargs='*', default=[])",
                "args = parser.parse_args()",
                "pack = json.load(open(args.review_pack, encoding='utf-8'))",
                "payload = {",
                "  'schema_version': '1',",
                "  'artifact_kind': 'review-findings',",
                "  'review_id': pack['review_id'],",
                "  'loop_id': pack['loop_id'],",
                "  'review_pack_path': args.review_pack,",
                "  'provider_id': 'local-agent',",
                "  'model_selector': 'current',",
                "  'resolved_model': 'gpt-5',",
                "  'verdict': 'clean'",
                "}",
                "json.dump(payload, open(args.output, 'w', encoding='utf-8'))",
                "sys.exit(10)",
            ]
        ),
        encoding="utf-8",
    )

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert result.exit_code == 10
    assert "exit code does not match findings.verdict" in result.blocker
    assert result.findings is None
    assert json.loads(Path(result.findings_path).read_text(encoding="utf-8"))["verdict"] == "clean"
    assert result.invocation is not None and result.invocation.exit_code == 10


def test_local_agent_blocks_clean_verdict_with_required_findings(tmp_path) -> None:
    review_pack_path = _write_review_pack(tmp_path)
    script = tmp_path / "clean_with_required.py"
    script.write_text(
        "\n".join(
            [
                "import argparse, json, sys",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--review-pack', required=True)",
                "parser.add_argument('--output', required=True)",
                "parser.add_argument('--model')",
                "parser.add_argument('--resolved-model')",
                "parser.add_argument('--allowlist', nargs='*', default=[])",
                "args = parser.parse_args()",
                "pack = json.load(open(args.review_pack, encoding='utf-8'))",
                "payload = {",
                "  'schema_version': '1',",
                "  'artifact_kind': 'review-findings',",
                "  'review_id': pack['review_id'],",
                "  'loop_id': pack['loop_id'],",
                "  'review_pack_path': args.review_pack,",
                "  'provider_id': 'local-agent',",
                "  'model_selector': 'current',",
                "  'resolved_model': 'gpt-5',",
                "  'verdict': 'clean',",
                "  'findings': [{",
                "    'id': 'LOCAL-001',",
                "    'severity': 'REQUIRED',",
                "    'file': 'src/app.py',",
                "    'claim': 'Required issue.',",
                "    'evidence': 'The provider reported a required issue.',",
                "    'risk': 'The gate could close incorrectly.',",
                "    'suggested_fix': 'Return changes_required instead.',",
                "    'confidence': 0.8",
                "  }]",
                "}",
                "json.dump(payload, open(args.output, 'w', encoding='utf-8'))",
                "sys.exit(0)",
            ]
        ),
        encoding="utf-8",
    )

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert result.exit_code == 0
    assert "schema validation failed" in result.blocker
    assert result.findings is None


def test_local_agent_blocks_findings_for_different_review_pack(tmp_path) -> None:
    review_pack_path = _write_review_pack(tmp_path, review_id="review-current")
    script = tmp_path / "stale_scope.py"
    script.write_text(
        "\n".join(
            [
                "import argparse, json",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--review-pack', required=True)",
                "parser.add_argument('--output', required=True)",
                "parser.add_argument('--model')",
                "parser.add_argument('--resolved-model')",
                "parser.add_argument('--allowlist', nargs='*', default=[])",
                "args = parser.parse_args()",
                "payload = {",
                "  'schema_version': '1',",
                "  'artifact_kind': 'review-findings',",
                "  'review_id': 'review-stale',",
                "  'loop_id': 'review-stale-loop',",
                "  'review_pack_path': args.review_pack,",
                "  'provider_id': 'local-agent',",
                "  'model_selector': 'current',",
                "  'resolved_model': 'gpt-5',",
                "  'verdict': 'clean',",
                "  'findings': []",
                "}",
                "json.dump(payload, open(args.output, 'w', encoding='utf-8'))",
            ]
        ),
        encoding="utf-8",
    )

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "review_id does not match" in result.blocker


def test_local_agent_blocks_findings_outside_review_allowlist(tmp_path) -> None:
    review_pack_path = _write_review_pack(tmp_path, review_id="review-allowlist")
    script = tmp_path / "outside_allowlist.py"
    script.write_text(
        "\n".join(
            [
                "import argparse, json, sys",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--review-pack', required=True)",
                "parser.add_argument('--output', required=True)",
                "parser.add_argument('--model')",
                "parser.add_argument('--resolved-model')",
                "parser.add_argument('--allowlist', nargs='*', default=[])",
                "args = parser.parse_args()",
                "pack = json.load(open(args.review_pack, encoding='utf-8'))",
                "payload = {",
                "  'schema_version': '1',",
                "  'artifact_kind': 'review-findings',",
                "  'review_id': pack['review_id'],",
                "  'loop_id': pack['loop_id'],",
                "  'review_pack_path': args.review_pack,",
                "  'provider_id': 'local-agent',",
                "  'model_selector': 'current',",
                "  'resolved_model': 'gpt-5',",
                "  'verdict': 'changes_required',",
                "  'findings': [{",
                "    'id': 'LOCAL-OUT',",
                "    'severity': 'REQUIRED',",
                "    'file': 'src/other.py',",
                "    'claim': 'Outside scope.',",
                "    'evidence': 'Fixture emitted unrelated file.',",
                "    'risk': 'Scope drift could be hidden.',",
                "    'suggested_fix': 'Reject this finding.',",
                "    'confidence': 0.8",
                "  }]",
                "}",
                "json.dump(payload, open(args.output, 'w', encoding='utf-8'))",
                "sys.exit(10)",
            ]
        ),
        encoding="utf-8",
    )

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "outside the review allowlist" in result.blocker
    assert "src/other.py" in result.blocker


def test_local_agent_blocks_when_reviewer_mutates_worktree(tmp_path) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('before')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    review_pack_path = _write_review_pack(tmp_path)
    script = _write_mutating_reviewer_script(tmp_path)

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "modified files outside expected provider output artifacts" in result.blocker
    assert "src/app.py" in result.blocker


def test_local_agent_blocks_when_reviewer_mutates_dirty_path_with_space(
    tmp_path,
) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('before')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    _write_file(tmp_path, "file with space.txt", "before\n")
    _git(tmp_path, "add", "file with space.txt")
    _git(tmp_path, "commit", "-m", "add spaced file")
    review_pack_path = _write_review_pack(tmp_path)
    script = tmp_path / "space_path_mutating_reviewer.py"
    script.write_text(
        "\n".join(
            [
                "import argparse, json",
                "from pathlib import Path",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--review-pack', required=True)",
                "parser.add_argument('--output', required=True)",
                "parser.add_argument('--model')",
                "parser.add_argument('--resolved-model')",
                "parser.add_argument('--allowlist', nargs='*', default=[])",
                "args = parser.parse_args()",
                "pack = json.load(open(args.review_pack, encoding='utf-8'))",
                "Path('file with space.txt').write_text('after\\n', encoding='utf-8')",
                "payload = {",
                "  'schema_version': '1',",
                "  'artifact_kind': 'review-findings',",
                "  'review_id': pack['review_id'],",
                "  'loop_id': pack['loop_id'],",
                "  'review_pack_path': args.review_pack,",
                "  'provider_id': 'local-agent',",
                "  'model_selector': 'current',",
                "  'resolved_model': 'gpt-5',",
                "  'verdict': 'clean',",
                "  'findings': []",
                "}",
                "json.dump(payload, open(args.output, 'w', encoding='utf-8'))",
            ]
        ),
        encoding="utf-8",
    )

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "modified files outside expected provider output artifacts" in result.blocker
    assert "file with space.txt" in result.blocker


def test_local_agent_reports_worktree_mutation_when_reviewer_times_out(
    tmp_path,
) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('before')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    review_pack_path = _write_review_pack(tmp_path)
    script = tmp_path / "timeout_mutating_reviewer.py"
    script.write_text(
        "\n".join(
            [
                "import pathlib, time",
                "pathlib.Path('src/app.py').write_text(\"print('after')\\n\", encoding='utf-8')",
                "time.sleep(5)",
            ]
        ),
        encoding="utf-8",
    )

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
            timeout_seconds=0.2,
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "modified files outside expected provider output artifacts" in result.blocker
    assert "src/app.py" in result.blocker
    assert "Restore the worktree" in result.next_action


def test_local_agent_blocks_when_reviewer_mutates_ignored_file(tmp_path) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, ".gitignore", ".env\n")
    _write_file(tmp_path, "src/app.py", "print('before')\n")
    _git(tmp_path, "add", ".gitignore", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    review_pack_path = _write_review_pack(tmp_path)
    script = tmp_path / "ignored_mutating_reviewer.py"
    script.write_text(
        "\n".join(
            [
                "import argparse, json",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--review-pack', required=True)",
                "parser.add_argument('--output', required=True)",
                "parser.add_argument('--model')",
                "parser.add_argument('--resolved-model')",
                "parser.add_argument('--allowlist', nargs='*', default=[])",
                "args = parser.parse_args()",
                "pack = json.load(open(args.review_pack, encoding='utf-8'))",
                "open('.env', 'w', encoding='utf-8').write('TOKEN=secret\\n')",
                "payload = {",
                "  'schema_version': '1',",
                "  'artifact_kind': 'review-findings',",
                "  'review_id': pack['review_id'],",
                "  'loop_id': pack['loop_id'],",
                "  'review_pack_path': args.review_pack,",
                "  'provider_id': 'local-agent',",
                "  'model_selector': 'current',",
                "  'resolved_model': 'gpt-5',",
                "  'verdict': 'clean'",
                "}",
                "json.dump(payload, open(args.output, 'w', encoding='utf-8'))",
            ]
        ),
        encoding="utf-8",
    )

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "modified files outside expected provider output artifacts" in result.blocker
    assert ".env" in result.blocker


def test_local_agent_snapshot_does_not_hash_ignored_directories(
    tmp_path,
    monkeypatch,
) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, ".gitignore", "node_modules/\n")
    _write_file(tmp_path, "src/app.py", "print('before')\n")
    _write_file(tmp_path, "node_modules/pkg/index.js", "console.log('ignored')\n")
    _git(tmp_path, "add", ".gitignore", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    review_pack_path = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    original_path_digest = pr_review_provider._path_digest

    def fail_for_ignored_dir(path: Path) -> str:
        if path.is_dir() and path.name == "node_modules":
            raise AssertionError("ignored directories must not be recursively hashed")
        return original_path_digest(path)

    monkeypatch.setattr(pr_review_provider, "_path_digest", fail_for_ignored_dir)

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.SUCCESS


def test_local_agent_blocks_when_snapshot_capture_fails_after_reviewer(
    tmp_path,
    monkeypatch,
) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('before')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    review_pack_path = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    original_run = pr_review_provider.subprocess.run
    original_owned = pr_review_provider.run_controlled_process
    reviewer_completed = False

    def observe_completion(options):
        nonlocal reviewer_completed
        result = original_owned(options)
        reviewer_completed = True
        return result

    monkeypatch.setattr(pr_review_provider, "run_controlled_process", observe_completion)

    def fail_second_snapshot(args, *pargs, **kwargs):
        nonlocal reviewer_completed
        argv = list(args)
        if (
            len(argv) >= 2
            and argv[0] == "git"
            and argv[1] == "status"
            and "--ignored=matching" in argv
            and reviewer_completed
        ):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 30)
        result = original_run(args, *pargs, **kwargs)
        if argv[:2] == [sys.executable, str(script)]:
            reviewer_completed = True
        return result

    monkeypatch.setattr(pr_review_provider.subprocess, "run", fail_second_snapshot)

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "Unable to verify reviewer worktree isolation" in result.blocker
    assert "git status timed out" in result.blocker
    assert "Fix git status access" in result.next_action
    assert result.invocation is not None
    assert result.invocation.workspace_check is not None
    assert result.invocation.workspace_check.status == "unproven"


def test_ignored_dir_digest_ignores_directory_metadata_churn(tmp_path) -> None:
    ignored_dir = tmp_path / "node_modules"
    package_dir = ignored_dir / "pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "index.js").write_text("console.log('ignored')\n", encoding="utf-8")

    before = pr_review_provider._ignored_dir_digest(ignored_dir)
    os.utime(package_dir, (1_700_000_000, 1_700_000_000))
    after = pr_review_provider._ignored_dir_digest(ignored_dir)

    assert after == before


def test_local_agent_blocks_mutation_inside_ignored_directory(tmp_path) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, ".gitignore", "node_modules/\n")
    _write_file(tmp_path, "src/app.py", "print('before')\n")
    _write_file(tmp_path, "node_modules/pkg/index.js", "console.log('before')\n")
    _git(tmp_path, "add", ".gitignore", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    review_pack_path = _write_review_pack(tmp_path)
    script = tmp_path / "mutate_ignored_dir.py"
    script.write_text(
        "\n".join(
            [
                "import argparse, json, pathlib",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--review-pack', required=True)",
                "parser.add_argument('--output', required=True)",
                "parser.add_argument('--model')",
                "parser.add_argument('--resolved-model')",
                "parser.add_argument('--allowlist', nargs='*', default=[])",
                "args = parser.parse_args()",
                "pathlib.Path('node_modules/pkg/index.js').write_text(",
                "  \"console.log('after mutation with longer content')\\n\",",
                "  encoding='utf-8'",
                ")",
                "pack = json.load(open(args.review_pack, encoding='utf-8'))",
                "payload = {",
                "  'schema_version': '1',",
                "  'artifact_kind': 'review-findings',",
                "  'review_id': pack['review_id'],",
                "  'loop_id': pack['loop_id'],",
                "  'review_pack_path': args.review_pack,",
                "  'provider_id': 'local-agent',",
                "  'model_selector': args.model,",
                "  'resolved_model': args.resolved_model,",
                "  'verdict': 'clean',",
                "  'findings': []",
                "}",
                "json.dump(payload, open(args.output, 'w', encoding='utf-8'))",
            ]
        ),
        encoding="utf-8",
    )

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "modified files outside expected provider output artifacts" in result.blocker
    assert "node_modules" in result.blocker


def test_local_agent_blocks_review_pack_artifact_tampering(tmp_path) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('before')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    review_pack_path = _write_review_pack(tmp_path)
    script = tmp_path / "tamper_review_pack.py"
    script.write_text(
        "\n".join(
            [
                "import argparse, json, pathlib",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--review-pack', required=True)",
                "parser.add_argument('--output', required=True)",
                "parser.add_argument('--model')",
                "parser.add_argument('--resolved-model')",
                "parser.add_argument('--allowlist', nargs='*', default=[])",
                "args = parser.parse_args()",
                "pack = json.load(open(args.review_pack, encoding='utf-8'))",
                "payload = {",
                "  'schema_version': '1',",
                "  'artifact_kind': 'review-findings',",
                "  'review_id': pack['review_id'],",
                "  'loop_id': pack['loop_id'],",
                "  'review_pack_path': args.review_pack,",
                "  'provider_id': 'local-agent',",
                "  'model_selector': 'current',",
                "  'resolved_model': 'gpt-5',",
                "  'verdict': 'clean',",
                "  'findings': []",
                "}",
                "json.dump(payload, open(args.output, 'w', encoding='utf-8'))",
                "pathlib.Path(args.review_pack).write_text('{}\\n', encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "modified files outside expected provider output artifacts" in result.blocker
    assert "review-pack.json" in result.blocker


def test_local_agent_blocks_when_reviewer_commits_worktree_mutation(tmp_path) -> None:
    _init_git_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('before')\n")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    review_pack_path = _write_review_pack(tmp_path)
    script = tmp_path / "committing_reviewer.py"
    script.write_text(
        "\n".join(
            [
                "import argparse, json, subprocess",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--review-pack', required=True)",
                "parser.add_argument('--output', required=True)",
                "parser.add_argument('--model')",
                "parser.add_argument('--resolved-model')",
                "parser.add_argument('--allowlist', nargs='*', default=[])",
                "args = parser.parse_args()",
                "pack = json.load(open(args.review_pack, encoding='utf-8'))",
                "open('src/app.py', 'w', encoding='utf-8').write(\"print('after')\\n\")",
                "subprocess.run(['git', 'add', 'src/app.py'], check=True)",
                "subprocess.run(['git', 'commit', '-m', 'reviewer mutation'], check=True)",
                "payload = {",
                "  'schema_version': '1',",
                "  'artifact_kind': 'review-findings',",
                "  'review_id': pack['review_id'],",
                "  'loop_id': pack['loop_id'],",
                "  'review_pack_path': args.review_pack,",
                "  'provider_id': 'local-agent',",
                "  'model_selector': 'current',",
                "  'resolved_model': 'gpt-5',",
                "  'verdict': 'clean'",
                "}",
                "json.dump(payload, open(args.output, 'w', encoding='utf-8'))",
            ]
        ),
        encoding="utf-8",
    )

    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=review_pack_path,
            command=[sys.executable, str(script)],
        )
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "modified files outside expected provider output artifacts" in result.blocker
    assert "<git:HEAD>" in result.blocker


def test_mock_reviewer_supports_clean_changes_required_and_blocked(
    tmp_path,
) -> None:
    clean_pack_path = _write_review_pack(tmp_path, review_id="review-clean")
    changes_pack_path = _write_review_pack(tmp_path, review_id="review-changes")
    blocked_pack_path = _write_review_pack(tmp_path, review_id="review-blocked")

    clean = run_mock_reviewer(
        root=tmp_path,
        review_pack_path=clean_pack_path,
        fixture=MockReviewerFixture.CLEAN,
    )
    changes = run_mock_reviewer(
        root=tmp_path,
        review_pack_path=changes_pack_path,
        fixture=MockReviewerFixture.CHANGES_REQUIRED,
    )
    blocked = run_mock_reviewer(
        root=tmp_path,
        review_pack_path=blocked_pack_path,
        fixture=MockReviewerFixture.BLOCKED,
    )

    assert clean.status == ProviderRunStatus.SUCCESS
    assert changes.status == ProviderRunStatus.CHANGES_REQUIRED
    assert blocked.status == ProviderRunStatus.BLOCKED
    assert "Mock reviewer blocked" in blocked.blocker
    assert "blocked review provider" in blocked.next_action
    assert clean.invocation is not None
    assert clean.invocation.provider_mode == "mock"
    assert clean.invocation.command == "mock-reviewer"
    assert clean.invocation.code_egress is False


def test_mock_reviewer_malformed_fixture_blocks_with_schema_report(tmp_path) -> None:
    review_pack_path = _write_review_pack(tmp_path, review_id="review-malformed")

    result = run_mock_reviewer(
        root=tmp_path,
        review_pack_path=review_pack_path,
        fixture=MockReviewerFixture.MALFORMED,
    )

    assert result.status == ProviderRunStatus.BLOCKED
    assert "schema validation failed" in result.blocker
    assert Path(result.findings_path).is_file()
    assert Path(result.schema_validation_path).is_file()


def _write_review_pack(
    root: Path,
    *,
    review_id: str = "review-001",
    diff_source_kind: DiffSourceKind = DiffSourceKind.LOCAL_GIT_RANGE,
    base_ref: str = "main",
    head_ref: str = "HEAD",
    model_selector: str = "current",
    resolved_model: str = "gpt-5",
    source: ModelResolutionSource = ModelResolutionSource.CURRENT_AGENT,
    changed_files: list[str] | None = None,
    reviewer_allowlist: list[str] | None = None,
    diff_coverage: dict[str, int | float | str] | None = None,
    policy_decisions: dict[str, str | bool | int | float] | None = None,
    head_commit: str | None = None,
    patch_file: str = "",
    patch_hash: str = "",
) -> Path:
    if not (root / ".git").exists():
        _init_git_repo(root)
        _write_file(root, "src/app.py", "print('base')\n")
        _git(root, "add", "src/app.py")
        _git(root, "commit", "-m", "initial")
    store = LoopArtifactStore(root)
    review_dir = store.create_review_run_dir(review_id)
    diff_path = store.write_markdown_artifact(
        review_dir / "diff.patch",
        "diff --git a/src/app.py b/src/app.py\n",
    )
    staged_tree_oid = (
        _git(root, "write-tree")
        if diff_source_kind == DiffSourceKind.LOCAL_STAGED
        else ""
    )
    review_pack = ReviewPack(
        review_id=review_id,
        loop_id=f"{review_id}-loop",
        diff_source=DiffSourceDescriptor(
            source_kind=diff_source_kind,
            adapter_id=diff_source_kind.value,
            base_ref=base_ref,
            head_ref=head_ref,
            patch_file=patch_file,
            patch_hash=patch_hash,
            staged_tree_oid=staged_tree_oid,
        ),
        source_adapter=diff_source_kind.value,
        repo_root=str(root),
        base_ref=base_ref,
        head_ref=head_ref,
        base_commit="a" * 40,
        head_commit=head_commit or _maybe_git_head(root) or "b" * 40,
        staged_tree_oid=staged_tree_oid,
        changed_files=changed_files or ["src/app.py"],
        diff_path=str(diff_path),
        diff_coverage=diff_coverage or {},
        policy_decisions=policy_decisions or {},
        reviewer_allowlist=reviewer_allowlist or ["src/app.py"],
        model_selector=model_selector,
        resolved_model=resolved_model,
        model_resolution_status=ModelResolutionStatus.RESOLVED,
        model_resolution_source=source,
        provider_mode=ProviderMode.LOCAL_AGENT,
        code_egress=False,
    )
    return store.write_json_artifact(
        review_dir / "review-pack.json",
        review_pack,
    )


def _maybe_git_head(path: Path) -> str:
    try:
        return _git(path, "rev-parse", "HEAD")
    except AssertionError:
        return ""


def _init_git_repo(path: Path) -> None:
    _git(path, "init")
    if _git(path, "symbolic-ref", "--short", "HEAD") != "main":
        _git(path, "checkout", "-b", "main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")


def _write_file(path: Path, file_path: str, content: str) -> None:
    target = path / file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _git(path: Path, *args: str) -> str:
    return _git_raw(path, *args).strip()


def _git_raw(path: Path, *args: str) -> str:
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
    return result.stdout


def _write_reviewer_script(
    tmp_path: Path,
    *,
    exit_code: int,
    verdict: str = "changes_required",
) -> Path:
    script = tmp_path / "reviewer.py"
    finding_lines = (
        ["  'findings': []"]
        if verdict == "clean"
        else [
            "  'findings': [{",
            "    'id': 'LOCAL-001',",
            "    'severity': 'REQUIRED',",
            "    'file': 'src/app.py',",
            "    'claim': 'Focused fixture finding.',",
            "    'evidence': 'The local command fixture ran.',",
            "    'risk': 'Fixture risk.',",
            "    'suggested_fix': 'Fix the fixture finding.',",
            "    'confidence': 0.8",
            "  }]",
        ]
    )
    script.write_text(
        "\n".join(
            [
                "import argparse, json, sys",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--review-pack', required=True)",
                "parser.add_argument('--output', required=True)",
                "parser.add_argument('--model', required=True)",
                "parser.add_argument('--resolved-model', required=True)",
                "parser.add_argument('--allowlist', nargs='*', default=[])",
                "args = parser.parse_args()",
                "pack = json.load(open(args.review_pack, encoding='utf-8'))",
                "payload = {",
                "  'schema_version': '1',",
                "  'artifact_kind': 'review-findings',",
                "  'review_id': pack['review_id'],",
                "  'loop_id': pack['loop_id'],",
                "  'review_pack_path': args.review_pack,",
                "  'provider_id': 'local-agent',",
                "  'model_selector': args.model,",
                "  'resolved_model': args.resolved_model,",
                f"  'verdict': '{verdict}',",
                *finding_lines,
                "}",
                "json.dump(payload, open(args.output, 'w', encoding='utf-8'))",
                f"sys.exit({exit_code})",
            ]
        ),
        encoding="utf-8",
    )
    return script


def _write_mutating_reviewer_script(tmp_path: Path) -> Path:
    script = tmp_path / "mutating_reviewer.py"
    script.write_text(
        "\n".join(
            [
                "import argparse, json",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--review-pack', required=True)",
                "parser.add_argument('--output', required=True)",
                "parser.add_argument('--model')",
                "parser.add_argument('--resolved-model')",
                "parser.add_argument('--allowlist', nargs='*', default=[])",
                "args = parser.parse_args()",
                "pack = json.load(open(args.review_pack, encoding='utf-8'))",
                "open('src/app.py', 'w', encoding='utf-8').write(\"print('after')\\n\")",
                "payload = {",
                "  'schema_version': '1',",
                "  'artifact_kind': 'review-findings',",
                "  'review_id': pack['review_id'],",
                "  'loop_id': pack['loop_id'],",
                "  'review_pack_path': args.review_pack,",
                "  'provider_id': 'local-agent',",
                "  'model_selector': 'current',",
                "  'resolved_model': 'gpt-5',",
                "  'verdict': 'clean'",
                "}",
                "json.dump(payload, open(args.output, 'w', encoding='utf-8'))",
            ]
        ),
        encoding="utf-8",
    )
    return script


def test_local_agent_allows_findings_under_ignored_review_directory(tmp_path):
    _init_git_repo(tmp_path)
    _write_file(tmp_path, ".gitignore", ".ai-sdlc/reviews/\n")
    _write_file(tmp_path, "src/app.py", "print('before')\n")
    _git(tmp_path, "add", ".gitignore", "src/app.py")
    _git(tmp_path, "commit", "-m", "initial")
    pack = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path, review_pack_path=pack, command=[sys.executable, str(script)]
        )
    )
    assert result.status == ProviderRunStatus.SUCCESS
    assert Path(result.findings_path).is_file()
    assert Path(result.invocation_path).is_file()


def test_ignored_review_output_exclusion_preserves_other_originals(tmp_path):
    _init_git_repo(tmp_path)
    _write_file(tmp_path, ".gitignore", ".ai-sdlc/reviews/\n")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-m", "initial")
    folder = tmp_path / ".ai-sdlc/reviews/pr/probe"
    folder.mkdir(parents=True)
    private = folder / "private-history.json"
    private.write_text("original")
    findings = folder / "findings.json"
    mutable = frozenset({findings.resolve()})
    before = pr_review_provider._worktree_snapshot(tmp_path, mutable)
    findings.write_text("only allowed output")
    assert (
        pr_review_provider._worktree_mutation_blocker(tmp_path, mutable, before) == ""
    )
    private.write_text("different private original")
    assert (
        "modified files outside expected provider output artifacts"
        in pr_review_provider._worktree_mutation_blocker(tmp_path, mutable, before)
    )


def test_ignored_output_does_not_shift_protected_entry_limit(tmp_path):
    tmp_path = tmp_path / "ignored"
    tmp_path.mkdir()
    first = tmp_path / "a-private"
    last = tmp_path / "z-private"
    first.write_text("first")
    last.write_text("last")
    output = tmp_path / "b-findings.json"
    options = {"max_entries": 2, "mutable_provider_outputs": frozenset({output})}
    before = pr_review_provider._ignored_dir_digest(tmp_path, **options)
    output.write_text("allowed")
    assert pr_review_provider._ignored_dir_digest(tmp_path, **options) == before
    last.write_text("changed protected item at the original limit")
    assert pr_review_provider._ignored_dir_digest(tmp_path, **options) != before


def test_ignored_output_exclusion_does_not_follow_symlink_alias(tmp_path):
    output = tmp_path / "findings.json"
    output.write_text("allowed")
    options = {"mutable_provider_outputs": frozenset({output})}
    before = pr_review_provider._ignored_dir_digest(tmp_path, **options)
    alias = tmp_path / "another-path"
    try:
        alias.symlink_to(output)
    except OSError:
        import pytest

        pytest.skip("symlink creation is not available")
    assert pr_review_provider._ignored_dir_digest(tmp_path, **options) != before
    alias.unlink()
    output.unlink()
    output.symlink_to(tmp_path / "outside-output")
    assert pr_review_provider._ignored_dir_digest(tmp_path, **options) != before


@pytest.mark.parametrize("failure", ["host-read", "inconsistent", "ignored-read"])
def test_provider_workspace_capture_failure_is_persisted_unproven(
    tmp_path, monkeypatch, failure
):
    pack = _write_review_pack(tmp_path)
    ignored = tmp_path / "ordinary-ignored"
    ignored.mkdir()
    (ignored / "note").write_text("original")
    (tmp_path / ".git/info/exclude").write_text("ordinary-ignored/\n")
    marker = tmp_path.parent / (tmp_path.name + "-reviewer-ran")
    script = tmp_path / "failed.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
        "raise SystemExit(20)\n"
    )
    if failure == "host-read":
        original = pr_review_provider._host_artifact_snapshot

        def unavailable(root, paths):
            if marker.exists():
                raise ValueError("host artifact changed while reading")
            return original(root, paths)

        monkeypatch.setattr(pr_review_provider, "_host_artifact_snapshot", unavailable)
    elif failure == "inconsistent":
        original = pr_review_provider._worktree_snapshot

        def inconsistent(root, outputs):
            snapshot = original(root, outputs)
            if marker.exists() and len(outputs) > 1:
                snapshot["ordinary-new-file"] = "??:changed-during-capture"
            return snapshot

        monkeypatch.setattr(pr_review_provider, "_worktree_snapshot", inconsistent)
    else:
        original = pr_review_provider.os.scandir

        def unreadable(path):
            if marker.exists() and isinstance(path, (str, os.PathLike)) and Path(path) == ignored:
                raise OSError("ignored directory read failed")
            return original(path)

        monkeypatch.setattr(pr_review_provider.os, "scandir", unreadable)
    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path, review_pack_path=pack, command=[sys.executable, str(script)]
        )
    )
    assert marker.read_text() == "ran"
    assert result.status == "blocked" and result.exit_code == 20
    invocation = ProviderRunnerInvocation.model_validate_json(
        Path(result.invocation_path).read_bytes()
    )
    assert invocation.workspace_check is not None
    assert invocation.workspace_check.status == "unproven"
    assert invocation.workspace_check.reason


def test_completed_legacy_invocation_without_workspace_proof_remains_readable(tmp_path):
    pack = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path, review_pack_path=pack, command=[sys.executable, str(script)]
        )
    )
    assert result.status == "success"
    payload = json.loads(Path(result.invocation_path).read_bytes())
    payload.pop("workspace_check")
    legacy = ProviderRunnerInvocation.model_validate(payload)
    assert legacy.workspace_check is None
    assert "workspace_check" not in legacy.model_dump()


def _fail_reviewer_communication(monkeypatch, script):
    from ai_sdlc.core import quality_command as quality

    original_exit = quality._current_child_exit
    failed = False

    def fail_after_launch(process):
        nonlocal failed
        result = original_exit(process)
        if str(script) in list(process.args) and result is not None and not failed:
            failed = True
            raise OSError("reviewer wait failed after launch")
        return result

    monkeypatch.setattr(quality, "_current_child_exit", fail_after_launch)


@pytest.mark.parametrize("failure", ["enoent", "eacces"])
def test_provider_records_proven_never_started_launch(tmp_path, failure):
    if failure == "eacces" and os.name == "nt":
        pytest.skip("POSIX executable permission boundary")
    pack = _write_review_pack(tmp_path)
    command = tmp_path / "unavailable-reviewer"
    if failure == "eacces":
        command.write_text("#!/bin/sh\nexit 20\n")
        command.chmod(0o644)
    result = run_provider_command(
        ProviderCommandOptions(root=tmp_path, review_pack_path=pack, command=[str(command)])
    )
    assert result.status == "blocked"
    assert result.invocation is not None
    invocation = result.invocation.model_dump(mode="json")
    assert invocation.get("launch_status") == "never_started"
    assert invocation["exit_code"] is None
    assert invocation["workspace_check"]["status"] == "unchanged"
    assert not Path(result.findings_path).exists()


@pytest.mark.parametrize("failure", ["postlaunch-oserror", "timeout"])
def test_provider_does_not_classify_started_failure_as_never_started(
    tmp_path, monkeypatch, failure
):
    pack = _write_review_pack(tmp_path)
    script = tmp_path / "started-failure.py"
    script.write_text("import time\ntime.sleep(2)\n" if failure == "timeout" else "raise SystemExit(20)\n")
    if failure == "postlaunch-oserror":
        _fail_reviewer_communication(monkeypatch, script)
    result = run_provider_command(
        ProviderCommandOptions(
            root=tmp_path,
            review_pack_path=pack,
            command=[sys.executable, str(script)],
            timeout_seconds=0.05 if failure == "timeout" else 10,
        )
    )
    assert result.status == "blocked"
    assert result.invocation is not None
    assert result.invocation.model_dump(mode="json").get("launch_status") == "started"
    assert result.invocation.completion_proof is not None
    raw = result.invocation.completion_proof.require_complete()
    assert raw["launch_status"] == "started"
    assert raw["exit_code"] == result.invocation.exit_code
    assert not Path(result.findings_path).exists()


def test_completed_legacy_invocation_defaults_to_unknown_launch(tmp_path):
    pack = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    result = run_provider_command(
        ProviderCommandOptions(root=tmp_path, review_pack_path=pack, command=[sys.executable, str(script)])
    )
    assert result.status == "success"
    payload = json.loads(Path(result.invocation_path).read_bytes())
    assert payload.get("launch_status") == "started"
    payload.pop("launch_status")
    legacy = ProviderRunnerInvocation.model_validate(payload)
    assert legacy.model_dump(mode="json").get("launch_status") == "unknown"
    assert legacy.exit_code == 0

@pytest.mark.parametrize("exit_code,verdict", [(0, "clean"), (10, "changes_required")])
def test_provider_accepts_findings_only_with_bound_owned_completion(tmp_path, exit_code, verdict):
    pack = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=exit_code, verdict=verdict)
    result = run_provider_command(
        ProviderCommandOptions(root=tmp_path, review_pack_path=pack, command=[sys.executable, str(script)])
    )
    assert result.exit_code == exit_code
    assert result.invocation is not None
    assert result.invocation.model_dump(mode="json").get("completion_proof"), "正常判定须保留所属执行与终止证明"
    assert result.invocation.completion_proof.require_complete()["exit_code"] == result.invocation.exit_code == exit_code


def _observe_provider_output_read(monkeypatch, *, fault_stream=None, error_type=OSError, error=None):
    from ai_sdlc.core import quality_command as quality

    original_read = quality._digest_and_tail
    observed = {"injected": 0, "originals": {}, "proof_root": None}

    def read_after_receipts(stream, *args, **kwargs):
        path = Path(stream.name)
        if path.name in {"stdout", "stderr"} and path.parent.name.startswith("ai-sdlc-provider-owned-"):
            observed["proof_root"] = path.parent
            originals = {name: (path.parent / name).read_bytes()
                         for name in ("process.json", "raw-result.json", "cleanup.json")}
            if observed["originals"]:
                assert observed["originals"] == originals
            observed["originals"] = originals
            if path.name == fault_stream and not observed["injected"]:
                observed["injected"] += 1
                raise error if error is not None else error_type(f"ordinary post-receipt {path.name} read failure")
        return original_read(stream, *args, **kwargs)

    monkeypatch.setattr(quality, "_digest_and_tail", read_after_receipts)
    return observed


@pytest.mark.parametrize("exit_code,verdict", [(0, "clean"), (10, "changes_required"), (20, "blocked")])
@pytest.mark.parametrize("fault_stream", ["stdout", "stderr"])
def test_postreceipt_output_read_failure_preserves_actual_exit_and_blocked_result(
    tmp_path, monkeypatch, exit_code, verdict, fault_stream
):
    pack = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=exit_code, verdict=verdict)
    observed = _observe_provider_output_read(monkeypatch, fault_stream=fault_stream)
    result = run_provider_command(ProviderCommandOptions(
        root=tmp_path, review_pack_path=pack, command=[sys.executable, str(script)],
    ))

    assert observed["injected"] == 1
    assert result.status == ProviderRunStatus.BLOCKED and result.findings is None
    assert f"ordinary post-receipt {fault_stream} read failure" in result.blocker
    invocation = ProviderRunnerInvocation.model_validate_json(Path(result.invocation_path).read_bytes())
    assert invocation == result.invocation
    assert invocation.launch_status == "started" and invocation.status == "blocked"
    assert invocation.workspace_check.status == "unchanged"
    assert invocation.execution_failure.exception_type == "OSError"
    assert invocation.execution_failure.findings_status == "present"
    assert invocation.execution_failure.findings_sha256 == hashlib.sha256(Path(result.findings_path).read_bytes()).hexdigest()
    raw = invocation.completion_proof.require_complete()
    assert raw["exit_code"] == invocation.exit_code == result.exit_code == exit_code
    assert not raw["timed_out"] and not raw["output_io_error"]
    assert {name: content.encode("utf-8") for name, content in invocation.completion_proof.originals.items()} == observed["originals"]
    assert json.loads(Path(result.findings_path).read_bytes())["verdict"] == verdict


def test_postreceipt_missing_output_keeps_actual_failure_diagnostic(tmp_path, monkeypatch):
    pack = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    observed = _observe_provider_output_read(
        monkeypatch, fault_stream="stdout", error_type=FileNotFoundError,
    )
    result = run_provider_command(ProviderCommandOptions(
        root=tmp_path, review_pack_path=pack, command=[sys.executable, str(script)],
    ))
    assert observed["injected"] == 1
    assert result.status == ProviderRunStatus.BLOCKED and result.exit_code == 0
    assert "ordinary post-receipt stdout read failure" in result.blocker
    assert "Reviewer command not found" not in result.blocker
    assert result.invocation.launch_status == "started"


@pytest.mark.parametrize("after_read_error", [False, True], ids=["normal-return", "postreceipt-error"])
@pytest.mark.parametrize("damage", ["missing-original", "hash-drift", "invalid-exit", "unclean", "identity-conflict"])
def test_completion_paths_reject_invalid_originals_before_assigning_exit(
    tmp_path, monkeypatch, after_read_error, damage
):
    pack = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    observed = _observe_provider_output_read(
        monkeypatch, fault_stream="stderr" if after_read_error else None,
    )
    original_proof = pr_review_provider.ProviderCompletionProof

    def corrupt_captured_proof(**kwargs):
        proof = original_proof(**kwargs)
        assert proof.require_complete()["exit_code"] == 0
        originals, hashes = dict(proof.originals), dict(proof.sha256)
        if damage == "missing-original":
            originals.pop("cleanup.json")
            hashes.pop("cleanup.json")
        elif damage == "hash-drift":
            originals["raw-result.json"] += " "
        else:
            name = "cleanup.json" if damage == "unclean" else "raw-result.json"
            payload = json.loads(originals[name])
            key, value = {"invalid-exit": ("exit_code", "0"),
                          "unclean": ("status", "incomplete"),
                          "identity-conflict": ("ownership_nonce", "another-invocation")}[damage]
            payload[key] = value
            originals[name] = json.dumps(payload)
            hashes[name] = hashlib.sha256(originals[name].encode("utf-8")).hexdigest()
        return proof.model_copy(update={"originals": originals, "sha256": hashes})

    monkeypatch.setattr(pr_review_provider, "ProviderCompletionProof", corrupt_captured_proof)
    result = run_provider_command(ProviderCommandOptions(
        root=tmp_path, review_pack_path=pack, command=[sys.executable, str(script)],
    ))
    assert observed["originals"] and observed["injected"] == int(after_read_error)
    assert result.status == ProviderRunStatus.BLOCKED and result.findings is None
    assert result.exit_code is None and result.invocation.exit_code is None
    assert result.invocation.launch_status == "started" and result.invocation.status == "blocked"
    assert "Reviewer completion could not be verified" in result.blocker
    if after_read_error:
        assert "ordinary post-receipt stderr read failure" in result.blocker
    with pytest.raises(ValueError):
        result.invocation.completion_proof.require_complete()

def test_provider_preserves_user_environment_under_owned_parent(tmp_path, monkeypatch):
    pack = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    script.write_text(
        "import os\nassert os.environ['PROVIDER_PROJECT_CHOICE']=='kept'\n"
        "assert os.environ.get('_AI_SDLC_ATTEMPT_OWNER') != 'outer-attempt'\n"
        + script.read_text()
    )
    monkeypatch.setenv("_AI_SDLC_ATTEMPT_OWNER", "outer-attempt")
    monkeypatch.setenv("PROVIDER_PROJECT_CHOICE", "kept")
    result = run_provider_command(
        ProviderCommandOptions(root=tmp_path, review_pack_path=pack, command=[sys.executable, str(script)])
    )
    assert result.status == "success"
    assert result.invocation.completion_proof.require_complete()["launch_status"] == "started"

@pytest.mark.skipif(os.name == "nt", reason="POSIX 相对可执行文件入口；Windows 入口由平台验收覆盖")
def test_provider_resolves_relative_executable_in_review_root(tmp_path):
    pack = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    script.write_text(f"#!{sys.executable}\n" + script.read_text())
    script.chmod(0o755)
    result = run_provider_command(
        ProviderCommandOptions(root=tmp_path, review_pack_path=pack, command=["./reviewer.py"])
    )
    assert result.status == "success"
    assert result.invocation.argv[0] == "./reviewer.py"


@pytest.mark.skipif(os.name == "nt", reason="POSIX 相对 PATH 入口；Windows 入口由平台验收覆盖")
def test_provider_resolves_relative_path_entry_in_review_root(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    _write_file(tmp_path, "src/app.py", "print('base')\n")
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    script.write_text(f"#!{sys.executable}\n" + script.read_text())
    script.chmod(0o755)
    bin_dir = tmp_path / "review-tools"
    bin_dir.mkdir()
    script.rename(bin_dir / "reviewer.py")
    _git(tmp_path, "add", "src/app.py", "review-tools/reviewer.py")
    _git(tmp_path, "commit", "-m", "initial")
    pack = _write_review_pack(tmp_path)
    monkeypatch.setenv("PATH", "review-tools" + os.pathsep + os.environ.get("PATH", os.defpath))
    result = run_provider_command(
        ProviderCommandOptions(root=tmp_path, review_pack_path=pack, command=["reviewer.py"])
    )
    assert result.status == "success"
    assert result.invocation.argv[0] == "reviewer.py"


@pytest.mark.parametrize("entries", [3, 4])
def test_v11_complete_workspace_scan_preserves_exact_limit(tmp_path, entries):
    directory = tmp_path / "ignored"
    directory.mkdir()
    for index in range(entries):
        (directory / f"note-{index}").write_text(str(index))
    first = pr_review_provider._ignored_dir_digest(directory, max_entries=4, require_complete=True)
    assert len(first) == 64
    assert first == pr_review_provider._ignored_dir_digest(directory, max_entries=4, require_complete=True)


@pytest.mark.parametrize("exact_entries", [False, True])
def test_v11_complete_workspace_scan_rejects_excess_including_host_entries(tmp_path, exact_entries):
    directory = tmp_path / "ignored"
    directory.mkdir()
    for index in range(5):
        (directory / f"note-{index}").write_text(str(index))
    declared = {p: "file" for p in directory.iterdir()} if exact_entries else None
    with pytest.raises(pr_review_provider.WorktreeSnapshotError, match="limit"):
        pr_review_provider._ignored_dir_digest(directory, max_entries=4,
            require_complete=True, exact_host_entries=declared)


def test_v11_complete_workspace_neutral_directories_keep_unknown_children(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    known = archive / "known.json"
    known.write_text("first")
    declared = {archive: "directory", known: "file"}
    before = pr_review_provider._ignored_dir_digest(tmp_path, max_entries=16,
                require_complete=True, exact_host_entries=declared)
    known.write_text("legitimate exact replacement")
    assert pr_review_provider._ignored_dir_digest(tmp_path, max_entries=16,
                require_complete=True, exact_host_entries=declared) == before
    (archive / "unexpected.json").write_text("ordinary new entry")
    assert pr_review_provider._ignored_dir_digest(tmp_path, max_entries=16,
                require_complete=True, exact_host_entries=declared) != before


@pytest.mark.parametrize("failure", ["head", "source", "allowlist", "guard", "final-guard", "dirty", "snapshot"])
def test_v12_prelaunch_bound_failure_has_current_never_started_receipt(tmp_path, monkeypatch, failure):
    review_pack_path = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    command = [sys.executable, str(script)]
    options = dict(root=tmp_path, review_pack_path=review_pack_path, command=command)
    hooks = {"head": "_reviewed_head_launch_blocker", "source": "_reviewed_diff_source_launch_blocker",
             "allowlist": "_reviewer_allowlist_launch_blocker", "dirty": "_preexisting_dirty_worktree_blocker"}
    if failure in hooks:
        monkeypatch.setattr(pr_review_provider, hooks[failure], lambda *a, **k: "ordinary preflight condition")
    elif failure == "final-guard":
        guard_calls = 0

        def unavailable_at_launch():
            nonlocal guard_calls
            guard_calls += 1
            if guard_calls == 2:
                raise pr_review_provider.WorktreeSnapshotError(
                    "ordinary final preflight read unavailable"
                )

        options["pre_launch_guard"] = unavailable_at_launch
    else:
        def unavailable(*a, **k):
            raise pr_review_provider.WorktreeSnapshotError("ordinary preflight read unavailable")
        if failure == "guard":
            options["pre_launch_guard"] = unavailable
        else:
            monkeypatch.setattr(pr_review_provider, "_worktree_snapshot", unavailable)
    def must_not_start(*a, **k):
        pytest.fail("preflight failure started the provider")
    monkeypatch.setattr(pr_review_provider, "run_controlled_process", must_not_start)
    result = run_provider_command(ProviderCommandOptions(**options))
    assert result.status == ProviderRunStatus.BLOCKED
    original = Path(result.invocation_path).read_bytes()
    invocation = ProviderRunnerInvocation.model_validate_json(original)
    assert invocation.preflight_incomplete is True
    assert invocation.launch_status == "never_started" and invocation.completion_proof is None
    assert invocation.workspace_check.status == "unproven"
    assert invocation.workspace_check.review_pack_digest == hashlib.sha256(review_pack_path.read_bytes()).hexdigest()
    assert invocation.argv[:2] == command
    assert not Path(result.findings_path).exists()
    if failure == "final-guard":
        assert guard_calls == 2


def test_withdrawn_snapshot_mode_preserves_artifacts_without_launch(tmp_path, monkeypatch):
    review_pack_path = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    for name in ("reviewer-invocation.json", "findings.json", "schema-validation.json"):
        (review_pack_path.parent / name).write_bytes(b"original retained evidence\n")
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }

    def must_not_start(*args, **kwargs):
        pytest.fail("unsupported mode started the provider")

    monkeypatch.setattr(pr_review_provider, "run_controlled_process", must_not_start)
    result = run_provider_command(ProviderCommandOptions(
        root=tmp_path, review_pack_path=review_pack_path,
        command=[sys.executable, str(script)], snapshot_max_entries=4097,
    ))
    assert result.status == ProviderRunStatus.BLOCKED
    assert result.blocker == "Historical provider recovery and workspace adoption are unsupported."
    # 不支持的能力在当前调用建立之前拒绝，不能覆盖旧原件或伪称新回执已生成。
    assert result.invocation_path == result.findings_path == result.schema_validation_path == ""
    assert result.invocation is None and result.findings is None
    assert before == {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }


def test_final_prelaunch_guard_does_not_swallow_unrelated_runtime_error(tmp_path, monkeypatch):
    review_pack_path = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    guard_calls = 0
    failure = RuntimeError("unrelated guard programming error")

    def guard():
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 2:
            raise failure

    def must_not_start(*args, **kwargs):
        pytest.fail("failed final guard started the provider")

    monkeypatch.setattr(pr_review_provider, "run_controlled_process", must_not_start)
    with pytest.raises(RuntimeError, match="unrelated guard programming error") as raised:
        run_provider_command(
            ProviderCommandOptions(
                root=tmp_path,
                review_pack_path=review_pack_path,
                command=[sys.executable, str(script)],
                pre_launch_guard=guard,
            )
        )
    assert raised.value is failure
    assert guard_calls == 2
    assert not (review_pack_path.parent / "reviewer-invocation.json").exists()


@pytest.mark.parametrize(
    ("stage", "error_type"),
    [(stage, error_type) for stage in ("first", "final")
     for error_type in (ValueError, OSError, pr_review_provider.WorktreeSnapshotError)],
    ids=[f"{stage}-{error_type.__name__}" for stage in ("first", "final")
         for error_type in (ValueError, OSError, pr_review_provider.WorktreeSnapshotError)],
)
def test_prelaunch_guard_expected_errors_share_never_started_receipt(tmp_path, monkeypatch, stage, error_type):
    review_pack_path = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    command = [sys.executable, str(script)]
    calls = 0
    fail_at = 1 if stage == "first" else 2

    def guard():
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise error_type("ordinary expected guard failure")

    def must_not_start(*args, **kwargs):
        pytest.fail("expected guard failure started the provider")

    monkeypatch.setattr(pr_review_provider, "run_controlled_process", must_not_start)
    result = run_provider_command(ProviderCommandOptions(
        root=tmp_path, review_pack_path=review_pack_path, command=command,
        pre_launch_guard=guard,
    ))
    assert calls == fail_at and result.status == ProviderRunStatus.BLOCKED
    invocation = ProviderRunnerInvocation.model_validate_json(Path(result.invocation_path).read_bytes())
    assert invocation.preflight_incomplete is True
    assert invocation.launch_status == "never_started" and invocation.completion_proof is None
    assert invocation.workspace_check.status == "unproven"
    assert invocation.workspace_check.review_pack_digest == hashlib.sha256(review_pack_path.read_bytes()).hexdigest()
    assert invocation.argv[:2] == command
    assert not Path(result.findings_path).exists()


@pytest.mark.parametrize("source_mode", ["filesystem", "head", "base", "deleted"])
@pytest.mark.parametrize("sample_kind", ["placeholder", "environment"])
@pytest.mark.parametrize("extra_comment", [False, True])
def test_fix26_sample_exemption_keeps_comment_credentials_high_risk(
    tmp_path, source_mode, sample_kind, extra_comment,
):
    from ai_sdlc.core.loop_models import LoopPolicyProfile
    from ai_sdlc.core.pr_review_redaction import analyze_redaction

    samples = {
        "placeholder": 'api_key = "abcdefghijklmnop"\n',
        "environment": "api_key = get_from_env()\n",
    }
    sample = samples[sample_kind]
    if extra_comment:
        # 全部样本均为合成；分片构造使测试源码本身不携带完整凭据赋值。
        credential_value = "opaque-value-not-a-placeholder"
        keyword = "token" if sample_kind == "placeholder" else "password"
        expression = '"abcdefghijklmnop"' if sample_kind == "placeholder" else "get_from_env()"
        sample = ('api_key = (\n # ' + keyword + ' = "' + credential_value
                  + '"\n ' + expression + '\n)')
    source = "# 中文前缀\f\n标记 = '中文'; " + repr(sample)
    path = "sample.py"
    target = tmp_path / path
    target.write_text(source, encoding="utf-8")
    options = {}
    if source_mode == "head":
        options["head_file_bytes"] = {path: source.encode()}
    elif source_mode == "base":
        options["head_file_bytes"] = {path: b"print('safe')\n"}
        options["base_file_bytes"] = {path: source.encode()}
    elif source_mode == "deleted":
        target.unlink()
        options["deleted_file_bytes"] = {path: source.encode()}
    report = analyze_redaction(
        tmp_path, [path], policy=LoopPolicyProfile(high_risk_secret_policy="forbid"),
        code_egress=True, code_egress_confirmed=True, **options,
    )
    if extra_comment:
        assert report.blocked is True and report.included_files == []
        assert report.high_risk_secret_files == report.redacted_files == [path]
        assert report.decisions[0].redacted_occurrences == 1
    else:
        assert report.blocked is False and report.included_files == [path]
        assert report.high_risk_secret_files == report.redacted_files == report.omitted_files == []
    if source_mode != "deleted":
        assert target.read_bytes() == source.encode()


@pytest.mark.parametrize("never_started", [False, True])
def test_raw_publication_original_is_retained_without_promoting_provider_success(
    tmp_path, monkeypatch, never_started
):
    from ai_sdlc.core.loop_artifacts import LoopArtifactStore
    from ai_sdlc.core.quality_command import _CONTROLLED_LAUNCHER

    pack = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    write = LoopArtifactStore.write_bytes_artifact
    popen = subprocess.Popen
    observed = {"failures": 0, "proof_root": None}

    def fail_raw_once(store, path, content, **kwargs):
        path = Path(path)
        if path.parent.name.startswith("ai-sdlc-provider-owned-"):
            observed["proof_root"] = path.parent
            if path.name == "raw-result.json" and not observed["failures"]:
                observed["failures"] += 1
                raise OSError("single raw publication failure")
        return write(store, path, content, **kwargs)

    def fail_launch(args, *pargs, **kwargs):
        if never_started and _CONTROLLED_LAUNCHER in args:
            raise OSError("original controlled launch failed before creation")
        return popen(args, *pargs, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(LoopArtifactStore, "write_bytes_artifact", fail_raw_once)
        fault.setattr(subprocess, "Popen", fail_launch)
        result = run_provider_command(ProviderCommandOptions(
            root=tmp_path, review_pack_path=pack, command=[sys.executable, str(script)],
        ))
    assert observed["failures"] == 1 and not observed["proof_root"].exists()
    assert result.status == "blocked" and result.findings is None
    if never_started:
        assert "original controlled launch failed before creation" in result.blocker
    else:
        assert "single raw publication failure" in result.blocker
    original_bytes = Path(result.invocation_path).read_bytes()
    cold = ProviderRunnerInvocation.model_validate_json(original_bytes)
    assert cold.status == "blocked"
    assert cold.launch_status == ("never_started" if never_started else "started")
    proof = cold.completion_proof
    assert proof is not None
    assert set(proof.originals) == ({"cleanup.json"} if never_started else {"cleanup.json", "process.json"})
    raw = proof.require_complete()
    assert raw["exit_code"] == cold.exit_code == (None if never_started else 0)
    assert bool(raw["launch_error"]) == never_started
    # 首异常保留在主诊断；次生发布失败仍须能从真实冷读原件追踪。
    cleanup = json.loads(proof.originals["cleanup.json"])
    assert cleanup["raw_result_persistence_error"] == "OSError: single raw publication failure"
    if never_started:
        assert raw["launch_error"] == "original controlled launch failed before creation"
    # 缺失的独立文件仍缺失；相同持久化原件的损坏不能被cleanup备份宽松吞掉。
    import copy
    for damage in ("missing-cleanup", "byte-drift", "identity-drift", "conflicting-raw", "missing-backup"):
        candidate = copy.deepcopy(json.loads(original_bytes))
        originals = candidate["completion_proof"]["originals"]
        hashes = candidate["completion_proof"]["sha256"]
        if damage == "missing-cleanup":
            originals.pop("cleanup.json")
            hashes.pop("cleanup.json")
        elif damage == "byte-drift":
            originals["cleanup.json"] += " "
        elif damage == "conflicting-raw":
            contradictory = dict(raw, exit_code=37)
            originals["raw-result.json"] = json.dumps(contradictory)
            hashes["raw-result.json"] = hashlib.sha256(originals["raw-result.json"].encode()).hexdigest()
        else:
            cleanup = json.loads(originals["cleanup.json"])
            if damage == "identity-drift":
                cleanup["ownership_nonce"] = "another-owned-call"
            else:
                cleanup.pop("raw_result_original")
            originals["cleanup.json"] = json.dumps(cleanup)
            hashes["cleanup.json"] = hashlib.sha256(originals["cleanup.json"].encode()).hexdigest()
        damaged = ProviderRunnerInvocation.model_validate(candidate)
        with pytest.raises(ValueError):
            damaged.completion_proof.require_complete()
    assert Path(result.invocation_path).read_bytes() == original_bytes


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("exit_code,verdict", [(0, "clean"), (10, "changes_required")])
def test_postlaunch_output_exception_persists_before_original_exception(
    tmp_path, monkeypatch, error_type, exit_code, verdict
):
    pack = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=exit_code, verdict=verdict)
    failure = error_type("original post-launch failure")
    observed = _observe_provider_output_read(monkeypatch, fault_stream="stdout", error=failure)
    callbacks = []

    def persist_current(result):
        persisted = ProviderRunnerInvocation.model_validate_json(Path(result.invocation_path).read_bytes())
        assert result.invocation == persisted and result.findings is None
        assert observed["proof_root"].is_dir()
        assert persisted.completion_proof.require_complete()["exit_code"] == exit_code
        callbacks.append(result)

    with pytest.raises(error_type) as raised:
        run_provider_command(ProviderCommandOptions(
            root=tmp_path, review_pack_path=pack, command=[sys.executable, str(script)],
            on_execution_failure=persist_current,
        ))
    assert raised.value is failure and observed["injected"] == 1
    assert len(callbacks) == 1 and callbacks[0].status == "blocked"
    path = pack.parent / "reviewer-invocation.json"
    saved = path.read_bytes()
    cold = ProviderRunnerInvocation.model_validate_json(saved)
    assert cold.launch_status == "started" and cold.status == "blocked"
    assert cold.exit_code == exit_code and cold.workspace_check.status == "unchanged"
    assert cold.execution_failure.exception_type == error_type.__name__
    assert cold.execution_failure.message == str(failure)
    diagnostics = (pack.parent / "findings.json").read_bytes()
    assert cold.execution_failure.findings_status == "present"
    assert cold.execution_failure.findings_sha256 == hashlib.sha256(diagnostics).hexdigest()
    assert json.loads(diagnostics)["verdict"] == verdict
    assert {name: raw.encode("utf-8") for name, raw in cold.completion_proof.originals.items()} == observed["originals"]
    assert not observed["proof_root"].exists()
    assert path.read_bytes() == saved
    for changed in ({"status": "passed"}, {"launch_status": "never_started"}):
        with pytest.raises(ValueError, match="started blocked"):
            ProviderRunnerInvocation.model_validate({**json.loads(saved), **changed})


@pytest.mark.parametrize("stage", ["reader-start", "wait"])
def test_real_postlaunch_start_or_wait_exception_keeps_actual_originals(tmp_path, monkeypatch, stage):
    from ai_sdlc.core import quality_command as quality

    pack = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    script.write_text("import time\ntime.sleep(30)\n" + script.read_text())
    failure = RuntimeError("actual reader start failure") if stage == "reader-start" else KeyboardInterrupt("actual wait interrupted")
    real_start, real_sleep = quality.threading.Thread.start, quality.time.sleep
    real_write = LoopArtifactStore.write_bytes_artifact
    real_popen, real_open = quality.subprocess.Popen, Path.open
    observed = {"injected": 0, "readers_started": 0, "originals": {}, "proof_root": None}
    readers, processes, streams = [], [], []

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        if kwargs.get("stdin") == subprocess.PIPE:
            processes.append(process)
        return process

    def open_output(path, *args, **kwargs):
        stream = real_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if path.parent.name.startswith("ai-sdlc-provider-owned-") and mode == "r+b":
            streams.append(stream)
        return stream

    def write(store, path, content, **kwargs):
        result = real_write(store, path, content, **kwargs)
        path = Path(path)
        if path.parent.name.startswith("ai-sdlc-provider-owned-"):
            observed["proof_root"] = path.parent
            observed["originals"][path.name] = path.read_bytes()
        return result

    def start(reader):
        if getattr(getattr(reader, "_target", None), "__name__", "") == "capture":
            readers.append(reader)
            assert "process.json" in observed["originals"]
            if stage == "reader-start" and not observed["injected"]:
                observed["injected"] += 1
                raise failure
            result = real_start(reader)
            observed["readers_started"] += 1
            return result
        return real_start(reader)

    def sleep(seconds):
        if stage == "wait" and seconds == 0.01 and observed["readers_started"] == 2 and not observed["injected"]:
            observed["injected"] += 1
            raise failure
        return real_sleep(seconds)

    monkeypatch.setattr(LoopArtifactStore, "write_bytes_artifact", write)
    monkeypatch.setattr(quality.threading.Thread, "start", start)
    monkeypatch.setattr(quality.time, "sleep", sleep)
    monkeypatch.setattr(quality.subprocess, "Popen", popen)
    monkeypatch.setattr(Path, "open", open_output)
    try:
        with pytest.raises(type(failure)) as raised:
            run_provider_command(ProviderCommandOptions(
                root=tmp_path, review_pack_path=pack, command=[sys.executable, str(script)], timeout_seconds=5,
            ))
        assert raised.value is failure and observed["injected"] == 1
        cold = ProviderRunnerInvocation.model_validate_json((pack.parent / "reviewer-invocation.json").read_bytes())
        assert cold.status == "blocked" and cold.launch_status == "started"
        if stage == "reader-start":
            # start 未返回无法证明线程不存在；不能把测试知道未启动当成产品终止证明。
            with pytest.raises(ValueError, match="controlled-process-receipt-incomplete-or-conflicting"):
                cold.completion_proof.require_complete()
            assert "raw-result.json" not in cold.completion_proof.originals
            assert json.loads(cold.completion_proof.originals["cleanup.json"])["status"] == "incomplete"
            assert observed["proof_root"].is_dir()
            assert not (observed["proof_root"] / "raw-result.json").exists()
            assert observed["readers_started"] == 0
        else:
            raw = cold.completion_proof.require_complete()
            assert raw["launch_status"] == "started" and raw["exit_code"] == cold.exit_code
            assert not raw["timed_out"]
            assert not observed["proof_root"].exists()
        assert cold.execution_failure.findings_status == "absent"
        assert cold.execution_failure.exception_type == type(failure).__name__
        assert {name: value.encode("utf-8") for name, value in cold.completion_proof.originals.items()} == observed["originals"]
        assert not (pack.parent / "findings.json").exists()
        assert processes and all(process.poll() is not None for process in processes)
    finally:
        observation = {
            "stage": stage,
            "proof_root": str(observed["proof_root"]),
            "proof_root_preserved_by_product": bool(observed["proof_root"] and observed["proof_root"].exists()),
            "readers_alive_at_product_return": [reader.is_alive() for reader in readers],
            "output_streams_closed_at_product_return": [stream.closed for stream in streams],
            "owned_processes_reaped_at_product_return": [process.poll() is not None for process in processes],
            "original_sha256": {name: hashlib.sha256(value).hexdigest() for name, value in observed["originals"].items()},
        }
        # 产品返回事实先保存；随后只释放故障注入留下的测试自有句柄，原件目录保留供冷读。
        receipt = tmp_path / "postlaunch-product-and-test-cleanup.json"
        receipt.write_text(json.dumps(observation, indent=2), encoding="utf-8")
        for reader in readers:
            if reader.ident is not None:
                reader.join(timeout=3)
        if all(not reader.is_alive() for reader in readers):
            for process in processes:
                for name in ("stdin", "stdout", "stderr"):
                    pipe = getattr(process, name)
                    if pipe is not None and not pipe.closed:
                        pipe.close()
            for stream in streams:
                stream.close()
        observation["test_owned_cleanup"] = {
            "readers_alive": [reader.is_alive() for reader in readers],
            "output_streams_closed": [stream.closed for stream in streams],
            "not_product_completion_evidence": True,
        }
        receipt.write_text(json.dumps(observation, indent=2), encoding="utf-8")


@pytest.mark.parametrize("stage", ["invocation-write", "workspace", "callback"])
def test_postlaunch_preservation_failure_retains_only_originals_and_primary_exception(tmp_path, monkeypatch, stage):
    pack = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=0, verdict="clean")
    failure = RuntimeError("primary executed failure")
    secondary = RuntimeError("secondary preservation failure")
    observed = _observe_provider_output_read(monkeypatch, fault_stream="stdout", error=failure)
    real_write = LoopArtifactStore.write_json_artifact

    def write(store, path, payload):
        if Path(path).name == "reviewer-invocation.json":
            raise secondary
        return real_write(store, path, payload)

    def unavailable_workspace(*args, **kwargs):
        raise secondary

    def callback(result):
        assert Path(result.invocation_path).is_file()
        raise secondary

    if stage == "invocation-write":
        monkeypatch.setattr(LoopArtifactStore, "write_json_artifact", write)
    elif stage == "workspace":
        monkeypatch.setattr(pr_review_provider, "_check_provider_workspace", unavailable_workspace)
    try:
        with pytest.raises(RuntimeError) as raised:
            run_provider_command(ProviderCommandOptions(
                root=tmp_path, review_pack_path=pack, command=[sys.executable, str(script)],
                on_execution_failure=callback if stage == "callback" else None,
            ))
        assert raised.value is failure and observed["injected"] == 1
        proof_root = observed["proof_root"]
        assert proof_root.is_dir()
        assert {name: (proof_root / name).read_bytes() for name in observed["originals"]} == observed["originals"]
        notes = "\n".join(failure.__notes__)
        assert str(secondary) in notes and str(proof_root) in notes
        if stage != "invocation-write":
            cold = ProviderRunnerInvocation.model_validate_json((pack.parent / "reviewer-invocation.json").read_bytes())
            assert cold.execution_failure.message == str(failure)
            assert cold.workspace_check.status == ("unproven" if stage == "workspace" else "unchanged")
            assert cold.completion_proof.require_complete()["exit_code"] == 0
    finally:
        # 仅测试在断言保全事实后回收自己观察到的临时目录，不帮助产品补造原件。
        if observed["proof_root"] is not None and observed["proof_root"].exists():
            shutil.rmtree(observed["proof_root"])


@pytest.mark.parametrize("stage,exit_code,verdict", [
    ("normal", 0, "clean"), ("normal", 10, "changes_required"),
    ("schema-report", 0, "clean"), ("schema-report", 10, "changes_required"),
    ("workspace", 0, "clean"), ("invocation-write", 0, "clean"),
    ("temporary-cleanup", 0, "clean"),
])
def test_first_postprocessing_failure_preserves_actual_result(
    tmp_path, monkeypatch, stage, exit_code, verdict
):
    pack = _write_review_pack(tmp_path)
    script = _write_reviewer_script(tmp_path, exit_code=exit_code, verdict=verdict)
    failure = OSError("first actual postprocessing failure")
    observed = _observe_provider_output_read(monkeypatch)
    real_write = LoopArtifactStore.write_json_artifact
    real_workspace = pr_review_provider._check_provider_workspace
    real_remove = pr_review_provider.shutil.rmtree
    injected, callbacks = [], []

    def fail_once():
        assert observed["proof_root"] is not None
        assert json.loads(observed["originals"]["raw-result.json"])["exit_code"] == exit_code
        assert json.loads(observed["originals"]["cleanup.json"])["status"] == "complete"
        injected.append(stage)
        raise failure

    def write(store, path, payload):
        target = "schema-validation.json" if stage == "schema-report" else "reviewer-invocation.json"
        if stage in {"schema-report", "invocation-write"} and Path(path).name == target and not injected:
            fail_once()
        return real_write(store, path, payload)

    def workspace(*args, **kwargs):
        if stage == "workspace" and not injected:
            fail_once()
        return real_workspace(*args, **kwargs)

    def remove(path, *args, **kwargs):
        if stage == "temporary-cleanup" and Path(path) == observed["proof_root"] and not injected:
            fail_once()
        return real_remove(path, *args, **kwargs)

    def persist_current(result):
        persisted = ProviderRunnerInvocation.model_validate_json(Path(result.invocation_path).read_bytes())
        assert result.invocation == persisted and result.findings is None
        assert observed["proof_root"].is_dir()
        callbacks.append(result)

    monkeypatch.setattr(LoopArtifactStore, "write_json_artifact", write)
    monkeypatch.setattr(pr_review_provider, "_check_provider_workspace", workspace)
    monkeypatch.setattr(pr_review_provider.shutil, "rmtree", remove)
    options = ProviderCommandOptions(
        root=tmp_path, review_pack_path=pack, command=[sys.executable, str(script)],
        on_execution_failure=persist_current,
    )
    if stage == "normal":
        result = run_provider_command(options)
        assert result.findings.verdict == verdict and result.exit_code == exit_code
        assert not callbacks and not injected
    else:
        with pytest.raises(OSError) as raised:
            run_provider_command(options)
        assert raised.value is failure and injected == [stage]
        assert len(callbacks) == 1 and callbacks[0].status == "blocked"
    cold = ProviderRunnerInvocation.model_validate_json((pack.parent / "reviewer-invocation.json").read_bytes())
    assert cold.exit_code == exit_code and cold.completion_proof.require_complete()["exit_code"] == exit_code
    assert {name: raw.encode("utf-8") for name, raw in cold.completion_proof.originals.items()} == observed["originals"]
    assert cold.workspace_check.status == ("unproven" if stage == "workspace" else "unchanged")
    diagnostics = (pack.parent / "findings.json").read_bytes()
    assert json.loads(diagnostics)["verdict"] == verdict
    if stage == "normal":
        assert cold.execution_failure is None
    else:
        assert cold.status == "blocked" and cold.execution_failure.exception_type == "OSError"
        assert cold.execution_failure.findings_sha256 == hashlib.sha256(diagnostics).hexdigest()
    if stage == "workspace":
        # 工作区未证明时只保全真实原件；测试最后回收自己已核验的目录。
        assert observed["proof_root"].is_dir()
        assert str(observed["proof_root"]) in "\n".join(failure.__notes__)
        real_remove(observed["proof_root"])
    else:
        assert not observed["proof_root"].exists()


@pytest.mark.parametrize('held_reader,exit_code,verdict', [
    (False, 0, 'clean'), (False, 10, 'changes_required'), (True, 0, 'clean'),
], ids=['normal-0', 'normal-10', 'reader-still-running'])
def test_provider_preserves_unsealed_output_until_ownership_completion(tmp_path, monkeypatch, held_reader, exit_code, verdict):
    import threading
    import time

    from ai_sdlc.core import quality_command as quality

    provider = pr_review_provider
    root = tmp_path / 'project'
    root.mkdir()
    pack = _write_review_pack(root)
    script = _write_reviewer_script(root, exit_code=exit_code, verdict=verdict)
    script.write_text("print('unsealed-output', flush=True)\n" + script.read_text())
    held, release = threading.Event(), threading.Event()
    original_chunks, original_thread = quality._controlled_pipe_chunks, threading.Thread
    original_open, original_mkdtemp = Path.open, provider.tempfile.mkdtemp
    readers, streams, proof_roots, processes, primary_errors = [], [], [], [], []
    original_run, original_popen = provider.run_controlled_process, quality.subprocess.Popen

    def chunks(pipe, stop):
        for chunk in original_chunks(pipe, stop):
            yield chunk
            if held_reader and b'unsealed-output' in chunk:
                held.set()
                release.wait(15)

    def thread(*args, **kwargs):
        reader = original_thread(*args, **kwargs)
        readers.append(reader)
        if held_reader:
            # 相同底层 fixture，只使真实 reader 的等待窗口到期，不替换生产结果。
            monkeypatch.setattr(reader, 'join', lambda timeout=None: time.sleep(0.01))
        return reader

    def opened(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if Path(path).parent.name.startswith('ai-sdlc-provider-owned-') and args and args[0] == 'r+b':
            streams.append(stream)
        return stream

    def mkdtemp(*args, **kwargs):
        kwargs['dir'] = tmp_path
        result = original_mkdtemp(*args, **kwargs)
        proof_roots.append(Path(result))
        return result

    def run(options):
        try:
            return original_run(options)
        except BaseException as exc:
            primary_errors.append(exc)
            raise

    def popen(argv, *args, **kwargs):
        process = original_popen(argv, *args, **kwargs)
        if quality._CONTROLLED_LAUNCHER in argv:
            processes.append(process)
        return process

    monkeypatch.setattr(provider, 'run_controlled_process', run)
    monkeypatch.setattr(quality.subprocess, 'Popen', popen)
    monkeypatch.setattr(quality, '_controlled_pipe_chunks', chunks)
    monkeypatch.setattr(quality.threading, 'Thread', thread)
    monkeypatch.setattr(Path, 'open', opened)
    monkeypatch.setattr(provider.tempfile, 'mkdtemp', mkdtemp)
    observed = {}
    caught = None
    result = None
    try:
        try:
            result = provider.run_provider_command(provider.ProviderCommandOptions(
                root=root, review_pack_path=pack, command=[sys.executable, str(script)], timeout_seconds=5))
        except BaseException as exc:
            caught = exc
        cold = ProviderRunnerInvocation.model_validate_json((pack.parent / 'reviewer-invocation.json').read_bytes())
        observed = {'proof_root': str(proof_roots[-1]), 'proof_root_exists': proof_roots[-1].exists(),
                    'readers_alive': [r.is_alive() for r in readers],
                    'streams_open': [not s.closed for s in streams],
                    'output_paths_exist': [Path(s.name).exists() for s in streams],
                    'output_nlink': [os.fstat(s.fileno()).st_nlink if not s.closed else None for s in streams],
                    'exception': str(caught) if caught else None,
                    'execution_failure': cold.execution_failure.model_dump() if cold.execution_failure else None,
                    'original_names': list(cold.completion_proof.originals), 'status': cold.status}
        (tmp_path / 'before-test-cleanup.json').write_text(json.dumps(observed, indent=2) + '\n')
        if held_reader:
            assert held.is_set() and any(observed['readers_alive'])
            assert isinstance(caught, RuntimeError) and str(caught) == 'controlled-quality-output-readers-not-stopped'
            assert primary_errors == [caught] and cold.execution_failure.message == str(caught)
            assert cold.status == 'blocked' and cold.workspace_check.status == 'unchanged'
            assert set(cold.completion_proof.originals) == {'process.json', 'cleanup.json'}
            with pytest.raises(ValueError):
                cold.completion_proof.require_complete()
            # 核心防护：元数据已读取不能授权删除仍被真实 reader 持有的输出。
            assert observed['proof_root_exists'], observed
            assert all(observed['output_paths_exist']) and all(v > 0 for v in observed['output_nlink'])
            assert str(proof_roots[-1]) in '\n'.join(getattr(caught, '__notes__', []))
        else:
            assert caught is None and result.exit_code == exit_code and result.findings.verdict == verdict
            assert cold.execution_failure is None and cold.completion_proof.require_complete()['exit_code'] == exit_code
            assert not observed['proof_root_exists'] and not any(observed['readers_alive']) and not any(observed['streams_open'])
    finally:
        # 先保留真实失败观察，再释放测试自己的阻断及句柄；不补造 raw/cleanup。
        release.set()
        for reader in readers:
            original_thread.join(reader, timeout=2)
        for stream in streams:
            if not stream.closed:
                stream.close()
        for process in processes:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
        (tmp_path / 'test-cleanup.json').write_text(json.dumps({
            'test_hold_released': True, 'readers_alive': [r.is_alive() for r in readers],
            'all_output_streams_closed': all(s.closed for s in streams),
            'all_direct_children_exited': all(p.poll() is not None for p in processes),
        }, indent=2) + '\n')
        assert all(not reader.is_alive() for reader in readers)
        assert all(p.poll() is not None for p in processes)
