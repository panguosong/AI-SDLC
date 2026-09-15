"""Provider runner contracts for local adversarial PR review."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import cast

from pydantic import BaseModel, ConfigDict

from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_models import LoopStatus, SchemaValidationStatus, utc_now_iso
from ai_sdlc.core.pr_review_models import (
    DiffSourceKind,
    FindingSeverity,
    ModelResolutionSource,
    ProviderCompletionProof,
    ProviderExecutionFailure,
    ProviderIsolationStatus,
    ProviderLaunchStatus,
    ProviderMode,
    ProviderRunnerInvocation,
    ProviderWorkspaceCheck,
    ReviewFinding,
    ReviewFindings,
    ReviewPack,
    ReviewVerdict,
)
from ai_sdlc.core.pr_review_schema import validate_artifact_file
from ai_sdlc.core.quality_command import (
    _PROCESS_OWNER_ENV,
    ControlledQualityOptions,
    QualityCommandOptions,
    quality_command_environment,
    run_controlled_process,
)
from ai_sdlc.core.source_snapshot import SourceSnapshotOptions, build_source_snapshot
from ai_sdlc.core.stable_file_read import (
    _stable_regular_file_exists,
    read_stable_bytes,
)

EXIT_SUCCESS = 0
EXIT_CHANGES_REQUIRED = 10
EXIT_BLOCKED = 20


class ProviderRunStatus(StrEnum):
    """Normalized provider run status."""

    SUCCESS = "success"
    CHANGES_REQUIRED = "changes_required"
    BLOCKED = "blocked"
    NEEDS_USER = "needs_user"


class WorktreeSnapshotError(RuntimeError):
    """Raised when reviewer isolation cannot capture Git worktree state."""


class MockReviewerFixture(StrEnum):
    """Deterministic mock reviewer output fixtures."""

    CLEAN = "clean"
    CHANGES_REQUIRED = "changes_required"
    BLOCKED = "blocked"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class ProviderCommandOptions:
    """Inputs for running a configured local review provider command."""

    root: Path
    review_pack_path: Path
    command: list[str] = field(default_factory=list)
    provider_id: str = "local-agent"
    timeout_seconds: float = 60.0
    isolation_status: ProviderIsolationStatus = ProviderIsolationStatus.ISOLATED_PROCESS
    pre_launch_guard: Callable[[], None] | None = None
    snapshot_max_entries: int = 4096
    snapshot_require_complete: bool = False
    workspace_adoption_ref: dict[str, str] | None = None
    on_execution_failure: Callable[[ProviderRunResult], None] | None = None


class ProviderRunResult(BaseModel):
    """Machine-readable result of a provider run."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: ProviderRunStatus
    exit_code: int | None = None
    invocation_path: str = ""
    findings_path: str = ""
    schema_validation_path: str = ""
    blocker: str = ""
    next_action: str = ""
    invocation: ProviderRunnerInvocation | None = None
    findings: ReviewFindings | None = None


