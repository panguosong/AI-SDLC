"""Deterministic local runtime for the Loop Engine requirement loop."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_models import (
    LoopArtifactModel,
    LoopRound,
    LoopRun,
    LoopStatus,
    LoopType,
    utc_now_iso,
    validate_decision_identity,
)
from ai_sdlc.core.loop_resource_lock import _stage_write_guard
from ai_sdlc.core.loop_stage_input import (
    preserve_stage_run,
    validate_stage_material_update,
)
from ai_sdlc.core.review_kernel import (
    ReviewInputValidator,
    revalidate_review_input_at_transition,
)
from ai_sdlc.core.stable_file_read import read_stable_text
from ai_sdlc.utils.helpers import AI_SDLC_DIR

CURRENT_REQUIREMENT_PATH = (
    Path(AI_SDLC_DIR)
    / "loops"
    / LoopType.REQUIREMENT.value
    / "current-requirement.json"
)
_SAFE_EXPLICIT_LOOP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_DESIGN_SCOPE_FAMILIES = frozenset({"frontend-evidence", "implementation", "pr-review"})


class RequirementSourceKind(StrEnum):
    """Supported requirement input source kinds."""

    IDEA = "idea"
    INPUT_FILE = "input-file"


class RequirementCommandStatus(StrEnum):
    """Requirement loop command outcomes."""

    READY = "ready"
    NEEDS_USER = "needs_user"
    BLOCKED = "blocked"
    DRY_RUN = "dry_run"


class RequirementIntake(LoopArtifactModel):
    """Persisted requirement intake artifact."""

    artifact_kind: str = "requirement-intake"
    loop_id: str
    work_item_id: str = ""
    source_kind: RequirementSourceKind = RequirementSourceKind.IDEA
    source_path: str = ""
    raw_text: str
    summary: str
    clarification_questions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    design_scope_families: list[str] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
    )
    decision_mode: Literal["legacy", "adaptive-quantified"] = Field(
        default="legacy", exclude_if=lambda value: value == "legacy"
    )
    decision_capability: Literal["stage-simulation-v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _decision_identity(self):
        validate_decision_identity(
            LoopType.REQUIREMENT, self.decision_mode, self.decision_capability
        )
        return self

    @field_validator("loop_id", "raw_text", "summary")
    @classmethod
    def _require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value

    @field_validator("design_scope_families")
    @classmethod
    def _require_known_scope_families(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("design scope families must be unique")
        invalid = sorted(set(value) - _DESIGN_SCOPE_FAMILIES)
        if invalid:
            raise ValueError(f"unknown design scope families: {', '.join(invalid)}")
        return value


class RequirementFreeze(LoopArtifactModel):
    """Persisted requirement freeze confirmation artifact."""

    artifact_kind: str = "requirement-freeze"
    loop_id: str
    accepted_by: str = "local-user"
    accepted_at: str = Field(default_factory=utc_now_iso)
    intake_path: str
    intake_digest: str = Field(default="", exclude_if=lambda value: not value)
    acceptance_count: int = Field(ge=1)
    next_loop_type: LoopType = LoopType.DESIGN_CONTRACT

    @field_validator("loop_id", "intake_path")
    @classmethod
    def _require_freeze_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must not be empty")
        return value


class RequirementArtifactRef(BaseModel):
    """Artifact path surfaced by requirement loop commands."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    path: str
    exists: bool = False


class RequirementCommandSummary(BaseModel):
    """Requirement details surfaced directly by command results."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    summary: str = ""
    source_kind: RequirementSourceKind | str = ""
    source_path: str = ""
    clarification_count: int = 0
    acceptance_count: int = 0
    frozen: bool = False


class RequirementLoopCommandResult(BaseModel):
    """Machine-readable result for requirement loop commands."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: RequirementCommandStatus
    result: str = ""
    loop_id: str = ""
    loop_status: LoopStatus | str = ""
    summary: str = ""
    source_kind: RequirementSourceKind | str = ""
    source_path: str = ""
    clarification_count: int = 0
    acceptance_count: int = 0
    frozen: bool = False
    dry_run: bool = False
    blocker: str = ""
    next_action: str = ""
    artifacts: list[RequirementArtifactRef] = Field(default_factory=list)
    requirement: RequirementCommandSummary | None = None


@dataclass(frozen=True, slots=True)
class RequirementStartOptions:
    """Inputs for starting a requirement loop."""

    root: Path
    idea: str = ""
    input_file: str = ""
    acceptance: tuple[str, ...] = ()
    design_scope_families: tuple[str, ...] = ()
    work_item_id: str = ""
    loop_id: str = ""
    dry_run: bool = False
    decision_mode: str = "legacy"
    decision_capability: str | None = None


@dataclass(frozen=True, slots=True)
class RequirementFreezeOptions:
    """Inputs for freezing a requirement loop."""

    root: Path
    loop_id: str = ""
    yes: bool = False
    accepted_by: str = "local-user"
    expected_review_digest: str = ""


@dataclass(frozen=True, slots=True)
class _RequirementArtifacts:
    loop_dir: Path
    loop_run_path: Path
    intake_path: Path
    brief_path: Path
    questions_path: Path
    checklist_path: Path
    freeze_path: Path
    pointer_path: Path

    def refs(
        self, root: Path, *, include_freeze: bool = False
    ) -> list[RequirementArtifactRef]:
        paths = (
            ("loop-run", self.loop_run_path),
            ("requirement-intake", self.intake_path),
            ("requirement-brief", self.brief_path),
            ("clarification-questions", self.questions_path),
            ("acceptance-checklist", self.checklist_path),
            ("current-requirement-pointer", self.pointer_path),
        )
        refs = [_artifact_ref(root, kind, path) for kind, path in paths]
        if include_freeze:
            refs.append(_artifact_ref(root, "requirement-freeze", self.freeze_path))
        return refs


