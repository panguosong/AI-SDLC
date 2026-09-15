"""Review pack generation for local adversarial PR review."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ai_sdlc.branch.git_client import GitClient, GitError
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_models import LoopPolicyProfile
from ai_sdlc.core.loop_policy import (
    ModelResolutionRequest,
    load_loop_policy,
    resolve_model_for_review,
)
from ai_sdlc.core.pr_review_models import (
    DiffSourceKind,
    ModelResolution,
    ModelResolutionStatus,
    ProviderMode,
    ReviewPack,
    SourceAccessStatus,
    SourceAdapterResolution,
)
from ai_sdlc.core.pr_review_redaction import RedactionReport, analyze_redaction
from ai_sdlc.core.pr_review_source import (
    DiffSourceResolutionOptions,
    resolve_diff_source,
)
from ai_sdlc.core.source_snapshot import (
    SourceSnapshotOptions,
    build_source_snapshot,
)

STRICT_OMITTED_FILE_POLICIES = {"blocked", "forbid", "fail-closed", "strict"}


class ReviewPackBuildStatus(StrEnum):
    """Review pack build outcome."""

    READY = "ready"
    NEEDS_USER = "needs_user"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ReviewPackBuildOptions:
    """Inputs for a local PR review pack build."""

    root: Path
    base_ref: str
    review_id: str
    loop_id: str
    head_ref: str = "HEAD"
    diff_source: str = "local-git-range"
    patch_file: str = ""
    source_id: str = ""
    source_provider: str = ""
    requested_provider: str | None = None
    requested_model: str | None = None
    provider_default_model: str = ""
    current_model: str = ""
    provider_mode: ProviderMode = ProviderMode.LOCAL_AGENT
    code_egress: bool = False
    code_egress_confirmed: bool = False
    work_item_refs: list[str] = field(default_factory=list)
    test_results_refs: list[str] = field(default_factory=list)
    policy_refs: list[str] = field(default_factory=list)
    max_diff_bytes: int = 500_000
    max_file_bytes: int = 1_000_000
    clear_stale_artifacts: bool = True
    preserve_resolution_history: bool = False
    expected_repair_source: SourceAdapterResolution | None = None
    pre_publish_guard: Callable[[], None] | None = None
    publication_recorder: Callable[[Path, bytes], None] | None = None
    rejected_feedback_ref: dict[str, str] | None = None
    workspace_adoption_ref: dict[str, str] | None = None


class ReviewPackBuildResult(BaseModel):
    """Machine-readable result for review pack generation."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: ReviewPackBuildStatus
    review_id: str
    loop_id: str
    review_dir: str
    review_pack_path: str = ""
    diff_path: str = ""
    changed_files_path: str = ""
    redaction_report_path: str = ""
    model_resolution_path: str = ""
    source_resolution_path: str = ""
    blocker: str = ""
    next_action: str = ""
    changed_files_count: int = 0
    included_files_count: int = 0
    omitted_files_count: int = 0
    redacted_files_count: int = 0
    review_pack: ReviewPack | None = None
    model_resolution: ModelResolution | None = None
    source_resolution: SourceAdapterResolution | None = None


@dataclass(frozen=True, slots=True)
class IncompleteReviewPackDecision:
    """Policy decision for redacted or omitted review-pack inputs."""

    status: ReviewPackBuildStatus | None = None
    blocker: str = ""
    next_action: str = ""
    waiver_allowed: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedReviewInput:
    """Changed files and patch text derived from one resolved DiffSource."""

    changed_files: list[str]
    diff_text: str = ""
    uses_git_range: bool = False
    source_file_bytes: dict[str, bytes] = field(default_factory=dict)
    base_file_bytes: dict[str, bytes] = field(default_factory=dict)


def decide_incomplete_review_pack(
    policy: LoopPolicyProfile, redaction_report: RedactionReport
) -> IncompleteReviewPackDecision:
    """Return fail-closed behavior for incomplete review-pack coverage."""

    has_redacted = bool(redaction_report.redacted_files)
    has_omitted = bool(redaction_report.omitted_files)
    if not has_redacted and not has_omitted:
        return IncompleteReviewPackDecision()
    if (
        has_omitted
        and not has_redacted
        and policy.allowed_omitted_file_policy == "allow-with-waiver"
    ):
        return IncompleteReviewPackDecision(waiver_allowed=True)
    incomplete_status = (
        ReviewPackBuildStatus.BLOCKED
        if policy.allowed_omitted_file_policy in STRICT_OMITTED_FILE_POLICIES
        else ReviewPackBuildStatus.NEEDS_USER
    )
    return IncompleteReviewPackDecision(
        status=incomplete_status,
        blocker=(
            "Review pack is incomplete because changed files were redacted or omitted."
        ),
        next_action=(
            "Review redaction-report.json, split unsupported files, or explicitly "
            "allow omitted-file review risk by policy."
        ),
    )


