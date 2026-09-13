"""Local PR 当前暂存树量化接缝；不选实现路线、不执行代码。"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_decision_service import (
    DecisionPreparationError,
    _hash,
    _optional_bytes,
    _source_boundary,
    _write_context,
    validate_implementation_source_boundary,
)
from ai_sdlc.core.loop_resource_lock import _implementation_write_guard
from ai_sdlc.core.loop_simulation_context import (
    SimulationContext,
    SimulationPreparation,
    SimulationPrepareRequest,
    has_receipt,
    request_digest,
    transition_simulation,
    validate_simulation_context,
)
from ai_sdlc.core.pr_review_models import ReviewRun
from ai_sdlc.core.stable_file_read import read_stable_bytes

CAPABILITY = "stage-simulation-v1"


def pr_review_input_digest(run: ReviewRun) -> str:
    """初次树是合同身份；必要修复的当前树仍由原 provider/verify 重新证明。"""
    return "sha256:" + _hash(
        {
            "review_id": run.review_id,
            "loop_id": run.loop_id,
            "capability": run.decision_capability,
            "initial_staged_tree_oid": run.decision_staged_tree_oid,
        }
    )


def pr_review_context_path(root: Path, run: ReviewRun) -> Path:
    return (
        LoopArtifactStore(root).review_run_dir(run.review_id) / "decision-context.json"
    )


def validate_captured_pr_review_context(
    run: ReviewRun, context: SimulationContext
) -> SimulationContext:
    run = ReviewRun.model_validate(run.model_dump())
    if run.decision_begin_pending_digest is not None:
        raise DecisionPreparationError("pr-decision-begin-pending")
    context = validate_simulation_context(context)
    if (
        run.decision_mode != "adaptive-quantified"
        or run.decision_capability != CAPABILITY
        or context.capability != CAPABILITY
        or context.loop_id != run.loop_id
        or context.implementation_input_digest != pr_review_input_digest(run)
        or len(context.contracts) != 1
        or context.plan.loop_type != "local-pr-review"
        or context.plan.profile_id != "delivery-readiness-v1"
        or run.decision_started_at_ms is None
        or context.started_at_ms != run.decision_started_at_ms
    ):
        raise DecisionPreparationError("pr-decision-context-identity-mismatch")
    return context


def validate_pr_review_context(
    root: Path, run: ReviewRun, *, purpose: str = "read"
) -> SimulationContext | None:
    path = pr_review_context_path(root, run)
    content = _optional_bytes(root, path)
    if run.decision_begin_pending_digest is not None:
        raise DecisionPreparationError("pr-decision-begin-pending")
    if run.decision_mode == "legacy":
        if content is not None:
            raise DecisionPreparationError("pr-decision-context-conflicts-with-legacy")
        return None
    if content is None:
        if run.decision_started_at_ms is not None:
            raise DecisionPreparationError("pr-decision-context-missing-after-begin")
        raise DecisionPreparationError(
            "pr-decision-context-missing: run pr-review decision-prepare"
        )
    if run.decision_started_at_ms is None:
        raise DecisionPreparationError("pr-decision-start-marker-missing")
    context = validate_captured_pr_review_context(
        run, SimulationContext.model_validate_json(content)
    )
    if purpose == "review" and context.phase != "review_sealed":
        raise DecisionPreparationError("simulation-review-not-sealed")
    _validate_sources(root, run, context.sources, check_digest=False)
    return context


def pr_review_source_digest(root: Path, run: ReviewRun, sources=()) -> str:
    boundary = _source_boundary(root)
    # 正常提交只改变 HEAD，不改变已审暂存树；仍保留 index 与当前文件字节防漂移。
    return _hash(
        {
            "identity": pr_review_input_digest(run),
            "staged_tree_oid": run.staged_tree_oid,
            "source": {"index": boundary["index"], "files": boundary["files"]},
            "sources": {
                source.path: hashlib.sha256(
                    read_stable_bytes(root, root / source.path)
                ).hexdigest()
                for source in sources
            },
        }
    )


def prepare_pr_review_decision(
    root: Path,
    review_id: str,
    request: SimulationPrepareRequest,
    *,
    dry_run: bool = True,
    expected_digest: str = "",
) -> SimulationPreparation:
    """复用有限纯转换，仅原子写当前 review 的决策上下文。"""

    root = root.resolve(strict=True)
    request = SimulationPrepareRequest.model_validate(request)
    if type(dry_run) is not bool:
        raise DecisionPreparationError("decision-dry-run-invalid")
    # 同一 review 与原 stage 写锁共用键；模型调用始终在锁外。
    with _implementation_write_guard(root, f"local-pr-review:{review_id}"):
        preview = _prepare(root, review_id, request)
        if dry_run or preview.status == "existing":
            return preview
        if not expected_digest or preview.prepare_digest != expected_digest:
            raise DecisionPreparationError("decision-prepare-digest-mismatch")
        from ai_sdlc.core.pr_review_service import _load_current_review_run

        run, run_path = _load_current_review_run(root)
        if run.decision_started_at_ms is None:
            # 双文件中断只重放原 begin；摘要绑定请求、来源与首次时间，不新增预算。
            run = run.model_copy(
                update={
                    "decision_started_at_ms": preview.context.started_at_ms,
                    "decision_begin_pending_digest": preview.context.context_digest,
                }
            )
            LoopArtifactStore(root).write_json_artifact(run_path, run)
        _write_context(
            LoopArtifactStore(root).review_run_dir(review_id) / "decision-context.json",
            preview.context,
        )
        if run.decision_begin_pending_digest is not None:
            LoopArtifactStore(root).write_json_artifact(
                run_path,
                run.model_copy(update={"decision_begin_pending_digest": None}),
            )
        return preview.model_copy(update={"status": "prepared"})


def _validate_sources(root, run, sources, *, check_digest):
    validate_implementation_source_boundary(
        root, [root / source.path for source in sources]
    )
    for source in sources:
        path = root / source.path
        if path.is_relative_to(root / ".ai-sdlc/reviews/pr") and path.name not in {
            "review-pack.json",
            "findings.json",
            "verification-evidence.json",
            "resolution.yaml",
        }:
            raise DecisionPreparationError("pr-decision-derived-state-forbidden")
        content = read_stable_bytes(root, path)
        if check_digest and hashlib.sha256(content).hexdigest() != source.sha256:
            raise DecisionPreparationError("decision-source-digest-mismatch")


def _prepare(root, review_id, request):
    from ai_sdlc.core.loop_review_service import outcome_path
    from ai_sdlc.core.pr_review_service import (
        CURRENT_REVIEW_PATH,
        _load_current_review_run,
        _load_review_pack,
        _load_verification_evidence,
        _precommit_staged_source_blocker,
        _unsafe_explicit_review_id_blocker,
        _verification_evidence_blocker,
    )

    if not review_id or _unsafe_explicit_review_id_blocker(review_id):
        raise DecisionPreparationError("pr-decision-review-id-invalid")
    run, run_path = _load_current_review_run(root)
    if run.review_id != review_id or run.decision_capability != CAPABILITY:
        raise DecisionPreparationError("pr-decision-saved-identity-mismatch")
    directory = run_path.parent
    path = pr_review_context_path(root, run)
    content = _optional_bytes(root, path)
    pending_digest = run.decision_begin_pending_digest
    if pending_digest is not None and request.operation != "begin":
        raise DecisionPreparationError("pr-decision-begin-pending")
    if (
        content is None
        and run.decision_started_at_ms is not None
        and pending_digest is None
    ):
        raise DecisionPreparationError("pr-decision-context-missing-after-begin")
    if content is not None and run.decision_started_at_ms is None:
        raise DecisionPreparationError("pr-decision-start-marker-missing")
    context = (
        validate_captured_pr_review_context(
            run.model_copy(update={"decision_begin_pending_digest": None}),
            SimulationContext.model_validate_json(content),
        )
        if content is not None
        else None
    )
    if (
        pending_digest is not None
        and context is not None
        and context.context_digest != pending_digest
    ):
        raise DecisionPreparationError("pr-decision-pending-begin-digest-mismatch")
    sources = {source.id: source for source in context.sources} if context else {}
    sources.update({source.id: source for source in request.sources})
    _validate_sources(root, run, tuple(sources.values()), check_digest=False)
    tracked = [
        root / CURRENT_REVIEW_PATH,
        run_path,
        path,
        directory / "review-pack.json",
        directory / "findings.json",
        directory / "verification-evidence.json",
        directory / "resolution.yaml",
        directory / "final-report.md",
        *(outcome_path(directory, n) for n in (1, 2)),
    ]

    def snapshot():
        return {
            "artifacts": {
                item.name: hashlib.sha256(raw).hexdigest()
                if (raw := _optional_bytes(root, item)) is not None
                else None
                for item in tracked
            },
            "source": pr_review_source_digest(root, run, tuple(sources.values())),
            "manifest": {
                source.path: hashlib.sha256(
                    read_stable_bytes(root, root / source.path)
                ).hexdigest()
                for source in sources.values()
            },
        }

    before = snapshot()
    digest = _hash({"snapshot": before, "request": request_digest(request)})
    if pending_digest is None and context is not None and has_receipt(context, request):
        return SimulationPreparation(
            status="existing", prepare_digest=digest, context=context
        )
    if (
        any(
            _optional_bytes(root, outcome_path(directory, n)) is not None
            for n in (1, 2)
        )
        or run.status == "closed"
    ):
        raise DecisionPreparationError("simulation-review-already-recorded")
    if request.operation == "begin-improvement" or request.continue_search:
        raise DecisionPreparationError("pr-decision-current-tree-only")
    if request.candidates and (
        len(request.candidates) != 1
        or request.candidates[0].candidate_id != "current-staged-tree"
    ):
        raise DecisionPreparationError("pr-decision-current-tree-only")
    pack = _load_review_pack(root, run.review_pack_path)
    blocker = _precommit_staged_source_blocker(root, run, pack)
    if blocker:
        raise DecisionPreparationError(f"pr-decision-current-tree-mismatch: {blocker}")
    _validate_sources(root, run, tuple(sources.values()), check_digest=True)
    if request.operation == "seal-for-review":
        blocker = _verification_evidence_blocker(
            run, _load_verification_evidence(root, run)
        )
        if blocker:
            raise DecisionPreparationError(
                f"pr-decision-actual-verification-required: {blocker}"
            )
    now_ms = time.time_ns() // 1_000_000
    if pending_digest is not None:
        if run.decision_started_at_ms is None or now_ms < run.decision_started_at_ms:
            raise DecisionPreparationError("simulation-clock-moved-backwards")
        now_ms = run.decision_started_at_ms
    result = transition_simulation(
        None if pending_digest is not None else context,
        request,
        loop_id=run.loop_id,
        input_digest=pr_review_input_digest(run),
        source_digest=before["source"],
        source_manifest=before["manifest"],
        now_ms=now_ms,
    )
    if pending_digest is not None and result.context_digest != pending_digest:
        raise DecisionPreparationError("pr-decision-pending-begin-digest-mismatch")
    # 首次预演尚未保存开始标记，但保存后的捕获校验必须要求二者严格一致。
    validation_run = (
        run.model_copy(update={"decision_begin_pending_digest": None})
        if context is not None
        else run.model_copy(
            update={
                "decision_started_at_ms": result.started_at_ms,
                "decision_begin_pending_digest": None,
            }
        )
    )
    validate_captured_pr_review_context(validation_run, result)
    if before != snapshot():
        raise DecisionPreparationError("decision-prepare-snapshot-changed")
    return SimulationPreparation(
        status="preview", prepare_digest=digest, context=result
    )