def start_requirement_loop(
    options: RequirementStartOptions,
) -> RequirementLoopCommandResult:
    try:
        loop_id = _resolve_loop_id(options.loop_id)
    except ValueError:
        return _start_requirement_loop_locked(options)
    with _stage_write_guard(options.root.resolve(), "requirement", loop_id):
        return _start_requirement_loop_locked(replace(options, loop_id=loop_id))


def _start_requirement_loop_locked(
    options: RequirementStartOptions,
) -> RequirementLoopCommandResult:
    """Create a local requirement loop from idea text or a local input file."""

    root = options.root.resolve()
    prepared = _prepare_requirement_start(options, root)
    if isinstance(prepared, RequirementLoopCommandResult):
        return prepared
    loop_id, artifacts, existing_intake, source = prepared
    source_text, source_kind, source_path = source
    try:
        intake, loop_status, next_action = _build_requirement_intake(
            options,
            loop_id,
            existing_intake,
            source_text,
            source_kind,
            source_path,
        )
        revision = validate_stage_material_update(
            root, "requirement", artifacts.loop_dir, existing_intake, intake
        )
        if (
            existing_intake is not None
            and intake.decision_capability == "stage-simulation-v1"
        ):
            intake = intake.model_copy(
                update={"created_at": existing_intake.created_at}
            )
    except (ValueError, ValidationError) as exc:
        return RequirementLoopCommandResult(
            status=RequirementCommandStatus.BLOCKED,
            result="Requirement input is invalid.",
            loop_id=loop_id,
            blocker=str(exc),
            next_action="Correct the requirement input and rerun requirement start.",
        )
    if options.dry_run:
        return _requirement_start_result(
            intake,
            loop_status,
            next_action,
            artifacts.refs(root),
            dry_run=True,
        )
    _write_requirement_start(
        root, artifacts, intake, loop_status, next_action, revision=revision
    )
    return _requirement_start_result(
        intake,
        loop_status,
        next_action,
        artifacts.refs(root),
    )


def _prepare_requirement_start(
    options: RequirementStartOptions,
    root: Path,
) -> (
    tuple[
        str,
        _RequirementArtifacts,
        RequirementIntake | None,
        tuple[str, RequirementSourceKind, str],
    ]
    | RequirementLoopCommandResult
):
    resolved = _resolve_requirement_start_artifacts(options, root)
    if isinstance(resolved, RequirementLoopCommandResult):
        return resolved
    loop_id, artifacts = resolved
    existing_intake, existing_blocker = _existing_intake_for_start(options, artifacts)
    if existing_blocker:
        return _blocked_requirement_start(
            loop_id,
            existing_blocker,
            artifacts=artifacts.refs(root),
        )
    source_text, source_kind, source_path, blocker = _read_requirement_source(
        options,
        root,
        existing_intake,
    )
    if blocker:
        return _blocked_requirement_start(
            loop_id,
            blocker,
            artifacts=artifacts.refs(root),
        )
    return (
        loop_id,
        artifacts,
        existing_intake,
        (source_text, source_kind, source_path),
    )


def _resolve_requirement_start_artifacts(
    options: RequirementStartOptions,
    root: Path,
) -> tuple[str, _RequirementArtifacts] | RequirementLoopCommandResult:
    try:
        loop_id = _resolve_loop_id(options.loop_id)
    except ValueError as exc:
        return _blocked_requirement_start(
            options.loop_id.strip(),
            f"Invalid requirement loop id: {exc}",
        )
    try:
        artifacts = _requirement_artifacts(root, loop_id)
    except ValueError as exc:
        return _blocked_requirement_start(
            loop_id,
            f"Invalid requirement loop id: {exc}",
        )
    if artifacts.freeze_path.is_file() or _closed_loop_run_exists(artifacts):
        return RequirementLoopCommandResult(
            status=RequirementCommandStatus.BLOCKED,
            result="Requirement loop is already frozen.",
            loop_id=loop_id,
            blocker="Frozen requirement loops cannot be restarted with the same loop id.",
            next_action=_design_contract_next_action(loop_id),
            artifacts=artifacts.refs(root, include_freeze=True),
        )
    return loop_id, artifacts


def _build_requirement_intake(
    options: RequirementStartOptions,
    loop_id: str,
    existing_intake: RequirementIntake | None,
    source_text: str,
    source_kind: RequirementSourceKind,
    source_path: str,
) -> tuple[RequirementIntake, LoopStatus, str]:
    reuse_existing = existing_intake is not None and not _has_explicit_source(options)
    existing_acceptance = (
        tuple(existing_intake.acceptance_criteria)
        if reuse_existing and existing_intake is not None
        else ()
    )
    existing_scope_families = (
        tuple(existing_intake.design_scope_families)
        if reuse_existing and existing_intake is not None
        else ()
    )
    acceptance = _clean_items((*existing_acceptance, *options.acceptance))
    questions = _derive_clarification_questions(source_text, acceptance)
    summary = _summarize_requirement(source_text)
    loop_status = LoopStatus.NEEDS_REVIEW if acceptance else LoopStatus.NEEDS_USER
    next_action = _next_action_for_requirement(loop_status, loop_id)
    if existing_intake is not None and (
        existing_intake.decision_mode != options.decision_mode
        or existing_intake.decision_capability != options.decision_capability
    ):
        raise ValueError("requirement-decision-identity-change")
    return (
        RequirementIntake(
            loop_id=loop_id,
            work_item_id=options.work_item_id.strip()
            or (existing_intake.work_item_id if existing_intake is not None else ""),
            source_kind=source_kind,
            source_path=source_path,
            raw_text=source_text,
            summary=summary,
            clarification_questions=questions,
            acceptance_criteria=acceptance,
            design_scope_families=_clean_scope_families(
                (*existing_scope_families, *options.design_scope_families)
            ),
            decision_mode=options.decision_mode,
            decision_capability=options.decision_capability,
        ),
        loop_status,
        next_action,
    )