def analyze_pr_review_redaction(
    root: Path,
    *,
    base_ref: str,
    head_ref: str,
    changed_files: list[str],
    policy: LoopPolicyProfile,
    code_egress: bool = False,
    code_egress_confirmed: bool = False,
    max_file_bytes: int = 1_000_000,
) -> RedactionReport:
    """Analyze PR-scope files using immutable git blobs, including deletions."""

    git = GitClient(root)
    base_commit = git.merge_base(base_ref, head_ref)
    head_commit = git.resolve_revision(head_ref)
    deleted_files = _git_deleted_files(root, base_ref, head_ref)
    return analyze_redaction(
        root,
        changed_files,
        policy=policy,
        code_egress=code_egress,
        code_egress_confirmed=code_egress_confirmed,
        max_file_bytes=max_file_bytes,
        head_file_bytes=_git_file_blobs(root, head_commit, changed_files),
        base_file_bytes=_git_file_blobs(root, base_commit, changed_files),
        deleted_file_bytes=_git_file_blobs(root, base_commit, deleted_files),
    )


def build_review_pack(options: ReviewPackBuildOptions) -> ReviewPackBuildResult:
    """Build a bounded local PR review pack from Git state and policy."""

    # 拒绝未发布恢复引用，不触碰原评审或发布新材料。
    if options.rejected_feedback_ref is not None or options.workspace_adoption_ref is not None:
        return ReviewPackBuildResult(
            status=ReviewPackBuildStatus.BLOCKED,
            review_id=options.review_id, loop_id=options.loop_id, review_dir="",
            blocker="Historical provider recovery references are unsupported.",
            next_action="Preserve the original review and use only a supported normal review pack.",
        )

    root = options.root.resolve()
    store = LoopArtifactStore(root)
    pending_publication: dict[Path, bytes] = {}

    def publish_json(path: Path, payload: BaseModel) -> Path:
        # 有发布归属绑定时先准备原序列化字节；验证拒绝不能覆盖原包或制造发布记录。
        if options.publication_recorder is not None:
            raw = (json.dumps(payload.model_dump(mode="json"), ensure_ascii=False,
                              indent=2, sort_keys=False) + "\n").encode("utf-8")
            pending_publication[path] = raw
            return path
        return store.write_json_artifact(path, payload)

    def publish_markdown(path: Path, content: str) -> Path:
        if options.publication_recorder is not None:
            text = content if content.endswith("\n") else content + "\n"
            pending_publication[path] = text.encode("utf-8")
            return path
        return store.write_markdown_artifact(path, content)

    def publication_bytes(path: Path) -> bytes:
        return pending_publication[path] if options.publication_recorder is not None else path.read_bytes()

    def check_publication_guard() -> None:
        if options.pre_publish_guard is not None:
            try:
                options.pre_publish_guard()
            except (ValueError, OSError) as exc:
                raise GitError(f"Review publication guard rejected: {exc}") from exc

    if options.expected_repair_source is not None and (
        options.clear_stale_artifacts
        or not options.preserve_resolution_history
        or options.diff_source != "local-staged"
    ):
        raise GitError("repair scope requires the original staged rerun history")
    limit_blocker = review_diff_limit_blocker(options.max_diff_bytes)
    if limit_blocker:
        return _build_result(
            options=options,
            review_dir=store.review_run_dir(options.review_id),
            status=ReviewPackBuildStatus.NEEDS_USER,
            blocker=limit_blocker,
            next_action="Set --max-diff-bytes to a positive integer for this invocation.",
        )
    review_dir = store.create_review_run_dir(options.review_id)
    if options.clear_stale_artifacts:
        _clear_stale_run_artifacts(
            review_dir,
            preserve_resolution_history=options.preserve_resolution_history,
        )
    policy = load_loop_policy(root)
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
    repair_input = None
    if options.expected_repair_source is not None:
        _require_repair_source(source_resolution, options.expected_repair_source)
        repair_input = _resolve_review_input(root, source_resolution)
        # 暂存读取期间可能漂移；准备发布前核对源及实际文件字节。
        expected = options.expected_repair_source
        expected_paths = _git_name_only(
            root, "diff", expected.head_commit, expected.staged_tree_oid, "--name-only"
        )
        if (
            repair_input.changed_files != expected_paths
            or repair_input.source_file_bytes
            != _git_file_blobs(root, expected.staged_tree_oid, expected_paths)
            or repair_input.base_file_bytes
            != _git_file_blobs(root, expected.head_commit, expected_paths)
        ):
            raise GitError("repair scope staged input drifted before pack write")
        _require_repair_source(
            resolve_diff_source(
                DiffSourceResolutionOptions(root=root, source_kind="local-staged")
            ),
            expected,
        )
    if options.publication_recorder is None:
        check_publication_guard()
    source_resolution_path = publish_json(
        review_dir / "source-resolution.json",
        source_resolution,
    )
    if source_resolution.access_status != SourceAccessStatus.RESOLVED:
        return _build_result(
            options=options,
            review_dir=review_dir,
            source_resolution_path=source_resolution_path,
            status=ReviewPackBuildStatus.BLOCKED
            if source_resolution.access_status == SourceAccessStatus.BLOCKED
            else ReviewPackBuildStatus.NEEDS_USER,
            blocker=source_resolution.blocker or source_resolution.unavailable_reason,
            next_action=source_resolution.next_command,
            source_resolution=source_resolution,
        )
    try:
        review_input = (
            repair_input
            if repair_input is not None
            else _resolve_review_input(root, source_resolution)
        )
    except GitError as exc:
        return _build_result(
            options=options,
            review_dir=review_dir,
            source_resolution_path=source_resolution_path,
            status=ReviewPackBuildStatus.BLOCKED,
            blocker=str(exc),
            next_action="Fix the diff source and rerun local PR review.",
            source_resolution=source_resolution,
        )
    base_commit = source_resolution.base_commit
    head_commit = source_resolution.head_commit
    changed_files = review_input.changed_files

    model_resolution = resolve_model_for_review(
        policy,
        ModelResolutionRequest(
            requested_provider=options.requested_provider,
            requested_model=options.requested_model,
            provider_default_model=options.provider_default_model,
            current_model=options.current_model,
            provider_mode=options.provider_mode,
            code_egress=options.code_egress,
            code_egress_confirmed=options.code_egress_confirmed,
        ),
    )

    changed_files_path = publish_markdown(
        review_dir / "changed-files.txt",
        "\n".join(changed_files),
    )
    model_resolution_path = publish_json(
        review_dir / "model-resolution.json",
        model_resolution,
    )
    if review_input.uses_git_range:
        redaction_report = analyze_pr_review_redaction(
            root,
            base_ref=source_resolution.base_ref,
            head_ref=source_resolution.head_ref,
            changed_files=changed_files,
            policy=policy,
            code_egress=options.code_egress,
            code_egress_confirmed=options.code_egress_confirmed,
            max_file_bytes=options.max_file_bytes,
        )
    else:
        redaction_report = analyze_redaction(
            root,
            changed_files,
            policy=policy,
            code_egress=options.code_egress,
            code_egress_confirmed=options.code_egress_confirmed,
            max_file_bytes=options.max_file_bytes,
            head_file_bytes=review_input.source_file_bytes,
            base_file_bytes=review_input.base_file_bytes,
        )
    redaction_report_path = publish_json(
        review_dir / "redaction-report.json",
        redaction_report,
    )

    if model_resolution.status != ModelResolutionStatus.RESOLVED:
        return _build_result(
            options=options,
            review_dir=review_dir,
            changed_files_path=changed_files_path,
            redaction_report_path=redaction_report_path,
            model_resolution_path=model_resolution_path,
            source_resolution_path=source_resolution_path,
            status=_status_from_model_resolution(model_resolution),
            blocker=model_resolution.blocker,
            next_action="Configure or choose an executable local review model.",
            changed_files_count=len(changed_files),
            included_files_count=len(redaction_report.included_files),
            omitted_files_count=len(redaction_report.omitted_files),
            redacted_files_count=len(redaction_report.redacted_files),
            model_resolution=model_resolution,
            source_resolution=source_resolution,
        )

    if redaction_report.blocked or redaction_report.needs_user:
        return _build_result(
            options=options,
            review_dir=review_dir,
            changed_files_path=changed_files_path,
            redaction_report_path=redaction_report_path,
            model_resolution_path=model_resolution_path,
            source_resolution_path=source_resolution_path,
            status=ReviewPackBuildStatus.BLOCKED
            if redaction_report.blocked
            else ReviewPackBuildStatus.NEEDS_USER,
            blocker=redaction_report.blocker,
            next_action=redaction_report.next_action,
            changed_files_count=len(changed_files),
            included_files_count=len(redaction_report.included_files),
            omitted_files_count=len(redaction_report.omitted_files),
            redacted_files_count=len(redaction_report.redacted_files),
            model_resolution=model_resolution,
            source_resolution=source_resolution,
        )
    incomplete_decision = decide_incomplete_review_pack(policy, redaction_report)
    if incomplete_decision.status is not None:
        return _build_result(
            options=options,
            review_dir=review_dir,
            changed_files_path=changed_files_path,
            redaction_report_path=redaction_report_path,
            model_resolution_path=model_resolution_path,
            source_resolution_path=source_resolution_path,
            status=incomplete_decision.status,
            blocker=incomplete_decision.blocker,
            next_action=incomplete_decision.next_action,
            changed_files_count=len(changed_files),
            included_files_count=len(redaction_report.included_files),
            omitted_files_count=len(redaction_report.omitted_files),
            redacted_files_count=len(redaction_report.redacted_files),
            model_resolution=model_resolution,
            source_resolution=source_resolution,
        )

    included_files = list(redaction_report.included_files)
    if options.expected_repair_source is not None:
        # 已确认的请求只消费该不可变树；不把之后的 index 变化写成新的获准基线。
        diff = _git_diff_for_paths(
            root,
            ["diff", source_resolution.head_commit, source_resolution.staged_tree_oid],
            included_files,
        )
    else:
        diff = diff_for_review_source(
            root, source_resolution, included_files, review_input
        )
    diff_bytes = len(diff.encode("utf-8"))
    size_blocker = review_diff_size_blocker(diff, options.max_diff_bytes)
    if size_blocker:
        return _build_result(
            options=options,
            review_dir=review_dir,
            changed_files_path=changed_files_path,
            redaction_report_path=redaction_report_path,
            model_resolution_path=model_resolution_path,
            source_resolution_path=source_resolution_path,
            status=ReviewPackBuildStatus.NEEDS_USER,
            blocker=size_blocker,
            next_action="Set --max-diff-bytes to the required capacity for this invocation.",
            changed_files_count=len(changed_files),
            included_files_count=len(redaction_report.included_files),
            omitted_files_count=len(redaction_report.omitted_files),
            redacted_files_count=len(redaction_report.redacted_files),
            model_resolution=model_resolution,
            source_resolution=source_resolution,
        )

    diff_path = publish_markdown(review_dir / "diff.patch", diff)
    review_pack = ReviewPack(
        review_id=options.review_id,
        loop_id=options.loop_id,
        diff_source=source_resolution.to_descriptor(),
        source_adapter=source_resolution.adapter_id,
        source_access_status=source_resolution.access_status,
        source_resolution_path=_repo_relative(root, source_resolution_path),
        source_resolution_digest=(
            f"sha256:{hashlib.sha256(publication_bytes(source_resolution_path)).hexdigest()}"
        ),
        repo_root=str(root),
        base_ref=source_resolution.base_ref,
        head_ref=source_resolution.head_ref,
        base_commit=base_commit,
        head_commit=head_commit,
        staged_tree_oid=source_resolution.staged_tree_oid,
        changed_files=changed_files,
        diff_summary=(
            f"{len(changed_files)} changed file(s), "
            f"{len(redaction_report.included_files)} included, "
            f"{len(redaction_report.redacted_files)} redacted, "
            f"{len(redaction_report.omitted_files)} omitted."
        ),
        diff_path=_repo_relative(root, diff_path),
        diff_digest=f"sha256:{hashlib.sha256(publication_bytes(diff_path)).hexdigest()}",
        diff_coverage={
            "changed_files": len(changed_files),
            "included_files": len(redaction_report.included_files),
            "redacted_files": len(redaction_report.redacted_files),
            "omitted_files": len(redaction_report.omitted_files),
            "diff_bytes": diff_bytes,
        },
        work_item_refs=list(options.work_item_refs),
        test_results_refs=list(options.test_results_refs),
        policy_refs=list(options.policy_refs),
        policy_profile_id=policy.profile_id,
        policy_decisions={
            "provider_id": model_resolution.provider_id,
            "provider_mode": str(model_resolution.provider_mode),
            "diff_source": source_resolution.source_kind,
            "source_adapter": source_resolution.adapter_id,
            "source_access_status": source_resolution.access_status,
            "model_selector": model_resolution.model_selector,
            "resolved_model": model_resolution.resolved_model,
            "model_resolution_status": str(model_resolution.status),
            "remote_model_policy": policy.remote_model_policy,
            "high_risk_secret_policy": policy.high_risk_secret_policy,
            "allowed_omitted_file_policy": policy.allowed_omitted_file_policy,
            "incomplete_review_waiver": incomplete_decision.waiver_allowed,
            "default_close_mode": policy.default_close_mode,
            "code_egress": options.code_egress,
        },
        model_selector=model_resolution.model_selector,
        resolved_model=model_resolution.resolved_model,
        model_resolution_status=model_resolution.status,
        model_resolution_source=model_resolution.resolution_source,
        model_unavailable_reason=model_resolution.unavailable_reason,
        provider_mode=model_resolution.provider_mode,
        code_egress=options.code_egress,
        redaction_report_path=_repo_relative(root, redaction_report_path),
        reviewer_allowlist=included_files,
    )
    review_pack_path = publish_json(
        review_dir / "review-pack.json",
        review_pack,
    )
    result = _build_result(
        options=options,
        review_dir=review_dir,
        review_pack_path=review_pack_path,
        diff_path=diff_path,
        changed_files_path=changed_files_path,
        redaction_report_path=redaction_report_path,
        model_resolution_path=model_resolution_path,
        source_resolution_path=source_resolution_path,
        status=ReviewPackBuildStatus.READY,
        changed_files_count=len(changed_files),
        included_files_count=len(redaction_report.included_files),
        omitted_files_count=len(redaction_report.omitted_files),
        redacted_files_count=len(redaction_report.redacted_files),
        review_pack=review_pack,
        model_resolution=model_resolution,
        source_resolution=source_resolution,
    )
    if options.publication_recorder is not None:
        # 六件字节及 READY 结果均已构造成功，最终复核后才逐件登记并严格原子发布。
        check_publication_guard()
        for path, raw in pending_publication.items():
            options.publication_recorder(path, raw)
            store.write_bytes_artifact(path, raw)
    return result