def run_provider_command(options: ProviderCommandOptions) -> ProviderRunResult:
    """Run a configured local reviewer command and validate findings output."""

    # 不支持的恢复能力必须在写回执、清旧输出及创建子进程之前拒绝。
    if (options.snapshot_require_complete or options.snapshot_max_entries != 4096
            or options.workspace_adoption_ref is not None):
        return ProviderRunResult(
            status=ProviderRunStatus.BLOCKED,
            blocker="Historical provider recovery and workspace adoption are unsupported.",
            next_action="Preserve the failed invocation; use only supported normal provider execution.",
        )

    if not options.command:
        return ProviderRunResult(
            status=ProviderRunStatus.NEEDS_USER,
            blocker=(
                "local-agent provider is not configured with a local reviewer command."
            ),
            next_action=(
                "Configure a local reviewer command or run mock-reviewer for an "
                "offline dry run."
            ),
        )

    root = options.root.resolve()
    review_pack = _load_review_pack(options.review_pack_path)
    store = LoopArtifactStore(root)
    review_dir = store.create_review_run_dir(review_pack.review_id)
    findings_path = review_dir / "findings.json"
    invocation_path = review_dir / "reviewer-invocation.json"
    schema_validation_path = review_dir / "schema-validation.json"
    argv = _expand_command(options.command, review_pack, options.review_pack_path, findings_path)

    def prelaunch_failure(blocker: str, next_action: str = "Restore the reported preflight condition and rerun the same review.") -> ProviderRunResult:
        # 尚未创建子进程；现场读取失败不能写成工作区未变化，也不能留下旧调用冒充本次。
        check = ProviderWorkspaceCheck(
            status="unproven", reason=blocker,
            review_pack_digest=hashlib.sha256(read_stable_bytes(root, options.review_pack_path)).hexdigest(),
        )
        invocation = _write_invocation(
            store=store, path=invocation_path, review_pack=review_pack,
            provider_id=options.provider_id, argv=argv, input_path=options.review_pack_path,
            output_path=findings_path, cwd=root, isolation_status=ProviderIsolationStatus.NOT_PROVEN,
            launch_status=ProviderLaunchStatus.NEVER_STARTED, exit_code=None,
            status=LoopStatus.BLOCKED, workspace_check=check, preflight_incomplete=True,
        )
        _remove_previous_provider_outputs(findings_path, schema_validation_path)
        return ProviderRunResult(status=ProviderRunStatus.BLOCKED, findings_path=str(findings_path),
                                 invocation_path=str(invocation_path), invocation=invocation,
                                 blocker=blocker, next_action=next_action)

    def check_prelaunch_guard(stage: str) -> ProviderRunResult | None:
        if options.pre_launch_guard is not None:
            try:
                options.pre_launch_guard()
            except (ValueError, OSError, WorktreeSnapshotError) as exc:
                # 首次准备与最终启动检查共用未启动分类，包括 accepted 层归一后的读取错误。
                return prelaunch_failure(f"Accepted workspace {stage}: {exc}")
        return None

    for check in (
        lambda: _reviewer_allowlist_launch_blocker(review_pack),
        lambda: _reviewed_head_launch_blocker(root, review_pack),
        lambda: _reviewed_diff_source_launch_blocker(root, review_pack),
    ):
        blocker = check()
        if blocker:
            return prelaunch_failure(blocker)
    guard_failure = check_prelaunch_guard("changed before provider preparation")
    if guard_failure is not None:
        return guard_failure
    dirty_blocker = _preexisting_dirty_worktree_blocker(
        root,
        frozenset({review_dir.resolve(), (review_dir.parent / "current-review.json").resolve(),
                   *_provider_command_entry_paths(root, argv)}),
        _reviewed_dirty_paths_for_launch(root, review_pack),
        DiffSourceKind(review_pack.diff_source.source_kind),
    )
    if dirty_blocker:
        return prelaunch_failure(dirty_blocker, "Commit or discard unreviewed worktree changes, then rerun PR review.")
    _remove_previous_provider_outputs(findings_path, schema_validation_path)
    mutable_provider_outputs = frozenset({findings_path.resolve()})
    try:
        pack_digest = hashlib.sha256(
            read_stable_bytes(root, options.review_pack_path)
        ).hexdigest()
        host_paths = _provider_host_artifact_paths(root, review_pack.review_id)
        before_snapshot = _worktree_snapshot(root, mutable_provider_outputs)
        before_recovery = _worktree_snapshot(
            root, mutable_provider_outputs | host_paths
        )
        before_host = _host_artifact_snapshot(root, host_paths)
    except (WorktreeSnapshotError, ValueError, OSError) as exc:
        return prelaunch_failure(_snapshot_failure_blocker(exc), "Fix git status access, then rerun local PR review.")

    launch_status = ProviderLaunchStatus.NEVER_STARTED
    completion_proof = None
    exit_code: int | None = None
    execution_blocker = ""
    execution_exception: BaseException | None = None
    execution_traceback: TracebackType | None = None
    rethrow_execution_exception = False
    proof_root: Path | None = None
    proof_captured = False
    preserve_temporary = False
    try:
        # 预检只保留确定的未启动错误；通过后的启动竞态不能猜成 never_started。
        _require_provider_executable(argv[0], root)
        # 唯一临时原件只在持久化完成后删除；保存失败时保留实际目录供诊断。
        proof_root = Path(tempfile.mkdtemp(prefix="ai-sdlc-provider-owned-")).resolve()
        originals: dict[str, str] = {}
        for name in ("stdout", "stderr"):
            (proof_root / name).touch(exist_ok=False)
        nonce = secrets.token_hex(32)
        environment = quality_command_environment(os.environ)
        # 外层进程的内部归属标记不代表本次调用；核心仍为本次分配独立身份。
        environment.pop(_PROCESS_OWNER_ENV, None)
        prelaunch_return = False

        def persist(name: str):
            def write(payload: dict[str, object]) -> None:
                nonlocal launch_status
                if name == "process.json" or payload.get("launch_status") == "started":
                    launch_status = ProviderLaunchStatus.STARTED
                content = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
                assert proof_root is not None
                LoopArtifactStore(proof_root).write_bytes_artifact(
                    proof_root / name, content.encode("utf-8"), immutable=True
                )
            return write

        try:
            # 创建临时收尾原件也占用时间，实际启动前再次核对接受的现场。
            guard_failure = check_prelaunch_guard("could not be verified before provider launch")
            if guard_failure is not None:
                prelaunch_return = True
                return guard_failure
            try:
                run_controlled_process(
                    QualityCommandOptions(
                        root=root, cwd=root, argv=tuple(argv),
                        timeout_seconds=options.timeout_seconds,
                        controlled=ControlledQualityOptions(
                            environment=environment,
                            stdout_path=proof_root / "stdout", stderr_path=proof_root / "stderr",
                            # provider 原来不截断标准输出；这里只改为持久流式收集。
                            max_output_bytes=sys.maxsize, ownership_nonce=nonce,
                            on_started=persist("process.json"),
                            on_raw_result=persist("raw-result.json"),
                            on_cleanup=persist("cleanup.json"),
                        ),
                    )
                )
            except BaseException as exc:
                execution_exception, execution_traceback = exc, exc.__traceback__
                rethrow_execution_exception = not isinstance(exc, (OSError, ValueError))
                raise
        finally:
            # 发布可能在文件已落盘后抛错；只保存终止后实际存在的稳定原件。
            # 缺失 raw 不补造，cleanup 内保全的原始事实由共享读取判据核验。
            try:
                for name in ("process.json", "raw-result.json", "cleanup.json"):
                    original_path = proof_root / name
                    if _stable_regular_file_exists(proof_root, original_path):
                        originals[name] = read_stable_bytes(proof_root, original_path).decode("utf-8")
                proof_captured = True
            except BaseException as capture_error:
                preserve_temporary = True
                if execution_exception is None:
                    execution_exception, execution_traceback = capture_error, capture_error.__traceback__
                    rethrow_execution_exception = not isinstance(capture_error, (OSError, ValueError))
                    raise
                execution_exception.add_note(
                    f"Provider original capture failed: {type(capture_error).__name__}: {capture_error}"
                )
            finally:
                if originals:
                    completion_proof = ProviderCompletionProof(
                        ownership_nonce=nonce, originals=originals,
                        sha256={name: hashlib.sha256(raw.encode("utf-8")).hexdigest()
                                for name, raw in originals.items()},
                    )
                if prelaunch_return:
                    shutil.rmtree(proof_root)
    except FileNotFoundError as exc:
        execution_blocker = (
            f"Reviewer command not found: {argv[0]}"
            if launch_status == ProviderLaunchStatus.NEVER_STARTED
            else f"Reviewer command failed after launch: {argv[0]}: {exc}"
        )
    except (OSError, ValueError) as exc:
        execution_blocker = (
            f"Reviewer command could not be started: {argv[0]}: {exc}"
            if launch_status == ProviderLaunchStatus.NEVER_STARTED
            else f"Reviewer command failed after launch: {argv[0]}: {exc}"
        )
    except BaseException as exc:
        if execution_exception is None:
            execution_exception, execution_traceback = exc, exc.__traceback__
            rethrow_execution_exception = True
        elif exc is not execution_exception:
            preserve_temporary = True
            execution_exception.add_note(f"Provider original capture failed: {type(exc).__name__}: {exc}")
    if (execution_exception is not None and not rethrow_execution_exception
            and launch_status == ProviderLaunchStatus.NEVER_STARTED):
        execution_exception = None
    if execution_exception is not None and launch_status == ProviderLaunchStatus.NEVER_STARTED:
        # 未启动的未知程序异常仍原样传播，不借保全回调伪造一次 provider 调用。
        if proof_root is not None:
            try:
                shutil.rmtree(proof_root)
            except BaseException as cleanup_error:
                execution_exception.add_note(f"Provider temporary cleanup failed at {proof_root}: {cleanup_error}")
        raise execution_exception.with_traceback(execution_traceback)
    if execution_exception is not None:
        execution_blocker = f"Reviewer command failed after launch: {type(execution_exception).__name__}: {execution_exception}"

    mutation_blocker = ""
    workspace_check = ProviderWorkspaceCheck(
        status="unproven", review_pack_digest=pack_digest,
        reason="Provider workspace check has not completed.",
    )

    def finalize_result() -> ProviderRunResult:
        # 执行与后处理失败共用真实原件、诊断及回调；全部后处理结束后才删除临时副本。
        nonlocal preserve_temporary
        if execution_exception is not None and workspace_check.status == "unproven":
            preserve_temporary = True
            execution_exception.add_note(f"Provider workspace could not be proven: {workspace_check.reason}")
        execution_failure = None
        if execution_exception is not None:
            try:
                present = _stable_regular_file_exists(root, findings_path)
                diagnostic_digest = hashlib.sha256(read_stable_bytes(root, findings_path)).hexdigest() if present else None
                execution_failure = ProviderExecutionFailure(
                    exception_type=type(execution_exception).__name__, message=str(execution_exception),
                    findings_status="present" if present else "absent", findings_sha256=diagnostic_digest,
                )
            except BaseException as diagnostic_error:
                preserve_temporary = True
                reason = f"{type(diagnostic_error).__name__}: {diagnostic_error}"
                execution_exception.add_note(f"Provider diagnostic capture failed: {reason}")
                execution_failure = ProviderExecutionFailure(
                    exception_type=type(execution_exception).__name__, message=str(execution_exception),
                    findings_status="unavailable", findings_error=reason,
                )
        blocker = mutation_blocker or execution_blocker
        invocation = _write_invocation(
            store=store, path=invocation_path, review_pack=review_pack,
            provider_id=options.provider_id, argv=argv,
            input_path=options.review_pack_path, output_path=findings_path, cwd=root,
            isolation_status=(ProviderIsolationStatus.NOT_PROVEN
                              if launch_status == ProviderLaunchStatus.NEVER_STARTED else options.isolation_status),
            launch_status=launch_status, completion_proof=completion_proof, exit_code=exit_code,
            status=LoopStatus.BLOCKED if blocker else _loop_status_for_exit_code(exit_code),
            workspace_check=workspace_check, execution_failure=execution_failure,
        )
        if proof_root is not None and (preserve_temporary or not proof_captured):
            blocker = f"{blocker} Provider originals retained at {proof_root}".strip()
        blocked_result = ProviderRunResult(
            status=ProviderRunStatus.BLOCKED, exit_code=exit_code,
            invocation_path=str(invocation_path), findings_path=str(findings_path), blocker=blocker,
            next_action=_next_action_for_mutation_blocker(mutation_blocker) if mutation_blocker
            else "Fix the local reviewer command and rerun review.", invocation=invocation,
        )
        if (execution_exception is not None and rethrow_execution_exception
                and options.on_execution_failure is not None):
            options.on_execution_failure(blocked_result)
        result = blocked_result
        if not blocker:
            if exit_code not in {EXIT_SUCCESS, EXIT_CHANGES_REQUIRED, EXIT_BLOCKED}:
                result = ProviderRunResult(
                    status=ProviderRunStatus.BLOCKED, exit_code=exit_code,
                    invocation_path=str(invocation_path), findings_path=str(findings_path),
                    blocker=f"Reviewer command failed with exit code {exit_code}.",
                    next_action="Fix the local reviewer command and rerun review.", invocation=invocation,
                )
            else:
                result = _validate_findings_output(
                    store=store, review_pack=review_pack, review_pack_path=options.review_pack_path,
                    findings_path=findings_path, schema_validation_path=schema_validation_path,
                    invocation_path=invocation_path, exit_code=exit_code, invocation=invocation,
                )
        if proof_root is not None and proof_captured and not preserve_temporary:
            shutil.rmtree(proof_root)
        elif proof_root is not None and execution_exception is not None:
            execution_exception.add_note(f"Provider originals retained at {proof_root}")
        return result

    try:
        try:
            # 输出尾部读取可在真实收尾原件保存后失败；正常与异常路径只信同一严格完成证明。
            if completion_proof is not None or not execution_blocker:
                try:
                    if completion_proof is None:
                        raise ValueError("provider-completion-originals-missing")
                    raw = completion_proof.require_complete()
                except (OSError, ValueError) as exc:
                    # 元数据已读取不等于输出已稳定；保留无法证明完成的实际临时原件。
                    preserve_temporary = True
                    proof_blocker = f"Reviewer completion could not be verified: {exc}"
                    execution_blocker = f"{execution_blocker} {proof_blocker}".strip()
                else:
                    launch_status = ProviderLaunchStatus(raw["launch_status"])
                    exit_code = cast(int | None, raw["exit_code"])
                    # 已发生的读取/执行错误仍阻断判定，真实退出码只保留事实，不把失败改成成功。
                    if not execution_blocker:
                        if raw["launch_error"] or raw["output_io_error"] or raw["output_truncated"]:
                            execution_blocker = (
                                f"Reviewer command could not be started: {argv[0]}: {raw['launch_error']}"
                                if launch_status == ProviderLaunchStatus.NEVER_STARTED
                                else "Reviewer command execution is incomplete: " + str(raw["launch_error"])
                            )
                        elif raw["timed_out"]:
                            execution_blocker = "Reviewer command timed out."
            try:
                mutation_blocker, workspace_check = _check_provider_workspace(
                    root, mutable_provider_outputs, before_snapshot, before_recovery, before_host, pack_digest,
                )
            except BaseException as workspace_error:
                if execution_exception is None:
                    raise
                preserve_temporary = True
                mutation_blocker = f"Provider workspace capture failed: {type(workspace_error).__name__}: {workspace_error}"
                execution_exception.add_note(mutation_blocker)
                workspace_check = ProviderWorkspaceCheck(
                    status="unproven", review_pack_digest=pack_digest, reason=mutation_blocker,
                )
            result = finalize_result()
        except BaseException as postprocessing_error:
            if execution_exception is not None or launch_status == ProviderLaunchStatus.NEVER_STARTED:
                raise
            # 首次工作区、持久化或 findings 后处理异常也属于本次已启动调用。
            # 只保存失败结果，不重做执行或 findings 判断；未知工作区仍保持 unproven。
            execution_exception, execution_traceback = postprocessing_error, postprocessing_error.__traceback__
            rethrow_execution_exception = True
            execution_blocker = (
                f"Reviewer postprocessing failed after launch: {type(postprocessing_error).__name__}: {postprocessing_error}"
            )
            result = finalize_result()
    except BaseException as preservation_error:
        if execution_exception is not None:
            if preservation_error is not execution_exception:
                execution_exception.add_note(
                    f"Provider failure persistence failed: {type(preservation_error).__name__}: {preservation_error}"
                )
            if proof_root is not None:
                execution_exception.add_note(f"Provider originals retained at {proof_root}")
            # 二次保全错误已进入备注；重抛保留原执行异常及它原有的显式原因。
            raise execution_exception.with_traceback(execution_traceback) from execution_exception.__cause__
        if proof_root is not None:
            preservation_error.add_note(f"Provider originals retained at {proof_root}")
        raise
    if execution_exception is not None and rethrow_execution_exception:
        raise execution_exception.with_traceback(execution_traceback)
    return result


