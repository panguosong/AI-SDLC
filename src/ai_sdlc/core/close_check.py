"""Read-only close-stage checks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ai_sdlc.branch.git_client import GitClient, GitError
from ai_sdlc.core.plan_check import resolve_plan_path_from_wi, run_plan_check
from ai_sdlc.core.pr_review_models import ReviewFindings, ReviewPack, ReviewRun
from ai_sdlc.core.pr_review_service import CURRENT_REVIEW_PATH
from ai_sdlc.core.workitem_traceability import (
    analyze_completion_truth,
    evaluate_work_item_branch_lifecycle,
)
from ai_sdlc.utils.helpers import find_project_root

REQUIRED_LOG_MARKERS = (
    "统一验证命令",
    "代码审查",
    "任务/计划同步状态",
)
DOCS_UNIMPLEMENTED_HINTS = ("未实现前", "未来可能提供")
COMMIT_STATUS_RE = re.compile(
    r"(?m)^\s*-\s*\*\*已完成 git 提交\*\*：(?P<value>.+?)\s*$"
)
COMMIT_HASH_RE = re.compile(r"(?m)^\s*-\s*\*\*提交哈希\*\*：(?P<value>.+?)\s*$")
VERIFICATION_PROFILE_RE = re.compile(
    r"(?m)^\s*-\s*\*\*验证画像\*\*：(?P<value>.+?)\s*$"
)
CHANGED_PATHS_RE = re.compile(r"(?m)^\s*-\s*\*\*改动范围\*\*：(?P<value>.+?)\s*$")
PATH_TOKEN_RE = re.compile(r"`([^`]+)`|\[([^\]]+)\]\([^)]+\)")
VerificationCommandRequirement = str | tuple[str, ...]
VERIFICATION_PROFILE_REQUIRED_COMMANDS: dict[
    str, tuple[VerificationCommandRequirement, ...]
] = {
    "docs-only": ("uv run ai-sdlc verify constraints",),
    "rules-only": ("uv run ai-sdlc verify constraints",),
    "truth-only": ("uv run ai-sdlc verify constraints",),
    "code-change": (
        "uv run pytest",
        "uv run ruff check",
        "uv run ai-sdlc verify constraints",
    ),
}
# Default docs scan = work-item `*.md` plus these repo-relative paths when present.
DOCS_WHITELIST_RELS = (
    Path("docs/pull-request-checklist.zh.md"),
    Path("USER_GUIDE.zh-CN.md"),
)
GIT_CLOSURE_ALLOWED_DIRTY_RELS = (
    ".ai-sdlc/state/checkpoint.yml",
    ".ai-sdlc/state/checkpoint.yml.bak",
    ".ai-sdlc/state/resume-pack.yaml",
)


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _registered_command_strings() -> tuple[str, ...]:
    """Load commands from the Typer tree using a lazy import."""
    from ai_sdlc.cli.command_names import collect_flat_command_strings

    return collect_flat_command_strings()


@dataclass
class CloseCheckResult:
    """Result payload for `workitem close-check`."""

    ok: bool
    blockers: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    wi_dir: Path | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        self.blockers = _dedupe_text_items(self.blockers)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blockers": _dedupe_text_items(self.blockers),
            "checks": self.checks,
            "wi_dir": str(self.wi_dir) if self.wi_dir else None,
            "error": self.error,
        }


@dataclass
class BranchCheckResult:
    """Result payload for `workitem branch-check`."""

    ok: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    wi_dir: Path | None = None
    error: str | None = None
    branch_disposition: str | None = None
    worktree_disposition: str | None = None
    next_required_actions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.blockers = _dedupe_text_items(self.blockers)
        self.warnings = _dedupe_text_items(self.warnings)
        self.next_required_actions = _dedupe_text_items(self.next_required_actions)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blockers": _dedupe_text_items(self.blockers),
            "warnings": _dedupe_text_items(self.warnings),
            "entries": self.entries,
            "wi_dir": str(self.wi_dir) if self.wi_dir else None,
            "error": self.error,
            "branch_disposition": self.branch_disposition,
            "worktree_disposition": self.worktree_disposition,
            "next_required_actions": _dedupe_text_items(self.next_required_actions),
        }


def _dedupe_text_items(values: object) -> list[str]:
    deduped: list[str] = []
    for value in values or []:
        normalized = str(value).strip()
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return deduped


def _unchecked_tasks_count(tasks_md: str) -> int:
    return len(re.findall(r"(?m)^\s*-\s*\[\s\]\s+", tasks_md))


def _docs_scan_targets(root: Path, wi_dir: Path, *, all_docs: bool) -> list[Path]:
    """Use work-item markdown plus the allowlist; --all-docs adds docs/**."""
    paths: list[Path] = []
    paths.extend(p for p in sorted(wi_dir.glob("*.md")) if p.is_file())
    for rel in DOCS_WHITELIST_RELS:
        fp = root / rel
        if fp.is_file():
            paths.append(fp)
    if all_docs:
        docs_dir = root / "docs"
        if docs_dir.is_dir():
            paths.extend(sorted(docs_dir.rglob("*.md")))

    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in paths:
        key = p.resolve()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _docs_consistency_violations(
    root: Path, wi_dir: Path, *, all_docs: bool
) -> list[str]:
    """Doc paths that pair unimplemented wording with a real CLI command string."""
    violations: list[str] = []
    cmds = _registered_command_strings()
    for md in _docs_scan_targets(root, wi_dir, all_docs=all_docs):
        text = md.read_text(encoding="utf-8")
        has_hint = any(hint in text for hint in DOCS_UNIMPLEMENTED_HINTS)
        if not has_hint:
            continue
        for cmd in cmds:
            if cmd in text:
                violations.append(f"{md}: contains '{cmd}' with unimplemented wording")
                break
    return violations


def _last_log_marker(pattern: re.Pattern[str], text: str) -> str | None:
    matches = list(pattern.finditer(text))
    if not matches:
        return None
    return matches[-1].group("value").strip()


def _latest_batch_text(log_text: str) -> str:
    matches = list(re.finditer(r"(?m)^### Batch .+$", log_text))
    if not matches:
        return log_text
    return log_text[matches[-1].start() :]


def _normalize_marker_value(value: str | None) -> str:
    if value is None:
        return ""
    return value.replace("`", "").strip()


def _changed_paths_from_marker(value: str) -> list[str]:
    paths: list[str] = []
    for match in PATH_TOKEN_RE.finditer(value):
        token = match.group(1) or match.group(2) or ""
        normalized = token.strip()
        if normalized:
            paths.append(normalized)
    return _dedupe_strings(paths)


def _path_allowed_for_docs_profile(path: str) -> bool:
    normalized = path.strip()
    return normalized.endswith(".md")


def _path_allowed_for_truth_profile(path: str) -> bool:
    normalized = path.strip().replace("\\", "/")
    if normalized.startswith(".ai-sdlc/"):
        return True
    if normalized.startswith("specs/") and normalized.endswith(".md"):
        return True
    return normalized.startswith("docs/") and normalized.endswith(".md")


def _command_requirement_present(
    batch_text: str,
    requirement: VerificationCommandRequirement,
) -> bool:
    if isinstance(requirement, tuple):
        return any(command in batch_text for command in requirement)
    return requirement in batch_text


def _command_requirement_label(requirement: VerificationCommandRequirement) -> str:
    if isinstance(requirement, tuple):
        return " or ".join(requirement)
    return requirement


def _verification_profile_violation(log_text: str) -> str | None:
    batch_text = _latest_batch_text(log_text)
    profile = _normalize_marker_value(
        _last_log_marker(VERIFICATION_PROFILE_RE, batch_text)
    )
    if not profile:
        return "latest batch missing verification profile"
    required_commands = VERIFICATION_PROFILE_REQUIRED_COMMANDS.get(profile)
    if required_commands is None:
        return f"latest batch has unsupported verification profile: {profile}"

    for command in required_commands:
        if not _command_requirement_present(batch_text, command):
            return (
                "latest batch verification profile "
                f"{profile} missing required command: {_command_requirement_label(command)}"
            )

    if profile in {"docs-only", "rules-only"}:
        raw_paths = _last_log_marker(CHANGED_PATHS_RE, batch_text)
        paths = _changed_paths_from_marker(raw_paths or "")
        if not paths:
            return f"latest batch verification profile {profile} missing changed-path scope"
        disallowed = [
            path for path in paths if not _path_allowed_for_docs_profile(path)
        ]
        if disallowed:
            return (
                f"latest batch verification profile {profile} includes non-doc changes: "
                + ", ".join(_dedupe_text_items(disallowed)[:5])
            )
    if profile == "truth-only":
        raw_paths = _last_log_marker(CHANGED_PATHS_RE, batch_text)
        paths = _changed_paths_from_marker(raw_paths or "")
        if not paths:
            return "latest batch verification profile truth-only missing changed-path scope"
        disallowed = [
            path for path in paths if not _path_allowed_for_truth_profile(path)
        ]
        if disallowed:
            return (
                "latest batch verification profile truth-only includes non-truth changes: "
                + ", ".join(_dedupe_text_items(disallowed)[:5])
            )

    return None


def _git_closure_violation(root: Path, log_text: str) -> str | None:
    commit_status = _last_log_marker(COMMIT_STATUS_RE, log_text)
    commit_hash = _last_log_marker(COMMIT_HASH_RE, log_text)
    if commit_status is None or commit_hash is None:
        return (
            "task-execution-log.md missing git close-out markers for the latest batch"
        )
    if not commit_status.startswith("是"):
        return "latest batch is not marked as git committed"
    normalized_hash = commit_hash.replace("`", "").strip()
    if normalized_hash in {"", "N/A"}:
        return "latest batch is missing a committed git hash"
    try:
        if _has_uncommitted_changes_excluding_allowed(root):
            return "git working tree has uncommitted changes; close-out is not fully committed"
    except GitError as exc:
        return f"unable to inspect git closure state: {exc}"
    return None


def _has_uncommitted_changes_excluding_allowed(root: Path) -> bool:
    client = GitClient(root)
    status = client._run("status", "--porcelain", "--untracked-files=all")
    lines = [line for line in status.splitlines() if line.strip()]
    if not lines:
        return False
    allowed = {path.replace("\\", "/") for path in GIT_CLOSURE_ALLOWED_DIRTY_RELS}
    for line in lines:
        parts = line.split(maxsplit=1)
        if len(parts) == 1:
            return True
        path = parts[1]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        normalized = path.strip().replace("\\", "/")
        if normalized not in allowed:
            return True
    return False


def run_branch_check(*, cwd: Path | None, wi: Path) -> BranchCheckResult:
    """Run read-only work-item scoped branch lifecycle checks."""
    start = (cwd or Path.cwd()).resolve()
    root = find_project_root(start)
    if root is None:
        return BranchCheckResult(
            ok=False,
            error="Not inside an AI-SDLC project (.ai-sdlc/ not found).",
        )

    wi_dir = wi if wi.is_absolute() else (start / wi).resolve()
    if not wi_dir.is_dir():
        return BranchCheckResult(
            ok=False,
            wi_dir=wi_dir,
            error=f"Work item directory not found: {wi_dir}",
        )

    exec_log = wi_dir / "task-execution-log.md"
    log_text = exec_log.read_text(encoding="utf-8") if exec_log.is_file() else None
    lifecycle = evaluate_work_item_branch_lifecycle(
        root=root,
        wi_dir=wi_dir,
        log_text=log_text,
        _require_final_branch_disposition=False,
    )
    return BranchCheckResult(
        ok=lifecycle.ok,
        blockers=lifecycle.blockers,
        warnings=lifecycle.warnings,
        entries=[item.to_json_dict() for item in lifecycle.entries],
        wi_dir=wi_dir,
        error=None,
        branch_disposition=lifecycle.branch_disposition,
        worktree_disposition=lifecycle.worktree_disposition,
        next_required_actions=_dedupe_text_items(lifecycle.next_required_actions),
    )


def run_close_check(
    *,
    cwd: Path | None,
    wi: Path,
    all_docs: bool = False,
) -> CloseCheckResult:
    """Run read-only close checks for a `specs/<WI>/` directory.

    When ``all_docs`` is False (default), docs consistency only scans ``specs/<WI>/*.md``
    plus the paths in ``DOCS_WHITELIST_RELS``. Set ``all_docs=True`` for a full
    ``docs/**/*.md`` scan.
    """
    start = (cwd or Path.cwd()).resolve()
    root = find_project_root(start)
    if root is None:
        return CloseCheckResult(
            ok=False,
            error="Not inside an AI-SDLC project (.ai-sdlc/ not found).",
        )

    wi_dir = wi if wi.is_absolute() else (start / wi).resolve()
    if not wi_dir.is_dir():
        return CloseCheckResult(
            ok=False,
            wi_dir=wi_dir,
            error=f"Work item directory not found: {wi_dir}",
        )

    blockers: list[str] = []
    checks: list[dict[str, Any]] = []

    tasks_file = wi_dir / "tasks.md"
    if not tasks_file.is_file():
        blockers.append(f"BLOCKER: tasks.md not found: {tasks_file}")
        checks.append(
            {"name": "tasks_completion", "ok": False, "detail": "tasks.md missing"}
        )
    else:
        tasks_text = tasks_file.read_text(encoding="utf-8")
        unchecked = _unchecked_tasks_count(tasks_text)
        tasks_ok = unchecked == 0
        checks.append(
            {
                "name": "tasks_completion",
                "ok": tasks_ok,
                "detail": "all checklist items done"
                if tasks_ok
                else f"{unchecked} unchecked item(s)",
            }
        )
        if not tasks_ok:
            blockers.append(
                f"BLOCKER: tasks.md has {unchecked} unchecked checklist item(s)."
            )

    related_plan_path = resolve_plan_path_from_wi(root, wi_dir)
    if related_plan_path is None:
        checks.append(
            {
                "name": "related_plan_drift",
                "ok": True,
                "detail": "no related_plan declared; skipped",
            }
        )
    elif not related_plan_path.is_file():
        checks.append(
            {
                "name": "related_plan_drift",
                "ok": False,
                "detail": f"related_plan not found: {related_plan_path}",
            }
        )
        blockers.append(f"BLOCKER: related_plan file not found: {related_plan_path}")
    else:
        plan = run_plan_check(cwd=start, wi=None, plan=related_plan_path)
        drift_ok = (plan.error is None) and (not plan.drift)
        checks.append(
            {
                "name": "related_plan_drift",
                "ok": drift_ok,
                "detail": "no drift" if drift_ok else (plan.error or "drift detected"),
            }
        )
        if not drift_ok:
            blockers.append(
                "BLOCKER: related_plan drift detected (pending todos vs Git changes) or plan-check failed."
            )

    exec_log = wi_dir / "task-execution-log.md"
    if not exec_log.is_file():
        checks.append(
            {
                "name": "execution_log_fields",
                "ok": False,
                "detail": "task-execution-log.md missing",
            }
        )
        blockers.append(f"BLOCKER: task-execution-log.md not found: {exec_log}")
    else:
        log_text = exec_log.read_text(encoding="utf-8")
        missing = [marker for marker in REQUIRED_LOG_MARKERS if marker not in log_text]
        log_ok = len(missing) == 0
        checks.append(
            {
                "name": "execution_log_fields",
                "ok": log_ok,
                "detail": "required fields present"
                if log_ok
                else f"missing fields: {', '.join(missing)}",
            }
        )
        if not log_ok:
            blockers.append(
                "BLOCKER: task-execution-log.md missing required close-out fields: "
                + ", ".join(missing)
            )
        review_evidence_ok = "代码审查" in log_text or "review" in log_text.lower()
        review_gate_detail = (
            "review evidence recorded"
            if review_evidence_ok
            else "review evidence missing"
        )
        checks.append(
            {
                "name": "review_gate",
                "ok": review_evidence_ok,
                "detail": review_gate_detail,
            }
        )
        if not review_evidence_ok:
            blockers.append(f"BLOCKER: Review Gate failed: {review_gate_detail}.")
        verification_profile_violation = _verification_profile_violation(log_text)
        verification_profile_ok = verification_profile_violation is None
        checks.append(
            {
                "name": "verification_profile",
                "ok": verification_profile_ok,
                "detail": "latest batch verification profile matches required fresh evidence"
                if verification_profile_ok
                else verification_profile_violation,
            }
        )
        if not verification_profile_ok:
            blockers.append(
                "BLOCKER: verification profile evidence invalid: "
                f"{verification_profile_violation}"
            )
        git_closure_violation = _git_closure_violation(root, log_text)
        git_closure_ok = git_closure_violation is None
        checks.append(
            {
                "name": "git_closure",
                "ok": git_closure_ok,
                "detail": "latest batch marked committed and working tree clean"
                if git_closure_ok
                else git_closure_violation,
            }
        )
        if not git_closure_ok:
            blockers.append(
                f"BLOCKER: git close-out verification failed: {git_closure_violation}"
            )

        traceability = analyze_completion_truth(
            tasks_text if tasks_file.is_file() else "", log_text
        )
        traceability_ok = traceability.ok
        traceability_detail = "planned work matches execution evidence"
        if not traceability_ok:
            traceability_detail = "; ".join(traceability.blockers)
            blockers.extend(traceability.blockers)
        checks.append(
            {
                "name": "completion_truth",
                "ok": traceability_ok,
                "detail": traceability_detail,
            }
        )

        branch_lifecycle = evaluate_work_item_branch_lifecycle(
            root=root,
            wi_dir=wi_dir,
            log_text=log_text,
            _require_final_branch_disposition=True,
        )
        checks.append(
            {
                "name": "branch_lifecycle",
                "ok": branch_lifecycle.ok,
                "detail": branch_lifecycle.summary_detail(),
                "next_required_actions": _dedupe_text_items(
                    branch_lifecycle.next_required_actions
                ),
            }
        )
        blockers.extend(branch_lifecycle.blockers)

    doc_violations = _docs_consistency_violations(root, wi_dir, all_docs=all_docs)
    docs_ok = len(doc_violations) == 0
    checks.append(
        {
            "name": "docs_consistency",
            "ok": docs_ok,
            "detail": "no doc/command consistency drift"
            if docs_ok
            else f"{len(doc_violations)} inconsistency item(s)",
        }
    )
    if not docs_ok:
        blockers.append(
            "BLOCKER: docs consistency drift for registered commands: "
            + " | ".join(doc_violations)
        )

    local_pr_review = _local_pr_review_close_check_summary(root)
    checks.append(local_pr_review)
    if not local_pr_review["ok"]:
        blockers.append(f"BLOCKER: {local_pr_review['detail']}")

    checks.append(
        {
            "name": "done_gate",
            "ok": len(blockers) == 0,
            "detail": "ready for completion"
            if len(blockers) == 0
            else "completion still blocked",
        }
    )

    return CloseCheckResult(
        ok=len(blockers) == 0,
        blockers=blockers,
        checks=checks,
        wi_dir=wi_dir,
        error=None,
    )


def _local_pr_review_close_check_summary(root: Path) -> dict[str, Any]:
    pointer_path = root / CURRENT_REVIEW_PATH
    if not pointer_path.exists():
        return {
            "name": "local_pr_review",
            "ok": True,
            "detail": "no local PR review current pointer; skipped",
        }
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        review_run_path = _resolve_repo_path(
            root,
            str(pointer.get("review_run_path", "")),
        )
        review_run = ReviewRun.model_validate(
            json.loads(review_run_path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "name": "local_pr_review",
            "ok": False,
            "detail": f"local PR review state cannot be read: {exc}",
        }
    except ValidationError as exc:
        return {
            "name": "local_pr_review",
            "ok": False,
            "detail": f"local PR review state is invalid: {exc}",
        }

    verdict = str(review_run.verdict or "")
    final_report = str(review_run.final_report_path or "")
    final_report_path = (
        _resolve_repo_path(root, final_report) if final_report else Path()
    )
    unresolved_blockers = review_run.unresolved_blockers
    unresolved_required = review_run.unresolved_required
    stored_head_commit = review_run.head_commit.strip()
    stored_head_ref = review_run.head_ref.strip()
    stored_status = str(review_run.status or "").strip()
    if verdict == "blocked":
        return {
            "name": "local_pr_review",
            "ok": False,
            "detail": (
                "local PR review blocked: "
                f"unresolved_blockers={unresolved_blockers}, "
                f"unresolved_required={unresolved_required}"
            ),
            "review_id": review_run.review_id,
            "verdict": verdict,
        }
    if verdict in {"fully_clean", "risk_accepted"} and stored_status != "closed":
        return {
            "name": "local_pr_review",
            "ok": False,
            "detail": f"local PR review is not closed: status={stored_status or 'none'}",
            "review_id": review_run.review_id,
            "verdict": verdict,
            "status": stored_status,
        }
    if verdict in {"fully_clean", "risk_accepted"} and unresolved_blockers > 0:
        return {
            "name": "local_pr_review",
            "ok": False,
            "detail": (
                "local PR review has unresolved blockers: "
                f"unresolved_blockers={unresolved_blockers}"
            ),
            "review_id": review_run.review_id,
            "verdict": verdict,
            "unresolved_blockers": unresolved_blockers,
        }
    if verdict == "fully_clean" and unresolved_required > 0:
        return {
            "name": "local_pr_review",
            "ok": False,
            "detail": (
                "local PR review fully_clean has unresolved required findings: "
                f"unresolved_required={unresolved_required}"
            ),
            "review_id": review_run.review_id,
            "verdict": verdict,
            "unresolved_required": unresolved_required,
        }
    if verdict in {"fully_clean", "risk_accepted"} and not final_report_path.is_file():
        return {
            "name": "local_pr_review",
            "ok": False,
            "detail": "local PR review final report is missing",
            "review_id": review_run.review_id,
            "verdict": verdict,
        }
    if verdict in {"fully_clean", "risk_accepted"}:
        artifact_blocker = _local_pr_review_artifact_blocker(root, review_run)
        if artifact_blocker:
            return {
                "name": "local_pr_review",
                "ok": False,
                "detail": artifact_blocker,
                "review_id": review_run.review_id,
                "verdict": verdict,
            }
    if verdict in {"fully_clean", "risk_accepted"} and stored_head_commit:
        try:
            current_head = GitClient(root).resolve_revision(stored_head_ref or "HEAD")
        except GitError as exc:
            return {
                "name": "local_pr_review",
                "ok": False,
                "detail": f"local PR review head cannot be verified: {exc}",
                "review_id": review_run.review_id,
                "verdict": verdict,
                "head_commit": stored_head_commit,
            }
        if (
            current_head != stored_head_commit
            and not _local_pr_review_artifact_commit_only(
                root,
                review_run,
                stored_head_commit,
                current_head,
            )
        ):
            return {
                "name": "local_pr_review",
                "ok": False,
                "detail": (
                    "local PR review is stale: "
                    f"review_head={stored_head_commit[:12]}, "
                    f"current_head={current_head[:12]}"
                ),
                "review_id": review_run.review_id,
                "verdict": verdict,
                "head_commit": stored_head_commit,
                "current_head": current_head,
            }
    if verdict == "risk_accepted":
        detail = (
            f"local PR review risk_accepted; unresolved_required={unresolved_required}"
        )
    elif verdict == "fully_clean":
        detail = "local PR review fully_clean"
    else:
        detail = f"local PR review not closed yet: verdict={verdict or 'none'}"
    return {
        "name": "local_pr_review",
        "ok": verdict in {"fully_clean", "risk_accepted"},
        "detail": detail,
        "review_id": review_run.review_id,
        "verdict": verdict,
        "unresolved_blockers": unresolved_blockers,
        "unresolved_required": unresolved_required,
        "final_report_path": str(final_report_path) if final_report else "",
        "head_commit": stored_head_commit,
    }


def _local_pr_review_artifact_blocker(root: Path, review_run: ReviewRun) -> str:
    pack_blocker = _local_pr_review_artifact_digest_blocker(
        root,
        label="review-pack.json",
        path_text=review_run.review_pack_path,
        expected_digest=review_run.review_pack_digest,
        model=ReviewPack,
    )
    if pack_blocker:
        return pack_blocker
    findings_blocker = _local_pr_review_artifact_digest_blocker(
        root,
        label="findings.json",
        path_text=review_run.findings_path,
        expected_digest=review_run.findings_digest,
        model=ReviewFindings,
    )
    if findings_blocker:
        return findings_blocker
    return ""


def _local_pr_review_artifact_digest_blocker(
    root: Path,
    *,
    label: str,
    path_text: str,
    expected_digest: str,
    model: type[ReviewPack] | type[ReviewFindings],
) -> str:
    if not path_text.strip():
        return f"local PR review {label} path is missing"
    path = _resolve_repo_path(root, path_text)
    if not path.is_file():
        return f"local PR review {label} is missing"
    if not expected_digest.strip():
        return f"local PR review {label} digest is missing"
    try:
        actual_digest = _file_sha256(path)
    except OSError as exc:
        return f"local PR review {label} cannot be verified: {exc}"
    if actual_digest != expected_digest:
        return f"local PR review {label} changed after review close"
    try:
        model.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        return f"local PR review {label} is invalid: {exc}"
    return ""


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _local_pr_review_artifact_commit_only(
    root: Path,
    review_run: ReviewRun,
    stored_head_commit: str,
    current_head: str,
) -> bool:
    try:
        changed = GitClient(root)._run(
            "diff",
            "--name-only",
            f"{stored_head_commit}..{current_head}",
        )
    except GitError:
        return False
    changed_paths = [line.strip().replace("\\", "/") for line in changed.splitlines()]
    changed_paths = [path for path in changed_paths if path]
    if not changed_paths:
        return False
    return all(
        _is_current_local_pr_review_artifact_path(path, review_run)
        for path in changed_paths
    )


def _is_current_local_pr_review_artifact_path(path: str, review_run: ReviewRun) -> bool:
    review_id = review_run.review_id.strip()
    if not review_id:
        return False
    review_prefix = f".ai-sdlc/reviews/pr/{review_id}/"
    return path == str(CURRENT_REVIEW_PATH).replace("\\", "/") or path.startswith(
        review_prefix
    )


def _resolve_repo_path(root: Path, path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return root / path


def format_close_check_json(result: CloseCheckResult) -> str:
    return json.dumps(result.to_json_dict(), ensure_ascii=False, indent=2)


def format_branch_check_json(result: BranchCheckResult) -> str:
    return json.dumps(result.to_json_dict(), ensure_ascii=False, indent=2)