def _git_changed_files(root: Path, base_ref: str, head_ref: str) -> list[str]:
    return _git_name_only(root, "diff", "--name-only", f"{base_ref}...{head_ref}")


def _resolve_review_input(
    root: Path,
    source_resolution: SourceAdapterResolution,
) -> ResolvedReviewInput:
    source_kind = DiffSourceKind(source_resolution.source_kind)
    if source_kind in {
        DiffSourceKind.LOCAL_STAGED,
        DiffSourceKind.LOCAL_UNSTAGED,
    }:
        _require_visible_local_index(root)
    if source_kind == DiffSourceKind.LOCAL_GIT_RANGE:
        return ResolvedReviewInput(
            changed_files=_git_changed_files(
                root,
                source_resolution.base_ref,
                source_resolution.head_ref,
            ),
            uses_git_range=True,
        )
    if source_kind == DiffSourceKind.PATCH:
        patch_text = _read_patch_text(root, source_resolution.patch_file)
        return ResolvedReviewInput(
            changed_files=_patch_changed_files(patch_text),
            diff_text=patch_text,
            source_file_bytes=_diff_file_blobs(patch_text),
        )
    if source_kind == DiffSourceKind.LOCAL_STAGED:
        diff_text = _git_text(root, "diff", "--cached")
        changed_files = _git_name_only(root, "diff", "--cached", "--name-only")
        return ResolvedReviewInput(
            changed_files=changed_files,
            diff_text=diff_text,
            source_file_bytes=_git_index_file_blobs(root, changed_files),
            base_file_bytes=_git_file_blobs(root, "HEAD", changed_files),
        )
    if source_kind == DiffSourceKind.LOCAL_UNSTAGED:
        snapshot = build_source_snapshot(
            SourceSnapshotOptions(root=root, source_kind="local-unstaged")
        )
        tracked_diff = _git_text(
            root,
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
        )
        diff_text = tracked_diff + "".join(
            _untracked_file_diff(root, path) for path in snapshot.untracked_files
        )
        changed_files = snapshot.changed_files
        return ResolvedReviewInput(
            changed_files=changed_files,
            diff_text=diff_text,
            source_file_bytes=_worktree_file_blobs(root, changed_files),
            base_file_bytes=_git_index_file_blobs(root, changed_files),
        )
    raise GitError(f"Unsupported resolved diff source: {source_resolution.source_kind}")


