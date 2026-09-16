"""仅为 Requirement R1 追加修复准备度依据；不改质量历史和原轮次。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from ai_sdlc.core.loop_decision_models import (
    B1RepairReadiness,
    DecisionContext,
    DecisionSource,
    DecisionValue,
    Digest,
    Identifier,
    Text,
)
from ai_sdlc.core.loop_decision_service import B1ReviewSnapshot
from ai_sdlc.core.loop_models import utc_now_iso
from ai_sdlc.core.loop_resource_lock import _stage_write_guard
from ai_sdlc.core.loop_review_models import LoopReviewOutcome
from ai_sdlc.core.loop_simulation_context import SimulationContext
from ai_sdlc.core.loop_stage_decision_service import (
    StageReviewData,
    StageReviewSnapshot,
    validate_stage_source_boundary,
)
from ai_sdlc.core.stable_file_read import _stable_regular_file_exists, read_stable_bytes

if TYPE_CHECKING:
    from ai_sdlc.core.loop_review_service import (
        B1SnapshotResolver,
        LoopReviewServiceError,
        RecordLoopReviewOptions,
        ReviewInputResolver,
    )

SUPPLEMENT_NAME = "repair-readiness-supplement.json"


class RepairReadinessExpertResult(DecisionValue):
    """只评修复前提，不允许作者借此替换原义务、发现或评分。"""

    role: Identifier
    prepare_digest: Digest
    repair_readiness: B1RepairReadiness
    evidence: tuple[DecisionSource, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _coverage(self) -> RepairReadinessExpertResult:
        ids = [item.id for item in self.evidence]
        if len(ids) != len(set(ids)) or set(ids) != set(
            self.repair_readiness.evidence_refs
        ):
            raise ValueError("repair-readiness-evidence-coverage")
        return self


class RepairReadinessPreparation(DecisionValue):
    schema_version: Literal["requirement-repair-readiness-v1"] = (
        "requirement-repair-readiness-v1"
    )
    prepare_digest: Digest
    loop_type: Literal["requirement"] = "requirement"
    loop_id: Identifier
    first_outcome_sha256: Digest
    first_input_digest: Digest
    context_digest: Digest
    context_sha256: Digest
    selected_route_id: Identifier
    expert_roles: tuple[Identifier, ...] = Field(min_length=1, max_length=2)
    evidence_manifest: dict[str, Digest] = Field(min_length=1, max_length=16)
    original_manifest: dict[str, Digest] = Field(min_length=1)

    @model_validator(mode="after")
    def _paths(self) -> RepairReadinessPreparation:
        for path in (*self.evidence_manifest, *self.original_manifest):
            DecisionSource._canonical_relative_path(path)
        if len(set(self.expert_roles)) != len(self.expert_roles):
            raise ValueError("repair-readiness-role-mismatch")
        if set(self.evidence_manifest) & set(self.original_manifest):
            raise ValueError("repair-readiness-new-evidence-required")
        return self


class RepairReadinessSupplement(RepairReadinessPreparation):
    assessments: dict[str, RepairReadinessExpertResult] = Field(
        min_length=1, max_length=2
    )
    recorded_at: Text


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _error(reason: str) -> LoopReviewServiceError:
    from ai_sdlc.core.loop_review_service import LoopReviewServiceError

    return LoopReviewServiceError(reason)


def _preparation_digest(prepared: RepairReadinessPreparation) -> str:
    content = prepared.model_dump(
        mode="json",
        include=set(RepairReadinessPreparation.model_fields) - {"prepare_digest"},
    )
    return _sha(
        json.dumps(
            content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )


def _eligible(
    outcome: LoopReviewOutcome, context: DecisionContext | SimulationContext
) -> StageReviewData:
    data = outcome.simulation
    if (
        not isinstance(context, SimulationContext)
        or outcome.loop_type != "requirement"
        or outcome.round_number != 1
        or outcome.status != "completed"
        or not isinstance(data, StageReviewData)
        or context.capability != "stage-simulation-v1"
        or context.loop_type != "requirement"
        or context.loop_id != outcome.loop_id
        or context.phase != "review_sealed"
        or data.context_digest != context.context_digest
        or data.selected_route_id != context.initial_selection_id
        or data.decision.action != "blocked"
        or data.decision.reason != "repair-unavailable"
    ):
        raise _error("repair-readiness-not-eligible")
    # 明确无权、失败或平台拒绝不属于材料未知，不得借补录重新解释。
    if any(
        value == "FAIL"
        for assessment in data.assessments.values()
        for value in (
            assessment.repair_readiness.authorization,
            assessment.repair_readiness.facts,
            assessment.repair_readiness.verification,
        )
    ):
        raise _error("repair-readiness-explicit-failure")
    return data


def repair_readiness_can_prepare(
    outcome: LoopReviewOutcome, snapshot: B1ReviewSnapshot | None
) -> bool:
    """只用于展示确实适用的补证入口，不替代录入时完整复验。"""
    if not isinstance(snapshot, StageReviewSnapshot):
        return False
    try:
        data = _eligible(outcome, snapshot.context)
    except ValueError:
        return False
    return (
        snapshot.review_input.input_digest == outcome.input_digest
        and snapshot.source_digest == data.source_digest
        and snapshot.manifest == data.manifest
    )


def _evidence_bytes(root: Path, path: Path) -> bytes:
    relative = path.relative_to(root).as_posix()
    DecisionSource._canonical_relative_path(relative)
    validate_stage_source_boundary(root, [path])
    if path.name in {
        SUPPLEMENT_NAME,
        "loop-run.json",
        "decision-context.json",
        "requirement-freeze.json",
        "review-run.json",
        "implementation-close.json",
        "design-contract-close.json",
        "frontend-evidence-close.json",
        "review-continuation.json",
        "verdict.json",
        "status.json",
    } or re.fullmatch(r"review-outcome-round-\d+\.json", path.name):
        raise _error("repair-readiness-derived-evidence-forbidden")
    content = read_stable_bytes(root, path)
    if not content.strip() or len(content) > 1024 * 1024:
        raise _error("repair-readiness-evidence-invalid")
    content.decode("utf-8")
    return content


def prepare_repair_readiness(
    root: Path,
    *,
    loop_type: str,
    loop_id: str,
    loop_dir: Path,
    evidence_paths: tuple[Path, ...],
    input_resolver: ReviewInputResolver,
    b1_snapshot_resolver: B1SnapshotResolver,
) -> RepairReadinessPreparation:
    """只读绑定补充证据；原成果未修复前才允许申请，成功原件只追加一次。"""
    from ai_sdlc.core.loop_review_service import _validate_identity, prepare_loop_review

    if loop_type != "requirement":
        raise _error("repair-readiness-unsupported-loop")
    _validate_identity(root, "requirement", loop_id, loop_dir)
    if _stable_regular_file_exists(root, loop_dir / SUPPLEMENT_NAME):
        raise _error("repair-readiness-already-recorded")
    if any(
        _stable_regular_file_exists(root, loop_dir / name)
        for name in ("review-outcome-round-2.json", "requirement-freeze.json")
    ):
        raise _error("repair-readiness-lifecycle-exhausted")
    first_path = loop_dir / "review-outcome-round-1.json"
    first_bytes = read_stable_bytes(root, first_path)
    outcome = LoopReviewOutcome.model_validate_json(first_bytes)
    prepared = prepare_loop_review(
        root,
        loop_type="requirement",
        loop_id=loop_id,
        loop_dir=loop_dir,
        input_resolver=input_resolver,
        b1_snapshot_resolver=b1_snapshot_resolver,
    )
    snapshot = prepared.b1_snapshot
    if not isinstance(snapshot, StageReviewSnapshot):
        raise _error("repair-readiness-not-eligible")
    data = _eligible(outcome, snapshot.context)
    if (
        prepared.reason != "repair-unavailable"
        or prepared.current_outcome != outcome
        or snapshot.review_input.input_digest != outcome.input_digest
        or snapshot.source_digest != data.source_digest
        or snapshot.manifest != data.manifest
        or snapshot.review_input.expert_roles != outcome.expert_roles
    ):
        raise _error("repair-readiness-original-drift")
    if not evidence_paths or len(evidence_paths) > 16:
        raise _error("repair-readiness-new-evidence-required")
    manifest = {}
    for supplied in evidence_paths:
        path = supplied if supplied.is_absolute() else root / supplied
        relative = path.relative_to(root).as_posix()
        if relative in data.manifest or relative in manifest:
            raise _error("repair-readiness-new-evidence-required")
        manifest[relative] = _sha(_evidence_bytes(root, path))
    context_bytes = read_stable_bytes(root, loop_dir / "decision-context.json")
    context_path = (loop_dir / "decision-context.json").relative_to(root).as_posix()
    if _sha(context_bytes) != data.manifest.get(context_path):
        raise _error("repair-readiness-original-drift")
    result = RepairReadinessPreparation(
        prepare_digest="0" * 64,
        loop_id=loop_id,
        first_outcome_sha256=_sha(first_bytes),
        first_input_digest=outcome.input_digest,
        context_digest=data.context_digest,
        context_sha256=_sha(context_bytes),
        selected_route_id=data.selected_route_id,
        expert_roles=tuple(outcome.expert_roles),
        evidence_manifest=manifest,
        original_manifest=dict(data.manifest),
    )
    # 首尾复读保障 prepare/read-path 不把不同时刻的材料拼成同一快照。
    if read_stable_bytes(root, first_path) != first_bytes:
        raise _error("repair-readiness-original-drift")
    for relative_path, digest in manifest.items():
        if _sha(_evidence_bytes(root, root / relative_path)) != digest:
            raise _error("repair-readiness-evidence-drift")
    _require_original_binding(root, loop_dir, result, b1_snapshot_resolver)
    return result.model_copy(update={"prepare_digest": _preparation_digest(result)})


def _require_original_binding(
    root: Path,
    loop_dir: Path,
    expected: RepairReadinessPreparation,
    resolver: B1SnapshotResolver,
) -> None:
    """最后复读原成果；补证和结果的读取不能使较早的业务快照继续有效。"""
    if any(
        _stable_regular_file_exists(root, loop_dir / name)
        for name in (
            SUPPLEMENT_NAME,
            "review-outcome-round-2.json",
            "requirement-freeze.json",
        )
    ):
        raise _error("repair-readiness-lifecycle-exhausted")
    snapshot = resolver(1)
    first_bytes = read_stable_bytes(root, loop_dir / "review-outcome-round-1.json")
    if not isinstance(snapshot, StageReviewSnapshot):
        raise _error("repair-readiness-original-drift")
    outcome = LoopReviewOutcome.model_validate_json(first_bytes)
    data = _eligible(outcome, snapshot.context)
    if (
        _sha(first_bytes) != expected.first_outcome_sha256
        or snapshot.review_input.loop_id != expected.loop_id
        or snapshot.review_input.loop_type != expected.loop_type
        or snapshot.review_input.round_number != 1
        or snapshot.review_input.input_digest != expected.first_input_digest
        or tuple(snapshot.review_input.expert_roles) != expected.expert_roles
        or snapshot.manifest != expected.original_manifest
        or snapshot.source_digest != data.source_digest
        or snapshot.context.context_digest != expected.context_digest
        or data.selected_route_id != expected.selected_route_id
        or _sha(read_stable_bytes(root, loop_dir / "decision-context.json"))
        != expected.context_sha256
    ):
        raise _error("repair-readiness-original-drift")


def _validate_assessments(
    prepared: RepairReadinessPreparation,
    assessments: dict[str, RepairReadinessExpertResult],
) -> None:
    if set(assessments) != set(prepared.expert_roles):
        raise _error("repair-readiness-role-mismatch")
    for role, assessment in assessments.items():
        if (
            role != assessment.role
            or assessment.prepare_digest != prepared.prepare_digest
        ):
            raise _error("repair-readiness-assessment-identity")
        readiness = assessment.repair_readiness
        if (readiness.authorization, readiness.facts, readiness.verification) != (
            "PASS",
            "PASS",
            "PASS",
        ):
            raise _error("repair-readiness-not-ready")
        for source in assessment.evidence:
            if prepared.evidence_manifest.get(source.path) != source.sha256:
                raise _error("repair-readiness-evidence-unbound")


def record_repair_readiness(
    options: RecordLoopReviewOptions,
    *,
    loop_dir: Path,
    evidence_paths: tuple[Path, ...],
    input_resolver: ReviewInputResolver,
    b1_snapshot_resolver: B1SnapshotResolver,
) -> RepairReadinessSupplement:
    """与原评审共用阶段锁并在原子发布前复验，拒绝覆盖已有补录。"""
    root = options.root
    if options.loop_type != "requirement":
        raise _error("repair-readiness-unsupported-loop")

    def prepare() -> RepairReadinessPreparation:
        return prepare_repair_readiness(
            root,
            loop_type=options.loop_type,
            loop_id=options.loop_id,
            loop_dir=loop_dir,
            evidence_paths=evidence_paths,
            input_resolver=input_resolver,
            b1_snapshot_resolver=b1_snapshot_resolver,
        )

    with _stage_write_guard(root, options.loop_type, options.loop_id):
        prepared = prepare()
        if options.expected_digest.strip().lower() != prepared.prepare_digest:
            raise _error("repair-readiness-input-drift")
        assessments = {}
        result_bytes = {}
        for supplied in options.result_paths:
            path = supplied if supplied.is_absolute() else root / supplied
            content = read_stable_bytes(root, path)
            assessment = RepairReadinessExpertResult.model_validate_json(content)
            if assessment.role in assessments:
                raise _error("repair-readiness-role-mismatch")
            assessments[assessment.role] = assessment
            result_bytes[path] = content
        _validate_assessments(prepared, assessments)
        supplement = RepairReadinessSupplement(
            **prepared.model_dump(), assessments=assessments, recorded_at=utc_now_iso()
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".readiness-", suffix=".tmp", dir=loop_dir
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write((supplement.model_dump_json(indent=2) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
            if prepare() != prepared:
                raise _error("repair-readiness-input-drift")
            if any(
                read_stable_bytes(root, path) != content
                for path, content in result_bytes.items()
            ):
                raise _error("repair-readiness-result-drift")
            _require_original_binding(root, loop_dir, prepared, b1_snapshot_resolver)
            # link 是排他发布：即使非协作写者同时创建目标，也不能覆盖历史。
            os.link(temporary, loop_dir / SUPPLEMENT_NAME)
        finally:
            temporary.unlink(missing_ok=True)
        return supplement


def read_verified_repair_readiness(
    root: Path,
    loop_dir: Path,
    outcome: LoopReviewOutcome | None,
    context: DecisionContext | SimulationContext,
) -> bool:
    """派生修复准入，不生成 Close 凭据；业务修复可变，新依据及 R1 不可变。"""
    return read_verified_repair_supplement(root, loop_dir, outcome, context) is not None


def read_verified_repair_supplement(
    root: Path,
    loop_dir: Path,
    outcome: LoopReviewOutcome | None,
    context: DecisionContext | SimulationContext,
    *,
    captured_artifacts: Mapping[str, bytes] | None = None,
) -> RepairReadinessSupplement | None:
    """返回同一份已验证补录；快照消费者还须绑定捕获字节，不能另读 JSON 取依据。"""
    path = loop_dir / SUPPLEMENT_NAME
    if not _stable_regular_file_exists(root, path):
        if captured_artifacts is not None and path.relative_to(root).as_posix() in captured_artifacts:
            raise _error("repair-readiness-input-drift")
        return None
    if outcome is None:
        raise _error("repair-readiness-original-missing")

    def captured(content_path: Path, content: bytes) -> bytes:
        if (
            captured_artifacts is not None
            and captured_artifacts.get(content_path.relative_to(root).as_posix()) != content
        ):
            raise _error("repair-readiness-input-drift")
        return content

    supplement = RepairReadinessSupplement.model_validate_json(
        captured(path, read_stable_bytes(root, path))
    )
    data = _eligible(outcome, context)
    if (
        supplement.prepare_digest != _preparation_digest(supplement)
        or supplement.loop_id != outcome.loop_id
        or supplement.first_outcome_sha256
        != _sha(captured(
            loop_dir / "review-outcome-round-1.json",
            read_stable_bytes(root, loop_dir / "review-outcome-round-1.json"),
        ))
        or supplement.first_input_digest != outcome.input_digest
        or supplement.context_digest != data.context_digest
        or supplement.context_sha256
        != _sha(captured(
            loop_dir / "decision-context.json",
            read_stable_bytes(root, loop_dir / "decision-context.json"),
        ))
        or supplement.selected_route_id != data.selected_route_id
        or supplement.expert_roles != tuple(outcome.expert_roles)
        or supplement.original_manifest != data.manifest
    ):
        raise _error("repair-readiness-original-drift")
    _validate_assessments(supplement, supplement.assessments)
    for relative, digest in supplement.evidence_manifest.items():
        if _sha(captured(root / relative, _evidence_bytes(root, root / relative))) != digest:
            raise _error("repair-readiness-evidence-drift")
    return supplement