def _require_provider_executable(command: str, root: Path) -> None:
    """只读确认明确的入口缺失/执行权限；不把检查通过当作启动证明。"""
    candidate = Path(command)
    if candidate.is_absolute() or os.path.dirname(command):
        candidate = candidate if candidate.is_absolute() else root / candidate
        if not candidate.exists():
            raise FileNotFoundError(command)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise PermissionError("permission denied")
    else:
        # 相对 PATH 项与显式 ./ 入口都按实际子进程 cwd 解释，预检不改原 argv。
        search_path = os.pathsep.join(
            str(Path(entry) if Path(entry).is_absolute() else root / entry)
            for entry in os.get_exec_path()
        )
        if shutil.which(command, path=search_path) is None:
            raise FileNotFoundError(command)


def run_mock_reviewer(
    *,
    root: Path,
    review_pack_path: Path,
    fixture: MockReviewerFixture = MockReviewerFixture.CLEAN,
) -> ProviderRunResult:
    """Run deterministic mock reviewer fixtures without network or model access."""

    resolved_root = root.resolve()
    review_pack = _load_review_pack(review_pack_path)
    store = LoopArtifactStore(resolved_root)
    review_dir = store.create_review_run_dir(review_pack.review_id)
    findings_path = review_dir / "findings.json"
    invocation_path = review_dir / "reviewer-invocation.json"
    schema_validation_path = review_dir / "schema-validation.json"

    if fixture == MockReviewerFixture.MALFORMED:
        findings_path.write_text("{not-json", encoding="utf-8")
        exit_code = EXIT_SUCCESS
    else:
        findings = _mock_findings(review_pack, fixture, review_pack_path)
        store.write_json_artifact(findings_path, findings)
        exit_code = _exit_code_for_fixture(fixture)

    invocation = ProviderRunnerInvocation(
        provider_id="mock-reviewer",
        provider_mode=ProviderMode.MOCK,
        model_selector="fixture",
        resolved_model="mock-reviewer",
        model_resolution_source=ModelResolutionSource.MOCK_FIXTURE,
        code_egress=False,
        command="mock-reviewer",
        argv=["mock-reviewer", "--fixture", fixture.value],
        cwd=str(resolved_root),
        input_path=str(review_pack_path),
        output_path=str(findings_path),
        allowlist=list(review_pack.reviewer_allowlist),
        isolation_status=ProviderIsolationStatus.ISOLATED_PROCESS,
        exit_code=exit_code,
        status=_loop_status_for_exit_code(exit_code),
    )
    store.write_json_artifact(invocation_path, invocation)

    return _validate_findings_output(
        store=store,
        review_pack=review_pack,
        review_pack_path=review_pack_path,
        findings_path=findings_path,
        schema_validation_path=schema_validation_path,
        invocation_path=invocation_path,
        exit_code=exit_code,
        invocation=invocation,
    )