def _require_visible_local_index(root: Path) -> None:
    records = _git_text(root, "ls-files", "-v", "-z").split("\0")
    hidden: list[str] = []
    for record in records:
        if not record:
            continue
        tag, separator, path = record.partition(" ")
        if not separator or len(tag) != 1:
            raise GitError("git index flags are malformed")
        if tag == "S" or tag.islower():
            hidden.append(path)
    if hidden:
        raise GitError(
            "Local review cannot inspect paths with hidden index flags: "
            + ", ".join(sorted(hidden))
        )


def _require_repair_source(
    actual: SourceAdapterResolution, expected: SourceAdapterResolution
) -> None:
    if (
        actual.access_status != SourceAccessStatus.RESOLVED
        or actual.source_kind != DiffSourceKind.LOCAL_STAGED
        or actual.to_descriptor() != expected.to_descriptor()
    ):
        raise GitError("repair scope HEAD or staged source changed before pack write")


def resolve_review_input_for_source(
    root: Path,
    source_resolution: SourceAdapterResolution,
) -> ResolvedReviewInput:
    """Return source-specific review input for preview and pack generation."""

    return _resolve_review_input(root, source_resolution)


def review_diff_limit_blocker(max_diff_bytes: int) -> str:
    """在写入工件前校验本次调用的容量。"""

    if type(max_diff_bytes) is not int or max_diff_bytes <= 0:
        return "Review diff byte limit must be a positive integer."
    return ""


