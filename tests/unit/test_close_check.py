"""Focused tests for generic work-item and Local PR close checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ai_sdlc.core.close_check import (
    BranchCheckResult,
    CloseCheckResult,
    _changed_paths_from_marker,
    _local_pr_review_close_check_summary,
    format_branch_check_json,
    format_close_check_json,
    run_branch_check,
    run_close_check,
)


def _write_project(root: Path) -> Path:
    (root / ".ai-sdlc").mkdir()
    work_item = root / "specs" / "001-generic"
    work_item.mkdir(parents=True)
    (work_item / "tasks.md").write_text(
        "# Tasks\n\n- [ ] Finish retained behavior\n",
        encoding="utf-8",
    )
    return work_item


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_local_review(root: Path, *, verdict: str = "fully_clean") -> None:
    review_id = "review-generic"
    loop_id = "loop-review-generic"
    review_dir = root / ".ai-sdlc" / "reviews" / "pr" / review_id
    review_dir.mkdir(parents=True)
    report = review_dir / "final-report.md"
    report.write_text(f"# Final\n\nverdict: {verdict}\n", encoding="utf-8")
    pack = review_dir / "review-pack.json"
    pack.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "artifact_kind": "review-pack",
                "review_id": review_id,
                "loop_id": loop_id,
                "repo_root": str(root),
                "base_ref": "main",
                "head_ref": "HEAD",
                "base_commit": "0" * 40,
                "head_commit": "a" * 40,
                "changed_files": ["src/app.py"],
                "diff_path": (review_dir / "diff.patch").relative_to(root).as_posix(),
                "policy_decisions": {"incomplete_review_waiver": False},
                "model_selector": "current",
            }
        ),
        encoding="utf-8",
    )
    findings = review_dir / "findings.json"
    findings.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "artifact_kind": "review-findings",
                "review_id": review_id,
                "loop_id": loop_id,
                "review_pack_path": pack.relative_to(root).as_posix(),
                "provider_id": "local-reviewer",
                "model_selector": "current",
                "resolved_model": "local-reviewer",
                "verdict": "clean",
                "findings": [],
            }
        ),
        encoding="utf-8",
    )
    run = review_dir / "review-run.json"
    run.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "review_id": review_id,
                "loop_id": loop_id,
                "status": "closed" if verdict == "fully_clean" else "blocked",
                "verdict": verdict,
                "final_report_path": report.relative_to(root).as_posix(),
                "unresolved_blockers": 0 if verdict == "fully_clean" else 1,
                "unresolved_required": 0,
                "review_pack_path": pack.relative_to(root).as_posix(),
                "review_pack_digest": _sha256(pack),
                "findings_path": findings.relative_to(root).as_posix(),
                "findings_digest": _sha256(findings),
            }
        ),
        encoding="utf-8",
    )
    pointer = root / ".ai-sdlc" / "reviews" / "pr" / "current-review.json"
    pointer.write_text(
        json.dumps(
            {
                "review_id": review_id,
                "review_run_path": run.relative_to(root).as_posix(),
            }
        ),
        encoding="utf-8",
    )


def test_local_pr_review_summary_accepts_only_closed_clean_review(tmp_path: Path) -> None:
    _write_project(tmp_path)
    _write_local_review(tmp_path)

    summary = _local_pr_review_close_check_summary(tmp_path)

    assert summary["ok"] is True
    assert summary["verdict"] == "fully_clean"


def test_local_pr_review_summary_blocks_unresolved_review(tmp_path: Path) -> None:
    _write_project(tmp_path)
    _write_local_review(tmp_path, verdict="blocked")

    summary = _local_pr_review_close_check_summary(tmp_path)

    assert summary["ok"] is False
    assert summary["verdict"] == "blocked"


def test_close_check_reports_generic_task_and_log_blockers(tmp_path: Path) -> None:
    work_item = _write_project(tmp_path)

    result = run_close_check(cwd=tmp_path, wi=work_item)

    assert result.ok is False
    assert any("unchecked checklist" in blocker for blocker in result.blockers)
    assert any("task-execution-log.md" in blocker for blocker in result.blockers)
    local_review = next(
        check for check in result.checks if check["name"] == "local_pr_review"
    )
    assert local_review["ok"] is True
    assert all("program" not in blocker.lower() for blocker in result.blockers)
    assert all("provenance" not in blocker.lower() for blocker in result.blockers)


def test_close_check_reports_non_project_without_raising(tmp_path: Path) -> None:
    result = run_close_check(cwd=tmp_path, wi=Path("specs/001-missing"))

    assert result.ok is False
    assert result.error is not None
    assert "Not inside" in result.error


def test_branch_check_reports_non_project_without_raising(tmp_path: Path) -> None:
    result = run_branch_check(cwd=tmp_path, wi=Path("specs/001-missing"))

    assert result.ok is False
    assert result.error is not None


def test_changed_paths_and_result_payloads_are_deduplicated() -> None:
    assert _changed_paths_from_marker("`src/a.py`、`src/a.py`、`docs/a.md`") == [
        "src/a.py",
        "docs/a.md",
    ]
    close = CloseCheckResult(ok=False, blockers=["same", "same"])
    branch = BranchCheckResult(
        ok=False,
        blockers=["same", "same"],
        warnings=["warn", "warn"],
        next_required_actions=["next", "next"],
    )

    assert json.loads(format_close_check_json(close))["blockers"] == ["same"]
    branch_payload = json.loads(format_branch_check_json(branch))
    assert branch_payload["blockers"] == ["same"]
    assert branch_payload["warnings"] == ["warn"]
    assert branch_payload["next_required_actions"] == ["next"]