def _expand_command(
    command: list[str],
    review_pack: ReviewPack,
    review_pack_path: Path,
    findings_path: Path,
) -> list[str]:
    diff_path = _review_pack_diff_path(review_pack)
    context = {
        "review_pack": str(review_pack_path),
        "findings": str(findings_path),
        "diff": diff_path,
        "diff_path": diff_path,
        "model": review_pack.resolved_model,
        "model_selector": review_pack.model_selector,
        "allowlist": ",".join(review_pack.reviewer_allowlist),
    }
    if any(f"{{{name}}}" in item for item in command for name in context):
        return [_replace_known_placeholders(item, context) for item in command]
    return [
        *command,
        "--review-pack",
        str(review_pack_path),
        "--output",
        str(findings_path),
        "--model",
        review_pack.model_selector,
        "--resolved-model",
        review_pack.resolved_model,
        "--allowlist",
        *review_pack.reviewer_allowlist,
    ]


def _review_pack_diff_path(review_pack: ReviewPack) -> str:
    diff_path = review_pack.diff_path.strip()
    if not diff_path:
        return ""
    path = Path(diff_path)
    if path.is_absolute():
        return str(path)
    repo_root = Path(review_pack.repo_root)
    return str(repo_root / path)


def _replace_known_placeholders(item: str, context: dict[str, str]) -> str:
    expanded = item
    for name, value in context.items():
        expanded = expanded.replace(f"{{{name}}}", value)
    return expanded


def _reviewer_allowlist_launch_blocker(review_pack: ReviewPack) -> str:
    changed_files = {path.strip() for path in review_pack.changed_files if path.strip()}
    allowlist = {
        path.strip() for path in review_pack.reviewer_allowlist if path.strip()
    }
    redacted_count = int(review_pack.diff_coverage.get("redacted_files", 0) or 0)
    omitted_count = int(review_pack.diff_coverage.get("omitted_files", 0) or 0)
    missing = sorted(changed_files - allowlist)
    waiver_allowed = (
        review_pack.policy_decisions.get("incomplete_review_waiver") is True
    )
    if (
        waiver_allowed
        and redacted_count == 0
        and omitted_count > 0
        and len(missing) <= omitted_count
    ):
        return ""
    if redacted_count > 0 or omitted_count > 0 or missing:
        detail = ", ".join(missing[:5])
        return (
            "Local reviewer allowlist is incomplete; refusing to launch local-agent "
            "because omitted or redacted files cannot be protected by advisory argv"
            + (f": {detail}" if detail else ".")
        )
    return ""


def _reviewed_head_launch_blocker(root: Path, review_pack: ReviewPack) -> str:
    current_head = _current_worktree_head(root)
    if not current_head or current_head == review_pack.head_commit:
        return ""
    return (
        "Local reviewer must run against review_pack.head_commit; current "
        f"worktree HEAD is {current_head[:12]}, reviewed head is "
        f"{review_pack.head_commit[:12]}."
    )


def _current_worktree_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _reviewed_diff_source_launch_blocker(root: Path, review_pack: ReviewPack) -> str:
    source_kind = DiffSourceKind(review_pack.diff_source.source_kind)
    if source_kind in {DiffSourceKind.LOCAL_STAGED, DiffSourceKind.LOCAL_UNSTAGED}:
        return _reviewed_worktree_diff_launch_blocker(root, source_kind, review_pack)
    if source_kind != DiffSourceKind.PATCH:
        return ""
    patch_file = review_pack.diff_source.patch_file.strip()
    expected_hash = review_pack.diff_source.patch_hash.strip()
    if not patch_file:
        return (
            "Reviewed patch diff source is missing patch_file; regenerate review pack."
        )
    if not expected_hash:
        return "Reviewed patch diff source hash is missing; regenerate review pack."
    patch_path = _resolve_patch_source_path(root, patch_file)
    if patch_path is None or not patch_path.is_file():
        return f"Reviewed patch file is not accessible: {patch_file}"
    try:
        actual_hash = hashlib.sha256(patch_path.read_bytes()).hexdigest()
    except OSError as exc:
        return f"Reviewed patch file is not readable: {patch_file}: {exc}"
    if actual_hash != expected_hash:
        return (
            "Current patch file hash does not match reviewed diff source hash: "
            f"{actual_hash} != {expected_hash}."
        )
    return ""


