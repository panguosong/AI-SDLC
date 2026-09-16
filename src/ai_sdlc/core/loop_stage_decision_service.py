"""各阶段复用的决策文件适配与实际评价；不调度模型或执行路线。"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from pydantic import Field

from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_decision import decide_implementation, evaluate
from ai_sdlc.core.loop_decision_models import (
    B1Assessment,
    B1ReviewData,
    DecisionValue,
    Digest,
    ImplementationDecisionInput,
)
from ai_sdlc.core.loop_decision_service import (
    B1ReviewSnapshot,
    DecisionPreparationError,
    _bytes_digest,
    _check_manifest,
    _checked_assessments,
    _hash,
    _optional_bytes,
    _require_stable_snapshot,
    _source_boundary,
    _write_context,
    merge_actual_assessments,
    require_simulation_time_admission,
    validate_implementation_source_boundary,
)
from ai_sdlc.core.loop_models import LoopRun
from ai_sdlc.core.loop_resource_lock import (
    _ImplementationWriteLockError,
    _stage_write_guard,
)
from ai_sdlc.core.loop_simulation_context import (
    SimulationContext,
    SimulationPreparation,
    SimulationPrepareRequest,
    has_receipt,
    request_digest,
    transition_simulation,
    validate_simulation_context,
)
from ai_sdlc.core.review_kernel import ReviewInput
from ai_sdlc.core.stable_file_read import read_stable_bytes

STAGE_CAPABILITY = "stage-simulation-v1"
STAGE_KINDS = {"requirement", "design-contract", "implementation", "frontend-evidence"}


@dataclass(frozen=True)
class StageDecisionHost:
    """宿主从当前原件验证身份、上游及任务状态；不是外部 JSON 准入声明。"""

    stage_kind: str
    loop_id: str
    input_digest: str
    loop_dir: Path
    artifact_paths: tuple[Path, ...]
    actual_paths: tuple[Path, ...]
    initial_ready: bool
    actual_ready: bool
    review_started: bool
    execution_started: bool = False


@dataclass(frozen=True)
class StageReviewSnapshot(B1ReviewSnapshot):
    source_digest: str = ""
    observed_at_ms: int = field(default=0, compare=False)
    context_path: str = ""
    closed_review: ClosedStageReviewReplay | None = None


class StageActualDecision(DecisionValue):
    action: Literal["stop", "repair", "blocked", "improve"]
    reason: str = Field(min_length=1, max_length=256)


class StageReviewData(B1ReviewData):
    """时间观测保存在既有 outcome 内，不另建改善或评分历史库。"""

    decision: StageActualDecision
    source_digest: Digest
    observed_at_ms: int = Field(strict=True, ge=0)


@dataclass(frozen=True)
class ClosedStageReviewReplay:
    """宿主验证原生关闭后提供的内存回放绑定；不写入历史 outcome。"""

    round_number: int
    data: StageReviewData
    receipt_digest: str


def _closed_review_data(context, replay):
    if (
        not isinstance(replay, ClosedStageReviewReplay)
        or context.loop_type not in {"requirement", "design-contract"}
        or not isinstance(replay.data, StageReviewData)
        or replay.data.context_digest != context.context_digest
        or replay.data.selected_route_id != context.initial_selection_id
        or not re.fullmatch(r"[0-9a-f]{64}", replay.receipt_digest)
    ):
        raise DecisionPreparationError("simulation-closed-review-binding-mismatch")
    return replay.data


def stage_decision_write_guard(root: Path, stage_kind: str, loop_id: str):
    """Implementation 保留原锁键，其他阶段的宿主写入共用限定阶段键。"""
    return _stage_write_guard(root, stage_kind, loop_id)


def validate_stage_source_boundary(root: Path, paths: Sequence[Path]) -> None:
    """保留原始材料和血缘，但各阶段及 PR 自己生成的裁决不能自证。"""
    validate_implementation_source_boundary(root, paths)
    pr_actual_source_names = {
        "review-pack.json",
        "current.diff",
        "diff.patch",
        "verification-evidence.json",
        "findings.json",
        "resolution.yaml",
    }
    review_state_names = {
        "loop-run.json",
        "review-run.json",
        "decision-context.json",
        "requirement-freeze.json",
        "design-contract-close.json",
        "implementation-close.json",
        "frontend-evidence-close.json",
        "review-continuation.json",
        "repair-readiness-supplement.json",
        "current-review.json",
        "final-report.md",
        "reviewer-invocation.json",
        "schema-validation.json",
        "resolution-history.yaml",
        "verdict.json",
        "status.json",
    }
    for path in paths:
        relative = path.relative_to(root).parts
        # PR 保留目录只准入原合同已有实际输入；未知元数据默认拒绝，不追补名称黑名单。
        if tuple(part.casefold() for part in relative[:3]) == (
            ".ai-sdlc", "reviews", "pr"
        ) and (len(relative) != 5 or path.name not in pr_actual_source_names):
            raise DecisionPreparationError("decision-source-derived-state-forbidden")
        if (
            relative[:2] == (".ai-sdlc", "reviews")
            and (
                path.name in review_state_names
                or re.fullmatch(r"review-outcome-round-\d+\.json", path.name)
            )
        ) or (
            relative[:2] == (".ai-sdlc", "loops")
            and path.name in {
                "decision-context.json", "review-run.json", "repair-readiness-supplement.json",
            }
        ):
            raise DecisionPreparationError("decision-source-derived-state-forbidden")


def _checked_host(root, stage_kind, loop_id, resolver):
    host = resolver()
    expected = root / ".ai-sdlc" / "loops" / stage_kind / loop_id
    if (
        not isinstance(host, StageDecisionHost)
        or stage_kind not in STAGE_KINDS
        or host.stage_kind != stage_kind
        or host.loop_id != loop_id
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", loop_id)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", host.input_digest)
        or host.loop_dir != expected
        or any(
            type(value) is not bool
            for value in (
                host.initial_ready,
                host.actual_ready,
                host.review_started,
                host.execution_started,
            )
        )
    ):
        raise DecisionPreparationError("simulation-stage-host-identity-mismatch")
    for path in (*host.artifact_paths, *host.actual_paths):
        if not path.is_absolute() or not path.is_relative_to(root):
            raise DecisionPreparationError("simulation-stage-host-path-invalid")
    if host.actual_ready and not host.actual_paths:
        raise DecisionPreparationError("simulation-actual-evidence-missing")
    return host


def parse_stage_simulation_context(content: bytes) -> SimulationContext:
    context = validate_simulation_context(
        SimulationContext.model_validate_json(content)
    )
    if context.capability != STAGE_CAPABILITY:
        raise DecisionPreparationError("simulation-capability-mismatch")
    return context


def _check_context_identity(host, context):
    if (
        context.capability != STAGE_CAPABILITY
        or context.loop_id != host.loop_id
        or context.loop_type != host.stage_kind
        or context.implementation_input_digest != host.input_digest
    ):
        raise DecisionPreparationError("simulation-stage-context-identity-mismatch")


def validate_stage_start_binding(run: LoopRun, context: SimulationContext) -> None:
    """原 LoopRun 保存首次开始标记，丢失 context 不能重新起算原阶段窗口。"""
    run = LoopRun.model_validate(run.model_dump())
    if run.decision_begin_pending_digest is not None:
        raise DecisionPreparationError("simulation-begin-incomplete")
    if (
        run.decision_capability != STAGE_CAPABILITY
        or run.loop_type != context.loop_type
        or run.loop_id != context.loop_id
        or run.decision_started_at_ms is None
        or run.decision_started_at_ms != context.started_at_ms
    ):
        raise DecisionPreparationError("simulation-start-marker-mismatch")


def _stage_run(root, host):
    run = LoopRun.model_validate_json(
        read_stable_bytes(root, host.loop_dir / "loop-run.json")
    )
    if (
        run.loop_id != host.loop_id
        or run.loop_type != host.stage_kind
        or run.decision_capability != STAGE_CAPABILITY
    ):
        raise DecisionPreparationError("simulation-stage-host-identity-mismatch")
    return run


def _actual_manifest(root: Path, host: StageDecisionHost) -> dict[str, str]:
    # 原生上游关闭/输入也是当前快照绑定材料，但不能据此作为模拟项目事实来源。
    # 来源准入继续单独调用 validate_stage_source_boundary，不删快照材料避漂移。
    manifest = {}
    for path in host.actual_paths:
        if path == host.loop_dir / "decision-context.json":
            raise DecisionPreparationError("simulation-actual-context-self-reference")
        manifest[path.relative_to(root).as_posix()] = _bytes_digest(
            read_stable_bytes(root, path)
        )
    return manifest


def stage_material_digest(root: Path, host: StageDecisionHost) -> str:
    """同一源边界加当前真实工件；协议 context 更新不充当成果漂移。"""
    source = _source_boundary(root)
    content = _optional_bytes(root, host.loop_dir / "decision-context.json")
    context = parse_stage_simulation_context(content) if content is not None else None
    if context is not None:
        validate_stage_source_boundary(root, [root / s.path for s in context.sources])
    references = (
        {
            item.path: _bytes_digest(read_stable_bytes(root, root / item.path))
            for item in context.sources
        }
        if context is not None
        else {}
    )
    actual = _actual_manifest(root, host)
    current = _material_digest(source, actual, references)
    if host.stage_kind != "implementation" or _stage_run(root, host).status != "closed":
        return current
    # 已验源仍一致时保持原行为；既有 PR 不能使后续正常阶段反向采用旧 parent。
    for number in (2, 1):
        previous = _optional_bytes(
            root, host.loop_dir / f"review-outcome-round-{number}.json"
        )
        if previous is not None:
            from ai_sdlc.core.loop_review_models import LoopReviewOutcome

            outcome = LoopReviewOutcome.model_validate_json(previous)
            expected = getattr(outcome.simulation, "source_digest", None)
            if expected == current:
                return current
            break
    else:
        return current
    from ai_sdlc.core.implementation_loop import (
        _closed_implementation_delivery_proof,
        _require_unchanged_delivery_proof,
    )

    proof = _closed_implementation_delivery_proof(root, host.loop_id)
    if proof is None:
        return current
    # 重新计算当前 index/文件及原阶段材料，仅接受原完整交付证明绑定的 HEAD 转换。
    delivered = _material_digest(
        {**source, "head": proof.reviewed_head}, actual, references
    )
    if (
        _source_boundary(root) != source
        or _actual_manifest(root, host) != actual
        or _optional_bytes(root, host.loop_dir / "decision-context.json") != content
        or {
            path: _bytes_digest(read_stable_bytes(root, root / path))
            for path in references
        }
        != references
    ):
        raise ValueError("delivery-stage-material-drift")
    _require_unchanged_delivery_proof(root, host.loop_id, proof)
    return delivered if delivered == expected else current


def _material_digest(source, actual, references):
    # Git 已捕获的文件不重复计入；被运行态前缀排除的原始来源仍须完整绑定。
    external = {
        path: value
        for path, value in references.items()
        if path not in source["files"] and path not in actual
    }
    return _hash({"source": source, "actual": actual, "external_sources": external})


def _baseline_matches(context, source_digest):
    proposal = getattr(context, "improvement", None)
    if proposal is not None and proposal.baseline_digest != source_digest:
        raise DecisionPreparationError("simulation-improvement-baseline-drift")


def read_stage_simulation_context(
    root: Path,
    host: StageDecisionHost,
    *,
    purpose: str = "read",
    round_number: int = 1,
    closed_review: ClosedStageReviewReplay | None = None,
) -> SimulationContext:
    root = root.resolve(strict=True)
    host = _checked_host(root, host.stage_kind, host.loop_id, lambda: host)
    context = parse_stage_simulation_context(
        read_stable_bytes(root, host.loop_dir / "decision-context.json")
    )
    _check_context_identity(host, context)
    validate_stage_start_binding(_stage_run(root, host), context)
    validate_stage_source_boundary(root, [root / s.path for s in context.sources])
    revision = _completed_r1_allows_revision(root, host, context)
    # 仅已完成且绑定原 context 的 R1 修复可重读改后来源，轮次参数不能自授例外。
    for source in context.sources:
        content = read_stable_bytes(root, root / source.path)
        if not revision and _bytes_digest(content) != source.sha256:
            raise DecisionPreparationError("decision-source-digest-mismatch")
    if purpose in {"review", "close"}:
        if context.phase != "review_sealed":
            raise DecisionPreparationError("simulation-review-not-sealed")
        if round_number == 1 and not revision:
            # 已关闭文档的全仓摘要只回放原比较依据；宿主仍验证其当前完整材料。
            _baseline_matches(
                context,
                _closed_review_data(context, closed_review).source_digest
                if closed_review is not None
                else stage_material_digest(root, host),
            )
    elif purpose == "execute" and context.phase != "review_sealed":
        require_simulation_time_admission(
            context, execution_started=host.execution_started
        )
    return context


def _completed_r1_allows_revision(root, host, context):
    from ai_sdlc.core.loop_repair_readiness import read_verified_repair_readiness
    from ai_sdlc.core.loop_review_models import LoopReviewOutcome

    content = _optional_bytes(root, host.loop_dir / "review-outcome-round-1.json")
    if content is None:
        if host.stage_kind == "requirement":
            read_verified_repair_readiness(root, host.loop_dir, None, context)
        return False
    outcome = LoopReviewOutcome.model_validate_json(content)
    repair_ready = (
        read_verified_repair_readiness(root, host.loop_dir, outcome, context)
        if host.stage_kind == "requirement"
        else False
    )
    return (
        outcome.loop_id == host.loop_id
        and outcome.loop_type == host.stage_kind
        and outcome.round_number == 1
        and outcome.status == "completed"
        and outcome.simulation is not None
        and outcome.simulation.context_digest == context.context_digest
        and outcome.simulation.selected_route_id == context.initial_selection_id
        and (outcome.simulation.decision.action in {"repair", "improve"} or repair_ready)
    )


def _snapshot(root, host, sources):
    paths = (
        *host.artifact_paths,
        host.loop_dir / "loop-run.json",
        host.loop_dir / "decision-context.json",
        *(host.loop_dir / f"review-outcome-round-{n}.json" for n in (1, 2)),
    )
    source = _source_boundary(root)
    actual = _actual_manifest(root, host)
    return {
        "identity": (host.stage_kind, host.loop_id, host.input_digest),
        "state": (
            host.initial_ready,
            host.actual_ready,
            host.review_started,
            host.execution_started,
        ),
        "artifacts": {
            path.relative_to(root).as_posix(): _bytes_digest(
                _optional_bytes(root, path)
            )
            for path in paths
        },
        "source": source,
        "actual": actual,
        "referenced_sources": {
            item.path: _bytes_digest(read_stable_bytes(root, root / item.path))
            for item in sources
        },
    }


def prepare_stage_simulation_decision(
    root: Path,
    stage_kind: str,
    loop_id: str,
    request: SimulationPrepareRequest,
    *,
    host_resolver: Callable[[], StageDecisionHost],
    write_guard: Callable[[], AbstractContextManager] | None = None,
    dry_run: bool = True,
    expected_digest: str = "",
) -> SimulationPreparation:
    root = root.resolve(strict=True)
    request = SimulationPrepareRequest.model_validate(request)
    if type(dry_run) is not bool:
        raise DecisionPreparationError("decision-dry-run-invalid")
    preview = _prepare_stage(root, stage_kind, loop_id, request, host_resolver)
    if dry_run or preview.status == "existing":
        return preview
    if not expected_digest or expected_digest != preview.prepare_digest:
        raise DecisionPreparationError("decision-prepare-digest-mismatch")
    guard = write_guard or (
        lambda: stage_decision_write_guard(root, stage_kind, loop_id)
    )
    with ExitStack() as locks:
        try:
            locks.enter_context(guard())
        except _ImplementationWriteLockError as exc:
            raise DecisionPreparationError(str(exc)) from exc
        current = _prepare_stage(root, stage_kind, loop_id, request, host_resolver)
        if current.status == "existing":
            return current
        if current.prepare_digest != expected_digest:
            raise DecisionPreparationError("decision-prepare-digest-mismatch")
        if request.operation == "begin":
            host = _checked_host(root, stage_kind, loop_id, host_resolver)
            run = _stage_run(root, host)
            if run.decision_begin_pending_digest is None:
                # 同一原件先保存开始时间及待完成摘要；恢复不能换合同或重开窗口。
                run = run.model_copy(
                    update={
                        "decision_started_at_ms": current.context.started_at_ms,
                        "decision_begin_pending_digest": current.context.context_digest,
                    }
                )
                LoopArtifactStore(root).write_json_artifact(
                    host.loop_dir / "loop-run.json", run
                )
        _write_context(
            root
            / ".ai-sdlc"
            / "loops"
            / stage_kind
            / loop_id
            / "decision-context.json",
            current.context,
        )
        if request.operation == "begin":
            LoopArtifactStore(root).write_json_artifact(
                host.loop_dir / "loop-run.json",
                run.model_copy(update={"decision_begin_pending_digest": None}),
            )
        return current.model_copy(update={"status": "prepared"})


def _prepare_stage(root, stage_kind, loop_id, request, resolver):
    host = _checked_host(root, stage_kind, loop_id, resolver)
    content = _optional_bytes(root, host.loop_dir / "decision-context.json")
    context = parse_stage_simulation_context(content) if content is not None else None
    run = _stage_run(root, host)
    recovering = run.decision_begin_pending_digest is not None
    if recovering:
        if request.operation != "begin":
            raise DecisionPreparationError("simulation-begin-recovery-required")
        if (
            context is not None
            and context.context_digest != run.decision_begin_pending_digest
        ):
            raise DecisionPreparationError("simulation-begin-recovery-mismatch")
        # 只重建尚未完成的首次开始；成功后删除 context 没有 pending 凭证可用。
        context = None
    if context is not None:
        _check_context_identity(host, context)
        validate_stage_start_binding(run, context)
    elif run.decision_started_at_ms is not None and not recovering:
        raise DecisionPreparationError("simulation-context-lost-after-begin")
    sources = {s.id: s for s in context.sources} if context is not None else {}
    for source in request.sources:
        if source.id in sources and sources[source.id] != source:
            raise DecisionPreparationError("simulation-source-id-conflict")
        sources[source.id] = source
    validate_stage_source_boundary(root, [root / s.path for s in sources.values()])
    before = _snapshot(root, host, tuple(sources.values()))
    digest = _hash({"state": before, "request": request_digest(request)})
    if context is not None and not _completed_r1_allows_revision(root, host, context):
        for source in context.sources:
            if before["referenced_sources"][source.path] != source.sha256:
                raise DecisionPreparationError("decision-source-digest-mismatch")
    if context is not None and has_receipt(context, request):
        if request.operation == "seal-for-review" and not _completed_r1_allows_revision(
            root, host, context
        ):
            _baseline_matches(
                context,
                _material_digest(
                    before["source"], before["actual"], before["referenced_sources"]
                ),
            )
        _require_stable_snapshot(
            before,
            _snapshot(
                root,
                _checked_host(root, stage_kind, loop_id, resolver),
                tuple(sources.values()),
            ),
        )
        return SimulationPreparation(
            status="existing", prepare_digest=digest, context=context
        )
    if host.review_started:
        raise DecisionPreparationError("simulation-review-already-started")
    if (
        request.operation == "begin"
        or (context is not None and context.phase == "initial_search")
    ) and not host.initial_ready:
        raise DecisionPreparationError("decision-prepare-already-started")
    if request.operation in {"begin-improvement", "seal-for-review"} and (
        not host.actual_ready or not before["actual"]
    ):
        raise DecisionPreparationError("simulation-actual-tasks-not-ready")
    for source in request.sources:
        if before["referenced_sources"][source.path] != source.sha256:
            raise DecisionPreparationError("decision-source-digest-mismatch")
    if (
        context is not None
        and context.pending_batch is not None
        and context.pending_batch.candidates
        and context.pending_batch.source_manifest != before["referenced_sources"]
    ):
        raise DecisionPreparationError("simulation-source-manifest-drift")
    material_digest = _material_digest(
        before["source"],
        before["actual"],
        # 初始批次额外来源由声明 SHA 及封存 manifest 核对，新增引用不改变源码。
        {}
        if context is None
        or context.phase == "initial_search"
        or request.operation
        in {"correct-input", "revise-time-plan", "authorize-comparison"}
        else before["referenced_sources"],
    )
    if context is not None and context.phase == "improvement_search":
        _baseline_matches(context, material_digest)
    if request.operation == "seal-for-review" and context is not None:
        _baseline_matches(context, material_digest)
    now_ms = time.time_ns() // 1_000_000
    if recovering and now_ms < run.decision_started_at_ms:
        raise DecisionPreparationError("simulation-clock-moved-backwards")
    result = transition_simulation(
        context,
        request,
        loop_id=loop_id,
        input_digest=host.input_digest,
        source_digest=material_digest,
        source_manifest=before["referenced_sources"],
        now_ms=run.decision_started_at_ms if recovering else now_ms,
        actual_ready=host.actual_ready,
        review_started=host.review_started,
        execution_started=host.execution_started,
    )
    _check_context_identity(host, result)
    validate_simulation_context(result)
    if recovering and result.context_digest != run.decision_begin_pending_digest:
        raise DecisionPreparationError("simulation-begin-recovery-mismatch")
    _require_stable_snapshot(
        before,
        _snapshot(
            root,
            _checked_host(root, stage_kind, loop_id, resolver),
            tuple(sources.values()),
        ),
    )
    return SimulationPreparation(
        status="preview", prepare_digest=digest, context=result
    )


def _checked_stage_snapshot(snapshot):
    review = ReviewInput.model_validate(snapshot.review_input.model_dump())
    context = validate_simulation_context(snapshot.context)
    validate_stage_source_boundary(Path("."), [Path(s.path) for s in context.sources])
    paths = set(review.artifact_paths) | set(review.upstream_context_paths)
    context_paths = [
        path for path in paths if Path(path).name == "decision-context.json"
    ]
    expected_path = (
        f".ai-sdlc/loops/{review.loop_type}/{review.loop_id}/decision-context.json"
    )
    path = getattr(snapshot, "context_path", "") or (
        expected_path
        if review.loop_type != "local-pr-review"
        else context_paths[0]
        if len(context_paths) == 1
        else ""
    )
    if (
        context.capability != STAGE_CAPABILITY
        or context.phase != "review_sealed"
        or review.loop_id != context.loop_id
        or review.loop_type != context.loop_type
        or review.round_number not in (1, 2)
        or not path
        or (review.loop_type != "local-pr-review" and path != expected_path)
        or path not in review.artifact_paths
        or set(snapshot.manifest) != paths
        or not {source.path for source in context.sources} <= paths
    ):
        raise DecisionPreparationError("decision-review-snapshot-mismatch")
    _check_manifest(snapshot.manifest)
    if review.round_number == 1 and any(
        snapshot.manifest[source.path] != source.sha256 for source in context.sources
    ):
        raise DecisionPreparationError("decision-source-digest-mismatch")
    observed = getattr(snapshot, "observed_at_ms", 0)
    source_digest = getattr(snapshot, "source_digest", "")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", source_digest)
        or type(observed) is not int
        or observed < 0
    ):
        raise DecisionPreparationError("simulation-current-source-observation-required")
    checked = StageReviewSnapshot(
        review,
        context,
        dict(snapshot.manifest),
        source_digest,
        observed,
        path,
        getattr(snapshot, "closed_review", None),
    )
    stage_review_source_digest(checked)
    return checked


def stage_review_source_digest(snapshot: StageReviewSnapshot) -> str:
    """回放仅替换原判据的来源输入；snapshot.source_digest 始终是当前观测。"""
    replay = snapshot.closed_review
    if replay is None:
        return snapshot.source_digest
    data = _closed_review_data(snapshot.context, replay)
    if (
        snapshot.review_input.loop_type not in {"requirement", "design-contract"}
        or snapshot.review_input.round_number != replay.round_number
        or data.input_digest != snapshot.review_input.input_digest
        or data.manifest != snapshot.manifest
    ):
        raise DecisionPreparationError("simulation-closed-review-binding-mismatch")
    return data.source_digest


def _stage_assessment_input(snapshot, assessments):
    for assessment in assessments.values():
        paths = [Path(source.path) for source in assessment.evidence]
        # 血缘可绑定整个上游，但框架自己写出的完成/评分声明不能证明当前实际义务。
        validate_stage_source_boundary(Path("."), paths)
    return merge_actual_assessments(
        goal_contract=snapshot.context.goal_contract,
        context_digest=snapshot.context.context_digest,
        selected_route_id=snapshot.context.initial_selection_id,
        input_digest=snapshot.review_input.input_digest,
        manifest=snapshot.manifest,
        assessments=assessments,
        context_path=snapshot.context_path,
    )


def _stage_baseline(snapshot, baseline):
    if snapshot.review_input.round_number == 1:
        if baseline is not None:
            raise DecisionPreparationError("decision-baseline-not-allowed")
        return None
    if baseline is None:
        raise DecisionPreparationError("decision-baseline-missing")
    baseline = StageReviewData.model_validate(baseline.model_dump())
    if (
        baseline.context_digest != snapshot.context.context_digest
        or baseline.selected_route_id != snapshot.context.initial_selection_id
        or baseline.input_digest == snapshot.review_input.input_digest
    ):
        raise DecisionPreparationError("decision-baseline-identity-mismatch")
    _check_manifest(baseline.manifest)
    historical = replace(
        snapshot,
        review_input=snapshot.review_input.model_copy(
            update={"input_digest": baseline.input_digest, "round_number": 1}
        ),
        manifest=baseline.manifest,
        source_digest=baseline.source_digest,
        observed_at_ms=baseline.observed_at_ms,
    )
    previous, _ = _stage_assessment_input(
        historical, _checked_assessments(baseline.assessments)
    )
    if baseline.evaluation != evaluate(previous):
        raise DecisionPreparationError("decision-baseline-evaluation-mismatch")
    return previous


def build_stage_review_data(
    snapshot: StageReviewSnapshot,
    assessments_by_role: Mapping[str, B1Assessment],
    *,
    has_actionable_findings: bool,
    baseline: StageReviewData | None = None,
) -> StageReviewData:
    snapshot = _checked_stage_snapshot(snapshot)
    if type(has_actionable_findings) is not bool:
        raise DecisionPreparationError("decision-actionable-findings-invalid")
    if set(assessments_by_role) != set(snapshot.review_input.expert_roles):
        raise DecisionPreparationError("decision-assessment-role-mismatch")
    assessments = _checked_assessments(assessments_by_role)
    current, readiness = _stage_assessment_input(snapshot, assessments)
    evaluation = evaluate(current, baseline=_stage_baseline(snapshot, baseline))
    original = decide_implementation(
        ImplementationDecisionInput(
            current=current,
            round_number=snapshot.review_input.round_number,
            has_actionable_findings=has_actionable_findings,
            repair_readiness=readiness,
        )
    )
    decision = StageActualDecision.model_validate(original.model_dump())
    if snapshot.review_input.round_number == 1:
        _baseline_matches(snapshot.context, snapshot.source_digest)
        if (
            original.action == "stop"
            and snapshot.context.conditional_improvement is not None
        ):
            from ai_sdlc.core.loop_simulation_context import (
                conditional_improvement_admission,
            )

            reason = conditional_improvement_admission(
                snapshot.context,
                source_digest=snapshot.source_digest,
                now_ms=snapshot.observed_at_ms,
            )
            decision = StageActualDecision(
                action="improve" if reason is None else "stop",
                reason="supported-conditional-improvement"
                if reason is None
                else reason,
            )
        elif original.action == "stop" and snapshot.context.improvement is not None:
            decision = StageActualDecision(
                action="stop",
                reason=snapshot.context.improvement_stop_reason
                or "no_supported_improvement",
            )
    return StageReviewData(
        input_digest=snapshot.review_input.input_digest,
        context_digest=snapshot.context.context_digest,
        selected_route_id=snapshot.context.initial_selection_id,
        manifest=dict(snapshot.manifest),
        assessments=assessments,
        evaluation=evaluation,
        decision=decision,
        source_digest=snapshot.source_digest,
        observed_at_ms=snapshot.observed_at_ms,
    )


def validate_stage_review_data(
    snapshot: StageReviewSnapshot,
    saved: StageReviewData,
    *,
    has_actionable_findings: bool,
    baseline: StageReviewData | None = None,
) -> StageReviewData:
    saved = StageReviewData.model_validate(saved.model_dump())
    snapshot = _checked_stage_snapshot(snapshot)
    if snapshot.closed_review is not None and snapshot.closed_review.data != saved:
        raise DecisionPreparationError("simulation-closed-review-binding-mismatch")
    source_digest = stage_review_source_digest(snapshot)
    if source_digest != saved.source_digest:
        raise DecisionPreparationError("simulation-actual-source-drift")
    rebuilt = build_stage_review_data(
        replace(
            snapshot,
            source_digest=source_digest,
            observed_at_ms=saved.observed_at_ms,
            closed_review=None,
        ),
        saved.assessments,
        has_actionable_findings=has_actionable_findings,
        baseline=baseline,
    )
    if saved != rebuilt:
        raise DecisionPreparationError("decision-review-data-mismatch")
    return rebuilt