def review_diff_size_blocker(diff: str, max_diff_bytes: int) -> str:
    """预览和实际构建共用相同的 UTF-8 字节边界。"""

    invalid = review_diff_limit_blocker(max_diff_bytes)
    if invalid:
        return invalid
    diff_bytes = len(diff.encode("utf-8"))
    if diff_bytes > max_diff_bytes:
        return (
            f"Review diff is {diff_bytes} bytes, above the configured "
            f"{max_diff_bytes} byte limit."
        )
    return ""


def diff_for_review_source(
    root: Path,
    source_resolution: SourceAdapterResolution,
    included_files: list[str],
    review_input: ResolvedReviewInput,
) -> str:
    source_kind = DiffSourceKind(source_resolution.source_kind)
    if source_kind == DiffSourceKind.LOCAL_GIT_RANGE:
        return _git_diff(
            root,
            source_resolution.base_ref,
            source_resolution.head_ref,
            included_files,
        )
    if source_kind == DiffSourceKind.PATCH:
        return _filter_patch_diff(review_input.diff_text, included_files)
    if source_kind == DiffSourceKind.LOCAL_STAGED:
        return _git_diff_for_paths(root, ["diff", "--cached"], included_files)
    if source_kind == DiffSourceKind.LOCAL_UNSTAGED:
        return _filter_patch_diff(review_input.diff_text, included_files)
    raise GitError(f"Unsupported resolved diff source: {source_resolution.source_kind}")