def _reviewed_worktree_diff_launch_blocker(
    root: Path,
    source_kind: DiffSourceKind,
    review_pack: ReviewPack,
) -> str:
    if source_kind == DiffSourceKind.LOCAL_STAGED:
        expected_tree = review_pack.staged_tree_oid.strip()
        if not expected_tree:
            return "Reviewed staged tree is missing; regenerate review pack."
        try:
            actual_tree = build_source_snapshot(
                SourceSnapshotOptions(root=root, source_kind="local-staged")
            ).staged_tree_oid
        except ValueError as exc:
            return f"Cannot resolve current staged tree: {exc}"
        if actual_tree != expected_tree:
            return (
                "Current staged tree does not match reviewed staged tree: "
                f"{actual_tree} != {expected_tree}."
            )
    expected_hash = review_pack.diff_source.patch_hash.strip()
    if not expected_hash:
        return "Reviewed worktree diff source hash is missing; regenerate review pack."
    diff_args = (
        ["git", "diff", "--cached"]
        if source_kind == DiffSourceKind.LOCAL_STAGED
        else ["git", "diff"]
    )
    try:
        result = subprocess.run(
            diff_args,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except FileNotFoundError:
        return "git is unavailable; cannot verify reviewed worktree diff source."
    except subprocess.TimeoutExpired:
        return "git diff timed out while verifying reviewed worktree diff source."
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit code {result.returncode}"
        return (
            f"git diff failed while verifying reviewed worktree diff source: {detail}"
        )
    actual_hash = hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()
    if actual_hash != expected_hash:
        return (
            "Current worktree diff hash does not match reviewed diff source hash: "
            f"{actual_hash} != {expected_hash}."
        )
    return ""


def _remove_previous_provider_outputs(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue


def _preexisting_dirty_worktree_blocker(
    root: Path,
    allowed_artifact_roots: frozenset[Path],
    allowed_dirty_paths: frozenset[str] = frozenset(),
    allowed_dirty_source_kind: DiffSourceKind | None = None,
) -> str:
    dirty_paths = _dirty_worktree_paths(
        root,
        allowed_artifact_roots,
        allowed_dirty_paths,
        allowed_dirty_source_kind,
    )
    if not dirty_paths:
        return ""
    sample = ", ".join(dirty_paths[:5])
    return "Local reviewer cannot run with pre-existing unreviewed worktree changes" + (
        f": {sample}" if sample else "."
    )


def _dirty_worktree_paths(
    root: Path,
    allowed_artifact_roots: frozenset[Path],
    allowed_dirty_paths: frozenset[str] = frozenset(),
    allowed_dirty_source_kind: DiffSourceKind | None = None,
) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    dirty: list[str] = []
    for status_code, rel_path in _iter_porcelain_entries(result.stdout):
        normalized = rel_path.replace("\\", "/")
        if (
            not normalized
            or _is_reviewed_dirty_status_for_launch(
                allowed_dirty_source_kind,
                status_xy=status_code,
                path=normalized,
                allowed_dirty_paths=allowed_dirty_paths,
            )
            or _is_allowed_review_artifact(
                root,
                allowed_artifact_roots,
                normalized,
            )
        ):
            continue
        dirty.append(normalized)
    return sorted(dirty)


def _reviewed_dirty_paths_for_launch(
    root: Path,
    review_pack: ReviewPack,
) -> frozenset[str]:
    source_kind = DiffSourceKind(review_pack.diff_source.source_kind)
    if source_kind == DiffSourceKind.PATCH:
        patch_path = _repo_relative_patch_source_path(
            root,
            review_pack.diff_source.patch_file,
        )
        return frozenset({patch_path}) if patch_path else frozenset()
    if source_kind not in {
        DiffSourceKind.LOCAL_STAGED,
        DiffSourceKind.LOCAL_UNSTAGED,
    }:
        return frozenset()
    return frozenset(
        path.strip().replace("\\", "/")
        for path in review_pack.changed_files
        if path.strip()
    )


def _is_reviewed_dirty_status_for_launch(
    source_kind: DiffSourceKind | None,
    *,
    status_xy: str,
    path: str,
    allowed_dirty_paths: frozenset[str],
) -> bool:
    if path not in allowed_dirty_paths or source_kind is None or "U" in status_xy:
        return False
    index_status = status_xy[0] if len(status_xy) > 0 else " "
    worktree_status = status_xy[1] if len(status_xy) > 1 else " "
    if source_kind == DiffSourceKind.LOCAL_STAGED:
        return index_status not in {" ", "?"} and worktree_status == " "
    if source_kind == DiffSourceKind.LOCAL_UNSTAGED:
        return index_status == " " and worktree_status not in {" ", "?"}
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


def _provider_command_entry_paths(root: Path, argv: list[str]) -> list[Path]:
    if not argv:
        return []
    candidates = [argv[0]]
    if len(argv) > 1 and _looks_like_python_executable(argv[0]):
        candidates.append(argv[1])

    allowed: list[Path] = []
    root = root.resolve()
    for candidate in candidates:
        path = Path(candidate)
        if not path.is_absolute():
            path = root / path
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.exists():
            allowed.append(resolved)
    return allowed


def _looks_like_python_executable(command: str) -> bool:
    name = Path(command).name.lower()
    return name == "python" or name.startswith("python") or name.startswith("python3")


def _is_allowed_review_artifact(
    root: Path,
    allowed_artifact_roots: frozenset[Path],
    rel_path: str,
) -> bool:
    try:
        path = (root / rel_path).resolve()
    except OSError:
        return False
    return any(
        path == allowed_root or allowed_root in path.parents
        for allowed_root in allowed_artifact_roots
    )


def _worktree_mutation_blocker(
    root: Path,
    mutable_provider_outputs: frozenset[Path],
    before: dict[str, str],
    *,
    after: dict[str, str] | None = None,
) -> str:
    try:
        if after is None:
            after = _worktree_snapshot(root, mutable_provider_outputs)
    except WorktreeSnapshotError as exc:
        return _snapshot_failure_blocker(exc)
    if after == before:
        return ""
    changed = sorted(set(before) ^ set(after))
    changed.extend(
        sorted(
            path for path in before.keys() & after.keys() if before[path] != after[path]
        )
    )
    sample = ", ".join(changed[:5])
    return (
        "Reviewer command modified files outside expected provider output artifacts"
        + (f": {sample}" if sample else ".")
    )


def _provider_host_artifact_paths(root: Path, review_id: str) -> frozenset[Path]:
    # 只在恢复比较中转移三个宿主后写文件；运行期间仍由完整快照保护。
    from ai_sdlc.core.pr_review_service import CURRENT_REVIEW_PATH

    directory = LoopArtifactStore(root).review_run_dir(review_id)
    return frozenset(
        {
            directory / "reviewer-invocation.json",
            directory / "review-run.json",
            root / CURRENT_REVIEW_PATH,
        }
    )


def _host_artifact_snapshot(
    root: Path, paths: frozenset[Path]
) -> dict[str, str | None]:
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(read_stable_bytes(root, path)).hexdigest()
            if _stable_regular_file_exists(root, path)
            else None
        )
        for path in paths
    }


