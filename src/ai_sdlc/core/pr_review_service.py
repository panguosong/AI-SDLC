"""Service layer for local adversarial PR review commands."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Concatenate, ParamSpec, TypedDict, TypeVar, cast

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ai_sdlc.branch.git_client import GitClient, GitError
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_models import LoopPolicyProfile, LoopStatus, utc_now_iso
from ai_sdlc.core.loop_policy import (
    LoopPolicyError,
    ModelResolutionRequest,
    load_loop_policy,
    resolve_model_for_review,
)
from ai_sdlc.core.loop_resource_lock import (
    _ImplementationWriteLockError,
    _stage_write_guard,
)
from ai_sdlc.core.pr_review_models import (
    DiffSourceKind,
    FindingResolution,
    FindingResolutionStatus,
    FindingSeverity,
    ModelResolution,
    ModelResolutionStatus,
    ProviderLaunchStatus,
    ProviderMode,
    ProviderRunnerInvocation,
    PRReviewVerificationEvidence,
    RepairScopeInput,
    ReviewFinding,
    ReviewFindings,
    ReviewPack,
    ReviewRun,
    ReviewVerdict,
    SourceAccessStatus,
    SourceAdapterResolution,
)
from ai_sdlc.core.pr_review_pack import (
    ReviewPackBuildOptions,
    ReviewPackBuildResult,
    ReviewPackBuildStatus,
    analyze_pr_review_redaction,
    build_review_pack,
    decide_incomplete_review_pack,
    diff_for_review_source,
    resolve_review_input_for_source,
    review_diff_limit_blocker,
    review_diff_size_blocker,
)
from ai_sdlc.core.pr_review_provider import (
    MockReviewerFixture,
    ProviderCommandOptions,
    ProviderRunResult,
    ProviderRunStatus,
    WorktreeSnapshotError,
    _exit_code_verdict_blocker,
    _expand_command,
    _findings_scope_blocker,
    run_mock_reviewer,
    run_provider_command,
)
from ai_sdlc.core.pr_review_redaction import RedactionReport, analyze_redaction
from ai_sdlc.core.pr_review_source import (
    DiffSourceResolutionOptions,
    resolve_diff_source,
)
from ai_sdlc.core.quality_command import (
    QualityCommandOptions,
    quality_command_environment,
    run_quality_command,
)
from ai_sdlc.core.review_kernel import (
    ReviewInputValidator,
    revalidate_review_input_at_transition,
)
from ai_sdlc.core.stable_file_read import (
    _stable_regular_file_exists,
    read_stable_bytes,
)
from ai_sdlc.utils.helpers import AI_SDLC_DIR

CURRENT_REVIEW_PATH = Path(AI_SDLC_DIR) / "reviews" / "pr" / "current-review.json"
CURRENT_MODEL_ENV_KEYS = ("AI_SDLC_CURRENT_MODEL", "CODEX_MODEL", "OPENAI_MODEL")
SUPPORTED_PROVIDER_IDS = frozenset({"local-agent", "mock-reviewer"})
_FORMAL_ORIGINALS_REFERENCE = "pr-formal-originals-v1:"
_PROVIDER_FAILURE_REFERENCE = "pr-provider-failure-v1:"


class ResolutionFileError(ValueError):
    """Raised when a user-edited resolution artifact cannot be parsed."""


class PRReviewCommandStatus(StrEnum):
    """High-level PR review service result status."""

    READY = "ready"
    DRY_RUN = "dry_run"
    STARTED = "started"
    NEEDS_USER = "needs_user"
    BLOCKED = "blocked"
    CLOSED = "closed"
    NO_REVIEW = "no_review"


class PRReviewCheck(BaseModel):
    """One doctor/start readiness check."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    name: str
    status: PRReviewCommandStatus
    detail: str


@dataclass(frozen=True, slots=True)
class PRReviewStartOptions:
    """Inputs for starting or previewing a local PR review."""

    root: Path
    base_ref: str = ""
    head_ref: str = "HEAD"
    diff_source: str = "local-git-range"
    patch_file: str = ""
    source_id: str = ""
    source_provider: str = ""
    provider_id: str = ""
    model_selector: str = "current"
    current_model: str = ""
    provider_default_model: str = ""
    provider_command: list[str] = field(default_factory=list)
    code_egress: bool = False
    code_egress_confirmed: bool = False
    dry_run: bool = False
    review_id: str = ""
    loop_id: str = ""
    mock_fixture: MockReviewerFixture = MockReviewerFixture.CLEAN
    clear_stale_artifacts: bool = True
    preserve_resolution_history: bool = False
    provider_timeout_seconds: float = 60.0
    max_diff_bytes: int = 500_000
    decision_mode: str = "legacy"
    decision_capability: str | None = None
    decision_staged_tree_oid: str = ""
    decision_started_at_ms: int | None = None
    expected_repair_source: SourceAdapterResolution | None = None
    provider_snapshot_complete: bool = False
    provider_workspace_adoption_ref: dict[str, str] | None = None
    rejected_feedback_ref: dict[str, str] | None = None
    test_results_refs: list[str] = field(default_factory=list)


class PRReviewStartResult(BaseModel):
    """Start/dry-run result for CLI and service tests."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: PRReviewCommandStatus
    dry_run: bool = False
    provider_id: str
    review_id: str = ""
    loop_id: str = ""
    review_dir: str = ""
    review_pack_path: str = ""
    findings_path: str = ""
    review_run_path: str = ""
    current_review_path: str = ""
    model_selector: str = "current"
    resolved_model: str = ""
    diff_source: dict[str, object] = Field(default_factory=dict)
    source_adapter: str = ""
    source_access_status: str = ""
    source_resolution_path: str = ""
    code_egress: bool = False
    changed_files_count: int = 0
    included_files_count: int = 0
    omitted_files_count: int = 0
    redacted_files_count: int = 0
    verdict: ReviewVerdict | None = None
    provider_status: ProviderRunStatus | None = None
    exit_code: int | None = None
    blocker: str = ""
    next_action: str = ""
    checks: list[PRReviewCheck] = Field(default_factory=list)
    model_resolution: ModelResolution | None = None
    decision_mode: str = Field(
        default="legacy", exclude_if=lambda value: value == "legacy"
    )
    decision_capability: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class PRReviewDoctorResult(BaseModel):
    """Read-only readiness result for local PR review."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: PRReviewCommandStatus
    provider_id: str
    model_selector: str = "current"
    resolved_model: str = ""
    diff_source: dict[str, object] = Field(default_factory=dict)
    source_adapter: str = ""
    source_access_status: str = ""
    code_egress: bool = False
    changed_files_count: int = 0
    included_files_count: int = 0
    omitted_files_count: int = 0
    redacted_files_count: int = 0
    blocker: str = ""
    next_action: str = ""
    checks: list[PRReviewCheck] = Field(default_factory=list)


class PRReviewStatusResult(BaseModel):
    """Current review status result."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: PRReviewCommandStatus
    review_id: str = ""
    loop_id: str = ""
    review_run_path: str = ""
    review_pack_path: str = ""
    findings_path: str = ""
    verdict: ReviewVerdict | None = None
    unresolved_blockers: int = 0
    unresolved_required: int = 0
    unresolved_advisory: int = 0
    blocker: str = ""
    next_action: str = ""


class PRReviewFixResult(BaseModel):
    """Result for generating a fix plan and resolution scaffold."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: PRReviewCommandStatus
    dry_run: bool = False
    review_id: str = ""
    fix_plan_path: str = ""
    resolution_path: str = ""
    selected_findings_count: int = 0
    skipped_advisory_count: int = 0
    round_number: int = 0
    blocker: str = ""
    next_action: str = ""


class PRReviewEvidenceResult(BaseModel):
    """Result for recording verification evidence before expert review."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: PRReviewCommandStatus
    review_id: str = ""
    evidence_path: str = ""
    evidence_count: int = 0
    blocker: str = ""
    next_action: str = ""


class PRReviewCloseResult(BaseModel):
    """Result for closing a local PR review."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: PRReviewCommandStatus
    review_id: str = ""
    verdict: ReviewVerdict | None = None
    final_report_path: str = ""
    unresolved_blockers: int = 0
    unresolved_required: int = 0
    unresolved_advisory: int = 0
    blocker: str = ""
    next_action: str = ""