def _read_patch_text(root: Path, patch_file: str) -> str:
    path = Path(patch_file)
    resolved = path if path.is_absolute() else root / path
    try:
        return resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise GitError(f"Patch file is not accessible: {patch_file}") from exc


def _untracked_file_diff(root: Path, path: str) -> str:
    if "\n" in path or "\r" in path:
        raise GitError("Untracked file paths containing newlines cannot be reviewed.")
    source = root / path
    is_symlink = source.is_symlink()
    payload = os.fsencode(os.readlink(source)) if is_symlink else source.read_bytes()
    mode = "120000" if is_symlink else "100644"
    header = f"diff --git a/{path} b/{path}\nnew file mode {mode}\n"
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        digest = hashlib.sha256(payload).hexdigest()
        return (
            header
            + f"Binary files /dev/null and b/{path} differ\n"
            + f"binary-sha256 {digest}\n"
        )
    patch = difflib.unified_diff(
        [],
        lines,
        fromfile="/dev/null",
        tofile=f"b/{path}",
        lineterm="",
    )
    return header + "\n".join(patch) + "\n"


def _patch_changed_files(patch_text: str) -> list[str]:
    files: list[str] = []
    pending_old = ""
    for raw_line in patch_text.splitlines():
        line = raw_line.strip()
        if line.startswith("diff --git "):
            for path in _diff_git_paths(line):
                _append_unique(files, path)
            continue
        if line.startswith("--- "):
            pending_old = _normalize_patch_path(line.removeprefix("--- ").strip())
            continue
        if line.startswith("+++ "):
            new_path = _normalize_patch_path(line.removeprefix("+++ ").strip())
            _append_unique(files, new_path if new_path else pending_old)
            pending_old = ""
    return files


def _diff_file_blobs(diff_text: str) -> dict[str, bytes]:
    blobs: dict[str, bytes] = {}
    current_lines: list[str] = []
    current_paths: set[str] = set()

    def flush() -> None:
        if not current_lines or not current_paths:
            return
        content = ("\n".join(current_lines) + "\n").encode("utf-8")
        for path in current_paths:
            blobs[path] = content

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            flush()
            current_lines = [raw_line]
            current_paths = set()
            current_paths.update(_diff_git_paths(raw_line))
            continue
        if raw_line.startswith("--- ") or raw_line.startswith("+++ "):
            if (
                raw_line.startswith("--- ")
                and current_lines
                and not current_lines[0].startswith("diff --git ")
            ):
                flush()
                current_lines = []
                current_paths = set()
            if not current_lines:
                current_lines = [raw_line]
            else:
                current_lines.append(raw_line)
            path = _normalize_patch_path(raw_line[4:].strip())
            if path:
                current_paths.add(path)
            continue
        if not current_lines:
            continue
        current_lines.append(raw_line)
    flush()
    return blobs


