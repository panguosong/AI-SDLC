"""原生 PR 审查的 provider 超时透传与无效参数保护。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_sdlc.core.pr_review_service import (
    PRReviewCommandStatus,
    PRReviewStartOptions,
    rerun_pr_review,
    start_pr_review,
)


@pytest.mark.parametrize(
    ("provider_id", "timeout", "expected"),
    [("local-agent", None, 60.0), ("local-agent", 180.5, 180.5), ("", 180.5, 180.5)],
)
def test_start_provider_timeout_reaches_real_subprocess(
    tmp_path: Path, provider_id: str, timeout: float | None, expected: float
) -> None:
    base_commit = _init_repo(tmp_path)
    _commit_file(tmp_path, "src/app.py", "print('hello')\n", "add app")
    script = _write_clean_reviewer_script(tmp_path)
    extra = {} if timeout is None else {"provider_timeout_seconds": timeout}

    with patch(
        "ai_sdlc.core.pr_review_provider.subprocess.run", wraps=subprocess.run
    ) as run:
        result = start_pr_review(
            PRReviewStartOptions(
                root=tmp_path,
                base_ref=base_commit,
                provider_id=provider_id,
                current_model="gpt-5",
                provider_command=[sys.executable, str(script)],
                review_id="review-timeout-start",
                **extra,
            )
        )

    assert result.status == PRReviewCommandStatus.STARTED, result.blocker
    assert result.verdict == "clean"
    assert [
        call.kwargs["timeout"]
        for call in run.call_args_list
        if call.args[0][0] == sys.executable
    ] == [expected]


@pytest.mark.parametrize(
    "timeout", [0.0, -1.0, float("nan"), float("inf"), -float("inf")]
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_start_provider_timeout_rejects_invalid_without_artifacts(
    tmp_path: Path, timeout: float, dry_run: bool
) -> None:
    _init_repo(tmp_path)

    result = start_pr_review(
        PRReviewStartOptions(
            root=tmp_path,
            provider_id="mock-reviewer",
            provider_timeout_seconds=timeout,
            dry_run=dry_run,
        )
    )

    assert result.status == PRReviewCommandStatus.NEEDS_USER
    assert "finite and positive" in result.blocker
    assert not (tmp_path / ".ai-sdlc" / "reviews").exists()


@pytest.mark.parametrize(("timeout", "expected"), [(None, 60.0), (200.5, 200.5)])
def test_rerun_provider_timeout_is_explicit_or_default_not_inherited(
    tmp_path: Path, timeout: float | None, expected: float
) -> None:
    base_commit = _init_repo(tmp_path)
    _commit_file(tmp_path, "src/app.py", "print('hello')\n", "add app")
    script = _write_clean_reviewer_script(tmp_path)
    start = start_pr_review(
        PRReviewStartOptions(
            root=tmp_path,
            base_ref=base_commit,
            provider_id="local-agent",
            current_model="gpt-5",
            provider_command=[sys.executable, str(script)],
            provider_timeout_seconds=180.5,
            review_id="review-timeout-rerun",
        )
    )
    assert start.status == PRReviewCommandStatus.STARTED, start.blocker
    _commit_file(tmp_path, "src/app.py", "print('updated')\n", "update app")
    extra = {} if timeout is None else {"provider_timeout_seconds": timeout}

    with patch(
        "ai_sdlc.core.pr_review_provider.subprocess.run", wraps=subprocess.run
    ) as run:
        result = rerun_pr_review(tmp_path, **extra)

    assert result.status == PRReviewCommandStatus.STARTED, result.blocker
    assert result.verdict == "clean"
    assert [
        call.kwargs["timeout"]
        for call in run.call_args_list
        if call.args[0][0] == sys.executable
    ] == [expected]


@pytest.mark.parametrize(
    "timeout", [0.0, -1.0, float("nan"), float("inf"), -float("inf")]
)
def test_rerun_provider_timeout_rejects_invalid_without_mutating_review(
    tmp_path: Path, timeout: float
) -> None:
    base_commit = _init_repo(tmp_path)
    _commit_file(tmp_path, "src/app.py", "print('hello')\n", "add app")
    start = start_pr_review(
        PRReviewStartOptions(
            root=tmp_path,
            base_ref=base_commit,
            provider_id="mock-reviewer",
            review_id="review-timeout-invalid-rerun",
        )
    )
    assert start.status == PRReviewCommandStatus.STARTED
    before = _artifact_tree(tmp_path / ".ai-sdlc")

    result = rerun_pr_review(tmp_path, provider_timeout_seconds=timeout)

    assert result.status == PRReviewCommandStatus.NEEDS_USER
    assert "finite and positive" in result.blocker
    assert _artifact_tree(tmp_path / ".ai-sdlc") == before


def _write_clean_reviewer_script(path: Path) -> Path:
    script = path / "clean_reviewer.py"
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
    return script


def _artifact_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _init_repo(path: Path) -> str:
    (path / ".ai-sdlc").mkdir()
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    _commit_file(path, "README.md", "# Test\n", "initial")
    return _git(path, "rev-parse", "HEAD")


def _commit_file(path: Path, file_path: str, content: str, message: str) -> None:
    target = path / file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(path, "add", file_path)
    _git(path, "commit", "-m", message)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()