def _check_provider_workspace(
    root: Path,
    outputs: frozenset[Path],
    before: dict[str, str],
    before_recovery: dict[str, str],
    before_host: dict[str, str | None],
    pack_digest: str,
) -> tuple[str, ProviderWorkspaceCheck]:
    try:
        host_paths = frozenset(root / name for name in before_host)
        after = _worktree_snapshot(root, outputs)
        after_recovery = _worktree_snapshot(root, outputs | host_paths)
        after_host = _host_artifact_snapshot(root, host_paths)
        original_values = {
            key: before_recovery.get(key)
            for key in before_recovery.keys() | after_recovery.keys()
            if before_recovery.get(key) != after_recovery.get(key)
        }
        host_mutations = sorted(
            name for name in before_host if before_host[name] != after_host[name]
        )
        full_changes = {
            key
            for key in before.keys() | after.keys()
            if before.get(key) != after.get(key)
        }

        def host_boundary(key: str, names: list[str]) -> bool:
            return any(
                key == name or name.startswith(key.rstrip("/") + "/") for name in names
            )

        # 两次观察中未被精确宿主排除影响的键必须一致，不能补造安全的旧基线。
        for full, recovery in ((before, before_recovery), (after, after_recovery)):
            if any(
                full.get(key) != recovery.get(key)
                and not host_boundary(key, list(before_host))
                for key in full.keys() | recovery.keys()
            ):
                raise WorktreeSnapshotError(
                    "workspace observations changed during capture"
                )
        if any(
            key not in original_values and not host_boundary(key, host_mutations)
            for key in full_changes
        ):
            raise WorktreeSnapshotError(
                "full workspace mutation has no proven recovery boundary"
            )
        check = ProviderWorkspaceCheck(
            status="mutated" if original_values or host_mutations else "unchanged",
            review_pack_digest=pack_digest,
            original_values=original_values,
            host_artifact_mutations=host_mutations,
        )
        blocker = _worktree_mutation_blocker(root, outputs, before, after=after)
        if check.status == "mutated" and not blocker:
            blocker = "Reviewer command modified files outside expected provider output artifacts."
        return blocker, check
    except (WorktreeSnapshotError, ValueError, OSError) as exc:
        return _snapshot_failure_blocker(exc), ProviderWorkspaceCheck(
            status="unproven", review_pack_digest=pack_digest, reason=str(exc)
        )


def _worktree_snapshot(
    root: Path, mutable_provider_outputs: frozenset[Path], *,
    exact_host_entries: Mapping[Path, str] | None = None,
    ignored_max_entries: int = 4096,
    require_complete: bool = False,
) -> dict[str, str]:
    try:
        result = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise WorktreeSnapshotError("git status is unavailable.") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeSnapshotError("git status timed out.") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        if not detail:
            detail = f"exit code {result.returncode}"
        raise WorktreeSnapshotError(f"git status failed: {detail}")

    snapshot = _git_head_index_snapshot(root)
    for status_code, rel_path in _iter_porcelain_entries(result.stdout):
        normalized = rel_path.replace("\\", "/")
        if not normalized or _is_mutable_provider_output(
            root, mutable_provider_outputs, normalized
        ):
            continue
        path = root / normalized
        if exact_host_entries and path in exact_host_entries and exact_host_entries[path] == "file":
            if path.is_symlink() or not path.is_file():
                raise WorktreeSnapshotError("expected host output is not an ordinary file")
            continue
        if status_code == "!!" and path.is_dir():
            snapshot[normalized] = (
                f"{status_code}:{_ignored_dir_digest(path, max_entries=ignored_max_entries, require_complete=require_complete, mutable_provider_outputs=mutable_provider_outputs, exact_host_entries=exact_host_entries)}"
            )
            continue
        snapshot[normalized] = f"{status_code}:{_path_digest(path)}"
    return snapshot


def _snapshot_failure_blocker(exc: Exception) -> str:
    return f"Unable to verify reviewer worktree isolation: {exc}"


def _next_action_for_mutation_blocker(blocker: str) -> str:
    if blocker.startswith("Unable to verify reviewer worktree isolation:"):
        return "Fix git status access, then rerun local PR review."
    return "Restore the worktree, then rerun with a read-only reviewer command."


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


def _git_head_index_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for name, args in {
        "HEAD": ["rev-parse", "HEAD"],
        "INDEX": ["write-tree"],
    }.items():
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise WorktreeSnapshotError(f"git {name} snapshot is unavailable") from exc
        if result.returncode == 0:
            snapshot[f"<git:{name}>"] = result.stdout.strip()
        else:
            raise WorktreeSnapshotError(
                f"git {name} snapshot failed: {result.stderr.strip()}"
            )
    return snapshot


def _is_mutable_provider_output(
    root: Path, mutable_provider_outputs: frozenset[Path], rel_path: str
) -> bool:
    try:
        path = root / rel_path
        return (
            path in mutable_provider_outputs
            and path.is_file()
            and not path.is_symlink()
        )
    except OSError:
        return False


def _path_digest(path: Path) -> str:
    if not path.exists():
        return "missing"
    if path.is_dir():
        digest = hashlib.sha256()
        try:
            children = sorted(path.rglob("*"))
        except OSError as exc:
            raise WorktreeSnapshotError(
                f"directory snapshot is unreadable: {path}"
            ) from exc
        for child in children:
            try:
                relative = child.relative_to(path).as_posix()
            except ValueError:
                continue
            if child.is_dir():
                digest.update(f"D:{relative}\0".encode())
                continue
            digest.update(f"F:{relative}\0".encode())
            try:
                digest.update(child.read_bytes())
            except OSError as exc:
                raise WorktreeSnapshotError(
                    f"file snapshot is unreadable: {child}"
                ) from exc
            digest.update(b"\0")
        return digest.hexdigest()
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise WorktreeSnapshotError(f"file snapshot is unreadable: {path}") from exc