def _git_index_file_blobs(root: Path, paths: list[str]) -> dict[str, bytes]:
    blobs: dict[str, bytes] = {}
    for path in paths:
        normalized = _normalize_repo_path(path)
        if not normalized:
            continue
        try:
            result = subprocess.run(
                ["git", "show", f":{normalized}"],
                cwd=root,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            blobs[normalized] = result.stdout
    return blobs


def _worktree_file_blobs(root: Path, paths: list[str]) -> dict[str, bytes]:
    blobs: dict[str, bytes] = {}
    for path in paths:
        normalized = _normalize_repo_path(path)
        if not normalized:
            continue
        candidate = root / normalized
        try:
            if candidate.is_symlink():
                blobs[normalized] = os.fsencode(os.readlink(candidate))
                continue
            file_path = candidate.resolve()
            file_path.relative_to(root.resolve())
            if file_path.is_file():
                blobs[normalized] = file_path.read_bytes()
        except (OSError, ValueError):
            continue
    return blobs


def _filter_patch_diff(patch_text: str, included_files: list[str]) -> str:
    included = {_normalize_repo_path(path) for path in included_files}
    if not included:
        return ""
    discovered = _patch_changed_files(patch_text)
    if discovered and all(
        _normalize_repo_path(path) in included for path in discovered
    ):
        return patch_text

    output: list[str] = []
    current: list[str] = []
    current_paths: set[str] = set()

    def flush() -> None:
        if current and current_paths and current_paths <= included:
            output.extend(current)

    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            current = [line]
            current_paths = set()
            current_paths.update(_diff_git_paths(line))
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            if (
                line.startswith("--- ")
                and current
                and not current[0].startswith("diff --git ")
            ):
                flush()
                current = []
                current_paths = set()
            if not current:
                current = [line]
            else:
                current.append(line)
            normalized = _normalize_patch_path(line[4:].strip())
            if normalized:
                current_paths.add(normalized)
            continue
        if current:
            current.append(line)
    flush()
    return "\n".join(output) + ("\n" if output else "")


def _normalize_patch_path(value: str) -> str:
    text = value.strip()
    if "\t" in text:
        text = text.split("\t", 1)[0].rstrip()
    if text.startswith('"'):
        # 只解 Git C 字节转义，不改原 diff；未知转义和非法 UTF-8 仍拒绝。
        if not re.fullmatch(r'"(?:[^"\\]|\\(?:[0-3][0-7]{2}|[abfnrtv"\\]))*"', text):
            raise GitError("Malformed Git quoted patch path")
        try:
            encoded = ''.join(
                chr(byte) if byte < 128 else f"\\x{byte:02x}"
                for byte in text.encode("utf-8")
            )
            decoded = ast.literal_eval("b" + encoded)
            text = decoded.decode("utf-8")
        except (SyntaxError, ValueError, UnicodeError) as exc:
            raise GitError("Invalid Git quoted patch path encoding") from exc
    if text == "/dev/null":
        return ""
    if text.startswith("a/") or text.startswith("b/"):
        text = text[2:]
    return _normalize_patch_repo_path(text)


def _normalize_patch_repo_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or _looks_like_windows_absolute_path(normalized):
        raise GitError(f"Patch path escapes repository: {value}")
    parts: list[str] = []
    for part in normalized.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            raise GitError(f"Patch path escapes repository: {value}")
        parts.append(part)
    return "/".join(parts)


def _looks_like_windows_absolute_path(value: str) -> bool:
    return len(value) >= 2 and value[1] == ":" and value[0].isalpha()


def _diff_git_paths(line: str) -> list[str]:
    text = line.removeprefix("diff --git ").strip()
    if text.startswith("a/") and " b/" in text:
        if text.count(" b/") != 1:
            return []
        left, right_tail = text.rsplit(" b/", 1)
        return [
            path
            for path in (
                _normalize_patch_path(left),
                _normalize_patch_path(f"b/{right_tail}"),
            )
            if path
        ]
    if text.startswith('"') or ' "' in text:
        tokens = re.fullmatch(
            r'("(?:[^"\\]|\\.)*"|[^\s]+)\s+("(?:[^"\\]|\\.)*"|[^\s]+)',
            text,
        )
        if tokens is None:
            raise GitError("Malformed quoted diff Git header")
        return [_normalize_patch_path(part) for part in tokens.groups()]
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    return [_normalize_patch_path(part) for part in parts[:2] if part.strip()]


def _normalize_repo_path(value: str) -> str:
    return value.replace("\\", "/").lstrip("/")


def _append_unique(items: list[str], value: str) -> None:
    normalized = _normalize_repo_path(value)
    if normalized and normalized not in items:
        items.append(normalized)


def _git_name_only(root: Path, *args: str) -> list[str]:
    # Git 的 NUL 名单保留真实路径；不能把 C 引号转义当成仓库文件名。
    return [path for path in _git_text(root, *args, "-z").split("\0") if path]


def _git_diff_for_paths(root: Path, args: list[str], paths: list[str]) -> str:
    if not paths:
        return ""
    return _git_text(root, *args, "--", *paths)


def _git_diff(root: Path, base_ref: str, head_ref: str, paths: list[str]) -> str:
    if not paths:
        return ""
    cmd = ["git", "diff", f"{base_ref}...{head_ref}", "--", *paths]
    try:
        result = subprocess.run(
            cmd,
            cwd=root,
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
        raise GitError("git diff timed out while building review pack") from exc
    if result.returncode != 0:
        raise GitError(
            f"git diff failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def _git_text(root: Path, *args: str) -> str:
    cmd = ["git", *args]
    try:
        result = subprocess.run(
            cmd,
            cwd=root,
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
        raise GitError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def _git_deleted_files(root: Path, base_ref: str, head_ref: str) -> list[str]:
    return _git_name_only(
        root, "diff", "--name-only", "--diff-filter=D", f"{base_ref}...{head_ref}"
    )


def _git_file_blobs(root: Path, ref: str, paths: list[str]) -> dict[str, bytes]:
    blobs: dict[str, bytes] = {}
    for path in paths:
        cmd = ["git", "show", f"{ref}:{path}"]
        try:
            result = subprocess.run(
                cmd,
                cwd=root,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise GitError("git is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError("git show timed out while reading deleted file") from exc
        if result.returncode == 0:
            blobs[path] = result.stdout
    return blobs


def _status_from_model_resolution(
    model_resolution: ModelResolution,
) -> ReviewPackBuildStatus:
    if model_resolution.status == ModelResolutionStatus.BLOCKED:
        return ReviewPackBuildStatus.BLOCKED
    return ReviewPackBuildStatus.NEEDS_USER


def _clear_stale_run_artifacts(
    review_dir: Path,
    *,
    preserve_resolution_history: bool,
) -> None:
    artifact_names = [
        "resolution.yaml",
        "fix-plan.md",
        "final-report.md",
    ]
    if not preserve_resolution_history:
        artifact_names.append("resolution-history.yaml")
    for name in artifact_names:
        try:
            (review_dir / name).unlink()
        except FileNotFoundError:
            continue


def _build_result(
    *,
    options: ReviewPackBuildOptions,
    review_dir: Path,
    status: ReviewPackBuildStatus,
    review_pack_path: Path | None = None,
    diff_path: Path | None = None,
    changed_files_path: Path | None = None,
    redaction_report_path: Path | None = None,
    model_resolution_path: Path | None = None,
    source_resolution_path: Path | None = None,
    blocker: str = "",
    next_action: str = "",
    changed_files_count: int = 0,
    included_files_count: int = 0,
    omitted_files_count: int = 0,
    redacted_files_count: int = 0,
    review_pack: ReviewPack | None = None,
    model_resolution: ModelResolution | None = None,
    source_resolution: SourceAdapterResolution | None = None,
) -> ReviewPackBuildResult:
    if options.publication_recorder is not None and status != ReviewPackBuildStatus.READY:
        # 缓冲中的诊断没有发布，不能把磁盘旧件路径呈现为本次拒绝的新证据。
        review_pack_path = diff_path = changed_files_path = None
        redaction_report_path = model_resolution_path = source_resolution_path = None
    return ReviewPackBuildResult(
        status=status,
        review_id=options.review_id,
        loop_id=options.loop_id,
        review_dir=str(review_dir),
        review_pack_path=str(review_pack_path or ""),
        diff_path=str(diff_path or ""),
        changed_files_path=str(changed_files_path or ""),
        redaction_report_path=str(redaction_report_path or ""),
        model_resolution_path=str(model_resolution_path or ""),
        source_resolution_path=str(source_resolution_path or ""),
        blocker=blocker,
        next_action=next_action,
        changed_files_count=changed_files_count,
        included_files_count=included_files_count,
        omitted_files_count=omitted_files_count,
        redacted_files_count=redacted_files_count,
        review_pack=review_pack,
        model_resolution=model_resolution,
        source_resolution=source_resolution,
    )


def _repo_relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


__all__ = [
    "ReviewPackBuildOptions",
    "ReviewPackBuildResult",
    "ReviewPackBuildStatus",
    "analyze_pr_review_redaction",
    "build_review_pack",
    "decide_incomplete_review_pack",
    "diff_for_review_source",
    "resolve_review_input_for_source",
    "review_diff_limit_blocker",
    "review_diff_size_blocker",
]
