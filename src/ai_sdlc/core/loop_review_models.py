"""Minimal persisted values for bounded Loop-native expert review."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_sdlc.core.loop_decision_models import B1Assessment, B1ReviewData
from ai_sdlc.core.loop_stage_decision_service import StageReviewData
from ai_sdlc.core.review_kernel import (
    LoopReviewType,
    ReviewExecution,
    ReviewExecutionStatus,
    ReviewFinding,
)

ReviewOverlayStatus = Literal[
    "review_missing",
    "failed",
    "needs_fix",
    "needs_user",
    "passed",
]


class ContinuationReference(BaseModel):
    """只读解析历史续办原件引用，不提供激活或恢复能力。"""

    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _canonical_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or path.as_posix() != value
            or any(part in {"..", ".git"} for part in path.parts)
            or any(char in value for char in "\\:*?[]")
        ):
            raise ValueError("continuation path must be an exact project-relative file")
        return value


class ContinuationSource(ContinuationReference):
    """固定全集中的缺失不是读取失败；缺失记录不能携带内容摘要。"""

    state: Literal["present", "absent"]
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _presence(self) -> ContinuationSource:
        if (self.state == "present") != (self.sha256 is not None):
            raise ValueError("continuation source presence mismatch")
        return self


class ContinuationMaterialManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_files: list[ContinuationSource]
    verification_evidence: list[dict[str, object]]
    prior_outcomes: list[ContinuationReference] = Field(default_factory=list)


class LoopReviewOutcome(BaseModel):
    """One completed result or preserved failed execution; never an implicit retry."""

    model_config = ConfigDict(extra="forbid")

    loop_id: str
    loop_type: LoopReviewType
    round_number: int = Field(strict=True, ge=1)
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ReviewExecutionStatus
    expert_roles: list[str] = Field(min_length=1, max_length=2)
    findings: list[ReviewFinding] = Field(default_factory=list)
    completed_expert_results: list[ReviewExecution] = Field(
        default_factory=list, max_length=2, exclude_if=lambda value: not value
    )
    failure_kind: str = ""
    failure_reason: str = ""
    recorded_at: str
    b1: B1ReviewData | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    simulation: StageReviewData | B1ReviewData | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    infra_retry_count: int | None = Field(
        default=None, strict=True, ge=0, le=1, exclude_if=lambda value: value is None
    )
    # 旧续办字段仅保留读取兼容；所有活动入口拒绝退休实例，不重写原件。
    continuation_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", exclude_if=lambda value: value is None
    )
    continuation_material_manifest: ContinuationMaterialManifest | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    continuation_retry_count: int | None = Field(
        default=None, strict=True, ge=0, le=1, exclude_if=lambda value: value is None
    )

    @field_validator("loop_id", "recorded_at")
    @classmethod
    def _require_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("review outcome text is required")
        return text

    @model_validator(mode="after")
    def _validate_outcome_shape(self) -> LoopReviewOutcome:
        values = (
            self.continuation_digest,
            self.continuation_material_manifest,
            self.continuation_retry_count,
        )
        if any(value is not None for value in values):
            if (
                not all(value is not None for value in values)
                or self.round_number <= 2
                or self.loop_type != "implementation"
                or self.b1 is not None
                or self.simulation is not None
                or self.infra_retry_count is not None
            ):
                raise ValueError("legacy continuation review identity is invalid")
        elif self.round_number > 2:
            raise ValueError("review-round-limit")
        roles = [role.strip() for role in self.expert_roles]
        if any(not role for role in roles):
            raise ValueError("expert role cannot be empty")
        if len(roles) != len(set(roles)):
            raise ValueError("expert roles must be unique")
        if {finding.role for finding in self.findings} - set(roles):
            raise ValueError("finding role must be selected for this outcome")
        self.expert_roles = roles

        failure_kind = self.failure_kind.strip()
        failure_reason = self.failure_reason.strip()
        if self.status == "completed":
            if failure_kind or failure_reason:
                raise ValueError("completed outcome cannot carry failure state")
        else:
            if not failure_kind or not failure_reason:
                raise ValueError("failed outcome requires failure details")
            if self.findings:
                raise ValueError("failed outcome cannot carry findings")
        self.failure_kind = failure_kind
        self.failure_reason = failure_reason
        # 部分执行完成不等于整体通过；旧失败原件缺此字段仍保持原序列化。
        if self.completed_expert_results:
            completed_roles = [
                role
                for result in self.completed_expert_results
                for role in result.roles
            ]
            if (
                self.status != "failed"
                or any(
                    result.status != "completed" or len(result.roles) != 1
                    for result in self.completed_expert_results
                )
                or len(completed_roles) != len(set(completed_roles))
                or not set(completed_roles) < set(roles)
            ):
                raise ValueError(
                    "partial results require distinct completed selected roles"
                )
        if self.b1 is not None and self.simulation is not None:
            raise ValueError("quantified review capabilities are mutually exclusive")
        actual = self.b1 if self.b1 is not None else self.simulation
        if actual is not None or self.infra_retry_count is not None:
            if (
                self.infra_retry_count is None
                or (self.b1 is not None and self.loop_type != "implementation")
                or (
                    self.simulation is not None
                    and not isinstance(self.simulation, StageReviewData)
                    and self.loop_type != "implementation"
                )
            ):
                raise ValueError("B1 review identity is invalid")
            if (self.status == "completed") != (actual is not None):
                raise ValueError("Only completed B1 outcomes require an assessment")
        return self


class B1ExpertResult(BaseModel):
    """原独立专家结果的窄包裹；失败不能携带实际评分。"""

    model_config = ConfigDict(extra="forbid")

    execution: ReviewExecution
    assessment: B1Assessment | None = None

    @model_validator(mode="after")
    def _require_actual_assessment(self) -> B1ExpertResult:
        if len(self.execution.roles) != 1:
            raise ValueError("B1 result requires exactly one independent expert")
        if (self.execution.status == "completed") != (self.assessment is not None):
            raise ValueError("Only completed B1 executions require an assessment")
        return self


class ReviewStatusOverlay(BaseModel):
    """Derived display state; never a reusable Close credential."""

    model_config = ConfigDict(extra="forbid")

    status: ReviewOverlayStatus
    reason: str
    next_action: str
    round_number: int = Field(ge=0)

    @field_validator("reason", "next_action")
    @classmethod
    def _require_overlay_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("review overlay text is required")
        return text


__all__ = [
    "LoopReviewOutcome",
    "ReviewOverlayStatus",
    "ReviewStatusOverlay",
]