def _ignored_dir_digest(
    path: Path,
    *,
    max_entries: int = 4096,
    mutable_provider_outputs: frozenset[Path] = frozenset(),
    exact_host_entries: Mapping[Path, str] | None = None,
    require_complete: bool = False,
) -> str:
    if not path.exists():
        return "missing"
    digest = hashlib.sha256()
    entries_seen = 0
    pending = [path]
    while pending and (require_complete or entries_seen < max_entries):
        current = pending.pop(0)
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as exc:
            raise WorktreeSnapshotError(
                f"ignored directory snapshot is unreadable: {current}"
            ) from exc
        for entry in entries:
            child = Path(entry.path)
            if require_complete:
                entries_seen += 1
                if entries_seen > max_entries:
                    raise WorktreeSnapshotError("complete workspace metadata scan exceeds entry limit")
            # 精确允许的普通输出不占受保护条目的限额；不追随软链接排除其他路径。
            if child in mutable_provider_outputs:
                try:
                    if entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    pass
            # 发布前的宿主条目仍计入扫描限额，目录继续遍历，未知子项不能被忽略。
            if exact_host_entries and child in exact_host_entries:
                if not require_complete:
                    entries_seen += 1
                    if entries_seen > max_entries:
                        raise WorktreeSnapshotError("accepted workspace exceeds ignored-entry scan limit")
                expected = exact_host_entries[child]
                if entry.is_symlink() or (expected == "directory") != entry.is_dir(follow_symlinks=False):
                    raise WorktreeSnapshotError("expected host entry changed type")
                if expected == "directory":
                    pending.append(child)
                elif not entry.is_file(follow_symlinks=False):
                    raise WorktreeSnapshotError("expected host output is not an ordinary file")
                continue
            if not require_complete:
                entries_seen += 1
            try:
                relative = child.relative_to(path).as_posix()
                stat = entry.stat(follow_symlinks=False)
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise WorktreeSnapshotError(
                    f"ignored entry snapshot is unreadable: {child}"
                ) from exc
            if is_dir:
                digest.update(f"D:{relative}\0".encode())
            else:
                digest.update(
                    f"F:{relative}:{stat.st_size}:{stat.st_mtime_ns}\0".encode()
                )
            if is_dir:
                pending.append(child)
            if entries_seen >= max_entries and not require_complete:
                digest.update(b"truncated")
                break
    if pending:
        digest.update(b"truncated")
    return digest.hexdigest()


def _load_review_pack(path: Path) -> ReviewPack:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ReviewPack.model_validate(payload)


def _write_invocation(
    *,
    store: LoopArtifactStore,
    path: Path,
    review_pack: ReviewPack,
    provider_id: str,
    argv: list[str],
    input_path: Path,
    output_path: Path,
    cwd: Path,
    isolation_status: ProviderIsolationStatus,
    launch_status: ProviderLaunchStatus,
    exit_code: int | None,
    status: LoopStatus,
    workspace_check: ProviderWorkspaceCheck | None = None,
    completion_proof: ProviderCompletionProof | None = None,
    preflight_incomplete: bool = False,
    execution_failure: ProviderExecutionFailure | None = None,
) -> ProviderRunnerInvocation:
    source = review_pack.model_resolution_source
    if source is None:
        source = ModelResolutionSource.PROJECT_POLICY
    recorded_at = utc_now_iso()
    invocation = ProviderRunnerInvocation(
        started_at=recorded_at,
        completed_at=recorded_at if preflight_incomplete else "",
        provider_id=provider_id,
        provider_mode=review_pack.provider_mode,
        model_selector=review_pack.model_selector,
        resolved_model=review_pack.resolved_model,
        model_resolution_source=source,
        code_egress=review_pack.code_egress,
        command=argv[0],
        argv=argv,
        cwd=str(cwd),
        input_path=str(input_path),
        output_path=str(output_path),
        allowlist=list(review_pack.reviewer_allowlist),
        isolation_status=isolation_status,
        launch_status=launch_status,
        completion_proof=completion_proof,
        preflight_incomplete=preflight_incomplete,
        execution_failure=execution_failure,
        exit_code=exit_code,
        status=status,
        workspace_check=workspace_check,
    )
    store.write_json_artifact(path, invocation)
    return invocation