class PRReviewCommitResult(BaseModel):
    """Result for committing exactly the reviewed staged tree."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: PRReviewCommandStatus
    review_id: str = ""
    commit: str = ""
    tree_oid: str = ""
    blocker: str = ""
    next_action: str = ""


def _policy_blocker(exc: LoopPolicyError) -> str:
    return str(exc)


def _policy_next_action() -> str:
    return (
        "Fix .ai-sdlc/project/config/loop-policy.yaml and rerun the PR review command."
    )


def doctor_pr_review(
    *,
    root: Path,
    base_ref: str,
    head_ref: str = "HEAD",
    diff_source: str = "local-git-range",
    patch_file: str = "",
    source_id: str = "",
    source_provider: str = "",
    provider_id: str = "",
    model_selector: str = "current",
    current_model: str = "",
    provider_default_model: str = "",
    provider_command: list[str] | None = None,
    code_egress: bool = False,
    code_egress_confirmed: bool = False,
    max_diff_bytes: int = 500_000,
) -> PRReviewDoctorResult:
    """Read-only readiness checks for local PR review."""

    (
        checks,
        status,
        blocker,
        next_action,
        model_resolution,
        redaction,
        source_resolution,
    ) = _preview(
        PRReviewStartOptions(
            root=root,
            base_ref=base_ref,
            head_ref=head_ref,
            diff_source=diff_source,
            patch_file=patch_file,
            source_id=source_id,
            source_provider=source_provider,
            provider_id=provider_id,
            model_selector=model_selector,
            current_model=current_model,
            provider_default_model=provider_default_model,
            provider_command=provider_command or [],
            code_egress=code_egress,
            code_egress_confirmed=code_egress_confirmed,
            dry_run=True,
            max_diff_bytes=max_diff_bytes,
        )
    )
    return PRReviewDoctorResult(
        status=status,
        provider_id=model_resolution.provider_id if model_resolution else provider_id,
        model_selector=model_resolution.model_selector
        if model_resolution
        else model_selector,
        resolved_model=model_resolution.resolved_model if model_resolution else "",
        diff_source=source_resolution.to_descriptor().model_dump(mode="json")
        if source_resolution
        else {},
        source_adapter=source_resolution.adapter_id if source_resolution else "",
        source_access_status=str(source_resolution.access_status)
        if source_resolution
        else "",
        code_egress=code_egress,
        changed_files_count=redaction.changed_files_count if redaction else 0,
        included_files_count=len(redaction.included_files) if redaction else 0,
        omitted_files_count=len(redaction.omitted_files) if redaction else 0,
        redacted_files_count=len(redaction.redacted_files) if redaction else 0,
        blocker=blocker,
        next_action=next_action,
        checks=checks,
    )


def start_pr_review(options: PRReviewStartOptions) -> PRReviewStartResult:
    """Start or dry-run a local PR review."""

    # 尚未发布的恢复引用不能成为正常新评审的启动前提，也不改写旧失败。
    if (options.provider_snapshot_complete or options.provider_workspace_adoption_ref is not None
            or options.rejected_feedback_ref is not None):
        return PRReviewStartResult(
            status=PRReviewCommandStatus.BLOCKED, provider_id=options.provider_id,
            review_id=options.review_id, loop_id=options.loop_id,
            blocker="Historical provider recovery and workspace adoption are unsupported.",
            next_action="Preserve the original failed review; continue only a supported normal review lifecycle.",
        )

    limit_blocker = review_diff_limit_blocker(options.max_diff_bytes)
    if limit_blocker:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.NEEDS_USER,
            dry_run=options.dry_run,
            provider_id=options.provider_id,
            blocker=limit_blocker,
            next_action="Set --max-diff-bytes to a positive integer for this invocation.",
        )
    timeout_blocker = _provider_timeout_blocker(options.provider_timeout_seconds)
    if timeout_blocker:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.NEEDS_USER,
            dry_run=options.dry_run,
            provider_id=options.provider_id,
            blocker=timeout_blocker,
            next_action="Set --provider-timeout-seconds to a finite positive number.",
        )
    root = options.root.resolve()
    identity_blocker = _decision_start_blocker(root, options)
    if identity_blocker:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.BLOCKED,
            dry_run=options.dry_run,
            provider_id=options.provider_id,
            blocker=identity_blocker,
            next_action="Preserve the saved review identity; use pr-review rerun for the same review.",
        )
    review_id_blocker = _unsafe_explicit_review_id_blocker(options.review_id)
    if review_id_blocker:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.NEEDS_USER,
            dry_run=options.dry_run,
            provider_id=options.provider_id,
            review_id=options.review_id.strip(),
            blocker=review_id_blocker,
            next_action=(
                "Use letters, numbers, dots, underscores, or hyphens in --review-id."
            ),
        )

    if options.dry_run:
        (
            checks,
            status,
            blocker,
            next_action,
            model_resolution,
            redaction,
            source_resolution,
        ) = _preview(options)
        if status == PRReviewCommandStatus.READY:
            status = PRReviewCommandStatus.DRY_RUN
            next_action = "Run ai-sdlc pr-review start without --dry-run."
        return PRReviewStartResult(
            status=status,
            dry_run=True,
            decision_mode=options.decision_mode,
            decision_capability=options.decision_capability,
            provider_id=model_resolution.provider_id
            if model_resolution
            else options.provider_id,
            review_id=_resolve_review_id(options),
            loop_id=_resolve_loop_id(options),
            review_dir=str(
                LoopArtifactStore(options.root.resolve()).review_run_dir(
                    _resolve_review_id(options)
                )
            ),
            model_selector=model_resolution.model_selector
            if model_resolution
            else options.model_selector,
            resolved_model=model_resolution.resolved_model if model_resolution else "",
            diff_source=source_resolution.to_descriptor().model_dump(mode="json")
            if source_resolution
            else {},
            source_adapter=source_resolution.adapter_id if source_resolution else "",
            source_access_status=str(source_resolution.access_status)
            if source_resolution
            else "",
            code_egress=options.code_egress,
            changed_files_count=redaction.changed_files_count if redaction else 0,
            included_files_count=len(redaction.included_files) if redaction else 0,
            omitted_files_count=len(redaction.omitted_files) if redaction else 0,
            redacted_files_count=len(redaction.redacted_files) if redaction else 0,
            blocker=blocker,
            next_action=next_action,
            checks=checks,
            model_resolution=model_resolution,
        )

    try:
        policy = load_loop_policy(root)
    except LoopPolicyError as exc:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.BLOCKED,
            provider_id=options.provider_id,
            review_id=_resolve_review_id(options),
            loop_id=_resolve_loop_id(options),
            review_dir=str(
                LoopArtifactStore(root).review_run_dir(_resolve_review_id(options))
            ),
            blocker=_policy_blocker(exc),
            next_action=_policy_next_action(),
        )
    provider_options = _normalize_provider_options(
        _apply_policy_provider_default(options, policy)
    )
    review_id = _resolve_review_id(provider_options)
    loop_id = _resolve_loop_id(provider_options)
    provider_blocker = _unsupported_provider_blocker(provider_options.provider_id)
    if provider_blocker:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.NEEDS_USER,
            provider_id=provider_options.provider_id,
            review_id=review_id,
            loop_id=loop_id,
            review_dir=str(LoopArtifactStore(root).review_run_dir(review_id)),
            blocker=provider_blocker,
            next_action="Choose local-agent or mock-reviewer.",
        )
    if provider_options.provider_id == "local-agent" and not provider_options.provider_command:
        # 尚无可执行命令，不发布无法绑定调用原件的新 pack/run；配置后仍可使用原 review ID。
        return PRReviewStartResult(status=PRReviewCommandStatus.NEEDS_USER, provider_id=provider_options.provider_id,
                                   review_id=review_id, loop_id=loop_id,
                                   blocker="local-agent provider is not configured with a local reviewer command.",
                                   next_action="Configure a local reviewer command and start the same review.")

    publication: _ReviewPackPublication | None = None

    def finish_publication_failure(reason: str, next_action: str) -> str:
        if publication is None or not publication.expected_publication:
            return next_action
        try:
            archived = publication.rollback_publication(reason)
        except (ValueError, OSError, WorktreeSnapshotError) as recovery_error:
            return str(recovery_error)
        resume = "Inspect the preserved publication error before using a supported normal review action."
        return f"Original review pack restored; failed publication preserved at {archived}. {resume}"

    try:
        # 同评审各生产入口在原生 R1/历史保全之后，才绑定本次六件发布的原始字节。
        publication = _ReviewPackPublication(root, LoopArtifactStore(root).review_run_dir(review_id))
        pack_result = build_review_pack(
            ReviewPackBuildOptions(
                root=root,
                base_ref=provider_options.base_ref,
                head_ref=provider_options.head_ref,
                diff_source=provider_options.diff_source,
                patch_file=provider_options.patch_file,
                source_id=provider_options.source_id,
                source_provider=provider_options.source_provider,
                requested_provider=provider_options.provider_id,
                requested_model=_requested_model(provider_options),
                provider_default_model=provider_options.provider_default_model,
                current_model=_current_model(provider_options),
                provider_mode=_provider_mode(provider_options.provider_id),
                code_egress=provider_options.code_egress,
                code_egress_confirmed=provider_options.code_egress_confirmed,
                review_id=review_id,
                loop_id=loop_id,
                clear_stale_artifacts=provider_options.clear_stale_artifacts,
                preserve_resolution_history=provider_options.preserve_resolution_history,
                max_diff_bytes=provider_options.max_diff_bytes,
                expected_repair_source=provider_options.expected_repair_source,
                pre_publish_guard=publication.before_publish,
                publication_recorder=publication.record_publication,
                test_results_refs=provider_options.test_results_refs,
            )
        )
        pack_next_action = pack_result.next_action
        if pack_result.status != ReviewPackBuildStatus.READY:
            pack_next_action = finish_publication_failure(pack_result.blocker, pack_next_action)
        else:
            publication.seal_pack(pack_result)
    except (GitError, ValueError, OSError, WorktreeSnapshotError) as exc:
        recovery = finish_publication_failure(str(exc), "Check the base/head refs.")
        return PRReviewStartResult(
            status=PRReviewCommandStatus.BLOCKED,
            provider_id=provider_options.provider_id,
            review_id=review_id,
            loop_id=loop_id,
            review_dir=str(LoopArtifactStore(root).review_run_dir(review_id)),
            blocker=str(exc) if isinstance(exc, GitError) else f"Review pack publication failed: {exc}",
            next_action=recovery,
        )
    except Exception as exc:
        # 发布准备或写入的编程异常仍原样传播；只在原 owner 窗口内保全并撤回已登记自写件。
        try:
            recovery = finish_publication_failure(str(exc), "")
        except Exception as recovery_error:
            exc.add_note(f"Publication recovery failed: {recovery_error}")
        else:
            if recovery:
                exc.add_note(recovery)
        raise
    if pack_result.status != ReviewPackBuildStatus.READY:
        return PRReviewStartResult(
            status=_status_from_pack_result(pack_result.status),
            provider_id=provider_options.provider_id,
            review_id=review_id,
            loop_id=loop_id,
            review_dir=pack_result.review_dir,
            review_pack_path=pack_result.review_pack_path,
            source_resolution_path=pack_result.source_resolution_path,
            model_selector=pack_result.model_resolution.model_selector
            if pack_result.model_resolution
            else provider_options.model_selector,
            resolved_model=pack_result.model_resolution.resolved_model
            if pack_result.model_resolution
            else "",
            diff_source=pack_result.source_resolution.to_descriptor().model_dump(
                mode="json"
            )
            if pack_result.source_resolution
            else {},
            source_adapter=pack_result.source_resolution.adapter_id
            if pack_result.source_resolution
            else "",
            source_access_status=str(pack_result.source_resolution.access_status)
            if pack_result.source_resolution
            else "",
            code_egress=provider_options.code_egress,
            changed_files_count=pack_result.changed_files_count,
            included_files_count=pack_result.included_files_count,
            omitted_files_count=pack_result.omitted_files_count,
            redacted_files_count=pack_result.redacted_files_count,
            blocker=pack_result.blocker,
            next_action=pack_next_action,
            model_resolution=pack_result.model_resolution,
        )

    def persist_execution_failure(provider_result: ProviderRunResult) -> None:
        # 原件已由 provider 保存；异常传播前将失败归到本次候选，不沿用旧 run。
        failed_run_path = _write_review_run(
            root=root, options=provider_options, review_id=review_id, loop_id=loop_id,
            pack_result=pack_result, provider_result=provider_result,
        )
        _write_current_review(root=root, review_id=review_id, loop_id=loop_id, review_run_path=failed_run_path)

    provider_result = _run_provider(
        provider_options, Path(pack_result.review_pack_path),
        pre_launch_guard=publication.assert_published,
        on_execution_failure=persist_execution_failure,
    )
    review_run_path = _write_review_run(
        root=root,
        options=provider_options,
        review_id=review_id,
        loop_id=loop_id,
        pack_result=pack_result,
        provider_result=provider_result,
    )
    current_review_path = _write_current_review(
        root=root,
        review_id=review_id,
        loop_id=loop_id,
        review_run_path=review_run_path,
    )

    return PRReviewStartResult(
        status=_status_from_provider_result(provider_result.status),
        decision_mode=provider_options.decision_mode,
        decision_capability=provider_options.decision_capability,
        provider_id=provider_options.provider_id,
        review_id=review_id,
        loop_id=loop_id,
        review_dir=pack_result.review_dir,
        review_pack_path=str(_resolve_repo_path(root, pack_result.review_pack_path)),
        source_resolution_path=pack_result.source_resolution_path,
        findings_path=str(_resolve_repo_path(root, provider_result.findings_path))
        if provider_result.findings_path
        else "",
        review_run_path=str(review_run_path),
        current_review_path=str(current_review_path),
        model_selector=pack_result.review_pack.model_selector
        if pack_result.review_pack
        else provider_options.model_selector,
        resolved_model=pack_result.review_pack.resolved_model
        if pack_result.review_pack
        else "",
        diff_source=pack_result.review_pack.diff_source.model_dump(mode="json")
        if pack_result.review_pack
        else {},
        source_adapter=pack_result.review_pack.source_adapter
        if pack_result.review_pack
        else "",
        source_access_status=str(pack_result.review_pack.source_access_status)
        if pack_result.review_pack
        else "",
        code_egress=provider_options.code_egress,
        changed_files_count=pack_result.changed_files_count,
        included_files_count=pack_result.included_files_count,
        omitted_files_count=pack_result.omitted_files_count,
        redacted_files_count=pack_result.redacted_files_count,
        verdict=provider_result.findings.verdict if provider_result.findings else None,
        provider_status=provider_result.status,
        exit_code=provider_result.exit_code,
        blocker=provider_result.blocker,
        next_action=_decision_next_action(provider_options, provider_result),
        model_resolution=pack_result.model_resolution,
    )


def status_pr_review(root: Path) -> PRReviewStatusResult:
    """Recover the current PR review run."""

    pointer_path = root.resolve() / CURRENT_REVIEW_PATH
    if not pointer_path.exists():
        return PRReviewStatusResult(
            status=PRReviewCommandStatus.NO_REVIEW,
            next_action="Run ai-sdlc pr-review start --base <branch>.",
        )
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        return PRReviewStatusResult(
            status=PRReviewCommandStatus.BLOCKED,
            blocker=f"Current review pointer is malformed: {exc}",
            next_action="Rerun ai-sdlc pr-review start.",
        )
    if not isinstance(pointer, dict):
        return PRReviewStatusResult(
            status=PRReviewCommandStatus.BLOCKED,
            blocker="Current review pointer is malformed: root must be an object.",
            next_action="Rerun ai-sdlc pr-review start.",
        )
    review_run_path = _resolve_repo_path(
        root.resolve(), str(pointer.get("review_run_path", ""))
    )
    if not review_run_path.exists():
        return PRReviewStatusResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=str(pointer.get("review_id", "")),
            blocker="Current review pointer references a missing review-run.json.",
            next_action="Rerun ai-sdlc pr-review start.",
        )
    try:
        review_run = ReviewRun.model_validate(
            json.loads(review_run_path.read_text(encoding="utf-8"))
        )
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        return PRReviewStatusResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=str(pointer.get("review_id", "")),
            review_run_path=str(review_run_path),
            blocker=f"Current review-run.json is malformed: {exc}",
            next_action="Rerun ai-sdlc pr-review start.",
        )
    return _status_result(root, review_run_path, review_run, "")


def record_pr_review_verification_evidence(
    root: Path,
    *,
    evidence: list[str],
) -> PRReviewEvidenceResult:
    """拒绝把人工字符串冒充为 Local PR 可执行证据。"""

    del evidence
    return PRReviewEvidenceResult(
        status=PRReviewCommandStatus.BLOCKED,
        blocker="Legacy verification strings cannot satisfy Local PR Review.",
        next_action="Run ai-sdlc pr-review verify --cwd . -- <argv...>.",
    )


_PRParameters = ParamSpec("_PRParameters")
_PRResult = TypeVar("_PRResult", bound=BaseModel)


def _quantified_pr_write_guard(
    function: Callable[Concatenate[Path, _PRParameters], _PRResult],
) -> Callable[Concatenate[Path, _PRParameters], _PRResult]:
    """新能力的证据/交付写入共用当前 review 锁；旧实例不改变锁路径。"""

    @wraps(function)
    def guarded(root: Path, *args: _PRParameters.args, **kwargs: _PRParameters.kwargs) -> _PRResult:
        # 非法容量先走原函数的参数拒绝，不能先创建量化写锁。
        if function.__name__ == "rerun_pr_review" and review_diff_limit_blocker(
            cast(int, kwargs.get("max_diff_bytes", 500_000))
        ):
            return function(root, *args, **kwargs)
        try:
            run, _ = _load_current_review_run(root)
        except (OSError, ValueError):
            return function(root, *args, **kwargs)
        if run.decision_capability != "stage-simulation-v1":
            return function(root, *args, **kwargs)
        with ExitStack() as locks:
            from ai_sdlc.core.pr_review_decision import (
                pr_review_context_path,
                validate_pr_review_context,
            )

            result_type = {
                "verify_pr_review_command": PRReviewEvidenceResult,
                "commit_pr_review": PRReviewCommitResult,
                "fix_pr_review": PRReviewFixResult,
                "close_pr_review": PRReviewCloseResult,
                "rerun_pr_review": PRReviewStartResult,
            }[function.__name__]
            try:
                locks.enter_context(_stage_write_guard(root, "local-pr-review", run.review_id))
                current, _ = _load_current_review_run(root)
                if (
                    current.review_id != run.review_id
                    or current.decision_capability != run.decision_capability
                ):
                    raise ValueError("pr-decision-current-review-drift")
                if (
                    current.decision_started_at_ms is not None
                    or pr_review_context_path(root, current).exists()
                ):
                    validate_pr_review_context(root, current)
                if function.__name__ == "close_pr_review":
                    digest = cast(str, kwargs.get("expected_review_digest", "")).strip()
                    if not digest or kwargs.get("review_input_validator") is None:
                        raise ValueError("pr-decision-guarded-close-required")
                    # 公开 core 入口也必须消费实际评审；空参数或空回调不能走旧 Close。
                    blocker = _current_expert_outcome_blocker(
                        root, current, expected_digest=digest
                    )
                    if blocker:
                        raise ValueError(blocker)
            except (OSError, ValueError, _ImplementationWriteLockError) as exc:
                payload: dict[str, object] = {
                    "status": PRReviewCommandStatus.BLOCKED,
                    "blocker": str(exc),
                    "next_action": "Preserve the current review identity and repair its original decision evidence before retrying.",
                }
                if result_type is PRReviewStartResult:
                    payload["provider_id"] = run.provider_id
                if result_type is PRReviewFixResult:
                    payload["dry_run"] = kwargs.get("dry_run", False)
                # 固定的命令到结果模型映射保持原返回类型，不改变各命令的参数签名。
                return cast(_PRResult, result_type.model_validate(payload))
            return function(root, *args, **kwargs)

    return guarded


@_quantified_pr_write_guard
def verify_pr_review_command(
    root: Path,
    *,
    cwd: str,
    argv: tuple[str, ...],
    timeout_seconds: float = 300.0,
) -> PRReviewEvidenceResult:
    """执行并记录一次绑定 reviewed staged tree 的质量命令。"""

    resolved_root = root.resolve()
    try:
        review_run, _ = _load_current_review_run(resolved_root)
        review_pack = _load_review_pack(resolved_root, review_run.review_pack_path)
        recovery_originals = read_pr_recovery_originals(resolved_root, review_run, review_pack)
        recovery_originals.assert_unchanged()
    except FileNotFoundError as exc:
        return PRReviewEvidenceResult(
            status=PRReviewCommandStatus.NO_REVIEW,
            blocker=str(exc),
            next_action="Run ai-sdlc pr-review start --diff-source local-staged.",
        )
    except (json.JSONDecodeError, ValidationError, ValueError, OSError) as exc:
        return PRReviewEvidenceResult(
            status=PRReviewCommandStatus.BLOCKED,
            blocker=f"Current PR review artifacts are malformed: {exc}",
            next_action="Rerun ai-sdlc pr-review start --diff-source local-staged.",
        )
    if review_run.status == LoopStatus.CLOSED:
        return PRReviewEvidenceResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            blocker="Closed PR review evidence cannot be changed.",
            next_action="Start a new local PR review for a changed staged tree.",
        )
    if (
        DiffSourceKind(review_run.diff_source.source_kind)
        != DiffSourceKind.LOCAL_STAGED
    ):
        return PRReviewEvidenceResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            blocker="Only local-staged review is eligible for delivery verification.",
            next_action="Restart PR review with --diff-source local-staged.",
        )
    source_blocker = _precommit_staged_source_blocker(
        resolved_root,
        review_run,
        review_pack,
    )
    if source_blocker:
        return PRReviewEvidenceResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            blocker=source_blocker,
            next_action="Rerun PR review for the current staged tree.",
        )
    try:
        formal_originals = _read_pr_formal_originals(recovery_originals, review_run, review_pack)
        recovery_originals.assert_unchanged()
        if formal_originals is not None and formal_originals["inputs"]:
            # verify 也会替换 R1 输入；在执行命令之前保全，不能等到下一次 rerun 才补救。
            _preserve_formal_originals(resolved_root, review_run, formal_originals)
    except (ValueError, OSError, GitError) as exc:
        return PRReviewEvidenceResult(
            status=PRReviewCommandStatus.BLOCKED, review_id=review_run.review_id,
            blocker=f"Unable to preserve formal review before verification: {exc}",
            next_action="Restore the original formal review inputs before verification.",
        )
    try:
        result = run_quality_command(
            QualityCommandOptions(
                root=resolved_root,
                cwd=resolved_root / (cwd.strip() or "."),
                argv=argv,
                timeout_seconds=timeout_seconds,
            )
        )
    except ValueError as exc:
        return PRReviewEvidenceResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            blocker=str(exc),
            next_action="Fix the verification command and rerun it.",
        )
    evidence_path = (
        LoopArtifactStore(resolved_root).review_run_dir(review_run.review_id)
        / "verification-evidence.json"
    )
    previous_results = []
    if evidence_path.is_file():
        try:
            previous = PRReviewVerificationEvidence.model_validate_json(
                evidence_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            return PRReviewEvidenceResult(
                status=PRReviewCommandStatus.BLOCKED,
                review_id=review_run.review_id,
                blocker=f"Current verification evidence is malformed: {exc}",
                next_action="Restart PR review for the current staged tree.",
            )
        if (
            previous.review_id != review_run.review_id
            or previous.loop_id != review_run.loop_id
            or previous.staged_tree_oid != review_run.staged_tree_oid
        ):
            return PRReviewEvidenceResult(
                status=PRReviewCommandStatus.BLOCKED,
                review_id=review_run.review_id,
                blocker="Current verification evidence belongs to another staged tree.",
                next_action="Restart PR review for the current staged tree.",
            )
        previous_results = previous.results
    evidence = PRReviewVerificationEvidence(
        review_id=review_run.review_id,
        loop_id=review_run.loop_id,
        staged_tree_oid=review_run.staged_tree_oid,
        results=[*previous_results, result],
    )
    LoopArtifactStore(resolved_root).write_json_artifact(evidence_path, evidence)
    return PRReviewEvidenceResult(
        status=(
            PRReviewCommandStatus.READY
            if result.successful
            else PRReviewCommandStatus.BLOCKED
        ),
        review_id=review_run.review_id,
        evidence_path=str(evidence_path),
        evidence_count=len(evidence.results),
        blocker="" if result.successful else f"Verification command {result.status}.",
        next_action=(
            "Run bounded Local PR expert review."
            if result.successful
            else "Fix the command failure or source mutation, then verify again."
        ),
    )


@_quantified_pr_write_guard
def commit_pr_review(root: Path, *, message: str) -> PRReviewCommitResult:
    """使用普通 Git hooks 提交完全相同的 reviewed staged tree。"""

    resolved_root = root.resolve()
    commit_message = message.strip()
    if not commit_message:
        return PRReviewCommitResult(
            status=PRReviewCommandStatus.BLOCKED,
            blocker="Commit message is required.",
            next_action="Pass --message with a non-empty commit message.",
        )
    try:
        review_run, review_run_path = _load_current_review_run(resolved_root)
        review_pack = _load_review_pack(resolved_root, review_run.review_pack_path)
        evidence = _load_verification_evidence(resolved_root, review_run)
        recovery_originals = read_pr_recovery_originals(resolved_root, review_run, review_pack)
        recovery_originals.assert_unchanged()
    except FileNotFoundError as exc:
        return PRReviewCommitResult(
            status=PRReviewCommandStatus.NO_REVIEW,
            blocker=str(exc),
            next_action="Run ai-sdlc pr-review start --diff-source local-staged.",
        )
    except (json.JSONDecodeError, ValidationError, ValueError, OSError) as exc:
        return PRReviewCommitResult(
            status=PRReviewCommandStatus.BLOCKED,
            blocker=f"Current PR review artifacts are malformed: {exc}",
            next_action="Restart PR review for the current staged tree.",
        )
    if (
        DiffSourceKind(review_run.diff_source.source_kind)
        != DiffSourceKind.LOCAL_STAGED
    ):
        return PRReviewCommitResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            blocker="Diagnostic review sources cannot authorize a delivery commit.",
            next_action="Restart PR review with --diff-source local-staged.",
        )
    evidence_blocker = _verification_evidence_blocker(review_run, evidence)
    if evidence_blocker:
        return PRReviewCommitResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            blocker=evidence_blocker,
            next_action="Run ai-sdlc pr-review verify before commit.",
        )
    outcome_blocker = _current_expert_outcome_blocker(resolved_root, review_run)
    if outcome_blocker:
        return PRReviewCommitResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            blocker=outcome_blocker,
            next_action="Complete the bounded Local PR expert review before commit.",
        )
    source_blocker = _precommit_staged_source_blocker(
        resolved_root,
        review_run,
        review_pack,
    )
    if source_blocker:
        return PRReviewCommitResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            blocker=source_blocker,
            next_action="Rerun PR review for the current staged tree.",
        )
    try:
        process = subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=resolved_root,
            env=quality_command_environment(os.environ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return PRReviewCommitResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            blocker=f"Git commit could not complete: {exc}",
            next_action="Inspect hooks and repository state; history was not rolled back.",
        )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        return PRReviewCommitResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            blocker=f"Git commit failed: {detail or process.returncode}",
            next_action="Inspect hooks and repository state; history was not rolled back.",
        )
    delivery_blocker, commit, tree_oid = _delivery_commit_state(
        resolved_root,
        review_run,
        review_pack,
    )
    if delivery_blocker:
        return PRReviewCommitResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            commit=commit,
            tree_oid=tree_oid,
            blocker=delivery_blocker,
            next_action="Review the current HEAD as a new staged change; history was not rolled back.",
        )
    try:
        recovery_originals.assert_unchanged()
        # 只约束提交副作用前后的同一 run；后续合法 Close 可继续写入其状态。
        current_run, current_path = _load_current_review_run(resolved_root)
        if current_path != review_run_path or current_run != review_run:
            raise ValueError("recovery-current-review-changed-during-commit")
    except (ValueError, OSError) as exc:
        return PRReviewCommitResult(
            status=PRReviewCommandStatus.BLOCKED, review_id=review_run.review_id,
            commit=commit, tree_oid=tree_oid, blocker=f"Recovery originals changed during commit: {exc}",
            next_action="Inspect the committed history and restore the original review evidence; history was not rolled back.",
        )
    review_run.delivery_commit = commit
    review_run.delivery_parent_commit = review_run.head_commit
    review_run.next_action = "Close the unchanged Local PR review."
    review_run.updated_at = utc_now_iso()
    LoopArtifactStore(resolved_root).write_json_artifact(review_run_path, review_run)
    return PRReviewCommitResult(
        status=PRReviewCommandStatus.READY,
        review_id=review_run.review_id,
        commit=commit,
        tree_oid=tree_oid,
        next_action="Run ai-sdlc pr-review close with the reviewed digest.",
    )


def _status_result(
    root: Path,
    review_run_path: Path,
    review_run: ReviewRun,
    blocker: str,
) -> PRReviewStatusResult:
    return PRReviewStatusResult(
        status=(
            PRReviewCommandStatus.BLOCKED
            if blocker
            else _status_from_loop_status(review_run.status)
        ),
        review_id=review_run.review_id,
        loop_id=review_run.loop_id,
        review_run_path=str(review_run_path),
        review_pack_path=str(
            _resolve_repo_path(root.resolve(), review_run.review_pack_path)
        ),
        findings_path=str(_resolve_repo_path(root.resolve(), review_run.findings_path))
        if review_run.findings_path
        else "",
        verdict=review_run.verdict,
        unresolved_blockers=review_run.unresolved_blockers,
        unresolved_required=review_run.unresolved_required,
        unresolved_advisory=review_run.unresolved_advisory,
        blocker=blocker,
        next_action="Rerun local PR review." if blocker else review_run.next_action,
    )


@_quantified_pr_write_guard
def fix_pr_review(
    root: Path,
    *,
    max_rounds: int = 2,
    dry_run: bool = False,
) -> PRReviewFixResult:
    """Generate a fix plan and resolution scaffold without modifying code."""

    try:
        policy = load_loop_policy(root.resolve())
    except LoopPolicyError as exc:
        return PRReviewFixResult(dry_run=dry_run,
            status=PRReviewCommandStatus.BLOCKED,
            blocker=_policy_blocker(exc),
            next_action=_policy_next_action(),
        )
    effective_max_rounds = min(max_rounds, policy.max_rounds)
    try:
        review_run, review_run_path = _load_current_review_run(root)
    except FileNotFoundError as exc:
        return PRReviewFixResult(dry_run=dry_run,
            status=PRReviewCommandStatus.NO_REVIEW,
            blocker=str(exc),
            next_action="Run ai-sdlc pr-review start --base <branch>.",
        )
    except (json.JSONDecodeError, ValidationError, ValueError, OSError) as exc:
        return PRReviewFixResult(dry_run=dry_run,
            status=PRReviewCommandStatus.BLOCKED,
            blocker=f"Current PR review artifacts are malformed: {exc}",
            next_action="Rerun ai-sdlc pr-review start.",
        )
    try:
        findings = _load_findings(root.resolve(), review_run)
    except FileNotFoundError as exc:
        return PRReviewFixResult(dry_run=dry_run,
            status=PRReviewCommandStatus.NO_REVIEW,
            review_id=review_run.review_id,
            blocker=str(exc),
            next_action="Run ai-sdlc pr-review start --base <branch>.",
        )
    except (json.JSONDecodeError, ValidationError, ValueError, OSError) as exc:
        return PRReviewFixResult(dry_run=dry_run,
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            blocker=f"Current findings.json is malformed: {exc}",
            next_action="Rerun ai-sdlc pr-review start.",
        )

    try:
        original_pack = _load_review_pack(root.resolve(), review_run.review_pack_path)
        feedback_blocker = _reviewer_outputs_tamper_blocker(root.resolve(), review_run, findings) or _findings_scope_blocker(
            findings, review_pack=original_pack, review_pack_path=_resolve_repo_path(root.resolve(), review_run.review_pack_path))
        if feedback_blocker:
            raise ValueError(feedback_blocker)
        recovery_originals = read_pr_recovery_originals(
            root.resolve(), review_run, original_pack,
            diagnostic_only=True, require_formal_repair=True,
        )
        recovery_originals.assert_unchanged()
    except (ValueError, OSError, GitError) as exc:
        return PRReviewFixResult(dry_run=dry_run,status=PRReviewCommandStatus.BLOCKED, review_id=review_run.review_id,
                                 blocker=f"Rejected reviewer feedback cannot authorize a fix: {exc}",
                                 next_action="Preserve the rejected feedback and rerun within the original review scope.")

    store = LoopArtifactStore(root.resolve())
    review_dir = store.review_run_dir(review_run.review_id)
    resolution_path = review_dir / "resolution.yaml"
    try:
        existing_round = _read_resolution_round(resolution_path)
    except ResolutionFileError as exc:
        return PRReviewFixResult(dry_run=dry_run,
            status=PRReviewCommandStatus.NEEDS_USER,
            review_id=review_run.review_id,
            resolution_path=str(resolution_path),
            blocker=str(exc),
            next_action="Fix resolution.yaml syntax before continuing PR review.",
        )
    if existing_round >= effective_max_rounds:
        return PRReviewFixResult(dry_run=dry_run,
            status=PRReviewCommandStatus.NEEDS_USER,
            review_id=review_run.review_id,
            resolution_path=str(resolution_path),
            round_number=existing_round,
            blocker=(
                f"PR review fix loop reached max rounds ({effective_max_rounds})."
            ),
            next_action="Inspect unresolved findings manually or increase --max-rounds.",
        )

    resolution_statuses = _load_resolution_statuses(resolution_path)
    resolution_records = _load_resolution_records(resolution_path)
    selected = [
        finding
        for finding in findings.findings
        if finding.severity in {FindingSeverity.BLOCKER, FindingSeverity.REQUIRED}
        and resolution_statuses.get(finding.id, FindingResolutionStatus.UNRESOLVED)
        == FindingResolutionStatus.UNRESOLVED
    ]
    advisory = [
        finding
        for finding in findings.findings
        if finding.severity == FindingSeverity.ADVISORY
        and resolution_statuses.get(finding.id, FindingResolutionStatus.UNRESOLVED)
        == FindingResolutionStatus.UNRESOLVED
    ]
    round_number = existing_round + 1
    fix_plan_path = review_dir / "fix-plan.md"
    if dry_run:
        return PRReviewFixResult(dry_run=dry_run,
            status=PRReviewCommandStatus.READY,
            review_id=review_run.review_id,
            fix_plan_path=str(fix_plan_path),
            resolution_path=str(resolution_path),
            selected_findings_count=len(selected),
            skipped_advisory_count=len(advisory),
            round_number=round_number,
            next_action="Dry run only; rerun without --dry-run to write fix artifacts.",
        )
    try:
        formal_originals = _read_pr_formal_originals(
            recovery_originals, review_run, original_pack,
            require_repair=True, normal_production=True,
        )
        recovery_originals.assert_unchanged()
        if formal_originals is not None and formal_originals["inputs"]:
            # fix 会覆盖已有的 R1 输入；与 verify/rerun 共用完整 manifest 原件保全。
            _preserve_formal_originals(root.resolve(), review_run, formal_originals)
    except (ValueError, OSError, GitError) as exc:
        return PRReviewFixResult(
            dry_run=dry_run, status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            blocker=f"Unable to preserve formal review before fix: {exc}",
            next_action="Restore the original formal review inputs before author repair.",
        )
    store.write_markdown_artifact(
        fix_plan_path,
        _render_fix_plan(review_run, selected, advisory, round_number),
    )
    store.write_yaml_artifact(
        resolution_path,
        {
            "schema_version": "1",
            "artifact_kind": "review-resolution",
            "review_id": review_run.review_id,
            "loop_id": review_run.loop_id,
            "round_number": round_number,
            "finding_resolutions": _next_resolution_records(
                findings=findings,
                selected=selected,
                resolution_statuses=resolution_statuses,
                resolution_records=resolution_records,
            ),
        },
    )
    review_run.next_action = _FIX_REVIEW_NEXT_ACTION
    LoopArtifactStore(root.resolve()).write_json_artifact(review_run_path, review_run)
    return PRReviewFixResult(dry_run=dry_run,
        status=PRReviewCommandStatus.READY,
        review_id=review_run.review_id,
        fix_plan_path=str(fix_plan_path),
        resolution_path=str(resolution_path),
        selected_findings_count=len(selected),
        skipped_advisory_count=len(advisory),
        round_number=round_number,
        next_action=review_run.next_action,
    )


def _next_resolution_records(
    *,
    findings: ReviewFindings,
    selected: list[ReviewFinding],
    resolution_statuses: dict[str, FindingResolutionStatus],
    resolution_records: dict[str, FindingResolution],
) -> list[dict[str, object]]:
    current_ids = {finding.id for finding in findings.findings}
    preserved = [
        record.model_dump(mode="json")
        for finding in findings.findings
        if finding.id in current_ids
        and resolution_statuses.get(finding.id)
        in {
            FindingResolutionStatus.FIXED,
            FindingResolutionStatus.WAIVED,
            FindingResolutionStatus.NOT_APPLICABLE,
        }
        for record in [resolution_records.get(finding.id)]
        if record is not None
    ]
    generated = [
        {
            "finding_id": finding.id,
            "status": FindingResolutionStatus.UNRESOLVED.value,
            "reason": "",
            "evidence_refs": [],
            "operator": "",
            "resolved_at": "",
        }
        for finding in selected
    ]
    return [*preserved, *generated]


@_quantified_pr_write_guard
def rerun_pr_review(
    root: Path,
    *,
    provider_command: list[str] | None = None,
    mock_fixture: MockReviewerFixture = MockReviewerFixture.CLEAN,
    provider_timeout_seconds: float = 60.0,
    max_diff_bytes: int = 500_000,
    repair_scope_input: str = "",
    repair_scope_sha256: str = "",
) -> PRReviewStartResult:
    """Regenerate review pack and rerun provider after scope drift checks."""

    limit_blocker = review_diff_limit_blocker(max_diff_bytes)
    if limit_blocker:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.NEEDS_USER,
            provider_id="",
            blocker=limit_blocker,
            next_action="Set --max-diff-bytes to a positive integer for this invocation.",
        )
    timeout_blocker = _provider_timeout_blocker(provider_timeout_seconds)
    if timeout_blocker:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.NEEDS_USER,
            provider_id="",
            blocker=timeout_blocker,
            next_action="Set --provider-timeout-seconds to a finite positive number.",
        )
    try:
        review_run, _ = _load_current_review_run(root)
    except FileNotFoundError as exc:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.NO_REVIEW,
            provider_id="",
            blocker=str(exc),
            next_action="Run ai-sdlc pr-review start --base <branch>.",
        )
    except (json.JSONDecodeError, ValidationError, ValueError, OSError) as exc:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.BLOCKED,
            provider_id="",
            blocker=f"Current PR review artifacts are malformed: {exc}",
            next_action="Rerun ai-sdlc pr-review start.",
        )
    try:
        old_pack = _load_review_pack(root.resolve(), review_run.review_pack_path)
    except FileNotFoundError as exc:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.NO_REVIEW,
            provider_id=review_run.provider_id,
            review_id=review_run.review_id,
            blocker=str(exc),
            next_action="Run ai-sdlc pr-review start --base <branch>.",
        )
    except (json.JSONDecodeError, ValidationError, ValueError, OSError) as exc:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.BLOCKED,
            provider_id=review_run.provider_id,
            review_id=review_run.review_id,
            blocker=f"Current PR review artifacts are malformed: {exc}",
            next_action="Rerun ai-sdlc pr-review start.",
        )
    try:
        rerun_originals = read_pr_rerun_originals(root.resolve(), review_run, old_pack)
        findings = rerun_originals.findings
        formal_originals = rerun_originals.formal_originals
        rerun_originals.reader.assert_unchanged()
    except (ValueError, OSError, GitError) as exc:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.BLOCKED, provider_id=review_run.provider_id,
            review_id=review_run.review_id, loop_id=review_run.loop_id,
            blocker=f"PR review originals cannot authorize continuation: {exc}",
            next_action="Preserve the original formal review and its remaining repair obligation.",
        )

    try:
        current_changed = set(
            _current_changed_paths_for_review_run(root.resolve(), review_run)
        )
    except GitError as exc:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.BLOCKED,
            provider_id=review_run.provider_id,
            review_id=review_run.review_id,
            blocker=str(exc),
            next_action="Fix the saved diff source before rerunning PR review.",
        )

    finding_files = {finding.file for finding in findings.findings}
    old_changed = set(old_pack.changed_files)
    expanded = current_changed - old_changed - finding_files
    if expanded and not (repair_scope_input or repair_scope_sha256):
        return PRReviewStartResult(
            status=PRReviewCommandStatus.NEEDS_USER,
            provider_id=review_run.provider_id,
            review_id=review_run.review_id,
            blocker=(
                "Scope drift detected outside reviewed findings: "
                + ", ".join(sorted(expanded))
            ),
            next_action="Split unrelated changes or start a fresh PR review.",
        )

    resolution_path = _resolve_repo_path(
        root.resolve(),
        review_run.review_pack_path,
    ).with_name("resolution.yaml")
    try:
        resolution_statuses = _load_resolution_statuses(resolution_path)
    except ResolutionFileError as exc:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.BLOCKED,
            provider_id=review_run.provider_id,
            review_id=review_run.review_id,
            blocker=str(exc),
            next_action="Fix resolution.yaml syntax before rerunning PR review.",
        )
    unresolved = _unresolved_counts(findings, resolution_statuses)
    if (
        unresolved[FindingSeverity.BLOCKER] > 0
        or unresolved[FindingSeverity.REQUIRED] > 0
    ):
        return PRReviewStartResult(
            status=PRReviewCommandStatus.BLOCKED,
            provider_id=review_run.provider_id,
            review_id=review_run.review_id,
            blocker=(
                "Unresolved PR review findings remain before rerun: "
                f"{unresolved[FindingSeverity.BLOCKER]} BLOCKER, "
                f"{unresolved[FindingSeverity.REQUIRED]} REQUIRED."
            ),
            next_action=(
                "Run ai-sdlc pr-review fix, update resolution.yaml, then rerun."
            ),
        )

    try:
        resolution_round = _read_resolution_round(resolution_path)
    except ResolutionFileError as exc:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.BLOCKED,
            provider_id=review_run.provider_id,
            review_id=review_run.review_id,
            blocker=str(exc),
            next_action="Fix resolution.yaml syntax before rerunning PR review.",
        )
    expected_repair_source = None
    if repair_scope_input or repair_scope_sha256:
        try:
            expected_repair_source = _admit_repair_scope(
                root.resolve(),
                review_run,
                old_pack,
                findings,
                resolution_path,
                resolution_round,
                expanded,
                repair_scope_input,
                repair_scope_sha256,
            )
        except (ValueError, OSError, GitError, subprocess.SubprocessError, WorktreeSnapshotError) as exc:
            return PRReviewStartResult(
                status=PRReviewCommandStatus.BLOCKED,
                provider_id=review_run.provider_id,
                review_id=review_run.review_id,
                blocker=f"Repair scope confirmation rejected: {exc}",
                next_action="Preserve the original review and confirm the exact current repair scope.",
            )
    try:
        rerun_originals.reader.assert_unchanged()
        test_results_refs = _formal_originals_pack_refs(root.resolve(), review_run, old_pack, formal_originals)
        if rerun_originals.technical_failure:
            previous_findings_path = rerun_originals.previous_findings_path
            reference = _preserve_pr_provider_failure(root.resolve(), review_run, rerun_originals)
            test_results_refs = [*test_results_refs, reference]
        else:
            previous_findings_path = _snapshot_previous_findings(
                root.resolve(), review_run, round_number=resolution_round,
                formal_originals=formal_originals,
            )
        if formal_originals is not None:
            current_formal = _RecoveryOriginalReader(root.resolve())
            _read_pr_formal_originals(
                current_formal, review_run, old_pack, require_repair=True,
                require_preserved=True, publishing_originals=formal_originals,
            )
            current_formal.assert_unchanged()
    except (OSError, ValueError) as exc:
        return PRReviewStartResult(
            status=PRReviewCommandStatus.BLOCKED,
            provider_id=review_run.provider_id,
            review_id=review_run.review_id,
            blocker=f"Unable to preserve previous findings before rerun: {exc}",
            next_action="Inspect findings.json before rerunning PR review.",
        )
    result = start_pr_review(
        PRReviewStartOptions(
            root=root,
            base_ref=review_run.base_ref,
            head_ref=_resolvable_head_ref_for_diff_source(review_run),
            diff_source=review_run.diff_source.source_kind,
            patch_file=review_run.diff_source.patch_file,
            source_id=review_run.diff_source.source_id,
            source_provider=review_run.diff_source.scm_host_type,
            provider_id=review_run.provider_id,
            model_selector=review_run.model_selector,
            current_model=review_run.resolved_model,
            provider_command=provider_command or review_run.provider_command,
            provider_timeout_seconds=provider_timeout_seconds,
            max_diff_bytes=max_diff_bytes,
            code_egress=review_run.code_egress,
            code_egress_confirmed=review_run.code_egress_confirmed,
            review_id=review_run.review_id,
            loop_id=review_run.loop_id,
            mock_fixture=mock_fixture,
            clear_stale_artifacts=False,
            preserve_resolution_history=True,
            decision_mode=review_run.decision_mode,
            decision_capability=review_run.decision_capability,
            decision_staged_tree_oid=review_run.decision_staged_tree_oid,
            decision_started_at_ms=review_run.decision_started_at_ms,
            expected_repair_source=expected_repair_source,
            test_results_refs=test_results_refs,
        )
    )
    if result.status == PRReviewCommandStatus.STARTED:
        try:
            _write_finding_history(
                root.resolve(),
                review_run=review_run,
                previous_findings=findings,
                previous_findings_path=previous_findings_path,
                current_findings_path=result.findings_path,
            )
        except (json.JSONDecodeError, ValidationError, ValueError, OSError) as exc:
            return PRReviewStartResult(
                status=PRReviewCommandStatus.BLOCKED,
                provider_id=review_run.provider_id,
                review_id=review_run.review_id,
                blocker=f"Unable to write finding-history.json: {exc}",
                next_action="Inspect current findings.json before continuing PR review.",
            )
        try:
            _reset_rerun_resolution_artifacts(root.resolve(), review_run.review_id)
        except ResolutionFileError as exc:
            return PRReviewStartResult(
                status=PRReviewCommandStatus.BLOCKED,
                provider_id=review_run.provider_id,
                review_id=review_run.review_id,
                blocker=str(exc),
                next_action="Fix resolution.yaml syntax before rerunning PR review.",
            )
    return result





class _ReviewPackPublication:
    """六件发布共用保全及归属校验；现场准入仍由既有 workspace 职责负责。"""

    def __init__(self, root: Path, directory: Path) -> None:
        self.root, self.directory = root, directory
        self.expected_publication: dict[Path, bytes] = {}
        self.published: dict[Path, bytes] = {}
        self.publication_originals = {directory / name: self._read_optional(directory / name)
                                      for name in _REPAIR_PACK_OUTPUTS}
        self.protected: dict[Path, bytes] | None = None

    def _read_optional(self, path: Path) -> bytes | None:
        return read_stable_bytes(self.root, path) if _stable_regular_file_exists(self.root, path) else None

    def before_publish(self) -> None:
        if any(self._read_optional(path) != raw for path, raw in self.publication_originals.items()):
            raise ValueError("review publication originals changed before writing")
        if self.protected is None:
            # 原生 R1 保全和 legacy 合法 stale 清理均已完成，只在首次写入前绑定一次。
            self.protected = {path: read_stable_bytes(self.root, path) for path in self.directory.iterdir()
                              if path.name not in _REPAIR_PACK_OUTPUTS and path.is_file()}

    def record_publication(self, path: Path, raw: bytes) -> None:
        if self.protected is None:
            raise ValueError("review publication guard has not captured its originals")
        if path.parent != self.directory or path.name not in _REPAIR_PACK_OUTPUTS:
            raise ValueError("unexpected publication outside the exact review pack")
        if path in self.expected_publication and self.expected_publication[path] != raw:
            raise ValueError("review pack publication changed its intended bytes")
        self.expected_publication[path] = raw

    def seal_pack(self, result: ReviewPackBuildResult) -> None:
        published = {self.directory / name: read_stable_bytes(self.root, self.directory / name)
                     for name in _REPAIR_PACK_OUTPUTS}
        if published != self.expected_publication:
            raise ValueError("published review artifact differs from its intended original bytes")
        pack = result.review_pack
        if pack is None or ReviewPack.model_validate_json(published[self.directory / "review-pack.json"]) != pack:
            raise ValueError("published review pack differs from the completed build")
        for name, expected in (("diff.patch", pack.diff_digest), ("source-resolution.json", pack.source_resolution_digest)):
            if "sha256:" + hashlib.sha256(published[self.directory / name]).hexdigest() != expected:
                raise ValueError("published source or diff differs from the completed build")
        self.published = published

    def rollback_publication(self, reason: str) -> Path:
        """先保全失败发布；只撤回六件原产物中仍可逐项归属本调用的自写子集。"""
        if self.protected is None or not self.expected_publication or not self.expected_publication.keys() <= self.publication_originals.keys():
            raise ValueError("publication is outside the original six-file binding")
        observed = {path: self._read_optional(path) for path in self.publication_originals}
        protected = self.protected
        manifest = {
            "schema_version": 1, "reason": reason,
            "original_review_refs": {str(path.relative_to(self.root)) if path.is_relative_to(self.root) else str(path):
                                     hashlib.sha256(raw).hexdigest() for path, raw in protected.items()},
            "before": {path.name: hashlib.sha256(raw).hexdigest() if raw is not None else None
                       for path, raw in self.publication_originals.items()},
            "intended": {path.name: hashlib.sha256(raw).hexdigest() for path, raw in self.expected_publication.items()},
            "observed": {path.name: hashlib.sha256(raw).hexdigest() if raw is not None else None
                         for path, raw in observed.items()},
        }
        manifest_raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        archived = self.directory / "technical-failures" / ("publication-" + hashlib.sha256(manifest_raw).hexdigest())
        store = LoopArtifactStore(self.root)
        for prefix, values in (("before", self.publication_originals), ("intended", self.expected_publication), ("observed", observed)):
            for path, raw in values.items():
                if raw is not None:
                    store.write_bytes_artifact(archived / (prefix + "-" + path.name), raw, immutable=True)
        store.write_bytes_artifact(archived / "publication-manifest.json", manifest_raw, immutable=True)
        if any(path not in self.expected_publication and read_stable_bytes(path.parent, path) != raw for path, raw in protected.items()):
            raise ValueError(f"publication recovery original changed; preserved at {archived}")
        if any(raw != self.publication_originals[path]
               and (path not in self.expected_publication or raw != self.expected_publication[path])
               for path, raw in observed.items()):
            raise ValueError(f"published bytes changed or are missing; no restoration performed; preserved at {archived}")
        for path, original in self.publication_originals.items():
            if self._read_optional(path) != observed[path]:
                raise ValueError(f"publication changed before restoring {path.name}; preserved at {archived}")
            if observed[path] != original:
                if original is None:
                    path.unlink()
                else:
                    store.write_bytes_artifact(path, original)
        if any(self._read_optional(path) != raw for path, raw in self.publication_originals.items()):
            raise ValueError(f"restored publication changed; preserved at {archived}")
        return archived

    def assert_published(self) -> None:
        if not self.published or any(read_stable_bytes(self.root, p) != raw for p, raw in self.published.items()):
            raise ValueError("published review pack changed before provider launch")


_REPAIR_PACK_OUTPUTS = ("source-resolution.json", "changed-files.txt", "model-resolution.json",
                        "redaction-report.json", "diff.patch", "review-pack.json")


def _admit_repair_scope(
    root: Path,
    run: ReviewRun,
    pack: ReviewPack,
    findings: ReviewFindings,
    resolution_path: Path,
    resolution_round: int,
    expanded: set[str],
    input_path: str,
    requested_sha256: str,
) -> SourceAdapterResolution:
    rerun_originals = read_pr_rerun_originals(root, run, pack)
    rerun_originals.reader.assert_unchanged()
    # 旧输出可省略 created_at；解析时补出的当前时间不能冒充原件中的差异。
    if rerun_originals.findings.model_dump(exclude_unset=True) != findings.model_dump(exclude_unset=True):
        raise ValueError("repair scope findings differ from their authenticated original")
    if not input_path or not re.fullmatch(r"[0-9a-f]{64}", requested_sha256):
        raise ValueError("request file and its raw SHA256 are required")
    request_path = Path(input_path)
    if not request_path.is_absolute():
        request_path = root / request_path
    request_path = request_path.parent.resolve(strict=True) / request_path.name
    raw_request = read_stable_bytes(request_path.parent, request_path)
    if hashlib.sha256(raw_request).hexdigest() != requested_sha256:
        raise ValueError("request raw SHA256 mismatch")
    request = RepairScopeInput.model_validate_json(raw_request)
    if (
        run.diff_source.source_kind != DiffSourceKind.LOCAL_STAGED
        or pack.diff_source.source_kind != DiffSourceKind.LOCAL_STAGED
        or (request.review_id, request.loop_id, request.head_commit)
        != (run.review_id, run.loop_id, run.head_commit)
        or (pack.review_id, pack.loop_id, pack.head_commit)
        != (run.review_id, run.loop_id, run.head_commit)
        or request.resolution_round != resolution_round
        or {item.path for item in request.dependencies} != expanded
    ):
        raise ValueError(
            "original review identity, round or exact expanded files mismatch"
        )
    directory = LoopArtifactStore(root).review_run_dir(run.review_id)
    paths = {
        "review-pack.json": _resolve_repo_path(root, run.review_pack_path),
        "findings.json": _resolve_repo_path(root, rerun_originals.previous_findings_path or run.findings_path),
        "resolution.yaml": resolution_path,
        "review-run.json": directory / "review-run.json",
        "current-review.json": root / CURRENT_REVIEW_PATH,
    }
    originals = {name: read_stable_bytes(root, path) for name, path in paths.items()}
    pointed_run, pointed_path = _load_current_review_run(root)
    if pointed_run != run or pointed_path != paths["review-run.json"]:
        raise ValueError("current review pointer changed during confirmation")
    for name, expected in (
        ("review-pack.json", request.review_pack_sha256),
        ("findings.json", request.findings_sha256),
        ("resolution.yaml", request.resolution_sha256),
    ):
        if hashlib.sha256(originals[name]).hexdigest() != expected:
            raise ValueError(f"original {name} SHA256 mismatch")
    if (
        request.review_pack_sha256 != run.review_pack_digest
        or request.findings_sha256 != hashlib.sha256(rerun_originals.reader.read(paths["findings.json"])).hexdigest()
        or ReviewRun.model_validate_json(originals["review-run.json"]) != run
        or ReviewPack.model_validate_json(originals["review-pack.json"]) != pack
    ):
        raise ValueError("original review changed during confirmation")
    resolution = _parse_resolution_payload(
        originals["resolution.yaml"], name="resolution.yaml"
    )
    if not isinstance(resolution, dict) or not isinstance(
        resolution.get("finding_resolutions"), list
    ):
        raise ValueError("original finding resolutions are invalid")
    if (resolution.get("schema_version") != "1"
            or resolution.get("artifact_kind") != "review-resolution"
            or resolution.get("review_id") != run.review_id
            or resolution.get("loop_id") != run.loop_id):
        raise ValueError("original resolution schema or review identity mismatch")
    if _read_round_file(resolution_path) != resolution_round:
        raise ValueError("current resolution does not bind the original round")
    records = [
        FindingResolution.model_validate(item)
        for item in resolution["finding_resolutions"]
    ]
    ids = [item.finding_id for item in records]
    finding_ids = [item.id for item in findings.findings]
    if (
        len(ids) != len(set(ids))
        or len(finding_ids) != len(set(finding_ids))
        or set(ids) - set(finding_ids)
    ):
        raise ValueError("original finding and resolution IDs must be unique and known")
    by_id = {item.id: item for item in findings.findings}
    by_resolution = {item.finding_id: item for item in records}
    source = resolve_diff_source(
        DiffSourceResolutionOptions(root=root, source_kind="local-staged")
    )
    if (
        source.access_status != SourceAccessStatus.RESOLVED
        or source.head_commit != request.head_commit
        or source.staged_tree_oid != request.staged_tree_oid
    ):
        raise ValueError("current HEAD or staged tree mismatch")
    current = resolve_review_input_for_source(root, source)
    if (
        set(current.changed_files)
        - set(pack.changed_files)
        - {item.file for item in findings.findings}
        != expanded
    ):
        raise ValueError("changed files drifted during confirmation")
    for item in request.dependencies:
        finding = by_id.get(item.finding_id)
        fixed = by_resolution.get(item.finding_id)
        if (
            finding is None
            or finding.severity != FindingSeverity.REQUIRED
            or fixed is None
            or fixed.status != FindingResolutionStatus.FIXED
        ):
            raise ValueError(
                "dependency must bind a unique original REQUIRED finding fixed now"
            )
        entry = _delivery_git_bytes(root, "ls-files", "--stage", "-z", "--", item.path)
        rows = entry.rstrip(b"\0").split(b"\0")
        if len(rows) != 1:
            raise ValueError("dependency must be one ordinary staged file")
        metadata, separator, filename = rows[0].partition(b"\t")
        fields = metadata.split()
        if (
            not separator
            or filename.decode("utf-8") != item.path
            or len(fields) != 3
            or fields[0] not in {b"100644", b"100755"}
            or fields[2] != b"0"
        ):
            raise ValueError("dependency must be one ordinary staged file")
        blob = _delivery_git_bytes(root, "cat-file", "blob", fields[1].decode("ascii"))
        if (
            hashlib.sha256(blob).hexdigest() != item.blob_sha256
            or read_stable_bytes(root, root / item.path) != blob
            or current.source_file_bytes.get(item.path) != blob
        ):
            raise ValueError("dependency staged blob or working file mismatch")
    # 确认原件与当前源均未漂移后才留本地审计；审计先于原历史快照和 provider 基线。
    if (
        resolve_diff_source(
            DiffSourceResolutionOptions(root=root, source_kind="local-staged")
        ).to_descriptor()
        != source.to_descriptor()
        or _read_resolution_round(resolution_path) != resolution_round
    ):
        raise ValueError("source or resolution round drifted during confirmation")
    if (
        any(
            read_stable_bytes(root, paths[name]) != raw
            for name, raw in originals.items()
        )
        or read_stable_bytes(request_path.parent, request_path) != raw_request
    ):
        raise ValueError("confirmation originals changed before audit")
    rerun_originals.reader.assert_unchanged()
    audit = directory / "repair-scope" / request.request_id
    saved = {"request.json": raw_request, **originals}
    saved["audit.json"] = (
        json.dumps(
            {
                "request_sha256": requested_sha256,
                "findings_source_path": _repo_relative_path(root, paths["findings.json"]),
                "artifacts": {
                    name: hashlib.sha256(raw).hexdigest() for name, raw in saved.items()
                },
            },
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode()
    store = LoopArtifactStore(root)
    for name, raw in saved.items():
        path = audit / name
        if (
            _stable_regular_file_exists(root, path)
            and read_stable_bytes(root, path) != raw
        ):
            raise ValueError("request_id already binds different immutable audit bytes")
    for name, raw in saved.items():
        store.write_bytes_artifact(audit / name, raw, immutable=True)
    # 审计写入也占用时间；原状态改变时仅保留审计，不继续快照、覆盖或启动。
    if (
        any(
            read_stable_bytes(root, paths[name]) != raw
            for name, raw in originals.items()
        )
        or read_stable_bytes(request_path.parent, request_path) != raw_request
        or _read_resolution_round(resolution_path) != resolution_round
        or resolve_diff_source(
            DiffSourceResolutionOptions(root=root, source_kind="local-staged")
        ).to_descriptor()
        != source.to_descriptor()
        or any(
            hashlib.sha256(read_stable_bytes(root, root / item.path)).hexdigest()
            != item.blob_sha256
            for item in request.dependencies
        )
    ):
        raise ValueError("confirmation state drifted during audit")
    rerun_originals.reader.assert_unchanged()
    return source


@dataclass
class _RecoveryOriginalReader:
    """同一次消费中的原件闭合；捕获材料缺页不从现场补入。"""

    root: Path
    supplied: Mapping[str, bytes] | None = None
    originals: dict[str, bytes] = field(default_factory=dict)
    directories: dict[str, frozenset[str]] = field(default_factory=dict)
    absent_originals: set[str] = field(default_factory=set)

    def read(self, path: Path) -> bytes:
        key = _repo_relative_path(self.root, path)
        if self.supplied is None:
            raw = read_stable_bytes(self.root, path)
        else:
            captured = self.supplied.get(key)
            if not isinstance(captured, bytes):
                raise ValueError("recovery-original-missing-from-capture: " + key)
            raw = captured
        if key in self.originals and self.originals[key] != raw:
            raise ValueError("recovery-original-drift: " + key)
        self.originals[key] = raw
        return raw

    def names(self, path: Path) -> frozenset[str]:
        key = _repo_relative_path(self.root, path)
        if self.supplied is None:
            names = frozenset(p.name for p in path.iterdir())
        else:
            prefix = key + "/"
            names = frozenset(p[len(prefix):].split("/")[0] for p in self.supplied if p.startswith(prefix))
        if key in self.directories and self.directories[key] != names:
            raise ValueError("recovery-original-directory-drift: " + key)
        self.directories[key] = names
        return names

    def assert_unchanged(self, *, expected_writes: Mapping[str, bytes] | None = None) -> None:
        # 现场只作末尾漂移复核，不能为 supplied 缺页提供输入；预期写入只能由 owner 在写前声明。
        for key in self.absent_originals:
            if _stable_regular_file_exists(self.root, self.root / key):
                raise ValueError("recovery-original-absence-drift: " + key)
        for key, names in self.directories.items():
            if frozenset(p.name for p in (self.root / key).iterdir()) != names:
                raise ValueError("recovery-original-directory-drift: " + key)
        for key, raw in {**self.originals, **(expected_writes or {})}.items():
            if read_stable_bytes(self.root, self.root / key) != raw:
                raise ValueError("recovery-original-drift: " + key)
        for key, names in self.directories.items():
            if frozenset(p.name for p in (self.root / key).iterdir()) != names:
                raise ValueError("recovery-original-directory-drift: " + key)


class _FormalReviewOriginals(TypedDict):
    run: str
    outcome: str
    context: str | None
    inputs: dict[str, str]


def _parse_formal_originals(value: object) -> _FormalReviewOriginals:
    if not isinstance(value, dict) or set(value) != {"run", "outcome", "context", "inputs"}:
        raise ValueError("formal review original members are incomplete")
    run_raw, outcome_raw = value["run"], value["outcome"]
    context_raw, input_values = value["context"], value["inputs"]
    if (not isinstance(run_raw, str) or not isinstance(outcome_raw, str)
            or (context_raw is not None and not isinstance(context_raw, str))
            or not isinstance(input_values, dict)):
        raise ValueError("formal review original members are incomplete")
    inputs: dict[str, str] = {}
    for key, encoded in input_values.items():
        if not isinstance(key, str) or not isinstance(encoded, str):
            raise ValueError("formal review input original bytes are invalid")
        inputs[key] = encoded
    return {"run": run_raw, "outcome": outcome_raw, "context": context_raw, "inputs": inputs}


def _formal_originals_digest(originals: _FormalReviewOriginals) -> str:
    # finding-history 的映射正常更新；只有保存的原件内容参与永久身份。
    raw = json.dumps(originals, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _formal_originals_reference_digest(root: Path, run: ReviewRun, pack: ReviewPack) -> str | None:
    references = [ref for ref in pack.test_results_refs if ref.startswith("pr-formal-originals")]
    if not references:
        return None
    path = _repo_relative_path(root, LoopArtifactStore(root).review_run_dir(run.review_id) / "finding-history.json")
    prefix = f"{_FORMAL_ORIGINALS_REFERENCE}{path}#sha256:"
    if (len(references) != 1 or not references[0].startswith(prefix)
            or re.fullmatch(r"[0-9a-f]{64}", references[0][len(prefix):]) is None):
        raise ValueError("formal review original reference is malformed, duplicated, or conflicts with its location")
    return references[0][len(prefix):]


def _formal_originals_pack_refs(
    root: Path, run: ReviewRun, pack: ReviewPack, originals: _FormalReviewOriginals | None,
) -> list[str]:
    if originals is None:
        return list(pack.test_results_refs)
    digest = _formal_originals_digest(originals)
    previous = _formal_originals_reference_digest(root, run, pack)
    if previous is not None and previous != digest:
        raise ValueError("formal review original reference changed before publication")
    if previous is not None:
        return list(pack.test_results_refs)
    path = _repo_relative_path(root, LoopArtifactStore(root).review_run_dir(run.review_id) / "finding-history.json")
    return [*pack.test_results_refs, f"{_FORMAL_ORIGINALS_REFERENCE}{path}#sha256:{digest}"]


def pr_review_requires_original_capture(
    run: Mapping[str, object], pack: Mapping[str, object],
) -> bool:
    """原生各种提供方共用原件捕获；旧通用映射保留原解析契约。"""
    refs = pack.get("test_results_refs")
    return (run.get("artifact_kind") == "review-run" or run.get("provider_id") == "local-agent"
            or (isinstance(refs, list) and any(isinstance(ref, str) and ref.startswith("pr-formal-originals") for ref in refs)))


def _read_pr_formal_originals(
    reader: _RecoveryOriginalReader, run: ReviewRun, pack: ReviewPack, *, require_repair: bool = False,
    require_preserved: bool = False, publishing_originals: _FormalReviewOriginals | None = None,
    normal_production: bool = False,
) -> _FormalReviewOriginals | None:
    """原生 R1、其输入原件及当前读取共用同一身份；缺件不能降回未评审。"""
    from ai_sdlc.core.loop_review_service import (
        outcome_path,
        validate_preserved_local_pr_review,
    )

    root = reader.root
    directory = LoopArtifactStore(root).review_run_dir(run.review_id)
    first_path = outcome_path(directory, 1)
    second_path = outcome_path(directory, 2)
    history_path = directory / "finding-history.json"
    if any(path.name.startswith("review-outcome") and path not in {first_path, second_path}
           for path in directory.iterdir()):
        raise ValueError("formal review has an unrecognized outcome original")
    expected_digest = _formal_originals_reference_digest(root, run, pack)
    saved: _FormalReviewOriginals | None = None
    # 当前 pack 的引用即是必须存在的义务；不能先看目录为空就退回从未评审。
    if expected_digest is not None or _stable_regular_file_exists(root, history_path):
        history = json.loads(reader.read(history_path))
        if not isinstance(history, dict):
            raise ValueError("formal review finding history is invalid")
        if "formal_review_originals" in history:
            if (history.get("artifact_kind") != "review-finding-history"
                    or history.get("review_id") != run.review_id
                    or history.get("loop_id") != run.loop_id):
                raise ValueError("formal review finding history identity changed")
            saved = _parse_formal_originals(history["formal_review_originals"])
        if expected_digest is not None and (saved is None or _formal_originals_digest(saved) != expected_digest):
            raise ValueError("formal review original reference does not match preserved content")
    # 原 pack 的首次保全仍须完整；合法提交、关闭的变化在认证 R1 输入后单独判断。
    if saved is not None and expected_digest is None and (
        run.verdict is None or (publishing_originals is not None and publishing_originals != saved)
    ):
        raise ValueError("formal review original reference is missing")
    present = _stable_regular_file_exists(root, first_path)
    second_present = _stable_regular_file_exists(root, second_path)
    # 即使原件与引用一起被移除，已保存的 pack 摘要也不能随之失效；旧无摘要普通模式保持读取。
    if run.review_pack_digest or present or saved is not None or second_present:
        pack_raw = reader.read(_resolve_repo_path(root, run.review_pack_path))
        if (ReviewPack.model_validate_json(pack_raw) != pack
                or hashlib.sha256(pack_raw).hexdigest() != run.review_pack_digest
                or (pack.review_id, pack.loop_id, pack.head_commit, pack.staged_tree_oid)
                != (run.review_id, run.loop_id, run.head_commit, run.staged_tree_oid)):
            raise ValueError("formal review root pack identity changed")
    if not present and saved is None:
        if second_present:
            raise ValueError("formal review round two is missing original round one")
        return None
    first_raw = reader.read(first_path)
    context_path = directory / "decision-context.json"
    context_raw = reader.read(context_path) if run.decision_capability is not None else None
    original_run = run
    if saved is not None:
        original_run = ReviewRun.model_validate_json(saved["run"])
        if (saved["outcome"].encode("utf-8") != first_raw
                or (saved["context"].encode("utf-8") if saved["context"] is not None else None) != context_raw
                or any(getattr(original_run, key) != getattr(run, key) for key in (
                    "review_id", "loop_id", "provider_id", "model_selector", "resolved_model",
                    "code_egress", "code_egress_confirmed", "decision_mode", "decision_capability",
                    "decision_staged_tree_oid", "decision_started_at_ms",
                ))):
            raise ValueError("formal review originals or current identity changed")
    elif require_preserved:
        raise ValueError("formal review originals were not preserved before provider execution")
    first, snapshot, action = validate_preserved_local_pr_review(original_run, first_raw, context_raw)
    if require_repair and (second_present or action not in {"repair", "improve"}):
        raise ValueError("formal review is completed, exhausted, or has no executable repair")
    manifest = first.simulation.manifest if first.simulation is not None else {}
    pack_key = _repo_relative_path(root, _resolve_repo_path(root, run.review_pack_path))
    if saved is not None:
        if set(saved["inputs"]) != set(manifest):
            raise ValueError("formal review input originals are incomplete")
        for key, digest in manifest.items():
            if hashlib.sha256(bytes.fromhex(saved["inputs"][key])).hexdigest() != digest:
                raise ValueError("formal review input original bytes changed")
        if expected_digest is None and first.simulation is not None and (
            manifest.get(pack_key) != run.review_pack_digest
            or bytes.fromhex(saved["inputs"].get(pack_key, "")) != pack_raw
        ):
            raise ValueError("formal review original reference is missing")
        if expected_digest is None and not _matches_formal_run_transition(reader, original_run, run, pack, saved):
            raise ValueError("formal review original reference is missing")
        return saved
    first_pack_digest = manifest.get(pack_key)
    if (saved is None and first_pack_digest and first_pack_digest != run.review_pack_digest
            and action in {"repair", "improve"}
            and not (require_repair or normal_production or publishing_originals is not None)):
        # 旧格式正常修复已替换 R1 输入；R2 尚未判断或已完成，都只保留原判断和上下文。
        # 同一 R1 输入仍完整校验；不同输入不从当前现场补造历史，当前材料另按当前摘要验证。
        # 新引用缺件已在上方拒绝，修复、保存及技术恢复仍须走完整的原件校验。
        reader.read(directory / "review-run.json")
        return None
    # 正常修复时现有治理材料尚在；业务文件只允许从 R1 已有 Git 树读取同 SHA 原件。
    # 技术失败缺失旧材料时上面已拒绝，不能在恢复阶段重新补造此历史。
    inputs: dict[str, str] = {}
    source_paths = {source.path for source in snapshot.context.sources} if snapshot is not None else set()
    for key, digest in manifest.items():
        path = _resolve_repo_path(root, key)
        original = reader.read(path)
        if hashlib.sha256(original).hexdigest() != digest and key in source_paths:
            original = _delivery_git_bytes(root, "show", f"{original_run.staged_tree_oid}:{key}")
        if hashlib.sha256(original).hexdigest() != digest:
            raise ValueError("formal review input original is unavailable: " + key)
        inputs[key] = original.hex()
    originals: _FormalReviewOriginals = {
        "run": reader.read(directory / "review-run.json").decode("utf-8"),
        "outcome": first_raw.decode("utf-8"),
        "context": context_raw.decode("utf-8") if context_raw is not None else None,
        "inputs": inputs,
    }
    return originals


def _matches_formal_run_transition(
    reader: _RecoveryOriginalReader, original: ReviewRun, current: ReviewRun,
    pack: ReviewPack, saved: _FormalReviewOriginals,
) -> bool:
    """同一 R1 原 pack 的历史身份不随原生提交、Close 输出字段改变。"""
    root = reader.root
    directory = LoopArtifactStore(root).review_run_dir(current.review_id)
    current_raw = reader.read(directory / "review-run.json")
    if _matches_fixed_run_bytes(saved["run"].encode("utf-8"), current_raw):
        return True
    if (original.delivery_commit or original.final_report_path or not current.delivery_commit
            or current.delivery_parent_commit != original.head_commit
            or current.diff_source.source_kind != DiffSourceKind.LOCAL_STAGED):
        return False
    # 复用真正提交的父节点、树及当前源码检查；身份允许变化不等于正式评审通过。
    blocker, commit, _tree = _delivery_commit_state(root, current, pack)
    if blocker:
        return False
    expected = original.model_copy(update={
        "delivery_commit": commit, "delivery_parent_commit": original.head_commit,
        "next_action": "Close the unchanged Local PR review.", "updated_at": current.updated_at,
    })
    if current.final_report_path:
        report_path = directory / "final-report.md"
        if current.final_report_path != _repo_relative_path(root, report_path):
            return False
        report = reader.read(report_path)
        findings_path = _resolve_repo_path(root, current.findings_path)
        reader.read(findings_path)
        verification_path = directory / "verification-evidence.json"
        resolution_path = directory / "resolution.yaml"
        for path in (verification_path, resolution_path):
            if _stable_regular_file_exists(root, path):
                reader.read(path)
        findings = _load_findings(root, current, reviewed_artifacts=reader.originals)
        verification = _load_verification_evidence(root, current, reviewed_artifacts=reader.originals)
        resolution_raw = reader.originals.get(_repo_relative_path(root, resolution_path))
        resolution = _parse_resolution_payload(resolution_raw, name=resolution_path.name) if resolution_raw is not None else {}
        statuses, records = _resolution_statuses(resolution), _resolution_records(resolution)
        unresolved = _unresolved_counts(findings, statuses)
        # require-no-blockers 是既有显式 Close 模式；这里重算记录是否为合法输出，
        # 当前是否允许交付仍由原正式评审、严格 Close 及交付读取入口判断。
        verdict, status, _blocker, next_action = _pr_review_close_outcome(
            pack, statuses, unresolved,
            require_no_blockers=(pack.policy_decisions.get("default_close_mode") == "require-no-blockers"
                                 or current.verdict == ReviewVerdict.RISK_ACCEPTED),
        )
        rendered = _render_final_report(
            review_run=expected, review_pack=pack, findings=findings,
            resolution_statuses=statuses, resolution_records=records, verdict=verdict,
            unresolved=unresolved, verification_evidence=verification, next_action=next_action,
        )
        expected_report = (rendered if rendered.endswith("\n") else rendered + "\n").encode("utf-8")
        if report != expected_report:
            return False
        _set_closed_review_run(
            expected, verdict, unresolved, next_action, current.final_report_path,
            hashlib.sha256(report).hexdigest(), status,
        )
    expected_raw = (json.dumps(expected.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")
    return expected_raw == current_raw


def _provider_invocation_matches_review(
    root: Path, run: ReviewRun, pack: ReviewPack, invocation: ProviderRunnerInvocation,
) -> bool:
    directory = LoopArtifactStore(root).review_run_dir(run.review_id)
    pack_path = directory / "review-pack.json"
    output = directory / "findings.json"
    return (
        invocation.provider_id == run.provider_id
        and invocation.resolved_model == run.resolved_model
        and invocation.model_selector == run.model_selector
        and invocation.code_egress == run.code_egress
        and invocation.cwd == str(root)
        and invocation.input_path == str(pack_path)
        and invocation.output_path == str(output)
        and set(invocation.allowlist) == set(pack.reviewer_allowlist)
        and invocation.argv == _expand_command(run.provider_command, pack, pack_path, output)
    )





def _require_provider_feedback_authority(
    reader: _RecoveryOriginalReader, run: ReviewRun, pack: ReviewPack, *,
    diagnostic_only: bool = False,
) -> None:
    if run.provider_id != "local-agent":
        return
    root = reader.root
    pack_path = _resolve_repo_path(root, run.review_pack_path)
    pack_raw = reader.read(pack_path)
    findings_raw = reader.read(_resolve_repo_path(root, run.findings_path))
    findings = ReviewFindings.model_validate_json(findings_raw)
    if (ReviewPack.model_validate_json(pack_raw) != pack
            or (pack.review_id, pack.loop_id, pack.head_commit, pack.staged_tree_oid)
            != (run.review_id, run.loop_id, run.head_commit, run.staged_tree_oid)):
        raise ValueError("provider-feedback-original-pack-identity-mismatch")
    blocker = _reviewer_outputs_tamper_blocker(
        root, run, findings, reviewed_artifacts=reader.originals,
    ) or _findings_scope_blocker(findings, review_pack=pack, review_pack_path=pack_path)
    if blocker:
        raise ValueError(blocker)
    directory = LoopArtifactStore(root).review_run_dir(run.review_id)
    invocation_raw = reader.read(directory / "reviewer-invocation.json")
    invocation = ProviderRunnerInvocation.model_validate_json(invocation_raw)
    if invocation.execution_failure is not None:
        raise ValueError("provider execution failure cannot authorize reviewer feedback")
    if not _provider_invocation_matches_review(root, run, pack, invocation):
        raise ValueError("provider-feedback-original-invocation-identity-mismatch")
    blocker = _exit_code_verdict_blocker(invocation.exit_code, findings.verdict)
    if blocker:
        raise ValueError(blocker)
    if (invocation.completion_proof is not None
            or invocation.status not in {LoopStatus.PASSED, LoopStatus.NEEDS_FIX}):
        # 完成事实与协议都有效才可授权；工作区变化另走原恢复/采纳，不抹成协议失败。
        _validate_completed_provider_invocation(
            root, run, pack, invocation_original=invocation_raw, expected_verdict=findings.verdict,
        )
        if invocation.completion_proof is None:
            raise ValueError("provider-feedback-completion-proof-required")
        raw = invocation.completion_proof.require_complete()
        if raw["launch_status"] != "started" or any(
            raw[key] for key in ("timed_out", "launch_error", "output_io_error", "output_truncated")
        ):
            raise ValueError("provider-feedback-execution-is-incomplete")
        if not diagnostic_only:
            # 恢复现场只准许重新调用；交付必须消费真实重跑后未改工作区的当前调用。
            if invocation.workspace_check is None or invocation.workspace_check.status != "unchanged":
                raise ValueError("provider-feedback-workspace-not-recovered")
            if invocation.status not in {LoopStatus.PASSED, LoopStatus.NEEDS_FIX}:
                raise ValueError("provider-feedback-execution-is-not-eligible")
    elif invocation.launch_status != ProviderLaunchStatus.UNKNOWN:
        # 已发布的旧成功调用没有新字段；新 started 回执丢失证明不能降成旧版本。
        raise ValueError("provider-feedback-completion-proof-required")
    elif invocation.workspace_check is not None and (
        invocation.workspace_check.review_pack_digest != run.review_pack_digest
        or invocation.workspace_check.host_artifact_mutations
        or invocation.workspace_check.status != "unchanged"
    ):
        raise ValueError("provider-feedback-legacy-workspace-unproven")


def read_pr_recovery_originals(
    root: Path, run: ReviewRun, pack: ReviewPack, *,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
    diagnostic_only: bool = False,
    require_formal_repair: bool = False,
) -> _RecoveryOriginalReader:
    """交付和正常修复共用当前原件资格；不消费技术失败的恢复或采纳引用。"""
    reader = _RecoveryOriginalReader(root.resolve(), reviewed_artifacts)
    _require_provider_feedback_authority(reader, run, pack, diagnostic_only=diagnostic_only)
    if pack.diff_digest:
        # 当前 diff 的完整性由当前 pack 负责，不依赖历史 R1 是否恰好包含同一份材料。
        current_diff = reader.read(_resolve_repo_path(reader.root, pack.diff_path))
        if "sha256:" + hashlib.sha256(current_diff).hexdigest() != pack.diff_digest:
            raise ValueError("current review diff does not match its original pack")
    _read_pr_formal_originals(
        reader, run, pack, require_repair=require_formal_repair,
        normal_production=diagnostic_only,
    )
    if pack.workspace_adoption_ref is not None or pack.rejected_feedback_ref is not None:
        raise ValueError("Historical provider recovery references are unsupported.")
    _read_pr_provider_failure_history(reader, run, pack)
    return reader


@dataclass
class _PRRerunOriginals:
    reader: _RecoveryOriginalReader
    findings: ReviewFindings
    formal_originals: _FormalReviewOriginals | None
    previous_findings_path: str = ""

    @property
    def technical_failure(self) -> bool:
        return bool(self.previous_findings_path)


def read_pr_rerun_originals(root: Path, run: ReviewRun, pack: ReviewPack) -> _PRRerunOriginals:
    """只读认证本次续办的旧义务；技术失败分支永不作为当前验收判断。"""
    root = root.resolve()
    directory = LoopArtifactStore(root).review_run_dir(run.review_id)
    reader = _RecoveryOriginalReader(root)
    if ReviewRun.model_validate_json(reader.read(directory / "review-run.json")) != run:
        raise ValueError("current review run changed before continuation")
    formal = _read_pr_formal_originals(
        reader, run, pack, require_repair=True,
        require_preserved=run.status == LoopStatus.BLOCKED and run.verdict is None,
    )
    findings_path = _resolve_repo_path(root, run.findings_path)
    invocation = (
        ProviderRunnerInvocation.model_validate_json(reader.read(directory / "reviewer-invocation.json"))
        if run.provider_id == "local-agent" else None
    )
    execution_failure = invocation.execution_failure if invocation is not None else None
    if execution_failure is not None and (run.verdict is not None or run.findings_digest):
        raise ValueError("interrupted provider diagnostics cannot be a current judgment")
    if execution_failure is None and (findings_path.exists() or run.verdict is not None or run.findings_digest):
        current = read_pr_recovery_originals(root, run, pack, diagnostic_only=True)
        for key in current.originals:
            reader.read(root / key)
        findings = ReviewFindings.model_validate_json(reader.read(findings_path))
        blocker = _reviewer_outputs_tamper_blocker(root, run, findings, reviewed_artifacts=reader.originals)
        blocker = blocker or _findings_scope_blocker(findings, review_pack=pack, review_pack_path=root / run.review_pack_path)
        if blocker:
            raise ValueError(blocker)
        return _PRRerunOriginals(reader, findings, formal)

    if (run.provider_id != "local-agent" or run.status != LoopStatus.BLOCKED
            or run.delivery_commit or run.final_report_path
            or run.diff_source.source_kind != DiffSourceKind.LOCAL_STAGED
            or pack.repo_root != str(root)):
        raise ValueError("unsupported technical continuation without an active local review")
    head_blocker = _reviewed_head_mismatch(root, run)
    if head_blocker:
        raise ValueError(head_blocker)
    assert invocation is not None
    workspace = invocation.workspace_check
    if (not _provider_invocation_matches_review(root, run, pack, invocation)
            or invocation.launch_status != ProviderLaunchStatus.STARTED
            or invocation.isolation_status != "isolated_process"
            or invocation.status != LoopStatus.BLOCKED or invocation.completion_proof is None
            or (invocation.exit_code in {0, 10} and execution_failure is None)
            or workspace is None or workspace.status != "unchanged"
            or workspace.review_pack_digest != run.review_pack_digest
            or workspace.host_artifact_mutations or workspace.workspace_adoption_ref is not None):
        raise ValueError("technical continuation has no authentic failed provider invocation")
    raw = invocation.completion_proof.require_complete()
    if raw["launch_status"] != "started" or raw["exit_code"] != invocation.exit_code:
        raise ValueError("technical invocation differs from its original completion proof")
    if execution_failure is not None:
        # 异常输出的存在与缺席都是原事实；事后插入、删除或改字节不能授权续办。
        if findings_path != directory / "findings.json":
            raise ValueError("provider diagnostic output path differs from its invocation")
        present = _stable_regular_file_exists(root, findings_path)
        if execution_failure.findings_status == "unavailable":
            raise ValueError("provider diagnostic original is unavailable")
        if present != (execution_failure.findings_status == "present"):
            raise ValueError("provider diagnostic original presence changed")
        if not present:
            reader.absent_originals.add(_repo_relative_path(root, findings_path))
        if present and hashlib.sha256(reader.read(findings_path)).hexdigest() != execution_failure.findings_sha256:
            raise ValueError("provider diagnostic original bytes changed")
    diff = reader.read(_resolve_repo_path(root, pack.diff_path))
    if pack.diff_digest != "sha256:" + hashlib.sha256(diff).hexdigest():
        raise ValueError("technical continuation candidate diff original changed")
    resolution_path = directory / "resolution.yaml"
    resolution = _parse_resolution_payload(reader.read(resolution_path), name="resolution.yaml")
    if (not isinstance(resolution, dict) or resolution.get("artifact_kind") != "review-resolution"
            or resolution.get("review_id") != run.review_id or resolution.get("loop_id") != run.loop_id
            or resolution.get("schema_version") != "1"
            or not isinstance(resolution.get("finding_resolutions"), list)
            or type(resolution.get("round_number")) is not int or resolution["round_number"] < 1):
        raise ValueError("technical continuation requires the original author repair record")
    reader.read(directory / "fix-plan.md")
    round_number = resolution["round_number"]
    if _stable_regular_file_exists(root, directory / "resolution-history.yaml"):
        reader.read(directory / "resolution-history.yaml")
    if _read_resolution_round(resolution_path) != round_number:
        raise ValueError("technical continuation repair history round changed")
    previous_path = directory / f"previous-findings-round-{round_number + 1}.json"
    previous_run_path = directory / f"previous-review-run-round-{round_number + 1}.json"
    if any(int(match[1]) > round_number + 1 for path in directory.iterdir()
           if (match := re.fullmatch(r"previous-(?:findings|review-run)-round-(\d+)\.json", path.name))):
        raise ValueError("technical continuation cannot skip a newer preserved judgment")
    # 只认同轮原生快照；不遍历挑选一个较早的 clean，也不补造旧格式证明。
    original = ReviewRun.model_validate_json(reader.read(previous_run_path))
    findings_raw = reader.read(previous_path)
    findings = ReviewFindings.model_validate_json(findings_raw)
    identity = (
        "review_id", "loop_id", "loop_type", "provider_id", "provider_mode", "model_selector",
        "resolved_model", "code_egress", "code_egress_confirmed", "head_commit", "base_commit",
        "review_pack_path", "findings_path", "decision_mode", "decision_capability",
        "decision_staged_tree_oid", "decision_started_at_ms",
    )
    if (any(getattr(original, key) != getattr(run, key) for key in identity)
            or original.diff_source.source_kind != run.diff_source.source_kind
            or original.delivery_commit or original.final_report_path
            or (original.status not in {LoopStatus.PASSED, LoopStatus.NEEDS_FIX}
                and not (formal is not None and original.status == LoopStatus.NEEDS_REVIEW))
            or original.verdict not in {ReviewVerdict.CLEAN, ReviewVerdict.CHANGES_REQUIRED}
            or original.verdict != findings.verdict
            or not original.findings_digest
            or hashlib.sha256(findings_raw).hexdigest() != original.findings_digest
            or findings.provider_id != run.provider_id or findings.resolved_model != run.resolved_model
            or findings.model_selector != run.model_selector):
        raise ValueError("technical continuation previous judgment identity or original digest changed")
    if any(_count_findings(findings, severity) != count for severity, count in (
        (FindingSeverity.BLOCKER, original.unresolved_blockers),
        (FindingSeverity.REQUIRED, original.unresolved_required),
        (FindingSeverity.ADVISORY, original.unresolved_advisory),
    )):
        raise ValueError("technical continuation previous judgment counts changed")
    blocker = _findings_scope_blocker(findings, review_pack=pack, review_pack_path=root / run.review_pack_path)
    if blocker:
        raise ValueError(blocker)
    unresolved = _unresolved_counts(findings, _resolution_statuses(resolution))
    if unresolved[FindingSeverity.BLOCKER] or unresolved[FindingSeverity.REQUIRED]:
        raise ValueError("technical continuation still has unresolved BLOCKER/REQUIRED findings")
    _read_pr_provider_failure_history(reader, run, pack)
    return _PRRerunOriginals(reader, findings, formal, _repo_relative_path(root, previous_path))


def _preserve_pr_provider_failure(root: Path, run: ReviewRun, originals: _PRRerunOriginals) -> str:
    """覆盖 current 前保存当次原件，引用只证明历史完整，不授予通过。"""
    directory = LoopArtifactStore(root).review_run_dir(run.review_id)
    content = {key: raw for key, raw in originals.reader.originals.items() if (root / key).parent == directory}
    manifest = {
        "schema_version": "1", "artifact_kind": "pr-review-technical-failure",
        "review_id": run.review_id, "loop_id": run.loop_id,
        "previous_findings_path": originals.previous_findings_path,
        "originals": {key: hashlib.sha256(raw).hexdigest() for key, raw in content.items()},
    }
    raw_manifest = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    digest = hashlib.sha256(raw_manifest).hexdigest()
    archive = directory / "technical-failures" / ("provider-" + digest)
    store = LoopArtifactStore(root)
    for key, raw in content.items():
        store.write_bytes_artifact(archive / Path(key).name, raw, immutable=True)
    store.write_bytes_artifact(archive / "manifest.json", raw_manifest, immutable=True)
    return f"{_PROVIDER_FAILURE_REFERENCE}{_repo_relative_path(root, archive / 'manifest.json')}#sha256:{digest}"


def _read_pr_provider_failure_history(reader: _RecoveryOriginalReader, run: ReviewRun, pack: ReviewPack) -> None:
    """现场、捕获和交付读取同一份已绑定失败原件；捕获缺件不从现场补齐。"""
    directory = LoopArtifactStore(reader.root).review_run_dir(run.review_id)
    references = [ref for ref in pack.test_results_refs if ref.startswith("pr-provider-failure")]
    if len(set(references)) != len(references):
        raise ValueError("provider failure original reference is duplicated")
    for reference in references:
        path_text, separator, digest = reference.removeprefix(_PROVIDER_FAILURE_REFERENCE).partition("#sha256:")
        expected = directory / "technical-failures" / ("provider-" + digest) / "manifest.json"
        if (not reference.startswith(_PROVIDER_FAILURE_REFERENCE) or not separator
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or path_text != _repo_relative_path(reader.root, expected)):
            raise ValueError("provider failure original reference is malformed or has a foreign location")
        raw = reader.read(expected)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("provider failure original manifest changed")
        manifest = json.loads(raw)
        if (not isinstance(manifest, dict) or manifest.get("artifact_kind") != "pr-review-technical-failure"
                or manifest.get("schema_version") != "1" or manifest.get("review_id") != run.review_id
                or manifest.get("loop_id") != run.loop_id or not isinstance(manifest.get("originals"), dict)):
            raise ValueError("provider failure original manifest identity changed")
        required = {"review-run.json", "review-pack.json", "reviewer-invocation.json", "resolution.yaml", "fix-plan.md", "diff.patch"}
        if not required.issubset({Path(key).name for key in manifest["originals"]}):
            raise ValueError("provider failure original members are incomplete")
        for key, original_digest in manifest["originals"].items():
            if ((reader.root / key).parent != directory
                    or not isinstance(original_digest, str) or re.fullmatch(r"[0-9a-f]{64}", original_digest) is None):
                raise ValueError("provider failure original member identity changed")
            original_raw = reader.read(expected.parent / Path(key).name)
            if hashlib.sha256(original_raw).hexdigest() != original_digest:
                raise ValueError("provider failure original bytes changed: " + key)


def _validate_completed_provider_invocation(
    root: Path, run: ReviewRun, pack: ReviewPack, *,
    invocation_original: bytes, expected_verdict: ReviewVerdict,
) -> ProviderRunnerInvocation:
    """正常调用共用身份与完整清理事实；技术失败不能借诊断读取取得继续资格。"""
    invocation = ProviderRunnerInvocation.model_validate_json(invocation_original)
    if invocation.execution_failure is not None:
        raise ValueError("provider execution failure cannot authorize a completed review")
    # 用已校验 findings 的原判定；当前 run 的 verdict 可在原生 Close 后合法变化。
    completed_status = {
        ReviewVerdict.CLEAN: LoopStatus.PASSED,
        ReviewVerdict.CHANGES_REQUIRED: LoopStatus.NEEDS_FIX,
        ReviewVerdict.BLOCKED: LoopStatus.BLOCKED,
    }.get(expected_verdict)
    if (not _provider_invocation_matches_review(root, run, pack, invocation)
            or invocation.launch_status != ProviderLaunchStatus.STARTED
            or invocation.isolation_status != "isolated_process"
            or completed_status is None or invocation.status != completed_status
            or invocation.completion_proof is None):
        raise ValueError("provider completed invocation identity is unproven")
    raw = invocation.completion_proof.require_complete()
    if raw["launch_status"] != "started" or raw["exit_code"] != invocation.exit_code:
        raise ValueError("provider completed invocation differs from its original proof")
    workspace = invocation.workspace_check
    if (workspace is None or workspace.status != "unchanged"
            or workspace.review_pack_digest != run.review_pack_digest
            or workspace.host_artifact_mutations
            or workspace.workspace_adoption_ref is not None
            or pack.workspace_adoption_ref is not None):
        raise ValueError("provider completed workspace identity is unproven")
    return invocation








_FIX_REVIEW_NEXT_ACTION = "Fix BLOCKER/REQUIRED findings, update resolution.yaml, then run ai-sdlc pr-review rerun."


def _matches_fixed_run_bytes(original: bytes, current: bytes) -> bool:
    if current == original:
        return True
    previous = ReviewRun.model_validate_json(original)
    fixed = previous.model_copy(update={"next_action": _FIX_REVIEW_NEXT_ACTION})
    expected = (json.dumps(fixed.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")
    return current == expected
































def _write_finding_history(
    root: Path,
    *,
    review_run: ReviewRun,
    previous_findings: ReviewFindings,
    previous_findings_path: str,
    current_findings_path: str,
    formal_originals: _FormalReviewOriginals | None = None,
) -> Path:
    current_path = Path(current_findings_path)
    if not current_path.is_absolute():
        current_path = root / current_path
    current_findings = ReviewFindings.model_validate(
        json.loads(current_path.read_text(encoding="utf-8"))
    )
    previous_by_signature = {
        _finding_signature(finding): finding for finding in previous_findings.findings
    }
    current_by_signature = {
        _finding_signature(finding): finding for finding in current_findings.findings
    }
    mappings = [
        {
            "signature": signature,
            "previous_finding_id": previous_by_signature[signature].id,
            "current_finding_id": current_by_signature[signature].id,
            "file": current_by_signature[signature].file,
            "severity": current_by_signature[signature].severity,
        }
        for signature in sorted(
            previous_by_signature.keys() & current_by_signature.keys()
        )
    ]
    path = (
        LoopArtifactStore(root).review_run_dir(review_run.review_id)
        / "finding-history.json"
    )
    if formal_originals is None and _stable_regular_file_exists(root, path):
        previous_history = json.loads(read_stable_bytes(root, path))
        if not isinstance(previous_history, dict):
            raise ValueError("previous finding history is invalid")
        saved = previous_history.get("formal_review_originals")
        formal_originals = _parse_formal_originals(saved) if saved is not None else None
    return LoopArtifactStore(root).write_json_artifact(
        path,
        {
            "schema_version": "1",
            "artifact_kind": "review-finding-history",
            "review_id": review_run.review_id,
            "loop_id": review_run.loop_id,
            "generated_at": utc_now_iso(),
            "previous_findings_path": previous_findings_path,
            "current_findings_path": _repo_relative_path(root, current_path),
            "mappings": mappings,
            **({"formal_review_originals": formal_originals} if formal_originals is not None else {}),
        },
    )


def _preserve_formal_originals(
    root: Path, review_run: ReviewRun, originals: _FormalReviewOriginals,
    *, previous_findings_path: str | None = None,
) -> None:
    """fix、verify 与 rerun 共用原件写入；已保存的内容不重写可变映射。"""
    history_path = LoopArtifactStore(root).review_run_dir(review_run.review_id) / "finding-history.json"
    if _stable_regular_file_exists(root, history_path):
        history = json.loads(read_stable_bytes(root, history_path))
        if not isinstance(history, dict):
            raise ValueError("previous finding history is invalid")
        saved = history.get("formal_review_originals")
        if saved is not None:
            if _parse_formal_originals(saved) != originals:
                raise ValueError("formal review original changed before preserving rerun")
            current_path = _repo_relative_path(root, _resolve_repo_path(root, review_run.findings_path))
            if previous_findings_path and history.get("previous_findings_path") == current_path:
                # verify 时尚未产生上一轮快照；原 rerun 保存后只更新映射的实际去向。
                previous_raw = read_stable_bytes(root, _resolve_repo_path(root, previous_findings_path))
                if previous_raw != read_stable_bytes(root, _resolve_repo_path(root, current_path)):
                    raise ValueError("previous findings changed before formal history handoff")
                history["previous_findings_path"] = previous_findings_path
                LoopArtifactStore(root).write_json_artifact(history_path, history)
            return
    source = _resolve_repo_path(root, review_run.findings_path)
    current_path = _repo_relative_path(root, source)
    _write_finding_history(
        root, review_run=review_run,
        previous_findings=_load_findings(root, review_run),
        previous_findings_path=previous_findings_path or current_path,
        current_findings_path=current_path, formal_originals=originals,
    )


def _snapshot_previous_findings(
    root: Path,
    review_run: ReviewRun,
    *,
    round_number: int,
    formal_originals: _FormalReviewOriginals | None = None,
) -> str:
    source = _resolve_repo_path(root, review_run.findings_path)
    directory = LoopArtifactStore(root).review_run_dir(review_run.review_id)
    destination = directory / f"previous-findings-round-{round_number + 1}.json"
    original_findings = read_stable_bytes(root, source)
    original_run = read_stable_bytes(root, directory / "review-run.json")
    if (
        ReviewRun.model_validate_json(original_run) != review_run
        or not review_run.findings_digest
        or hashlib.sha256(original_findings).hexdigest() != review_run.findings_digest
    ):
        raise ValueError("original review changed before preserving rerun history")
    # 后续失败会覆盖 current run，必须同时保留其原摘要，不能事后现算证明。
    destination.write_bytes(original_findings)
    (directory / f"previous-review-run-round-{round_number + 1}.json").write_bytes(
        original_run
    )
    if formal_originals is not None:
        _preserve_formal_originals(
            root, review_run, formal_originals,
            previous_findings_path=_repo_relative_path(root, destination),
        )
    return _repo_relative_path(root, destination)


def _finding_signature(finding: ReviewFinding) -> str:
    parts = [
        str(finding.severity),
        finding.file,
        str(finding.line or ""),
        finding.claim.strip(),
        finding.risk.strip(),
    ]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _reset_rerun_resolution_artifacts(root: Path, review_id: str) -> None:
    review_dir = LoopArtifactStore(root).review_run_dir(review_id)
    resolution_path = review_dir / "resolution.yaml"
    round_number = _read_resolution_round(resolution_path)
    if round_number > 0:
        LoopArtifactStore(root).write_yaml_artifact(
            review_dir / "resolution-history.yaml",
            {
                "schema_version": "1",
                "artifact_kind": "review-resolution-history",
                "review_id": review_id,
                "round_number": round_number,
            },
        )
    for name in (
        "resolution.yaml",
        "fix-plan.md",
        "final-report.md",
        "verification-evidence.json",
    ):
        try:
            (review_dir / name).unlink()
        except FileNotFoundError:
            continue


@_quantified_pr_write_guard
def close_pr_review(
    root: Path,
    *,
    require_no_blockers: bool = False,
    expected_review_id: str = "",
    expected_loop_id: str = "",
    expected_review_digest: str = "",
    review_input_validator: ReviewInputValidator | None = None,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> PRReviewCloseResult:
    """Close current review with fail-closed verdict semantics."""

    try:
        review_run, review_run_path = _load_current_review_run(
            root,
            reviewed_artifacts=reviewed_artifacts,
        )
        if (
            expected_review_id.strip()
            and review_run.review_id != expected_review_id.strip()
        ) or (
            expected_loop_id.strip() and review_run.loop_id != expected_loop_id.strip()
        ):
            return PRReviewCloseResult(
                status=PRReviewCommandStatus.BLOCKED,
                verdict=ReviewVerdict.BLOCKED,
                blocker=(
                    "Current PR review does not match the reviewed identity; "
                    "rerun expert review for the current pointer before closing."
                ),
                next_action="Rerun local PR expert review before closing.",
            )
        not_closeable = _not_closeable_review_result(
            root.resolve(),
            review_run,
            reviewed_artifacts=reviewed_artifacts,
        )
        if not_closeable is not None:
            return not_closeable
        findings = _load_findings(
            root.resolve(),
            review_run,
            reviewed_artifacts=reviewed_artifacts,
        )
        review_pack = _load_review_pack(
            root.resolve(),
            review_run.review_pack_path,
            reviewed_artifacts=reviewed_artifacts,
        )
        recovery_originals = read_pr_recovery_originals(
            root.resolve(), review_run, review_pack, reviewed_artifacts=reviewed_artifacts,
        )
        # R1/R2 都绑定本次转换前的 current run；后续只接受同一 writer 声明的准确输出。
        if ReviewRun.model_validate_json(recovery_originals.read(review_run_path)) != review_run:
            raise ValueError("recovery-current-review-changed-before-close")
        # 已捕获的评审字节用于准备关闭判断；现场一致性在转换校验及写入前后检查。
        if reviewed_artifacts is None:
            recovery_originals.assert_unchanged()
        reviewed_close_mode = review_pack.policy_decisions.get("default_close_mode")
        if reviewed_close_mode not in {"strict", "require-no-blockers"}:
            raise ValueError(
                "review-pack.json has an invalid default_close_mode policy decision"
            )
        verification_evidence = _load_verification_evidence(
            root.resolve(),
            review_run,
            reviewed_artifacts=reviewed_artifacts,
        )
    except FileNotFoundError as exc:
        return PRReviewCloseResult(
            status=PRReviewCommandStatus.NO_REVIEW,
            blocker=str(exc),
            next_action="Run ai-sdlc pr-review start --base <branch>.",
        )
    except (json.JSONDecodeError, ValidationError, ValueError, OSError) as exc:
        return PRReviewCloseResult(
            status=PRReviewCommandStatus.BLOCKED,
            verdict=ReviewVerdict.BLOCKED,
            blocker=f"Current PR review artifacts are malformed: {exc}",
            next_action="Regenerate findings.json by rerunning PR review.",
        )

    effective_require_no_blockers = (
        require_no_blockers or reviewed_close_mode == "require-no-blockers"
    )

    tamper_blocker = _reviewer_outputs_tamper_blocker(
        root.resolve(),
        review_run,
        findings,
        reviewed_artifacts=reviewed_artifacts,
    )
    if tamper_blocker:
        return PRReviewCloseResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            verdict=ReviewVerdict.BLOCKED,
            unresolved_blockers=review_run.unresolved_blockers,
            unresolved_required=review_run.unresolved_required,
            unresolved_advisory=review_run.unresolved_advisory,
            blocker=tamper_blocker,
            next_action="Rerun PR review before closing.",
        )

    if (
        review_run.status == LoopStatus.BLOCKED
        and not review_run.final_report_path.strip()
    ):
        return PRReviewCloseResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            verdict=ReviewVerdict.BLOCKED,
            unresolved_blockers=review_run.unresolved_blockers,
            unresolved_required=review_run.unresolved_required,
            unresolved_advisory=review_run.unresolved_advisory,
            blocker=findings.blocker or "Current PR review provider run is blocked.",
            next_action="Fix the blocked review provider and rerun PR review before closing.",
        )

    delivery_close = bool(expected_review_digest.strip() or review_input_validator)
    source_kind = DiffSourceKind(review_run.diff_source.source_kind)
    if delivery_close:
        if source_kind != DiffSourceKind.LOCAL_STAGED:
            return PRReviewCloseResult(
                status=PRReviewCommandStatus.BLOCKED,
                review_id=review_run.review_id,
                verdict=ReviewVerdict.BLOCKED,
                blocker="Diagnostic review sources cannot complete delivery Close.",
                next_action="Restart PR review with --diff-source local-staged.",
            )

        delivery_blocker, delivery_commit, _delivery_tree = _delivery_commit_state(
            root.resolve(),
            review_run,
            review_pack,
        )
        if delivery_blocker:
            return PRReviewCloseResult(
                status=PRReviewCommandStatus.BLOCKED,
                review_id=review_run.review_id,
                verdict=ReviewVerdict.BLOCKED,
                unresolved_blockers=review_run.unresolved_blockers,
                unresolved_required=review_run.unresolved_required,
                unresolved_advisory=review_run.unresolved_advisory,
                blocker=delivery_blocker,
                next_action="Commit the exact reviewed staged tree or rerun PR review.",
            )
        review_run.delivery_commit = delivery_commit
        review_run.delivery_parent_commit = review_run.head_commit

        evidence_blocker = _verification_evidence_blocker(
            review_run,
            verification_evidence,
        )
        if evidence_blocker:
            return PRReviewCloseResult(
                status=PRReviewCommandStatus.BLOCKED,
                review_id=review_run.review_id,
                verdict=ReviewVerdict.BLOCKED,
                blocker=evidence_blocker,
                next_action=(
                    "Run ai-sdlc pr-review verify before expert review and Close."
                ),
            )
    else:
        head_mismatch = _reviewed_head_mismatch(root, review_run)
        source_mismatch = _reviewed_diff_source_mismatch(root, review_run)
        dirty_blocker = _reviewed_worktree_dirty(
            root,
            review_run,
            review_pack=review_pack,
        )
        legacy_blocker = head_mismatch or source_mismatch or dirty_blocker
        if legacy_blocker:
            return PRReviewCloseResult(
                status=PRReviewCommandStatus.BLOCKED,
                review_id=review_run.review_id,
                verdict=ReviewVerdict.BLOCKED,
                blocker=legacy_blocker,
                next_action="rerun PR review for the current source before closing.",
            )

    if findings.verdict == ReviewVerdict.BLOCKED:
        return PRReviewCloseResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            verdict=ReviewVerdict.BLOCKED,
            unresolved_blockers=review_run.unresolved_blockers,
            unresolved_required=review_run.unresolved_required,
            unresolved_advisory=review_run.unresolved_advisory,
            blocker=findings.blocker or "PR review provider is blocked.",
            next_action=(
                "Fix the blocked review provider and rerun PR review before closing."
            ),
        )

    resolution_path = _resolve_repo_path(
        root.resolve(),
        review_run.review_pack_path,
    ).with_name("resolution.yaml")
    try:
        if reviewed_artifacts is not None:
            resolution_bytes = _review_snapshot_optional_bytes(
                root.resolve(),
                resolution_path,
                reviewed_artifacts,
            )
            resolution_payload = (
                _parse_resolution_payload(
                    resolution_bytes,
                    name=resolution_path.name,
                )
                if resolution_bytes is not None
                else {}
            )
        else:
            resolution_payload = (
                _load_resolution_payload(resolution_path)
                if resolution_path.exists()
                else {}
            )
        resolution_statuses = _resolution_statuses(resolution_payload)
        resolution_records = _resolution_records(resolution_payload)
    except ResolutionFileError as exc:
        return PRReviewCloseResult(
            status=PRReviewCommandStatus.BLOCKED,
            review_id=review_run.review_id,
            verdict=ReviewVerdict.BLOCKED,
            blocker=str(exc),
            next_action="Fix resolution.yaml syntax before closing PR review.",
        )
    unresolved = _unresolved_counts(findings, resolution_statuses)
    verdict, status, blocker, next_action = _pr_review_close_outcome(
        review_pack, resolution_statuses, unresolved, require_no_blockers=effective_require_no_blockers,
    )

    final_report_path = (
        LoopArtifactStore(root.resolve()).review_run_dir(review_run.review_id)
        / "final-report.md"
    )

    expected_writes: dict[str, bytes] = {}

    def writer() -> PRReviewCloseResult:
        return _write_pr_review_close(
            root=root.resolve(),
            review_run=review_run,
            review_run_path=review_run_path,
            review_pack=review_pack,
            findings=findings,
            resolution_statuses=resolution_statuses,
            resolution_records=resolution_records,
            verdict=verdict,
            unresolved=unresolved,
            verification_evidence=verification_evidence,
            status=status,
            blocker=blocker,
            next_action=next_action,
            final_report_path=final_report_path,
            expected_writes=expected_writes,
            before_write=require_transition_originals_unchanged,
        )

    def require_transition_originals_unchanged() -> None:
        recovery_originals.assert_unchanged(expected_writes=expected_writes)

    revalidate_review_input_at_transition(
        root.resolve(),
        loop_type="local-pr-review",
        loop_id=review_run.loop_id,
        expected_digest=expected_review_digest,
        validator=review_input_validator,
    )
    require_transition_originals_unchanged()
    result = writer()
    require_transition_originals_unchanged()
    return result


def _pr_review_close_outcome(
    review_pack: ReviewPack, resolution_statuses: dict[str, FindingResolutionStatus],
    unresolved: dict[FindingSeverity, int], *, require_no_blockers: bool,
) -> tuple[ReviewVerdict, PRReviewCommandStatus, str, str]:
    """Close 写入与历史读取共用当前 findings 的纯判断。"""
    verdict: ReviewVerdict
    status: PRReviewCommandStatus
    blocker = ""
    next_action = ""
    if unresolved[FindingSeverity.BLOCKER] > 0:
        verdict = ReviewVerdict.BLOCKED
        status = PRReviewCommandStatus.BLOCKED
        blocker = "Unresolved BLOCKER findings remain."
        next_action = "Fix blockers and rerun PR review before closing."
    elif unresolved[FindingSeverity.REQUIRED] > 0 and not require_no_blockers:
        verdict = ReviewVerdict.BLOCKED
        status = PRReviewCommandStatus.BLOCKED
        blocker = "Unresolved REQUIRED findings remain."
        next_action = "Fix required findings or close with --require-no-blockers."
    elif unresolved[FindingSeverity.REQUIRED] > 0:
        verdict = ReviewVerdict.RISK_ACCEPTED
        status = PRReviewCommandStatus.CLOSED
        next_action = "Risk accepted with unresolved REQUIRED findings disclosed."
    elif FindingResolutionStatus.WAIVED in resolution_statuses.values():
        verdict = ReviewVerdict.RISK_ACCEPTED
        status = PRReviewCommandStatus.CLOSED
        next_action = "Risk accepted because one or more findings were waived."
    elif _review_pack_has_incomplete_waiver(review_pack):
        verdict = ReviewVerdict.RISK_ACCEPTED
        status = PRReviewCommandStatus.CLOSED
        next_action = (
            "Risk accepted because the review pack used an incomplete-review waiver."
        )
    else:
        verdict = ReviewVerdict.FULLY_CLEAN
        status = PRReviewCommandStatus.CLOSED
        next_action = "Local PR review closed."

    return verdict, status, blocker, next_action


def _write_pr_review_close(
    *,
    root: Path,
    review_run: ReviewRun,
    review_run_path: Path,
    review_pack: ReviewPack,
    findings: ReviewFindings,
    resolution_statuses: dict[str, FindingResolutionStatus],
    resolution_records: dict[str, FindingResolution],
    verdict: ReviewVerdict,
    unresolved: dict[FindingSeverity, int],
    verification_evidence: PRReviewVerificationEvidence | None,
    status: PRReviewCommandStatus,
    blocker: str,
    next_action: str,
    final_report_path: Path,
    expected_writes: dict[str, bytes] | None = None,
    before_write: Callable[[], None] | None = None,
) -> PRReviewCloseResult:
    store = LoopArtifactStore(root)
    rendered = _render_final_report(
        review_run=review_run,
        review_pack=review_pack,
        findings=findings,
        resolution_statuses=resolution_statuses,
        resolution_records=resolution_records,
        verdict=verdict,
        unresolved=unresolved,
        verification_evidence=verification_evidence,
        next_action=next_action,
    )
    if before_write is not None:
        before_write()
    if expected_writes is not None:
        expected_writes[_repo_relative_path(root, final_report_path)] = (
            rendered if rendered.endswith("\n") else rendered + "\n"
        ).encode("utf-8")
    store.write_markdown_artifact(final_report_path, rendered)
    _persist_closed_review_run(
        store,
        review_run_path,
        review_run,
        verdict,
        unresolved,
        next_action,
        final_report_path,
        status,
        expected_writes=expected_writes,
        before_write=before_write,
    )
    return _pr_review_close_result(
        review_run,
        verdict,
        unresolved,
        status,
        blocker,
        next_action,
        final_report_path,
    )


def _set_closed_review_run(
    review_run: ReviewRun, verdict: ReviewVerdict, unresolved: dict[FindingSeverity, int],
    next_action: str, final_report_path: str, final_report_digest: str, status: PRReviewCommandStatus,
) -> None:
    """只有 Close 负责发布这些字段；读取方用同一规则核对已有输出。"""
    review_run.verdict = verdict
    review_run.final_report_path = final_report_path
    review_run.final_report_digest = final_report_digest
    review_run.unresolved_blockers = unresolved[FindingSeverity.BLOCKER]
    review_run.unresolved_required = unresolved[FindingSeverity.REQUIRED]
    review_run.unresolved_advisory = unresolved[FindingSeverity.ADVISORY]
    review_run.status = (
        LoopStatus.CLOSED
        if status == PRReviewCommandStatus.CLOSED
        else LoopStatus.BLOCKED
    )
    review_run.next_action = next_action


def _persist_closed_review_run(
    store: LoopArtifactStore,
    review_run_path: Path,
    review_run: ReviewRun,
    verdict: ReviewVerdict,
    unresolved: dict[FindingSeverity, int],
    next_action: str,
    final_report_path: Path,
    status: PRReviewCommandStatus,
    *,
    expected_writes: dict[str, bytes] | None = None,
    before_write: Callable[[], None] | None = None,
) -> None:
    _set_closed_review_run(
        review_run, verdict, unresolved, next_action, _repo_relative_path(store.root, final_report_path),
        _file_sha256(final_report_path), status,
    )
    if before_write is not None:
        before_write()
    payload = review_run.model_dump(mode="json")
    if expected_writes is not None:
        # 与现有 JSON store 使用同一格式冻结写前计划，不能反读落盘内容来接受未知变化。
        expected_writes[_repo_relative_path(store.root, review_run_path)] = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
        ).encode("utf-8")
    store.write_json_artifact(review_run_path, payload)


def _pr_review_close_result(
    review_run: ReviewRun,
    verdict: ReviewVerdict,
    unresolved: dict[FindingSeverity, int],
    status: PRReviewCommandStatus,
    blocker: str,
    next_action: str,
    final_report_path: Path,
) -> PRReviewCloseResult:
    return PRReviewCloseResult(
        status=status,
        review_id=review_run.review_id,
        verdict=verdict,
        final_report_path=str(final_report_path),
        unresolved_blockers=unresolved[FindingSeverity.BLOCKER],
        unresolved_required=unresolved[FindingSeverity.REQUIRED],
        unresolved_advisory=unresolved[FindingSeverity.ADVISORY],
        blocker=blocker,
        next_action=next_action,
    )


def _not_closeable_review_result(
    root: Path,
    review_run: ReviewRun,
    *,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> PRReviewCloseResult | None:
    findings_exists = bool(review_run.findings_path.strip()) and (
        _review_snapshot_contains(
            root,
            _resolve_repo_path(root, review_run.findings_path),
            reviewed_artifacts,
        )
        if reviewed_artifacts is not None
        else _resolve_repo_path(root, review_run.findings_path).is_file()
    )
    if (
        review_run.status == LoopStatus.BLOCKED
        and review_run.findings_path.strip()
        and not findings_exists
    ):
        return _blocked_not_closeable_review_result(review_run)
    if review_run.status != LoopStatus.NEEDS_USER and review_run.findings_path.strip():
        return None
    return _blocked_not_closeable_review_result(review_run)


def _blocked_not_closeable_review_result(review_run: ReviewRun) -> PRReviewCloseResult:
    command_status = _status_from_loop_status(review_run.status)
    if command_status not in {
        PRReviewCommandStatus.NEEDS_USER,
        PRReviewCommandStatus.BLOCKED,
    }:
        command_status = PRReviewCommandStatus.BLOCKED
    return PRReviewCloseResult(
        status=command_status,
        review_id=review_run.review_id,
        verdict=ReviewVerdict.BLOCKED,
        unresolved_blockers=review_run.unresolved_blockers,
        unresolved_required=review_run.unresolved_required,
        unresolved_advisory=review_run.unresolved_advisory,
        blocker=(
            "Current PR review is not closeable because the provider run status "
            f"is {review_run.status}."
        ),
        next_action=review_run.next_action
        or "Resolve the provider run and rerun PR review.",
    )


def _review_pack_has_incomplete_waiver(review_pack: ReviewPack) -> bool:
    return review_pack.policy_decisions.get("incomplete_review_waiver") is True


def _precommit_staged_source_blocker(
    root: Path,
    review_run: ReviewRun,
    review_pack: ReviewPack,
) -> str:
    if not review_run.staged_tree_oid.strip():
        return "Reviewed staged tree is missing."
    head_mismatch = _reviewed_head_mismatch(root, review_run)
    if head_mismatch:
        return head_mismatch
    source_mismatch = _reviewed_diff_source_mismatch(root, review_run)
    if source_mismatch:
        return source_mismatch
    return _reviewed_worktree_dirty(root, review_run, review_pack=review_pack)


def _verification_evidence_blocker(
    review_run: ReviewRun,
    evidence: PRReviewVerificationEvidence | None,
) -> str:
    if evidence is None:
        return "Executable Local PR verification evidence is missing."
    if evidence.entries:
        return "Legacy verification strings cannot satisfy Local PR Review."
    if (
        evidence.review_id != review_run.review_id
        or evidence.loop_id != review_run.loop_id
        or evidence.staged_tree_oid != review_run.staged_tree_oid
    ):
        return "Local PR verification evidence does not match the reviewed staged tree."
    if not any(
        result.successful and result.source_digest_before == result.source_digest_after
        for result in evidence.results
    ):
        return "No successful executable verification result matches the reviewed tree."
    return ""


def _current_clean_expert_review(
    root: Path, review_run: ReviewRun, *, expected_digest: str = ""
):
    from ai_sdlc.cli.loop_review_cmd import prepare_current_loop_review
    from ai_sdlc.core.loop_review_service import validate_prepared_outcome_for_close

    prepared, _ = prepare_current_loop_review(
        root, "local-pr-review", review_run.loop_id
    )
    validate_prepared_outcome_for_close(
        prepared,
        expected_digest=expected_digest or prepared.review_input.input_digest,
    )
    return prepared


def _current_expert_outcome_blocker(
    root: Path, review_run: ReviewRun, *, expected_digest: str = ""
) -> str:
    try:
        _current_clean_expert_review(root, review_run, expected_digest=expected_digest)
    except (OSError, ValueError) as exc:
        return f"Local PR expert review is not current and clean: {exc}"
    return ""


@dataclass(frozen=True)
class VerifiedDeliveryCommit:
    """本次闭后消费的独立只读 guard；不补写原评审输入或历史凭据。"""

    root: Path
    review_id: str
    loop_id: str
    reviewed_head: str
    current_commit: str
    staged_tree: str
    review_input_digest: str
    source_boundary_digest: str
    artifact_digests: tuple[tuple[str, str | None], ...]


@dataclass
class _DeliveryReadScope:
    root: Path
    thread_id: int
    proof: VerifiedDeliveryCommit | None = None
    reusable: bool = False
    active: bool = True
    failed: bool = False
    reading: bool = False


_DELIVERY_READ_SCOPE: ContextVar[_DeliveryReadScope | None] = ContextVar(
    "verified_delivery_read_scope", default=None
)


def _active_delivery_read_scope(root: Path) -> _DeliveryReadScope | None:
    scope = _DELIVERY_READ_SCOPE.get()
    if (
        scope is not None
        and scope.active
        and scope.root == root
        and scope.thread_id == threading.get_ident()
    ):
        return scope
    return None


@contextmanager
def verified_delivery_read_scope(root: Path) -> Iterator[None]:
    """同一同步消费只共享已完整核验的 PR guard；退出后复制的 context 也失效。"""
    root = root.resolve(strict=True)
    if _active_delivery_read_scope(root) is not None:
        yield
        return
    scope = _DeliveryReadScope(root, threading.get_ident())
    scope_reset_handle = _DELIVERY_READ_SCOPE.set(scope)
    try:
        yield
        if scope.failed:
            raise ValueError("delivery-proof-scope-invalid")
        if scope.proof is not None:
            # 绕过作用域完整复验；PR prepare 不得借尚未完成的证明递归返回。
            scope.reading = True
            if _read_verified_delivery_commit_uncached(root) != scope.proof:
                raise ValueError("delivery-proof-drift")
    finally:
        scope.active = False
        _DELIVERY_READ_SCOPE.reset(scope_reset_handle)


def read_verified_delivery_commit(root: Path) -> VerifiedDeliveryCommit:
    """复用原合法提交及质量门禁；消费方仍须证明自己的原成果与该树等价。"""
    root = root.resolve(strict=True)
    scope = _active_delivery_read_scope(root)
    if scope is None:
        return _read_verified_delivery_commit_uncached(root)
    if scope.failed or scope.reading:
        scope.failed = True
        raise ValueError("delivery-proof-scope-invalid")
    scope.reading = True
    try:
        if scope.proof is None or not scope.reusable:
            proof = _read_verified_delivery_commit_uncached(root)
            if scope.proof is not None and scope.proof != proof:
                raise ValueError("delivery-proof-drift")
            if scope.proof is None:
                captured = _read_delivery_guard_artifacts(root, proof)
                scope.reusable = _delivery_action_is_time_independent(proof, captured)
                scope.proof = proof
            return proof
        _require_current_delivery_guard(root, scope.proof)
        return scope.proof
    except (GitError, subprocess.SubprocessError) as exc:
        scope.failed = True
        raise ValueError(f"delivery-proof-git-unavailable: {exc}") from exc
    except Exception:
        scope.failed = True
        raise
    finally:
        scope.reading = False


def _read_verified_delivery_commit_uncached(root: Path) -> VerifiedDeliveryCommit:
    try:
        first = _read_delivery_commit(root)
        if _read_delivery_commit(root) != first:
            raise ValueError("delivery-proof-drift")
    except (GitError, subprocess.SubprocessError) as exc:
        raise ValueError(f"delivery-proof-git-unavailable: {exc}") from exc
    return first


def _read_delivery_guard_artifacts(
    root: Path, proof: VerifiedDeliveryCommit
) -> dict[str, bytes]:
    captured = {}
    for path, digest in proof.artifact_digests:
        candidate = root / path
        raw = (
            read_stable_bytes(root, candidate)
            if _stable_regular_file_exists(root, candidate)
            else None
        )
        if (hashlib.sha256(raw).hexdigest() if raw is not None else None) != digest:
            raise ValueError("delivery-proof-artifact-drift")
        if raw is not None:
            captured[path] = raw
    return captured


def _delivery_action_is_time_independent(
    proof: VerifiedDeliveryCommit, captured: Mapping[str, bytes]
) -> bool:
    from ai_sdlc.core.loop_review_models import LoopReviewOutcome
    from ai_sdlc.core.loop_review_service import outcome_path

    directory = Path(".ai-sdlc/reviews/pr") / proof.review_id
    for number in (2, 1):
        path = outcome_path(directory, number).as_posix()
        raw = captured.get(path)
        if raw is not None:
            outcome = LoopReviewOutcome.model_validate_json(raw)
            actual = outcome.b1 or outcome.simulation
            # improve 的有效 stop 依赖当前时间，仍每次走原完整准备与核验。
            return actual is None or actual.decision.action == "stop"
    return False


def _require_current_delivery_guard(root: Path, proof: VerifiedDeliveryCommit) -> None:
    captured = _read_delivery_guard_artifacts(root, proof)
    source = _delivery_source_boundary(
        root, proof.staged_tree, verified_source_digest=proof.source_boundary_digest
    )
    if _delivery_source_digest(source) != proof.source_boundary_digest:
        raise ValueError("delivery-proof-source-drift")
    run, _ = _load_current_review_run(root, reviewed_artifacts=captured)
    pack = _load_review_pack(root, run.review_pack_path, reviewed_artifacts=captured)
    recovery_originals = read_pr_recovery_originals(root, run, pack, reviewed_artifacts=captured)
    recovery_originals.assert_unchanged()
    blocker, commit, tree = _delivery_commit_state(root, run, pack)
    if blocker:
        raise ValueError(blocker)
    if commit != proof.current_commit or tree != proof.staged_tree:
        raise ValueError("delivery-proof-commit-identity-mismatch")
    if source != _delivery_source_boundary(
        root, tree, verified_source_digest=proof.source_boundary_digest
    ):
        raise ValueError("delivery-proof-source-drift")
    recovery_originals.assert_unchanged()
    if _read_delivery_guard_artifacts(root, proof) != captured:
        raise ValueError("delivery-proof-artifact-drift")


def _delivery_source_digest(source: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read_delivery_commit(root: Path) -> VerifiedDeliveryCommit:
    from ai_sdlc.cli.loop_review_cmd import (
        _find_local_review_dir,
        resolve_review_input,
    )
    from ai_sdlc.core.loop_review_service import outcome_path

    # PR 后生材料在自己的 guard 中捕获，不能混入调用方原 R1 的材料映射。
    captured: dict[str, bytes | None] = {}

    def capture(path: Path) -> bytes | None:
        key = path.relative_to(root).as_posix()
        content = (
            read_stable_bytes(root, path)
            if _stable_regular_file_exists(root, path)
            else None
        )
        if key in captured and captured[key] != content:
            raise ValueError("delivery-proof-artifact-drift")
        captured[key] = content
        return content

    pointer = capture(root / CURRENT_REVIEW_PATH)
    if pointer is None:
        raise ValueError("delivery-proof-current-review-missing")
    payload = json.loads(pointer)
    if not isinstance(payload, dict):
        raise ValueError("delivery-proof-current-review-invalid")
    run_path = _resolve_repo_path(root, str(payload.get("review_run_path", "")))
    capture(run_path)
    present = {key: raw for key, raw in captured.items() if raw is not None}
    run, loaded_path = _load_current_review_run(root, reviewed_artifacts=present)
    directory, _, canonical_path = _find_local_review_dir(root, run.loop_id)
    if loaded_path != canonical_path:
        raise ValueError("delivery-proof-review-path-mismatch")
    if (
        run.diff_source.source_kind != DiffSourceKind.LOCAL_STAGED
        or not run.delivery_commit
        or run.delivery_parent_commit != run.head_commit
        or run.status
        in {LoopStatus.BLOCKED, LoopStatus.NEEDS_USER, LoopStatus.NEEDS_FIX}
    ):
        raise ValueError("delivery-proof-recorded-commit-required")
    for name in (
        "review-pack.json",
        "findings.json",
        "verification-evidence.json",
        "resolution.yaml",
        "decision-context.json",
        *(outcome_path(directory, number).name for number in (1, 2)),
        "final-report.md",
    ):
        capture(directory / name)
    present = {key: raw for key, raw in captured.items() if raw is not None}
    pack = _load_review_pack(root, run.review_pack_path, reviewed_artifacts=present)
    findings = _load_findings(root, run, reviewed_artifacts=present)
    evidence = _load_verification_evidence(root, run, reviewed_artifacts=present)
    if (
        pack.review_id != run.review_id
        or pack.loop_id != run.loop_id
        or Path(pack.repo_root).resolve(strict=True) != root
        or pack.head_commit != run.head_commit
        or pack.staged_tree_oid != run.staged_tree_oid
        or pack.diff_source != run.diff_source
    ):
        raise ValueError("delivery-proof-review-pack-identity-mismatch")
    blocker = _reviewer_outputs_tamper_blocker(
        root, run, findings, reviewed_artifacts=present
    ) or _verification_evidence_blocker(run, evidence)
    if blocker:
        raise ValueError(blocker)
    resolution_bytes = captured[
        (directory / "resolution.yaml").relative_to(root).as_posix()
    ]
    statuses = _resolution_statuses(
        _parse_resolution_payload(resolution_bytes, name="resolution.yaml")
        if resolution_bytes is not None
        else {}
    )
    unresolved = _unresolved_counts(findings, statuses)
    if (
        findings.verdict == ReviewVerdict.BLOCKED
        or unresolved[FindingSeverity.BLOCKER]
        or unresolved[FindingSeverity.REQUIRED]
        or FindingResolutionStatus.WAIVED in statuses.values()
        or _review_pack_has_incomplete_waiver(pack)
    ):
        raise ValueError("delivery-proof-review-not-clean")
    if run.status == LoopStatus.CLOSED:
        final_path = directory / "final-report.md"
        if (
            run.verdict != ReviewVerdict.FULLY_CLEAN
            or _resolve_repo_path(root, run.final_report_path) != final_path
            or _final_report_tamper_blocker(final_path, run)
        ):
            raise ValueError("delivery-proof-closed-report-invalid")

    source = _delivery_source_boundary(root, run.staged_tree_oid)
    if (
        source["head"] != run.delivery_commit
        or source["commit_parents"] != f"{run.delivery_commit} {run.head_commit}"
        or source["commit_tree"] != run.staged_tree_oid
    ):
        raise ValueError("delivery-proof-commit-identity-mismatch")
    prepared = _current_clean_expert_review(root, run)
    review_capture: dict[str, bytes] = {}
    reviewed = resolve_review_input(
        root,
        loop_type="local-pr-review",
        loop_id=run.loop_id,
        review_round_number=prepared.review_input.round_number,
        captured_artifacts=review_capture,
        capture_all=True,
    )
    if reviewed != prepared.review_input:
        raise ValueError("delivery-proof-review-input-drift")
    for key, raw in review_capture.items():
        if key in captured and captured[key] != raw:
            raise ValueError("delivery-proof-artifact-drift")
        captured[key] = raw
    blocker, commit, tree = _delivery_commit_state(root, run, pack)
    if blocker:
        raise ValueError(blocker)
    if source != _delivery_source_boundary(root, tree):
        raise ValueError("delivery-proof-source-drift")
    for key in tuple(captured):
        capture(root / key)
    recovery_originals = read_pr_recovery_originals(
        root, run, pack, reviewed_artifacts={key: raw for key, raw in captured.items() if raw is not None},
    )
    recovery_originals.assert_unchanged()
    return VerifiedDeliveryCommit(
        root=root,
        review_id=run.review_id,
        loop_id=run.loop_id,
        reviewed_head=run.head_commit,
        current_commit=commit,
        staged_tree=tree,
        review_input_digest=reviewed.input_digest,
        source_boundary_digest=_delivery_source_digest(source),
        artifact_digests=tuple(
            (key, hashlib.sha256(raw).hexdigest() if raw is not None else None)
            for key, raw in sorted(captured.items())
        ),
    )


def _delivery_source_boundary(
    root: Path, staged_tree: str, *, verified_source_digest: str | None = None
) -> dict[str, object]:
    from ai_sdlc.core.loop_decision_service import _source_boundary
    from ai_sdlc.core.pr_review_pack import _require_visible_local_index

    if verified_source_digest is None:
        _require_visible_local_index(root)
    else:
        scope = _active_delivery_read_scope(root)
        if (
            scope is None
            or scope.failed
            or not scope.reusable
            or scope.proof is None
            or scope.proof.staged_tree != staged_tree
            or scope.proof.source_boundary_digest != verified_source_digest
        ):
            raise ValueError("delivery-proof-source-guard-invalid")
    boundary = _source_boundary(root)
    if verified_source_digest is None:
        index = _delivery_git_bytes(root, "ls-files", "--stage", "-z")
        tree = _delivery_git_bytes(root, "ls-tree", "-r", "-z", staged_tree)

        def entries(raw: bytes, *, is_index: bool) -> dict[bytes, tuple[bytes, bytes]]:
            result = {}
            for record in raw.split(b"\0"):
                if not record:
                    continue
                metadata, separator, path = record.partition(b"\t")
                fields = metadata.split(b" ")
                if (
                    not separator
                    or len(fields) != 3
                    or (is_index and fields[2] != b"0")
                    or path in result
                ):
                    raise ValueError("delivery-proof-index-or-tree-invalid")
                result[path] = (fields[0], fields[1] if is_index else fields[2])
            return result

        if entries(index, is_index=True) != entries(tree, is_index=False):
            raise ValueError("delivery-proof-current-index-tree-mismatch")
        if hashlib.sha256(index).hexdigest() != boundary["index"]:
            raise ValueError("delivery-proof-source-drift")
    boundary["index_flags"] = hashlib.sha256(
        _delivery_git_bytes(root, "ls-files", "-v", "-z")
    ).hexdigest()
    boundary["commit_parents"] = (
        _delivery_git_bytes(root, "rev-list", "--parents", "-n", "1", "HEAD")
        .decode("ascii")
        .strip()
    )
    boundary["commit_tree"] = (
        _delivery_git_bytes(root, "rev-parse", "HEAD^{tree}").decode("ascii").strip()
    )
    # 冻结 proof 已完整证明 index/tree/flags；消费方仍双读全部字段，只省辅助重查。
    if (
        verified_source_digest is not None
        and _delivery_source_digest(boundary) != verified_source_digest
    ):
        raise ValueError("delivery-proof-source-drift")
    return boundary


def _delivery_git_bytes(root: Path, *args: str) -> bytes:
    environment = quality_command_environment(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", "--no-replace-objects", "-c", "core.fsmonitor=false", *args],
        cwd=root,
        env=environment,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise GitError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def _delivery_commit_state(
    root: Path,
    review_run: ReviewRun,
    review_pack: ReviewPack,
) -> tuple[str, str, str]:
    try:
        commit_line = _git_read_text(root, "rev-list", "--parents", "-n", "1", "HEAD")
        fields = commit_line.split()
        commit = fields[0] if fields else ""
        parents = fields[1:]
        tree_oid = _git_read_text(root, "rev-parse", "HEAD^{tree}")
    except GitError as exc:
        return f"Unable to verify delivered commit: {exc}", "", ""
    if len(parents) != 1:
        return "Delivered commit must have exactly one parent.", commit, tree_oid
    if parents[0] != review_run.head_commit:
        return (
            "Delivered commit parent does not match the reviewed HEAD: "
            f"{parents[0]} != {review_run.head_commit}.",
            commit,
            tree_oid,
        )
    if tree_oid != review_run.staged_tree_oid:
        return (
            "Delivered commit tree does not match the reviewed staged tree: "
            f"{tree_oid} != {review_run.staged_tree_oid}.",
            commit,
            tree_oid,
        )
    if review_pack.staged_tree_oid != review_run.staged_tree_oid:
        return (
            "Review pack staged tree no longer matches the review run.",
            commit,
            tree_oid,
        )
    if review_run.delivery_commit and review_run.delivery_commit != commit:
        return (
            "Current HEAD no longer matches the recorded delivery commit.",
            commit,
            tree_oid,
        )
    dirty = _reviewed_worktree_dirty(root, review_run, review_pack=review_pack)
    if dirty:
        return dirty, commit, tree_oid
    return "", commit, tree_oid


def _git_read_text(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            env=quality_command_environment(os.environ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out") from exc
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _reviewed_head_mismatch(root: Path, review_run: ReviewRun) -> str:
    try:
        current_reviewed_head = _resolved_reviewed_head_commit(
            root.resolve(), review_run
        )
    except GitError as exc:
        return f"Unable to verify reviewed head before closing PR review: {exc}"
    if current_reviewed_head != review_run.head_commit:
        return (
            "Current reviewed head_ref does not match reviewed head_commit: "
            f"{current_reviewed_head} != {review_run.head_commit}."
        )
    return ""


def _resolved_reviewed_head_commit(root: Path, review_run: ReviewRun) -> str:
    source_kind = DiffSourceKind(review_run.diff_source.source_kind)
    ref = (
        "HEAD"
        if source_kind in {DiffSourceKind.LOCAL_STAGED, DiffSourceKind.LOCAL_UNSTAGED}
        else review_run.head_ref
    )
    return GitClient(root.resolve()).resolve_revision(ref)


def _resolvable_head_ref_for_diff_source(review_run: ReviewRun) -> str:
    source_kind = DiffSourceKind(review_run.diff_source.source_kind)
    if source_kind in {DiffSourceKind.LOCAL_STAGED, DiffSourceKind.LOCAL_UNSTAGED}:
        return "HEAD"
    return review_run.head_ref


def _reviewed_diff_source_mismatch(root: Path, review_run: ReviewRun) -> str:
    source_kind = DiffSourceKind(review_run.diff_source.source_kind)
    source_resolution = resolve_diff_source(
        DiffSourceResolutionOptions(
            root=root.resolve(),
            source_kind=review_run.diff_source.source_kind,
            base_ref=review_run.base_ref,
            head_ref=_resolvable_head_ref_for_diff_source(review_run),
            patch_file=review_run.diff_source.patch_file,
            source_id=review_run.diff_source.source_id,
            source_provider=review_run.diff_source.scm_host_type,
        )
    )
    if source_resolution.access_status != SourceAccessStatus.RESOLVED:
        detail = source_resolution.blocker or source_resolution.unavailable_reason
        return f"Reviewed diff source is no longer available: {detail}"
    if source_kind == DiffSourceKind.LOCAL_GIT_RANGE:
        if source_resolution.base_commit != review_run.base_commit:
            return (
                "Current base commit does not match reviewed base commit: "
                f"{source_resolution.base_commit} != {review_run.base_commit}."
            )
        if source_resolution.head_commit != review_run.head_commit:
            return (
                "Current head commit does not match reviewed head commit: "
                f"{source_resolution.head_commit} != {review_run.head_commit}."
            )
        return ""
    if source_kind in {
        DiffSourceKind.LOCAL_STAGED,
        DiffSourceKind.LOCAL_UNSTAGED,
    }:
        try:
            resolve_review_input_for_source(root, source_resolution)
        except GitError as exc:
            return f"Reviewed diff source cannot be verified: {exc}"
    expected_hash = review_run.diff_source.patch_hash.strip()
    if not expected_hash:
        return "Reviewed diff source hash is missing; rerun PR review."
    current_hash = source_resolution.patch_hash.strip()
    if not current_hash:
        return "Current diff source hash is unavailable; rerun PR review."
    if current_hash != expected_hash:
        return (
            "Current diff source hash does not match reviewed diff source hash: "
            f"{current_hash} != {expected_hash}."
        )
    return ""


def _current_changed_paths_for_review_run(
    root: Path,
    review_run: ReviewRun,
) -> list[str]:
    source_kind = DiffSourceKind(review_run.diff_source.source_kind)
    if source_kind == DiffSourceKind.LOCAL_GIT_RANGE:
        return list(_pr_changed_paths(root, review_run.base_ref, review_run.head_ref))
    source_resolution = resolve_diff_source(
        DiffSourceResolutionOptions(
            root=root,
            source_kind=review_run.diff_source.source_kind,
            base_ref=review_run.base_ref,
            head_ref=_resolvable_head_ref_for_diff_source(review_run),
            patch_file=review_run.diff_source.patch_file,
            source_id=review_run.diff_source.source_id,
            source_provider=review_run.diff_source.scm_host_type,
        )
    )
    if source_resolution.access_status != SourceAccessStatus.RESOLVED:
        detail = source_resolution.blocker or source_resolution.unavailable_reason
        raise GitError(detail or "Saved diff source is unavailable.")
    return list(resolve_review_input_for_source(root, source_resolution).changed_files)


def _reviewed_worktree_dirty(
    root: Path,
    review_run: ReviewRun,
    *,
    review_pack: ReviewPack | None = None,
) -> str:
    try:
        dirty_paths = _unreviewed_dirty_paths(
            root.resolve(),
            review_run,
            review_pack=review_pack,
        )
    except GitError as exc:
        return f"Unable to verify clean worktree before closing PR review: {exc}"
    if dirty_paths:
        sample = ", ".join(dirty_paths[:5])
        return "Current worktree has uncommitted changes that were not reviewed" + (
            f": {sample}" if sample else "."
        )
    return ""


def _reviewer_outputs_tamper_blocker(
    root: Path,
    review_run: ReviewRun,
    findings: ReviewFindings,
    *,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> str:
    if not review_run.review_pack_digest.strip():
        return (
            "Current review-pack.json cannot be verified because its digest is missing."
        )
    review_pack_path = _resolve_repo_path(root, review_run.review_pack_path)
    try:
        actual_pack_digest = _review_artifact_sha256(
            root,
            review_pack_path,
            reviewed_artifacts,
        )
    except (KeyError, OSError, ValueError) as exc:
        return f"Current review-pack.json cannot be verified: {exc}"
    if actual_pack_digest != review_run.review_pack_digest:
        return "Current review-pack.json changed after the reviewer run."

    if review_run.findings_digest.strip():
        findings_path = _resolve_repo_path(root, review_run.findings_path)
        try:
            actual_digest = _review_artifact_sha256(
                root,
                findings_path,
                reviewed_artifacts,
            )
        except (KeyError, OSError, ValueError) as exc:
            return f"Current findings.json cannot be verified: {exc}"
        if actual_digest != review_run.findings_digest:
            return "Current findings.json changed after the reviewer run."
        return ""

    if str(findings.verdict or "") != str(review_run.verdict or ""):
        return "Current findings.json verdict no longer matches the reviewer run."
    expected_counts = {
        FindingSeverity.BLOCKER: review_run.unresolved_blockers,
        FindingSeverity.REQUIRED: review_run.unresolved_required,
        FindingSeverity.ADVISORY: review_run.unresolved_advisory,
    }
    for severity, expected in expected_counts.items():
        if _count_findings(findings, severity) != expected:
            return "Current findings.json counts no longer match the reviewer run."
    return ""


def _final_report_tamper_blocker(
    final_report_path: Path,
    review_run: ReviewRun,
) -> str:
    if not review_run.final_report_digest.strip():
        return "Final report digest is missing; the report cannot be verified."
    try:
        actual_digest = _file_sha256(final_report_path)
    except OSError as exc:
        return f"Final report cannot be verified: {exc}"
    if actual_digest != review_run.final_report_digest:
        return "Final report changed after PR review close."
    return ""


def _unreviewed_dirty_paths(
    root: Path,
    review_run: ReviewRun,
    *,
    review_pack: ReviewPack | None = None,
) -> list[str]:
    environment = quality_command_environment(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise GitError(str(exc)) from exc
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or "git status failed")

    source_kind = DiffSourceKind(review_run.diff_source.source_kind)
    allowed_dirty = _reviewed_dirty_paths_for_review_run(
        root,
        review_run,
        review_pack=review_pack,
    )
    dirty: list[str] = []
    for status_xy, rel_path in _iter_porcelain_entries(result.stdout):
        normalized = rel_path.replace("\\", "/")
        if (
            not normalized
            or _is_reviewed_dirty_status(
                source_kind,
                status_xy=status_xy,
                path=normalized,
                allowed_dirty=allowed_dirty,
            )
            or _is_current_review_artifact_path(normalized, review_run)
        ):
            continue
        dirty.append(normalized)
    return dirty


def _iter_porcelain_entries(output: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    parts = output.split("\0")
    index = 0
    while index < len(parts):
        item = parts[index]
        index += 1
        if not item or len(item) < 4:
            continue
        status_code = item[:2]
        rel_path = item[3:]
        entries.append((status_code, rel_path))
        if "R" in status_code or "C" in status_code:
            index += 1
    return entries


def _reviewed_dirty_paths_for_review_run(
    root: Path,
    review_run: ReviewRun,
    *,
    review_pack: ReviewPack | None = None,
) -> frozenset[str]:
    source_kind = DiffSourceKind(review_run.diff_source.source_kind)
    if source_kind == DiffSourceKind.PATCH:
        patch_path = _repo_relative_patch_source_path(
            root,
            review_run.diff_source.patch_file,
        )
        return frozenset({patch_path}) if patch_path else frozenset()
    if source_kind not in {
        DiffSourceKind.LOCAL_STAGED,
        DiffSourceKind.LOCAL_UNSTAGED,
    }:
        return frozenset()
    if review_pack is None:
        try:
            review_pack = _load_review_pack(root, review_run.review_pack_path)
        except (
            FileNotFoundError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
            OSError,
        ):
            return frozenset()
    return frozenset(
        path.strip().replace("\\", "/")
        for path in review_pack.changed_files
        if path.strip()
    )


def _is_reviewed_dirty_status(
    source_kind: DiffSourceKind,
    *,
    status_xy: str,
    path: str,
    allowed_dirty: frozenset[str],
) -> bool:
    if path not in allowed_dirty or "U" in status_xy:
        return False
    index_status = status_xy[0] if len(status_xy) > 0 else " "
    worktree_status = status_xy[1] if len(status_xy) > 1 else " "
    if source_kind == DiffSourceKind.LOCAL_STAGED:
        return index_status not in {" ", "?"} and worktree_status == " "
    if source_kind == DiffSourceKind.LOCAL_UNSTAGED:
        return status_xy == "??" or (
            index_status == " " and worktree_status not in {" ", "?"}
        )
    return source_kind == DiffSourceKind.PATCH


def _resolve_patch_source_path(root: Path, patch_file: str) -> Path | None:
    patch_file = patch_file.strip()
    if not patch_file:
        return None
    try:
        path = Path(patch_file)
        return path.resolve() if path.is_absolute() else (root / path).resolve()
    except OSError:
        return None


def _repo_relative_patch_source_path(root: Path, patch_file: str) -> str:
    patch_path = _resolve_patch_source_path(root, patch_file)
    if patch_path is None:
        return ""
    try:
        return patch_path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return ""


def _is_current_review_artifact_path(path: str, review_run: ReviewRun) -> bool:
    review_prefix = f"{AI_SDLC_DIR}/reviews/pr/{review_run.review_id}/"
    return path == str(CURRENT_REVIEW_PATH).replace("\\", "/") or path.startswith(
        review_prefix
    )


def parse_provider_command(raw: str) -> list[str]:
    """Parse provider command text with shell-like quoting."""

    if not raw.strip():
        return []
    parts = shlex.split(raw, posix=os.name != "nt")
    if os.name == "nt":
        return [_strip_wrapping_quotes(part) for part in parts]
    return parts


def detect_current_model() -> str:
    """Best-effort current model from the local CLI/agent environment."""

    for key in CURRENT_MODEL_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _preview(
    options: PRReviewStartOptions,
) -> tuple[
    list[PRReviewCheck],
    PRReviewCommandStatus,
    str,
    str,
    ModelResolution | None,
    RedactionReport | None,
    SourceAdapterResolution | None,
]:
    root = options.root.resolve()
    checks: list[PRReviewCheck] = []
    limit_blocker = review_diff_limit_blocker(options.max_diff_bytes)
    if limit_blocker:
        return (
            [
                PRReviewCheck(
                    name="diff_size",
                    status=PRReviewCommandStatus.NEEDS_USER,
                    detail=limit_blocker,
                )
            ],
            PRReviewCommandStatus.NEEDS_USER,
            limit_blocker,
            "Set --max-diff-bytes to a positive integer for this invocation.",
            None,
            None,
            None,
        )
    if not (root / AI_SDLC_DIR).is_dir():
        detail = "Project is not initialized; .ai-sdlc is missing."
        checks.append(
            PRReviewCheck(
                name="init", status=PRReviewCommandStatus.BLOCKED, detail=detail
            )
        )
        return (
            checks,
            PRReviewCommandStatus.BLOCKED,
            detail,
            "Run ai-sdlc init .",
            None,
            None,
            None,
        )
    checks.append(
        PRReviewCheck(
            name="init", status=PRReviewCommandStatus.READY, detail=".ai-sdlc exists."
        )
    )

    source_resolution = resolve_diff_source(
        DiffSourceResolutionOptions(
            root=root,
            source_kind=options.diff_source,
            base_ref=options.base_ref,
            head_ref=options.head_ref,
            patch_file=options.patch_file,
            source_id=options.source_id,
            source_provider=options.source_provider,
        )
    )
    if source_resolution.access_status != SourceAccessStatus.RESOLVED:
        status = (
            PRReviewCommandStatus.BLOCKED
            if source_resolution.access_status == SourceAccessStatus.BLOCKED
            else PRReviewCommandStatus.NEEDS_USER
        )
        detail = source_resolution.blocker or source_resolution.unavailable_reason
        checks.append(PRReviewCheck(name="diff_source", status=status, detail=detail))
        return (
            checks,
            status,
            detail,
            source_resolution.next_command,
            None,
            None,
            source_resolution,
        )
    try:
        review_input = resolve_review_input_for_source(root, source_resolution)
        changed_files = list(review_input.changed_files)
    except GitError as exc:
        detail = str(exc)
        checks.append(
            PRReviewCheck(
                name="diff_source", status=PRReviewCommandStatus.BLOCKED, detail=detail
            )
        )
        return (
            checks,
            PRReviewCommandStatus.BLOCKED,
            detail,
            "Check the selected diff source and rerun pr-review doctor.",
            None,
            None,
            source_resolution,
        )
    checks.append(
        PRReviewCheck(
            name="diff_source",
            status=PRReviewCommandStatus.READY,
            detail=(
                f"{source_resolution.adapter_id}: {len(changed_files)} changed file(s)."
            ),
        )
    )

    try:
        policy = load_loop_policy(root)
    except LoopPolicyError as exc:
        detail = _policy_blocker(exc)
        checks.append(
            PRReviewCheck(
                name="policy",
                status=PRReviewCommandStatus.BLOCKED,
                detail=detail,
            )
        )
        return (
            checks,
            PRReviewCommandStatus.BLOCKED,
            detail,
            _policy_next_action(),
            None,
            None,
            source_resolution,
        )
    provider_options = _normalize_provider_options(
        _apply_policy_provider_default(options, policy)
    )
    provider_blocker = _unsupported_provider_blocker(provider_options.provider_id)
    if provider_blocker:
        checks.append(
            PRReviewCheck(
                name="provider",
                status=PRReviewCommandStatus.NEEDS_USER,
                detail=provider_blocker,
            )
        )
        return (
            checks,
            PRReviewCommandStatus.NEEDS_USER,
            provider_blocker,
            "Choose local-agent or mock-reviewer.",
            None,
            None,
            source_resolution,
        )
    model_resolution = resolve_model_for_review(
        policy,
        ModelResolutionRequest(
            requested_provider=provider_options.provider_id,
            requested_model=_requested_model(provider_options),
            provider_default_model=provider_options.provider_default_model,
            current_model=_current_model(provider_options),
            provider_mode=_provider_mode(provider_options.provider_id),
            code_egress=provider_options.code_egress,
            code_egress_confirmed=provider_options.code_egress_confirmed,
        ),
    )
    if model_resolution.status != "resolved":
        model_status = (
            PRReviewCommandStatus.BLOCKED
            if model_resolution.status == ModelResolutionStatus.BLOCKED
            or str(model_resolution.status) == ModelResolutionStatus.BLOCKED.value
            else PRReviewCommandStatus.NEEDS_USER
        )
        next_action = (
            "Choose an allowed model or update loop-policy.yaml."
            if model_status == PRReviewCommandStatus.BLOCKED
            else "Choose or configure a local review model."
        )
        checks.append(
            PRReviewCheck(
                name="model",
                status=model_status,
                detail=model_resolution.blocker,
            )
        )
        return (
            checks,
            model_status,
            model_resolution.blocker,
            next_action,
            model_resolution,
            None,
            source_resolution,
        )
    checks.append(
        PRReviewCheck(
            name="model",
            status=PRReviewCommandStatus.READY,
            detail=f"{model_resolution.model_selector} -> {model_resolution.resolved_model}",
        )
    )

    if (
        provider_options.provider_id == "local-agent"
        and not provider_options.provider_command
    ):
        detail = "local-agent provider is not configured with a local reviewer command."
        checks.append(
            PRReviewCheck(
                name="provider",
                status=PRReviewCommandStatus.NEEDS_USER,
                detail=detail,
            )
        )
        return (
            checks,
            PRReviewCommandStatus.NEEDS_USER,
            detail,
            "Configure --provider-command or use --provider mock-reviewer.",
            model_resolution,
            None,
            source_resolution,
        )
    checks.append(
        PRReviewCheck(
            name="provider",
            status=PRReviewCommandStatus.READY,
            detail="Provider configuration is ready.",
        )
    )

    if review_input.uses_git_range:
        redaction = analyze_pr_review_redaction(
            root,
            base_ref=source_resolution.base_ref,
            head_ref=source_resolution.head_ref,
            changed_files=changed_files,
            policy=policy,
            code_egress=provider_options.code_egress,
            code_egress_confirmed=provider_options.code_egress_confirmed,
        )
    else:
        redaction = analyze_redaction(
            root,
            changed_files,
            policy=policy,
            code_egress=provider_options.code_egress,
            code_egress_confirmed=provider_options.code_egress_confirmed,
            head_file_bytes=review_input.source_file_bytes,
            base_file_bytes=review_input.base_file_bytes,
        )
    if redaction.blocked or redaction.needs_user:
        status = (
            PRReviewCommandStatus.BLOCKED
            if redaction.blocked
            else PRReviewCommandStatus.NEEDS_USER
        )
        checks.append(
            PRReviewCheck(
                name="redaction",
                status=status,
                detail=redaction.blocker,
            )
        )
        return (
            checks,
            status,
            redaction.blocker,
            redaction.next_action,
            model_resolution,
            redaction,
            source_resolution,
        )
    incomplete_decision = decide_incomplete_review_pack(policy, redaction)
    if incomplete_decision.status is not None:
        status = (
            PRReviewCommandStatus.BLOCKED
            if incomplete_decision.status == ReviewPackBuildStatus.BLOCKED
            else PRReviewCommandStatus.NEEDS_USER
        )
        checks.append(
            PRReviewCheck(
                name="redaction",
                status=status,
                detail=incomplete_decision.blocker,
            )
        )
        return (
            checks,
            status,
            incomplete_decision.blocker,
            incomplete_decision.next_action,
            model_resolution,
            redaction,
            source_resolution,
        )
    checks.append(
        PRReviewCheck(
            name="redaction",
            status=PRReviewCommandStatus.READY,
            detail=(
                f"{len(redaction.included_files)} included, "
                f"{len(redaction.redacted_files)} redacted, "
                f"{len(redaction.omitted_files)} omitted."
            ),
        )
    )

    try:
        diff = diff_for_review_source(
            root, source_resolution, list(redaction.included_files), review_input
        )
    except GitError as exc:
        detail = str(exc)
        return (
            checks
            + [
                PRReviewCheck(
                    name="diff_size",
                    status=PRReviewCommandStatus.BLOCKED,
                    detail=detail,
                )
            ],
            PRReviewCommandStatus.BLOCKED,
            detail,
            "Check the base/head refs.",
            model_resolution,
            redaction,
            source_resolution,
        )
    size_blocker = review_diff_size_blocker(diff, options.max_diff_bytes)
    size_status = (
        PRReviewCommandStatus.NEEDS_USER
        if size_blocker
        else PRReviewCommandStatus.READY
    )
    checks.append(
        PRReviewCheck(
            name="diff_size",
            status=size_status,
            detail=size_blocker
            or f"Review diff is {len(diff.encode('utf-8'))} bytes; limit is {options.max_diff_bytes} bytes.",
        )
    )
    if size_blocker:
        return (
            checks,
            size_status,
            size_blocker,
            "Set --max-diff-bytes to the required capacity for this invocation.",
            model_resolution,
            redaction,
            source_resolution,
        )

    review_root = root / AI_SDLC_DIR / "reviews" / "pr"
    if not os.access(
        review_root.parent if review_root.parent.exists() else root / AI_SDLC_DIR,
        os.W_OK,
    ):
        detail = f"Review artifact directory is not writable: {review_root.parent}"
        checks.append(
            PRReviewCheck(
                name="artifacts", status=PRReviewCommandStatus.BLOCKED, detail=detail
            )
        )
        return (
            checks,
            PRReviewCommandStatus.BLOCKED,
            detail,
            "Fix artifact directory permissions.",
            model_resolution,
            redaction,
            source_resolution,
        )
    checks.append(
        PRReviewCheck(
            name="artifacts",
            status=PRReviewCommandStatus.READY,
            detail="Review artifact path is writable.",
        )
    )

    return (
        checks,
        PRReviewCommandStatus.READY,
        "",
        "Start local PR review.",
        model_resolution,
        redaction,
        source_resolution,
    )


def _normalize_provider_options(options: PRReviewStartOptions) -> PRReviewStartOptions:
    if options.provider_id == "mock-reviewer":
        return PRReviewStartOptions(
            root=options.root,
            base_ref=options.base_ref,
            head_ref=options.head_ref,
            provider_id=options.provider_id,
            model_selector="mock-reviewer",
            diff_source=options.diff_source,
            patch_file=options.patch_file,
            source_id=options.source_id,
            source_provider=options.source_provider,
            current_model="mock-reviewer",
            provider_default_model="mock-reviewer",
            provider_command=options.provider_command,
            provider_timeout_seconds=options.provider_timeout_seconds,
            max_diff_bytes=options.max_diff_bytes,
            code_egress=False,
            code_egress_confirmed=True,
            dry_run=options.dry_run,
            review_id=options.review_id,
            loop_id=options.loop_id,
            mock_fixture=options.mock_fixture,
            clear_stale_artifacts=options.clear_stale_artifacts,
            preserve_resolution_history=options.preserve_resolution_history,
            decision_mode=options.decision_mode,
            decision_capability=options.decision_capability,
            decision_staged_tree_oid=options.decision_staged_tree_oid,
            decision_started_at_ms=options.decision_started_at_ms,
            expected_repair_source=options.expected_repair_source,
            provider_snapshot_complete=options.provider_snapshot_complete,
            provider_workspace_adoption_ref=options.provider_workspace_adoption_ref,
            rejected_feedback_ref=options.rejected_feedback_ref,
            test_results_refs=options.test_results_refs,
        )
    return options


def _apply_policy_provider_default(
    options: PRReviewStartOptions,
    policy: LoopPolicyProfile,
) -> PRReviewStartOptions:
    provider_id = options.provider_id.strip()
    if provider_id:
        return options
    return PRReviewStartOptions(
        root=options.root,
        base_ref=options.base_ref,
        head_ref=options.head_ref,
        provider_id=policy.default_provider or "local-agent",
        model_selector=options.model_selector,
        diff_source=options.diff_source,
        patch_file=options.patch_file,
        source_id=options.source_id,
        source_provider=options.source_provider,
        current_model=options.current_model,
        provider_default_model=options.provider_default_model,
        provider_command=options.provider_command,
        provider_timeout_seconds=options.provider_timeout_seconds,
        max_diff_bytes=options.max_diff_bytes,
        code_egress=options.code_egress,
        code_egress_confirmed=options.code_egress_confirmed,
        dry_run=options.dry_run,
        review_id=options.review_id,
        loop_id=options.loop_id,
        mock_fixture=options.mock_fixture,
        clear_stale_artifacts=options.clear_stale_artifacts,
        preserve_resolution_history=options.preserve_resolution_history,
        decision_mode=options.decision_mode,
        decision_capability=options.decision_capability,
        decision_staged_tree_oid=options.decision_staged_tree_oid,
        decision_started_at_ms=options.decision_started_at_ms,
        expected_repair_source=options.expected_repair_source,
        provider_snapshot_complete=options.provider_snapshot_complete,
        provider_workspace_adoption_ref=options.provider_workspace_adoption_ref,
        rejected_feedback_ref=options.rejected_feedback_ref,
        test_results_refs=options.test_results_refs,
    )


def _unsupported_provider_blocker(provider_id: str) -> str:
    if provider_id in SUPPORTED_PROVIDER_IDS:
        return ""
    return f"Unsupported PR review provider: {provider_id}"


def _provider_timeout_blocker(timeout_seconds: float) -> str:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        return "Provider timeout must be finite and positive."
    return ""


def _run_provider(
    options: PRReviewStartOptions,
    review_pack_path: Path,
    *,
    pre_launch_guard: Callable[[], None] | None = None,
    on_execution_failure: Callable[[ProviderRunResult], None] | None = None,
) -> ProviderRunResult:
    if options.provider_id == "mock-reviewer":
        # mock 没有进程回执；真实提供方由既有预启动入口保留本次 never_started 原件。
        if pre_launch_guard is not None:
            try:
                pre_launch_guard()
            except (ValueError, OSError, WorktreeSnapshotError) as exc:
                return ProviderRunResult(
                    status=ProviderRunStatus.BLOCKED,
                    blocker=f"Review publication could not be verified before mock dispatch: {exc}",
                    next_action="Preserve the changed publication and its original review; no mock result was produced.",
                )
        return run_mock_reviewer(
            root=options.root,
            review_pack_path=review_pack_path,
            fixture=options.mock_fixture,
        )
    if options.provider_id == "local-agent":
        return run_provider_command(
            ProviderCommandOptions(
                root=options.root,
                review_pack_path=review_pack_path,
                command=options.provider_command,
                provider_id=options.provider_id,
                timeout_seconds=options.provider_timeout_seconds,
                pre_launch_guard=pre_launch_guard,
                on_execution_failure=on_execution_failure,
            )
        )
    return ProviderRunResult(
        status=ProviderRunStatus.NEEDS_USER,
        blocker=f"Unsupported PR review provider: {options.provider_id}",
        next_action="Choose local-agent or mock-reviewer.",
    )


def _write_review_run(
    *,
    root: Path,
    options: PRReviewStartOptions,
    review_id: str,
    loop_id: str,
    pack_result: ReviewPackBuildResult,
    provider_result: ProviderRunResult,
) -> Path:
    store = LoopArtifactStore(root)
    findings = provider_result.findings
    review_pack = pack_result.review_pack
    if review_pack is None:
        raise ValueError("ready provider run requires review_pack")
    findings_path = (
        Path(provider_result.findings_path) if provider_result.findings_path else None
    )
    review_run = ReviewRun(
        review_id=review_id,
        loop_id=loop_id,
        decision_mode=options.decision_mode,
        decision_capability=options.decision_capability,
        decision_started_at_ms=options.decision_started_at_ms,
        decision_staged_tree_oid=(
            options.decision_staged_tree_oid or review_pack.staged_tree_oid
        )
        if options.decision_mode == "adaptive-quantified"
        else "",
        status=_loop_status_from_provider(provider_result.status),
        provider_id=options.provider_id,
        provider_mode=_provider_mode(options.provider_id),
        model_selector=review_pack.model_selector,
        resolved_model=review_pack.resolved_model,
        model_resolution_status=review_pack.model_resolution_status,
        model_resolution_source=review_pack.model_resolution_source,
        code_egress=options.code_egress,
        code_egress_confirmed=options.code_egress_confirmed,
        diff_source=review_pack.diff_source,
        source_adapter=review_pack.source_adapter,
        source_access_status=review_pack.source_access_status,
        source_resolution_path=review_pack.source_resolution_path,
        base_ref=review_pack.base_ref,
        head_ref=review_pack.head_ref,
        base_commit=review_pack.base_commit,
        head_commit=review_pack.head_commit,
        staged_tree_oid=review_pack.staged_tree_oid,
        provider_command=options.provider_command,
        review_pack_path=_repo_relative_path(root, Path(pack_result.review_pack_path)),
        review_pack_digest=_file_sha256(Path(pack_result.review_pack_path))
        if pack_result.review_pack_path and Path(pack_result.review_pack_path).is_file()
        else "",
        findings_path=_repo_relative_path(root, findings_path) if findings_path else "",
        # 已启动异常的现存输出只作诊断；其原字节由 invocation 单独绑定。
        findings_digest=_file_sha256(findings_path)
        if (findings_path and findings_path.is_file()
            and not (provider_result.invocation and provider_result.invocation.execution_failure is not None))
        else "",
        verdict=findings.verdict if findings else None,
        unresolved_blockers=_count_findings(findings, FindingSeverity.BLOCKER),
        unresolved_required=_count_findings(findings, FindingSeverity.REQUIRED),
        unresolved_advisory=_count_findings(findings, FindingSeverity.ADVISORY),
        next_action=_decision_next_action(options, provider_result),
    )
    path = store.review_run_dir(review_id) / "review-run.json"
    store.write_json_artifact(path, review_run)
    return path


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pr_changed_paths(root: Path, base_ref: str, head_ref: str) -> tuple[str, ...]:
    git = GitClient(root)
    merge_base = git.merge_base(base_ref, head_ref)
    return git.changed_paths(merge_base, head_ref)


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _write_current_review(
    *,
    root: Path,
    review_id: str,
    loop_id: str,
    review_run_path: Path,
) -> Path:
    path = root / CURRENT_REVIEW_PATH
    LoopArtifactStore(root).write_json_artifact(
        path,
        {
            "review_id": review_id,
            "loop_id": loop_id,
            "review_run_path": _repo_relative_path(root, review_run_path),
        },
    )
    return path


def _resolve_review_id(options: PRReviewStartOptions) -> str:
    if options.review_id:
        return options.review_id
    try:
        head = GitClient(options.root).resolve_revision(options.head_ref, short=True)
    except GitError:
        head = "unknown"
    return f"review-{head}"


def _unsafe_explicit_review_id_blocker(review_id: str) -> str:
    if not review_id:
        return ""
    text = review_id.strip()
    if (
        not text
        or text in {".", ".."}
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", text) is None
    ):
        return f"Unsafe PR review id: {review_id!r}"
    return ""


def _resolve_loop_id(options: PRReviewStartOptions) -> str:
    return options.loop_id or f"loop-{_resolve_review_id(options)}"


def _requested_model(options: PRReviewStartOptions) -> str:
    if options.provider_id == "mock-reviewer":
        return "mock-reviewer"
    return options.model_selector


def _current_model(options: PRReviewStartOptions) -> str:
    if options.current_model:
        return options.current_model
    return detect_current_model()


def _provider_mode(provider_id: str) -> ProviderMode:
    if provider_id == "mock-reviewer":
        return ProviderMode.MOCK
    return ProviderMode.LOCAL_AGENT


def _status_from_pack_result(status: ReviewPackBuildStatus) -> PRReviewCommandStatus:
    if status == ReviewPackBuildStatus.BLOCKED:
        return PRReviewCommandStatus.BLOCKED
    return PRReviewCommandStatus.NEEDS_USER


def _status_from_provider_result(status: ProviderRunStatus) -> PRReviewCommandStatus:
    if status == ProviderRunStatus.NEEDS_USER:
        return PRReviewCommandStatus.NEEDS_USER
    if status == ProviderRunStatus.BLOCKED:
        return PRReviewCommandStatus.BLOCKED
    return PRReviewCommandStatus.STARTED


def _status_from_loop_status(status: LoopStatus) -> PRReviewCommandStatus:
    if status == LoopStatus.BLOCKED:
        return PRReviewCommandStatus.BLOCKED
    if status == LoopStatus.NEEDS_USER:
        return PRReviewCommandStatus.NEEDS_USER
    if status == LoopStatus.CLOSED:
        return PRReviewCommandStatus.CLOSED
    return PRReviewCommandStatus.STARTED


def _loop_status_from_provider(status: ProviderRunStatus) -> LoopStatus:
    if status == ProviderRunStatus.SUCCESS:
        return LoopStatus.NEEDS_REVIEW
    if status == ProviderRunStatus.CHANGES_REQUIRED:
        return LoopStatus.NEEDS_FIX
    if status == ProviderRunStatus.NEEDS_USER:
        return LoopStatus.NEEDS_USER
    return LoopStatus.BLOCKED


def _count_findings(findings: ReviewFindings | None, severity: FindingSeverity) -> int:
    if findings is None:
        return 0
    return sum(1 for finding in findings.findings if finding.severity == severity)


def _next_action_for_provider(result: ProviderRunResult) -> str:
    if result.status == ProviderRunStatus.SUCCESS:
        return "Record verification evidence, then run bounded local PR expert review."
    if result.status == ProviderRunStatus.CHANGES_REQUIRED:
        return "Run ai-sdlc pr-review fix."
    if result.status == ProviderRunStatus.NEEDS_USER:
        return result.next_action or "Resolve provider configuration and rerun."
    return result.next_action or "Fix the blocked review provider and rerun."


def _decision_next_action(
    options: PRReviewStartOptions, result: ProviderRunResult
) -> str:
    if (
        options.decision_capability == "stage-simulation-v1"
        and result.status == ProviderRunStatus.SUCCESS
    ):
        return (
            "Host agent: instantiate delivery-readiness-v1 from the current staged tree and "
            "upstream obligations via ai-sdlc pr-review decision-prepare --schema --json; "
            "assess only current-staged-tree risks, record real staged verification, then "
            "seal and run the existing independent local PR expert review. Do not ask the "
            "user to fill scores or select an implementation route."
        )
    return result.next_action or _next_action_for_provider(result)


def _decision_start_blocker(root: Path, options: PRReviewStartOptions) -> str:
    identity = (options.decision_mode, options.decision_capability)
    if identity not in {
        ("legacy", None),
        ("adaptive-quantified", "stage-simulation-v1"),
    }:
        return "pr-decision-identity-invalid"
    if identity[1] is not None and options.diff_source != "local-staged":
        return "pr-decision-requires-current-staged-tree"
    if _unsafe_explicit_review_id_blocker(options.review_id):
        return ""
    directory = LoopArtifactStore(root).review_run_dir(_resolve_review_id(options))
    path = directory / "review-run.json"
    if not path.exists():
        if (
            options.decision_staged_tree_oid
            or options.decision_started_at_ms is not None
        ):
            return "pr-decision-initial-tree-must-be-current"
        if (directory / "decision-context.json").exists():
            return "pr-decision-orphan-context"
        return ""
    try:
        existing = ReviewRun.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        return f"pr-decision-existing-review-invalid: {exc}"
    if (existing.decision_mode, existing.decision_capability) != identity:
        return "pr-decision-saved-identity-mismatch"
    if identity[1] is not None and (
        options.clear_stale_artifacts
        or not options.preserve_resolution_history
        or options.decision_staged_tree_oid != existing.decision_staged_tree_oid
        or options.decision_started_at_ms != existing.decision_started_at_ms
    ):
        return "pr-decision-existing-review-requires-rerun"
    if identity[0] == "legacy" and (directory / "decision-context.json").exists():
        return "pr-decision-context-conflicts-with-legacy"
    if identity[1] is not None and (
        existing.decision_started_at_ms is not None
        or (directory / "decision-context.json").exists()
    ):
        from ai_sdlc.core.pr_review_decision import validate_pr_review_context

        try:
            validate_pr_review_context(root, existing)
        except (OSError, ValueError) as exc:
            return f"pr-decision-existing-context-invalid: {exc}"
    return ""


def _load_current_review_run(
    root: Path,
    *,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> tuple[ReviewRun, Path]:
    resolved_root = root.resolve()
    pointer_path = resolved_root / CURRENT_REVIEW_PATH
    if reviewed_artifacts is None:
        if not pointer_path.exists():
            raise FileNotFoundError("Current PR review pointer is missing.")
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    else:
        pointer = _parse_review_json_bytes(
            _review_snapshot_required_bytes(
                resolved_root,
                pointer_path,
                reviewed_artifacts,
                missing_message="Current PR review pointer is missing.",
            ),
            name=pointer_path.name,
        )
    if not isinstance(pointer, dict):
        raise ValueError("Current review pointer is malformed: root must be an object.")
    review_run_path = _resolve_repo_path(
        resolved_root, str(pointer.get("review_run_path", ""))
    )
    if reviewed_artifacts is None:
        if not review_run_path.exists():
            raise FileNotFoundError("Current review-run.json is missing.")
        run_payload = json.loads(review_run_path.read_text(encoding="utf-8"))
    else:
        run_payload = _parse_review_json_bytes(
            _review_snapshot_required_bytes(
                resolved_root,
                review_run_path,
                reviewed_artifacts,
                missing_message="Current review-run.json is missing.",
            ),
            name=review_run_path.name,
        )
    return (
        ReviewRun.model_validate(run_payload),
        review_run_path,
    )


def _load_findings(
    root: Path,
    review_run: ReviewRun,
    *,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> ReviewFindings:
    if not review_run.findings_path.strip():
        raise FileNotFoundError("Current findings.json is missing.")
    path = _resolve_repo_path(root, review_run.findings_path)
    if reviewed_artifacts is None:
        if not path.is_file():
            raise FileNotFoundError("Current findings.json is missing.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = _parse_review_json_bytes(
            _review_snapshot_required_bytes(
                root,
                path,
                reviewed_artifacts,
                missing_message="Current findings.json is missing.",
            ),
            name=path.name,
        )
    return ReviewFindings.model_validate(payload)


def _load_review_pack(
    root: Path,
    path_text: str,
    *,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> ReviewPack:
    path = _resolve_repo_path(root, path_text)
    if reviewed_artifacts is None:
        if not path.exists():
            raise FileNotFoundError("Current review-pack.json is missing.")
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = _parse_review_json_bytes(
            _review_snapshot_required_bytes(
                root,
                path,
                reviewed_artifacts,
                missing_message="Current review-pack.json is missing.",
            ),
            name=path.name,
        )
    return ReviewPack.model_validate(payload)


def _load_verification_evidence(
    root: Path,
    review_run: ReviewRun,
    *,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> PRReviewVerificationEvidence | None:
    path = LoopArtifactStore(root).review_run_dir(review_run.review_id) / (
        "verification-evidence.json"
    )
    if reviewed_artifacts is None:
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        content = _review_snapshot_optional_bytes(root, path, reviewed_artifacts)
        if content is None:
            return None
        payload = _parse_review_json_bytes(content, name=path.name)
    evidence = PRReviewVerificationEvidence.model_validate(payload)
    if evidence.review_id != review_run.review_id:
        raise ValueError("verification-evidence.json review id does not match")
    if evidence.loop_id != review_run.loop_id:
        raise ValueError("verification-evidence.json loop id does not match")
    return evidence


def _review_snapshot_key(root: Path, path: Path) -> str:
    resolved_root = root.resolve(strict=True)
    candidate = path if path.is_absolute() else resolved_root / path
    lexical = Path(os.path.abspath(candidate))
    try:
        return lexical.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise ValueError(f"Reviewed artifact escapes project: {path}") from exc


def _review_snapshot_required_bytes(
    root: Path,
    path: Path,
    reviewed_artifacts: Mapping[str, bytes],
    *,
    missing_message: str,
) -> bytes:
    content = _review_snapshot_optional_bytes(root, path, reviewed_artifacts)
    if content is None:
        raise FileNotFoundError(missing_message)
    return content


def _review_snapshot_optional_bytes(
    root: Path,
    path: Path,
    reviewed_artifacts: Mapping[str, bytes],
) -> bytes | None:
    return reviewed_artifacts.get(_review_snapshot_key(root, path))


def _review_snapshot_contains(
    root: Path,
    path: Path,
    reviewed_artifacts: Mapping[str, bytes],
) -> bool:
    return _review_snapshot_key(root, path) in reviewed_artifacts


def _parse_review_json_bytes(content: bytes, *, name: str) -> object:
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is malformed: {exc}") from exc


def _review_artifact_sha256(
    root: Path,
    path: Path,
    reviewed_artifacts: Mapping[str, bytes] | None,
) -> str:
    if reviewed_artifacts is None:
        return _file_sha256(path)
    content = _review_snapshot_required_bytes(
        root,
        path,
        reviewed_artifacts,
        missing_message=f"Reviewed artifact is missing: {path.name}",
    )
    return hashlib.sha256(content).hexdigest()


def _repo_relative_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _resolve_repo_path(root: Path, path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return root / path


def _read_resolution_round(path: Path) -> int:
    history_path = path.with_name("resolution-history.yaml")
    history_round = _read_round_file(history_path)
    current_round = _read_round_file(path)
    return max(history_round, current_round)


def _read_round_file(path: Path) -> int:
    if not path.exists():
        return 0
    payload = _load_resolution_payload(path)
    if not isinstance(payload, dict):
        return 0
    value = payload.get("round_number", 0) or 0
    if isinstance(value, bool):
        raise ResolutionFileError(f"{path.name} round_number must be an integer.")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise ResolutionFileError(f"{path.name} round_number must be an integer.")


def _load_resolution_statuses(path: Path) -> dict[str, FindingResolutionStatus]:
    if not path.exists():
        return {}
    return _resolution_statuses(_load_resolution_payload(path))


def _resolution_statuses(payload: object) -> dict[str, FindingResolutionStatus]:
    if not isinstance(payload, dict):
        return {}
    statuses: dict[str, FindingResolutionStatus] = {}
    for item in payload.get("finding_resolutions", []) or []:
        if not isinstance(item, dict):
            continue
        finding_id = str(item.get("finding_id", "")).strip()
        if not finding_id:
            continue
        try:
            resolution = FindingResolution.model_validate(item)
            statuses[finding_id] = resolution.status
        except ValueError:
            statuses[finding_id] = FindingResolutionStatus.UNRESOLVED
    return statuses


def _load_resolution_records(path: Path) -> dict[str, FindingResolution]:
    if not path.exists():
        return {}
    return _resolution_records(_load_resolution_payload(path))


def _resolution_records(payload: object) -> dict[str, FindingResolution]:
    if not isinstance(payload, dict):
        return {}
    records: dict[str, FindingResolution] = {}
    for item in payload.get("finding_resolutions", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            resolution = FindingResolution.model_validate(item)
        except ValueError:
            continue
        records[resolution.finding_id] = resolution
    return records


def _load_resolution_payload(path: Path) -> object:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ResolutionFileError(f"{path.name} is unreadable: {exc}") from exc
    return _parse_resolution_payload(content, name=path.name)


def _parse_resolution_payload(content: bytes, *, name: str) -> object:
    try:
        text = content.decode("utf-8")
        return yaml.safe_load(text) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ResolutionFileError(f"{name} is malformed: {exc}") from exc


def _unresolved_counts(
    findings: ReviewFindings,
    resolution_statuses: dict[str, FindingResolutionStatus],
) -> dict[FindingSeverity, int]:
    counts = {
        FindingSeverity.BLOCKER: 0,
        FindingSeverity.REQUIRED: 0,
        FindingSeverity.ADVISORY: 0,
    }
    for finding in findings.findings:
        status = resolution_statuses.get(
            finding.id,
            FindingResolutionStatus.UNRESOLVED,
        )
        if status in {
            FindingResolutionStatus.FIXED,
            FindingResolutionStatus.WAIVED,
            FindingResolutionStatus.NOT_APPLICABLE,
        }:
            continue
        counts[finding.severity] += 1
    return counts


def _render_fix_plan(
    review_run: ReviewRun,
    selected: list[ReviewFinding],
    advisory: list[ReviewFinding],
    round_number: int,
) -> str:
    lines = [
        f"# PR Review Fix Plan: {review_run.review_id}",
        "",
        f"- round: {round_number}",
        f"- review_pack: {review_run.review_pack_path}",
        f"- findings: {review_run.findings_path}",
        "",
        "## Required Fixes",
    ]
    if not selected:
        lines.append("- No BLOCKER or REQUIRED findings are currently unresolved.")
    for finding in selected:
        lines.extend(
            [
                f"- {finding.id} [{finding.severity}] {finding.file}",
                f"  - claim: {finding.claim}",
                f"  - risk: {finding.risk}",
                f"  - suggested_fix: {finding.suggested_fix}",
            ]
        )
    lines.extend(["", "## Advisory Not Auto-Planned"])
    if not advisory:
        lines.append("- No unresolved ADVISORY findings were skipped.")
    for finding in advisory:
        lines.append(
            f"- {finding.id} [{finding.severity}] {finding.file}: {finding.claim}"
        )
    return "\n".join(lines)


def _render_final_report(
    *,
    review_run: ReviewRun,
    review_pack: ReviewPack,
    findings: ReviewFindings,
    resolution_statuses: dict[str, FindingResolutionStatus],
    resolution_records: dict[str, FindingResolution],
    verdict: ReviewVerdict,
    unresolved: dict[FindingSeverity, int],
    verification_evidence: PRReviewVerificationEvidence | None,
    next_action: str,
) -> str:
    evidence = (
        [
            f"{' '.join(result.argv)} => {result.status} (exit={result.exit_code})"
            for result in verification_evidence.results
        ]
        + [f"[legacy] {entry}" for entry in verification_evidence.entries]
        if verification_evidence is not None
        else ["No executable verification evidence provided."]
    )
    coverage = review_pack.diff_coverage
    lines = [
        f"# Local PR Review Final Report: {review_run.review_id}",
        "",
        f"- verdict: {verdict}",
        f"- base_commit: {review_run.base_commit}",
        f"- head_commit: {review_run.head_commit}",
        f"- unresolved_blockers: {unresolved[FindingSeverity.BLOCKER]}",
        f"- unresolved_required: {unresolved[FindingSeverity.REQUIRED]}",
        f"- unresolved_advisory: {unresolved[FindingSeverity.ADVISORY]}",
        f"- changed_files: {coverage.get('changed_files', 0)}",
        f"- included_files: {coverage.get('included_files', 0)}",
        f"- redacted_files: {coverage.get('redacted_files', 0)}",
        f"- omitted_files: {coverage.get('omitted_files', 0)}",
    ]
    lines.extend([f"- next_action: {next_action}", "", "## Verification Evidence"])
    lines.extend(f"- {item}" for item in evidence)
    lines.extend(
        [
            "",
            "## Finding Outcomes",
            *_render_finding_outcome_lines(
                findings=findings,
                resolution_statuses=resolution_statuses,
                resolution_records=resolution_records,
                verdict=verdict,
            ),
        ]
    )
    return "\n".join(lines)


def _render_finding_outcome_lines(
    *,
    findings: ReviewFindings,
    resolution_statuses: dict[str, FindingResolutionStatus],
    resolution_records: dict[str, FindingResolution],
    verdict: ReviewVerdict,
) -> list[str]:
    lines: list[str] = []
    for finding in findings.findings:
        status = resolution_statuses.get(finding.id, finding.resolution)
        resolution = resolution_records.get(finding.id)
        outcome = str(status)
        if (
            verdict == ReviewVerdict.RISK_ACCEPTED
            and finding.severity == FindingSeverity.REQUIRED
            and status
            not in {
                FindingResolutionStatus.FIXED,
                FindingResolutionStatus.WAIVED,
                FindingResolutionStatus.NOT_APPLICABLE,
            }
        ):
            outcome = "risk_accepted"
        lines.extend(
            [
                f"- {finding.id} [{finding.severity}] {finding.file}",
                f"  - resolution: {outcome}",
                f"  - claim: {finding.claim}",
                f"  - risk: {finding.risk}",
            ]
        )
        if resolution is not None:
            lines.extend(
                [
                    f"  - reason: {resolution.reason or 'n/a'}",
                    f"  - evidence_refs: {', '.join(resolution.evidence_refs) or 'n/a'}",
                    f"  - operator: {resolution.operator or 'n/a'}",
                    f"  - resolved_at: {resolution.resolved_at or 'n/a'}",
                ]
            )
    if not lines:
        return ["- None."]
    return lines


__all__ = [
    "CURRENT_REVIEW_PATH",
    "PRReviewCheck",
    "PRReviewCommandStatus",
    "PRReviewCloseResult",
    "PRReviewCommitResult",
    "PRReviewDoctorResult",
    "PRReviewEvidenceResult",
    "PRReviewFixResult",
    "PRReviewStartOptions",
    "PRReviewStartResult",
    "PRReviewStatusResult",
    "close_pr_review",
    "commit_pr_review",
    "detect_current_model",
    "doctor_pr_review",
    "fix_pr_review",
    "parse_provider_command",
    "record_pr_review_verification_evidence",
    "rerun_pr_review",
    "start_pr_review",
    "status_pr_review",
    "verify_pr_review_command",
]