def _write_requirement_start(
    root: Path,
    artifacts: _RequirementArtifacts,
    intake: RequirementIntake,
    loop_status: LoopStatus,
    next_action: str,
    *,
    revision: bool = False,
) -> None:
    previous_run = (
        _read_loop_run(artifacts.loop_run_path)
        if artifacts.loop_run_path.is_file()
        and intake.decision_capability == "stage-simulation-v1"
        else None
    )
    store = LoopArtifactStore(root)
    store.create_loop_run_dir(intake.loop_id, loop_type=LoopType.REQUIREMENT.value)
    store.write_json_artifact(artifacts.intake_path, intake)
    store.write_markdown_artifact(
        artifacts.brief_path, _render_requirement_brief(intake)
    )
    store.write_markdown_artifact(
        artifacts.questions_path,
        _render_clarification_questions(intake),
    )
    store.write_markdown_artifact(
        artifacts.checklist_path,
        _render_acceptance_checklist(intake),
    )
    loop_run = _build_loop_run(
        intake=intake,
        loop_status=loop_status,
        next_action=next_action,
        artifacts=artifacts,
        root=root,
    )
    loop_run = preserve_stage_run(previous_run, loop_run, revision=revision)
    store.write_json_artifact(artifacts.loop_run_path, loop_run)
    store.write_json_artifact(
        artifacts.pointer_path,
        {
            "schema_version": "1",
            "artifact_kind": "current-requirement-pointer",
            "loop_id": intake.loop_id,
            "loop_run_path": _repo_relative_path(root, artifacts.loop_run_path),
        },
    )


def _requirement_start_result(
    intake: RequirementIntake,
    loop_status: LoopStatus,
    next_action: str,
    artifacts: list[RequirementArtifactRef],
    *,
    dry_run: bool = False,
) -> RequirementLoopCommandResult:
    return RequirementLoopCommandResult(
        status=(
            RequirementCommandStatus.DRY_RUN
            if dry_run
            else RequirementCommandStatus.READY
            if loop_status == LoopStatus.NEEDS_REVIEW
            else RequirementCommandStatus.NEEDS_USER
        ),
        result="Requirement loop dry run." if dry_run else "Requirement loop started.",
        loop_id=intake.loop_id,
        loop_status=loop_status,
        summary=intake.summary,
        source_kind=intake.source_kind,
        source_path=intake.source_path,
        clarification_count=len(intake.clarification_questions),
        acceptance_count=len(intake.acceptance_criteria),
        dry_run=dry_run,
        next_action=next_action,
        artifacts=artifacts,
        requirement=_command_requirement_summary(intake),
    )


def _blocked_requirement_start(
    loop_id: str,
    blocker: str,
    *,
    artifacts: list[RequirementArtifactRef] | None = None,
) -> RequirementLoopCommandResult:
    return RequirementLoopCommandResult(
        status=RequirementCommandStatus.BLOCKED,
        result="Requirement loop could not start.",
        loop_id=loop_id,
        blocker=blocker,
        next_action=(
            "Run ai-sdlc loop requirement start --idea "
            '"<需求描述>" --acceptance "<验收标准>".'
        ),
        artifacts=artifacts or [],
    )