def _validate_findings_output(
    *,
    store: LoopArtifactStore,
    review_pack: ReviewPack,
    review_pack_path: Path,
    findings_path: Path,
    schema_validation_path: Path,
    invocation_path: Path,
    exit_code: int | None,
    invocation: ProviderRunnerInvocation,
) -> ProviderRunResult:
    if not findings_path.exists():
        return ProviderRunResult(
            status=ProviderRunStatus.BLOCKED,
            exit_code=exit_code,
            invocation_path=str(invocation_path),
            findings_path=str(findings_path),
            blocker="Reviewer command did not write findings.json.",
            next_action="Fix the reviewer command output path and rerun review.",
            invocation=invocation,
        )

    schema_report = validate_artifact_file(findings_path, ReviewFindings)
    store.write_json_artifact(schema_validation_path, schema_report)
    if schema_report.status != SchemaValidationStatus.VALID:
        return ProviderRunResult(
            status=ProviderRunStatus.BLOCKED,
            exit_code=exit_code,
            invocation_path=str(invocation_path),
            findings_path=str(findings_path),
            schema_validation_path=str(schema_validation_path),
            blocker="Reviewer findings schema validation failed.",
            next_action="Regenerate findings.json with the expected schema.",
            invocation=invocation,
        )

    try:
        findings_payload = json.loads(findings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return ProviderRunResult(
            status=ProviderRunStatus.BLOCKED,
            exit_code=exit_code,
            invocation_path=str(invocation_path),
            findings_path=str(findings_path),
            schema_validation_path=str(schema_validation_path),
            blocker=f"Reviewer findings must be strict JSON: {exc}",
            next_action="Regenerate findings.json as JSON, not YAML or loose syntax.",
            invocation=invocation,
        )

    findings = ReviewFindings.model_validate(findings_payload)
    scope_blocker = _findings_scope_blocker(
        findings,
        review_pack=review_pack,
        review_pack_path=review_pack_path,
    )
    if scope_blocker:
        return ProviderRunResult(
            status=ProviderRunStatus.BLOCKED,
            exit_code=exit_code,
            invocation_path=str(invocation_path),
            findings_path=str(findings_path),
            schema_validation_path=str(schema_validation_path),
            blocker=scope_blocker,
            next_action="Regenerate findings.json for the current review pack.",
            invocation=invocation,
            # 原错误正文与摘要保留；被拒身份或范围不能成为已判断的修复依据。
        )
    verdict_blocker = _exit_code_verdict_blocker(exit_code, findings.verdict)
    if verdict_blocker:
        return ProviderRunResult(
            status=ProviderRunStatus.BLOCKED,
            exit_code=exit_code,
            invocation_path=str(invocation_path),
            findings_path=str(findings_path),
            schema_validation_path=str(schema_validation_path),
            blocker=verdict_blocker,
            next_action=(
                "Fix the reviewer command so its exit code matches findings.verdict."
            ),
            invocation=invocation,
            # 原输出仍保留在磁盘；协议不一致不能成为已判断的修复依据。
        )
    provider_status = _provider_status(exit_code, findings.verdict)
    return ProviderRunResult(
        status=provider_status,
        exit_code=exit_code,
        invocation_path=str(invocation_path),
        findings_path=str(findings_path),
        schema_validation_path=str(schema_validation_path),
        blocker=findings.blocker
        if provider_status == ProviderRunStatus.BLOCKED
        else "",
        next_action=(
            "Fix the blocked review provider and rerun review."
            if provider_status == ProviderRunStatus.BLOCKED
            else ""
        ),
        invocation=invocation,
        findings=findings,
    )


def _findings_scope_blocker(
    findings: ReviewFindings,
    *,
    review_pack: ReviewPack,
    review_pack_path: Path,
) -> str:
    if findings.review_id != review_pack.review_id:
        return (
            "Reviewer findings review_id does not match the current review pack: "
            f"{findings.review_id} != {review_pack.review_id}."
        )
    if findings.loop_id != review_pack.loop_id:
        return (
            "Reviewer findings loop_id does not match the current review pack: "
            f"{findings.loop_id} != {review_pack.loop_id}."
        )
    try:
        actual_pack_path = Path(findings.review_pack_path).resolve()
        expected_pack_path = review_pack_path.resolve()
    except OSError:
        actual_pack_path = Path(findings.review_pack_path)
        expected_pack_path = review_pack_path
    if actual_pack_path != expected_pack_path:
        return (
            "Reviewer findings review_pack_path does not match the current "
            f"review pack: {findings.review_pack_path} != {review_pack_path}."
        )
    allowed_files = {
        _normalize_review_path(path)
        for path in (review_pack.reviewer_allowlist or review_pack.changed_files)
    }
    for finding in findings.findings:
        finding_file = _normalize_review_path(finding.file)
        if finding_file not in allowed_files:
            return (
                "Reviewer findings include a file outside the review allowlist: "
                f"{finding.file}."
            )
    return ""


def _normalize_review_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def _exit_code_verdict_blocker(
    exit_code: int | None,
    verdict: ReviewVerdict,
) -> str:
    expected = {
        EXIT_SUCCESS: ReviewVerdict.CLEAN,
        EXIT_CHANGES_REQUIRED: ReviewVerdict.CHANGES_REQUIRED,
        EXIT_BLOCKED: ReviewVerdict.BLOCKED,
    }.get(exit_code)
    if expected is None:
        return f"Reviewer command exit code cannot authorize findings: {exit_code}."
    if verdict == expected:
        return ""
    return (
        "Reviewer command exit code does not match findings.verdict: "
        f"exit_code={exit_code}, verdict={verdict}, expected_verdict={expected}."
    )


def _provider_status(
    exit_code: int | None,
    verdict: ReviewVerdict,
) -> ProviderRunStatus:
    if exit_code == EXIT_BLOCKED or verdict == ReviewVerdict.BLOCKED:
        return ProviderRunStatus.BLOCKED
    if exit_code == EXIT_CHANGES_REQUIRED or verdict == ReviewVerdict.CHANGES_REQUIRED:
        return ProviderRunStatus.CHANGES_REQUIRED
    return ProviderRunStatus.SUCCESS


def _loop_status_for_exit_code(exit_code: int | None) -> LoopStatus:
    if exit_code == EXIT_SUCCESS:
        return LoopStatus.PASSED
    if exit_code == EXIT_CHANGES_REQUIRED:
        return LoopStatus.NEEDS_FIX
    return LoopStatus.BLOCKED


def _mock_findings(
    review_pack: ReviewPack,
    fixture: MockReviewerFixture,
    review_pack_path: Path,
) -> ReviewFindings:
    if fixture == MockReviewerFixture.CLEAN:
        return ReviewFindings(
            review_id=review_pack.review_id,
            loop_id=review_pack.loop_id,
            review_pack_path=str(review_pack_path),
            provider_id="mock-reviewer",
            model_selector="fixture",
            resolved_model="mock-reviewer",
            verdict=ReviewVerdict.CLEAN,
        )
    if fixture == MockReviewerFixture.BLOCKED:
        return ReviewFindings(
            review_id=review_pack.review_id,
            loop_id=review_pack.loop_id,
            review_pack_path=str(review_pack_path),
            provider_id="mock-reviewer",
            model_selector="fixture",
            resolved_model="mock-reviewer",
            verdict=ReviewVerdict.BLOCKED,
            blocker="Mock reviewer blocked by fixture.",
        )
    return ReviewFindings(
        review_id=review_pack.review_id,
        loop_id=review_pack.loop_id,
        review_pack_path=str(review_pack_path),
        provider_id="mock-reviewer",
        model_selector="fixture",
        resolved_model="mock-reviewer",
        verdict=ReviewVerdict.CHANGES_REQUIRED,
        findings=[
            ReviewFinding(
                id="MOCK-001",
                severity=FindingSeverity.REQUIRED,
                file=review_pack.changed_files[0]
                if review_pack.changed_files
                else "review-pack.json",
                claim="Mock reviewer fixture requires a change.",
                evidence="Fixture output requested changes_required.",
                risk="Deterministic fixture risk for integration tests.",
                suggested_fix="Adjust the fixture expectation or test input.",
                confidence=0.9,
            )
        ],
    )


def _exit_code_for_fixture(fixture: MockReviewerFixture) -> int:
    if fixture == MockReviewerFixture.CHANGES_REQUIRED:
        return EXIT_CHANGES_REQUIRED
    if fixture == MockReviewerFixture.BLOCKED:
        return EXIT_BLOCKED
    return EXIT_SUCCESS


__all__ = [
    "EXIT_BLOCKED",
    "EXIT_CHANGES_REQUIRED",
    "EXIT_SUCCESS",
    "MockReviewerFixture",
    "ProviderCommandOptions",
    "ProviderRunResult",
    "ProviderRunStatus",
    "run_mock_reviewer",
    "run_provider_command",
]