def freeze_requirement_loop(
    options: RequirementFreezeOptions,
    *,
    review_input_validator: ReviewInputValidator | None = None,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> RequirementLoopCommandResult:
    root = options.root.resolve()
    _, loop_id, blocker = _resolve_requirement_loop_run_path(root, options.loop_id)
    if blocker:
        return _freeze_requirement_loop_locked(
            options,
            review_input_validator=review_input_validator,
            reviewed_artifacts=reviewed_artifacts,
        )
    with _stage_write_guard(root, "requirement", loop_id):
        return _freeze_requirement_loop_locked(
            replace(options, loop_id=loop_id),
            review_input_validator=review_input_validator,
            reviewed_artifacts=reviewed_artifacts,
        )


def _freeze_requirement_loop_locked(
    options: RequirementFreezeOptions,
    *,
    review_input_validator: ReviewInputValidator | None = None,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> RequirementLoopCommandResult:
    """Freeze the current requirement loop after explicit user confirmation."""

    request = _prepare_requirement_freeze_request(options)
    if isinstance(request, RequirementLoopCommandResult):
        return request
    root, loop_run_path, expected_loop_id = request
    target = _load_requirement_freeze_target(
        root,
        loop_run_path,
        expected_loop_id,
        reviewed_artifacts=reviewed_artifacts,
    )
    if isinstance(target, RequirementLoopCommandResult):
        return target
    loop_run, artifacts = target
    from ai_sdlc.core.loop_stage_input import validate_stage_close_review

    try:
        validate_stage_close_review(
            root, loop_run, options.expected_review_digest, review_input_validator
        )
    except ValueError as exc:
        return RequirementLoopCommandResult(
            status=RequirementCommandStatus.BLOCKED,
            result="Requirement loop cannot be frozen without current actual review.",
            blocker=str(exc),
            next_action="Complete the current requirement review before freeze.",
        )
    intake = _load_requirement_freeze_intake(
        root,
        loop_run,
        expected_loop_id,
        artifacts,
        reviewed_artifacts=reviewed_artifacts,
    )
    if isinstance(intake, RequirementLoopCommandResult):
        return intake
    acceptance_result = _requirement_acceptance_result(
        loop_run,
        intake,
        artifacts,
        root,
    )
    if acceptance_result is not None:
        return acceptance_result
    if loop_run.status == LoopStatus.CLOSED and artifacts.freeze_path.is_file():
        return _refreeze_closed_requirement(root, loop_run, intake, artifacts)
    return _freeze_open_requirement(
        root,
        options,
        loop_run,
        intake,
        artifacts,
        review_input_validator=review_input_validator,
    )


def _prepare_requirement_freeze_request(
    options: RequirementFreezeOptions,
) -> tuple[Path, Path, str] | RequirementLoopCommandResult:
    root = options.root.resolve()
    loop_run_path, expected_loop_id, pointer_blocker = (
        _resolve_requirement_loop_run_path(
            root,
            options.loop_id,
        )
    )
    if pointer_blocker:
        return RequirementLoopCommandResult(
            status=RequirementCommandStatus.BLOCKED,
            result="Requirement loop cannot be frozen.",
            blocker=pointer_blocker,
            next_action="Run ai-sdlc loop requirement status.",
        )
    if not options.yes:
        return RequirementLoopCommandResult(
            status=RequirementCommandStatus.BLOCKED,
            result="Requirement freeze requires explicit confirmation.",
            blocker="Pass --yes after confirming the requirement and acceptance criteria.",
            next_action="Repeat the same guarded requirement freeze command with --yes.",
        )
    return root, loop_run_path, expected_loop_id


def _load_requirement_freeze_target(
    root: Path,
    loop_run_path: Path,
    expected_loop_id: str,
    *,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> tuple[LoopRun, _RequirementArtifacts] | RequirementLoopCommandResult:
    artifacts = _requirement_artifacts(root, expected_loop_id)
    path_issue = _requirement_artifact_path_issue(
        root,
        loop_run_path,
        artifacts.loop_run_path,
        "loop-run",
    )
    if path_issue:
        return RequirementLoopCommandResult(
            status=RequirementCommandStatus.BLOCKED,
            result="Requirement loop artifact is malformed.",
            loop_id=expected_loop_id,
            blocker=path_issue,
            next_action="Rerun ai-sdlc loop requirement start.",
        )
    try:
        loop_run = (
            _read_loop_run(loop_run_path)
            if reviewed_artifacts is None
            else LoopRun.model_validate_json(
                _reviewed_requirement_bytes(
                    root,
                    loop_run_path,
                    reviewed_artifacts,
                )
            )
        )
    except ValueError as exc:
        return RequirementLoopCommandResult(
            status=RequirementCommandStatus.BLOCKED,
            result="Requirement loop artifact is malformed.",
            blocker=str(exc),
            next_action="Rerun ai-sdlc loop requirement start.",
        )
    identity_issue = _requirement_loop_identity_issue(
        root,
        loop_run_path,
        expected_loop_id,
        loop_run,
    )
    if identity_issue:
        return RequirementLoopCommandResult(
            status=RequirementCommandStatus.BLOCKED,
            result="Requirement loop artifact is malformed.",
            loop_id=expected_loop_id,
            blocker=identity_issue,
            next_action="Rerun ai-sdlc loop requirement start.",
        )
    return loop_run, artifacts


def _load_requirement_freeze_intake(
    root: Path,
    loop_run: LoopRun,
    expected_loop_id: str,
    artifacts: _RequirementArtifacts,
    *,
    reviewed_artifacts: Mapping[str, bytes] | None = None,
) -> RequirementIntake | RequirementLoopCommandResult:
    path_issue = _requirement_artifact_path_issue(
        root,
        artifacts.intake_path,
        artifacts.intake_path,
        "intake",
    )
    if path_issue:
        return RequirementLoopCommandResult(
            status=RequirementCommandStatus.BLOCKED,
            result="Requirement intake artifact is malformed.",
            loop_id=expected_loop_id,
            blocker=path_issue,
            next_action="Rerun ai-sdlc loop requirement start.",
            artifacts=artifacts.refs(root),
        )
    try:
        intake = (
            _read_intake(artifacts.intake_path)
            if reviewed_artifacts is None
            else RequirementIntake.model_validate_json(
                _reviewed_requirement_bytes(
                    root,
                    artifacts.intake_path,
                    reviewed_artifacts,
                )
            )
        )
    except ValueError as exc:
        return RequirementLoopCommandResult(
            status=RequirementCommandStatus.BLOCKED,
            result="Requirement intake artifact is malformed.",
            loop_id=loop_run.loop_id,
            blocker=str(exc),
            next_action="Rerun ai-sdlc loop requirement start.",
            artifacts=artifacts.refs(root),
        )
    if (
        intake.loop_id != expected_loop_id
        or intake.work_item_id != loop_run.work_item_id
    ):
        return RequirementLoopCommandResult(
            status=RequirementCommandStatus.BLOCKED,
            result="Requirement intake artifact is malformed.",
            loop_id=expected_loop_id,
            blocker="Requirement intake identity does not match the confirmed loop.",
            next_action="Rerun ai-sdlc loop requirement start.",
            artifacts=artifacts.refs(root),
        )
    return intake


def _requirement_acceptance_result(
    loop_run: LoopRun,
    intake: RequirementIntake,
    artifacts: _RequirementArtifacts,
    root: Path,
) -> RequirementLoopCommandResult | None:
    if not intake.acceptance_criteria:
        return RequirementLoopCommandResult(
            status=RequirementCommandStatus.NEEDS_USER,
            result="Requirement loop needs acceptance criteria before freeze.",
            loop_id=loop_run.loop_id,
            loop_status=LoopStatus.NEEDS_USER,
            summary=intake.summary,
            source_kind=intake.source_kind,
            source_path=intake.source_path,
            clarification_count=len(intake.clarification_questions),
            acceptance_count=0,
            blocker="At least one acceptance criterion is required before freeze.",
            next_action=(
                f"Run ai-sdlc loop requirement start --loop-id {loop_run.loop_id} "
                '--acceptance "<验收标准>".'
            ),
            artifacts=artifacts.refs(root),
            requirement=_command_requirement_summary(intake),
        )
    return None


def _refreeze_closed_requirement(
    root: Path,
    loop_run: LoopRun,
    intake: RequirementIntake,
    artifacts: _RequirementArtifacts,
) -> RequirementLoopCommandResult:
    path_issue = _requirement_artifact_path_issue(
        root,
        artifacts.freeze_path,
        artifacts.freeze_path,
        "freeze",
    )
    if path_issue:
        return _malformed_requirement_freeze_result(
            root, loop_run, artifacts, path_issue
        )
    try:
        freeze = _read_freeze(artifacts.freeze_path)
    except ValueError as exc:
        return _malformed_requirement_freeze_result(root, loop_run, artifacts, str(exc))
    if freeze.loop_id != loop_run.loop_id:
        return _malformed_requirement_freeze_result(
            root,
            loop_run,
            artifacts,
            "Requirement freeze identity does not match the confirmed loop.",
        )
    return _existing_requirement_freeze_result(root, loop_run, intake, artifacts)


def _malformed_requirement_freeze_result(
    root: Path,
    loop_run: LoopRun,
    artifacts: _RequirementArtifacts,
    blocker: str,
) -> RequirementLoopCommandResult:
    return RequirementLoopCommandResult(
        status=RequirementCommandStatus.BLOCKED,
        result="Requirement freeze artifact is malformed.",
        loop_id=loop_run.loop_id,
        blocker=blocker,
        next_action="Start and freeze a new requirement loop.",
        artifacts=artifacts.refs(root, include_freeze=True),
    )


def _existing_requirement_freeze_result(
    root: Path,
    loop_run: LoopRun,
    intake: RequirementIntake,
    artifacts: _RequirementArtifacts,
) -> RequirementLoopCommandResult:
    return RequirementLoopCommandResult(
        status=RequirementCommandStatus.READY,
        result="Requirement loop is already frozen.",
        loop_id=loop_run.loop_id,
        loop_status=LoopStatus.CLOSED,
        summary=intake.summary,
        source_kind=intake.source_kind,
        source_path=intake.source_path,
        clarification_count=len(intake.clarification_questions),
        acceptance_count=len(intake.acceptance_criteria),
        frozen=True,
        next_action=loop_run.next_action,
        artifacts=artifacts.refs(root, include_freeze=True),
        requirement=_command_requirement_summary(intake, frozen=True),
    )


def _freeze_open_requirement(
    root: Path,
    options: RequirementFreezeOptions,
    loop_run: LoopRun,
    intake: RequirementIntake,
    artifacts: _RequirementArtifacts,
    *,
    review_input_validator: ReviewInputValidator | None,
) -> RequirementLoopCommandResult:
    accepted_by = options.accepted_by.strip() or "local-user"
    accepted_at = utc_now_iso()
    freeze = RequirementFreeze(
        loop_id=loop_run.loop_id,
        created_at=accepted_at,
        accepted_by=accepted_by,
        accepted_at=accepted_at,
        intake_path=_repo_relative_path(root, artifacts.intake_path),
        intake_digest=_requirement_intake_digest(intake),
        acceptance_count=len(intake.acceptance_criteria),
    )
    revalidate_review_input_at_transition(
        root,
        loop_type="requirement",
        loop_id=loop_run.loop_id,
        expected_digest=options.expected_review_digest,
        validator=review_input_validator,
    )
    return _write_requirement_freeze(
        root,
        loop_run,
        intake,
        freeze,
        artifacts,
    )


def _write_requirement_freeze(
    root: Path,
    loop_run: LoopRun,
    intake: RequirementIntake,
    freeze: RequirementFreeze,
    artifacts: _RequirementArtifacts,
) -> RequirementLoopCommandResult:
    loop_run.status = LoopStatus.CLOSED
    loop_run.updated_at = utc_now_iso()
    loop_run.next_action = _design_contract_next_action(loop_run.loop_id)
    loop_run.current_round = 1
    if loop_run.rounds:
        loop_run.rounds[0].status = LoopStatus.CLOSED
        loop_run.rounds[0].output_artifacts = _append_unique(
            loop_run.rounds[0].output_artifacts,
            _repo_relative_path(root, artifacts.freeze_path),
        )
        loop_run.rounds[0].next_action = loop_run.next_action

    store = LoopArtifactStore(root)
    store.write_json_artifact(artifacts.freeze_path, freeze)
    store.write_json_artifact(artifacts.loop_run_path, loop_run)

    return RequirementLoopCommandResult(
        status=RequirementCommandStatus.READY,
        result="Requirement loop frozen.",
        loop_id=loop_run.loop_id,
        loop_status=LoopStatus.CLOSED,
        summary=intake.summary,
        source_kind=intake.source_kind,
        source_path=intake.source_path,
        clarification_count=len(intake.clarification_questions),
        acceptance_count=len(intake.acceptance_criteria),
        frozen=True,
        next_action=loop_run.next_action,
        artifacts=artifacts.refs(root, include_freeze=True),
        requirement=_command_requirement_summary(intake, frozen=True),
    )


def _command_requirement_summary(
    intake: RequirementIntake,
    *,
    frozen: bool = False,
) -> RequirementCommandSummary:
    return RequirementCommandSummary(
        summary=intake.summary,
        source_kind=intake.source_kind,
        source_path=intake.source_path,
        clarification_count=len(intake.clarification_questions),
        acceptance_count=len(intake.acceptance_criteria),
        frozen=frozen,
    )


def _build_loop_run(
    *,
    intake: RequirementIntake,
    loop_status: LoopStatus,
    next_action: str,
    artifacts: _RequirementArtifacts,
    root: Path,
) -> LoopRun:
    output_artifacts = [
        _repo_relative_path(root, artifacts.intake_path),
        _repo_relative_path(root, artifacts.brief_path),
        _repo_relative_path(root, artifacts.questions_path),
        _repo_relative_path(root, artifacts.checklist_path),
    ]
    return LoopRun(
        loop_id=intake.loop_id,
        loop_type=LoopType.REQUIREMENT,
        decision_mode=intake.decision_mode,
        decision_capability=intake.decision_capability,
        status=loop_status,
        work_item_id=intake.work_item_id,
        current_round=1,
        rounds=[
            LoopRound(
                round_number=1,
                input_artifacts=[intake.source_path or "inline-idea"],
                output_artifacts=output_artifacts,
                command=["ai-sdlc", "loop", "requirement", "start"],
                status=loop_status,
                result="Requirement intake captured.",
                next_action=next_action,
            )
        ],
        next_action=next_action,
    )


def _read_requirement_source(
    options: RequirementStartOptions,
    root: Path,
    existing_intake: RequirementIntake | None = None,
) -> tuple[str, RequirementSourceKind, str, str]:
    idea = options.idea.strip()
    input_file = options.input_file.strip()
    if idea and input_file:
        return (
            "",
            RequirementSourceKind.IDEA,
            "",
            "Use either --idea or --input-file, not both.",
        )
    if idea:
        return idea, RequirementSourceKind.IDEA, "", ""
    if input_file:
        path = _resolve_local_input_file(root, input_file)
        if not path.is_file():
            return (
                "",
                RequirementSourceKind.INPUT_FILE,
                input_file,
                f"Requirement input file is not accessible: {input_file}",
            )
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return (
                "",
                RequirementSourceKind.INPUT_FILE,
                input_file,
                f"Requirement input file is not readable: {input_file}: {exc}",
            )
        if not text:
            return (
                "",
                RequirementSourceKind.INPUT_FILE,
                _repo_relative_path(root, path),
                "Requirement input file is empty.",
            )
        return (
            text,
            RequirementSourceKind.INPUT_FILE,
            _repo_relative_path(root, path),
            "",
        )
    if existing_intake is not None:
        return (
            existing_intake.raw_text,
            existing_intake.source_kind,
            existing_intake.source_path,
            "",
        )
    return (
        "",
        RequirementSourceKind.IDEA,
        "",
        "Requirement input requires --idea or --input-file.",
    )


def _existing_intake_for_start(
    options: RequirementStartOptions,
    artifacts: _RequirementArtifacts,
) -> tuple[RequirementIntake | None, str]:
    if not options.loop_id.strip():
        return None, ""
    if not artifacts.intake_path.is_file():
        return None, ""
    try:
        existing = _read_intake(artifacts.intake_path)
        if (
            _has_explicit_source(options)
            and existing.decision_mode == options.decision_mode == "legacy"
        ):
            return None, ""
        return existing, ""
    except ValueError as exc:
        return None, f"Existing requirement intake is malformed: {exc}"


def _has_explicit_source(options: RequirementStartOptions) -> bool:
    return bool(options.idea.strip() or options.input_file.strip())


def _closed_loop_run_exists(artifacts: _RequirementArtifacts) -> bool:
    if not artifacts.loop_run_path.is_file():
        return False
    try:
        return _read_loop_run(artifacts.loop_run_path).status == LoopStatus.CLOSED
    except ValueError:
        return False


def _resolve_local_input_file(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return root / path


def _resolve_loop_id(loop_id: str) -> str:
    text = loop_id.strip()
    if text:
        return _validate_explicit_loop_id(text)
    stamp = utc_now_iso().replace(":", "").replace("-", "").replace("T", "-")
    return f"requirement-{stamp.lower().removesuffix('z')}-{uuid4().hex[:8]}"


def _validate_explicit_loop_id(loop_id: str) -> str:
    if not _SAFE_EXPLICIT_LOOP_ID.fullmatch(loop_id):
        raise ValueError(
            "explicit loop id may contain only letters, digits, hyphen, and "
            "underscore, and must start with a letter or digit"
        )
    return loop_id


def _requirement_artifacts(root: Path, loop_id: str) -> _RequirementArtifacts:
    store = LoopArtifactStore(root)
    loop_dir = store.loop_run_dir(loop_id, loop_type=LoopType.REQUIREMENT.value)
    return _RequirementArtifacts(
        loop_dir=loop_dir,
        loop_run_path=loop_dir / "loop-run.json",
        intake_path=loop_dir / "requirement-intake.json",
        brief_path=loop_dir / "requirement-brief.md",
        questions_path=loop_dir / "clarification-questions.md",
        checklist_path=loop_dir / "acceptance-checklist.md",
        freeze_path=loop_dir / "requirement-freeze.json",
        pointer_path=root / CURRENT_REQUIREMENT_PATH,
    )


def _resolve_requirement_loop_run_path(
    root: Path,
    loop_id: str,
) -> tuple[Path, str, str]:
    text = loop_id.strip()
    if text:
        return _resolve_explicit_requirement_loop_run_path(root, text)
    return _resolve_current_requirement_loop_run_path(root)


def _resolve_explicit_requirement_loop_run_path(
    root: Path,
    loop_id: str,
) -> tuple[Path, str, str]:
    try:
        safe_loop_id = _validate_explicit_loop_id(loop_id)
        path = _requirement_artifacts(root, safe_loop_id).loop_run_path
    except ValueError as exc:
        return (
            root / CURRENT_REQUIREMENT_PATH,
            "",
            f"Invalid requirement loop id: {exc}",
        )
    return path, safe_loop_id, ""


def _resolve_current_requirement_loop_run_path(
    root: Path,
) -> tuple[Path, str, str]:
    pointer_path = root / CURRENT_REQUIREMENT_PATH
    if not pointer_path.is_file():
        return pointer_path, "", "No current requirement loop exists."
    safe_loop_id, path_text, blocker = _read_requirement_pointer(
        root,
        pointer_path,
    )
    if blocker:
        return pointer_path, "", blocker
    return _canonical_requirement_pointer_path(root, safe_loop_id, path_text)


def _read_requirement_pointer(
    root: Path,
    pointer_path: Path,
) -> tuple[str, str, str]:
    try:
        payload = LoopArtifactStore(root).read_json_artifact(pointer_path)
    except (OSError, ValueError) as exc:
        return "", "", f"Current requirement pointer is malformed: {exc}"
    loop_id_value = payload.get("loop_id")
    if not isinstance(loop_id_value, str) or not loop_id_value.strip():
        return "", "", "Current requirement pointer is missing loop_id."
    try:
        safe_loop_id = _validate_explicit_loop_id(loop_id_value.strip())
    except ValueError as exc:
        return "", "", f"Current requirement pointer loop identity is invalid: {exc}"
    path_text = payload.get("loop_run_path")
    if not isinstance(path_text, str) or not path_text.strip():
        return "", "", "Current requirement pointer is missing loop_run_path."
    return safe_loop_id, path_text, ""


def _canonical_requirement_pointer_path(
    root: Path,
    safe_loop_id: str,
    path_text: str,
) -> tuple[Path, str, str]:
    path = Path(path_text)
    if path.is_absolute() or ".." in path.parts:
        return (
            root / CURRENT_REQUIREMENT_PATH,
            "",
            "Current requirement pointer path must be project-relative.",
        )
    candidate = (root / path).resolve(strict=False)
    try:
        candidate.relative_to(root.resolve(strict=False))
    except ValueError:
        return (
            root / CURRENT_REQUIREMENT_PATH,
            "",
            "Current requirement pointer path must stay within project.",
        )
    canonical = _requirement_artifacts(root, safe_loop_id).loop_run_path.resolve(
        strict=False
    )
    if candidate != canonical:
        return (
            candidate,
            safe_loop_id,
            "Current requirement pointer identity does not match its loop-run path.",
        )
    return candidate, safe_loop_id, ""


def _requirement_loop_identity_issue(
    root: Path,
    loop_run_path: Path,
    expected_loop_id: str,
    loop_run: LoopRun,
) -> str:
    canonical = _requirement_artifacts(root, expected_loop_id).loop_run_path.resolve(
        strict=False
    )
    if loop_run_path.resolve(strict=False) != canonical:
        return "Requirement loop identity path is not canonical."
    if loop_run.loop_id != expected_loop_id:
        return "Requirement loop identity does not match the confirmed target."
    return ""


def _requirement_artifact_path_issue(
    root: Path,
    candidate: Path,
    expected: Path,
    artifact_name: str,
) -> str:
    root_path = root.absolute()
    candidate_path = candidate.absolute()
    expected_path = expected.absolute()
    if candidate_path != expected_path:
        return f"Requirement {artifact_name} artifact path is not canonical."
    try:
        relative = expected_path.relative_to(root_path)
    except ValueError:
        return f"Requirement {artifact_name} artifact must stay within project."
    current = root_path
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return f"Requirement {artifact_name} artifact cannot be a symlink."
    try:
        expected_path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return f"Requirement {artifact_name} artifact must stay within project."
    return ""


def _read_loop_run(path: Path) -> LoopRun:
    try:
        payload = json.loads(
            read_stable_text(_artifact_root(path), path, encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"Requirement loop-run.json is not readable: {exc}") from exc
    try:
        loop_run = LoopRun.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Requirement loop-run.json is invalid: {exc}") from exc
    if loop_run.loop_type != LoopType.REQUIREMENT:
        raise ValueError("Requirement freeze target is not a requirement loop.")
    return loop_run


def _read_intake(path: Path) -> RequirementIntake:
    try:
        payload = json.loads(
            read_stable_text(_artifact_root(path), path, encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"requirement-intake.json is not readable: {exc}") from exc
    try:
        return RequirementIntake.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"requirement-intake.json is invalid: {exc}") from exc


def _read_freeze(path: Path) -> RequirementFreeze:
    try:
        payload = json.loads(
            read_stable_text(_artifact_root(path), path, encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"requirement-freeze.json is not readable: {exc}") from exc
    try:
        return RequirementFreeze.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"requirement-freeze.json is invalid: {exc}") from exc


def _clean_items(values: tuple[str, ...]) -> list[str]:
    items: list[str] = []
    for value in values:
        text = value.strip()
        if text and text not in items:
            items.append(text)
    return items


def _clean_scope_families(values: tuple[str, ...]) -> list[str]:
    scopes = _clean_items(values)
    invalid = sorted(set(scopes) - _DESIGN_SCOPE_FAMILIES)
    if invalid:
        raise ValueError(f"unknown design scope families: {', '.join(invalid)}")
    return scopes


def _requirement_intake_digest(intake: RequirementIntake) -> str:
    return _requirement_artifact_digest(intake)


def _requirement_freeze_digest(freeze: RequirementFreeze) -> str:
    return _requirement_artifact_digest(freeze)


def _requirement_artifact_digest(
    artifact: RequirementIntake | RequirementFreeze,
) -> str:
    payload = artifact.model_dump(mode="json")
    return _requirement_payload_digest(payload)


def _requirement_payload_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _artifact_root(path: Path) -> Path:
    absolute = path.absolute()
    for parent in absolute.parents:
        if parent.name == ".ai-sdlc":
            return parent.parent
    raise ValueError(f"trusted requirement artifact is outside .ai-sdlc: {path}")


def _summarize_requirement(text: str) -> str:
    first_line = next(
        (line.strip() for line in text.splitlines() if line.strip()), text
    )
    if len(first_line) <= 140:
        return first_line
    return f"{first_line[:137]}..."


def _derive_clarification_questions(text: str, acceptance: list[str]) -> list[str]:
    lowered = text.lower()
    questions: list[str] = []
    if (
        not any(
            marker in text for marker in ("用户", "客户", "管理员", "工程师", "运营")
        )
        and "user" not in lowered
    ):
        questions.append("主要使用者是谁？请补充用户角色或操作者。")
    if not acceptance:
        questions.append("如何验收这个需求？请补充至少一条可验证的验收标准。")
    if (
        not any(
            marker in text for marker in ("不", "不得", "不能", "边界", "范围", "只")
        )
        and "non-goal" not in lowered
    ):
        questions.append("本需求明确不覆盖什么？请补充范围边界或非目标。")
    if len(text) < 20:
        questions.append("需求描述较短，请补充关键流程、输入输出或异常场景。")
    return questions


def _next_action_for_requirement(loop_status: LoopStatus, loop_id: str) -> str:
    if loop_status == LoopStatus.NEEDS_REVIEW:
        return f"Run ai-sdlc loop review --type requirement --loop-id {loop_id}."
    if loop_status == LoopStatus.NEEDS_USER:
        return (
            "Add acceptance criteria, then run ai-sdlc loop requirement start "
            f'--loop-id {loop_id} --acceptance "<验收标准>".'
        )
    if loop_status == LoopStatus.CLOSED:
        return _design_contract_next_action(loop_id)
    return "Run ai-sdlc loop requirement status."


def _design_contract_next_action(loop_id: str) -> str:
    return f"Start design-contract loop from requirement {loop_id}."


def _render_requirement_brief(intake: RequirementIntake) -> str:
    return "\n".join(
        [
            f"# Requirement Brief: {intake.loop_id}",
            "",
            f"- Summary: {intake.summary}",
            f"- Source kind: {intake.source_kind}",
            f"- Work item: {intake.work_item_id or '-'}",
            "",
            "## Raw Requirement",
            "",
            intake.raw_text,
            "",
        ]
    )


def _render_clarification_questions(intake: RequirementIntake) -> str:
    lines = [f"# Clarification Questions: {intake.loop_id}", ""]
    if not intake.clarification_questions:
        lines.append("- No clarification questions detected.")
    else:
        lines.extend(f"- {question}" for question in intake.clarification_questions)
    lines.append("")
    return "\n".join(lines)


def _render_acceptance_checklist(intake: RequirementIntake) -> str:
    lines = [f"# Acceptance Checklist: {intake.loop_id}", ""]
    if not intake.acceptance_criteria:
        lines.append("- [ ] 待补充：至少一条可验证的验收标准。")
    else:
        lines.extend(f"- [ ] {item}" for item in intake.acceptance_criteria)
    lines.append("")
    return "\n".join(lines)


def _artifact_ref(root: Path, kind: str, path: Path) -> RequirementArtifactRef:
    return RequirementArtifactRef(
        kind=kind,
        path=_repo_relative_path(root, path),
        exists=path.is_file(),
    )


def _repo_relative_path(root: Path, path: Path) -> str:
    try:
        return (
            path.resolve(strict=False)
            .relative_to(root.resolve(strict=False))
            .as_posix()
        )
    except ValueError:
        return path.as_posix()


def _reviewed_requirement_bytes(
    root: Path,
    path: Path,
    reviewed_artifacts: Mapping[str, bytes],
) -> bytes:
    key = _repo_relative_path(root, path)
    try:
        return reviewed_artifacts[key]
    except KeyError as exc:
        raise ValueError(f"Reviewed requirement snapshot is missing {key}.") from exc


def _append_unique(values: list[str], value: str) -> list[str]:
    if value in values:
        return values
    return [*values, value]


__all__ = [
    "CURRENT_REQUIREMENT_PATH",
    "RequirementArtifactRef",
    "RequirementCommandStatus",
    "RequirementFreeze",
    "RequirementFreezeOptions",
    "RequirementIntake",
    "RequirementLoopCommandResult",
    "RequirementSourceKind",
    "RequirementStartOptions",
    "freeze_requirement_loop",
    "start_requirement_loop",
]
