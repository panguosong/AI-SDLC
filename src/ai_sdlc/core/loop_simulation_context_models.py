"""模拟准备状态的有限值对象；D1 原始序列化与阶段新身份明确分支。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictBool, model_serializer, model_validator

from ai_sdlc.core.loop_decision_models import (
    DecisionSource,
    DecisionValue,
    Digest,
    Identifier,
    Text,
)
from ai_sdlc.core.loop_simulation_models import (
    STAGE_PROFILES,
    ConditionalImprovement,
    ImprovementSearch,
    SimulatedCandidate,
    SimulationCapability,
    SimulationJudgement,
    SimulationSelection,
    StageScoreContract,
)

CAPABILITY = "implementation-simulation-v1"
STAGE_CAPABILITY = "stage-simulation-v1"


class SimulationFailure(DecisionValue):
    stage: Literal["drafting", "judging"]
    reason: Text
    retry: StrictBool = False
    prior_call_terminated: StrictBool = False


class SimulationPrepareRequest(DecisionValue):
    operation: Literal[
        "begin",
        "freeze-comparison",
        "record-comparison",
        "seal-for-review",
        "begin-improvement",
        "correct-input",
    ]
    request_id: Identifier
    contracts: tuple[StageScoreContract, ...] = Field(default=(), max_length=2)
    sources: tuple[DecisionSource, ...] = Field(default=(), max_length=256)
    candidates: tuple[SimulatedCandidate, ...] = Field(default=(), max_length=3)
    judgement: SimulationJudgement | None = None
    failure: SimulationFailure | None = None
    continue_search: StrictBool = False
    reason: str = Field(default="", max_length=2048)
    improvement: ImprovementSearch | None = None

    @model_validator(mode="after")
    def _shape(self):
        supplied = set(self.model_fields_set) - {"operation", "request_id"}
        allowed = {
            "begin": {"contracts", "sources"},
            "freeze-comparison": {"candidates", "sources"},
            "record-comparison": {"judgement", "failure", "continue_search", "reason"},
            "seal-for-review": set(),
            "begin-improvement": {"improvement"},
            "correct-input": set(),
        }[self.operation]
        if supplied - allowed:
            raise ValueError("simulation-operation-fields-invalid")
        if self.operation == "begin" and (not self.contracts or not self.sources):
            raise ValueError("simulation-begin-requires-profile-bundle-and-sources")
        if self.operation == "begin-improvement" and self.improvement is None:
            raise ValueError("simulation-improvement-required")
        if self.operation == "freeze-comparison" and not self.candidates:
            raise ValueError("simulation-candidates-required")
        if self.operation == "record-comparison" and (
            (self.judgement is None) == (self.failure is None)
        ):
            raise ValueError("simulation-one-judgement-or-failure-required")
        if self.continue_search and (
            not self.reason.strip() or self.failure is not None
        ):
            raise ValueError("simulation-continuation-reason-required")
        if len({s.id for s in self.sources}) != len(self.sources):
            raise ValueError("simulation-source-ids-duplicate")
        return self


class SimulationBatch(DecisionValue):
    number: int = Field(strict=True, ge=1, le=2)
    decision_point: Text
    base_input_digest: Digest
    candidates: tuple[SimulatedCandidate, ...] = Field(default=(), max_length=3)
    source_manifest: dict[str, Digest] = Field(default_factory=dict)
    judge_input_digest: Digest | None = None
    failures: tuple[SimulationFailure, ...] = Field(default=(), max_length=2)
    judgement: SimulationJudgement | None = None
    elapsed_seconds: int | None = Field(default=None, strict=True, ge=0)
    outcome: Literal["pending", "success", "technical_failure"] = "pending"
    selection: SimulationSelection | None = None
    incumbent_id: Identifier | None = None
    continuation_requested: StrictBool = False
    continuation_stop_reason: str | None = None


class RequestReceipt(DecisionValue):
    request_id: Identifier
    request_digest: Digest


class InputCorrectionReceipt(DecisionValue):
    """引用纠错前的原上下文；第二批不覆盖第一次判断或重新起算。"""

    source_context_digest: Digest
    source_batch_digest: Digest
    source_count: int = Field(strict=True, ge=1, le=256)
    source_receipt_count: int = Field(strict=True, ge=1, le=12)
    source_observed_at_ms: int = Field(strict=True, ge=0)
    corrected_at_ms: int = Field(strict=True, ge=0)
    request_id: Identifier
    corrected_base_input_digest: Digest


class SimulationContext(DecisionValue):
    schema_version: SimulationCapability = CAPABILITY
    capability: SimulationCapability = CAPABILITY
    loop_id: Identifier
    implementation_input_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    contracts: tuple[StageScoreContract, ...] = Field(min_length=1, max_length=2)
    sources: tuple[DecisionSource, ...] = Field(min_length=1, max_length=256)
    started_at_ms: int = Field(strict=True, ge=0)
    last_observed_at_ms: int | None = Field(default=None, strict=True, ge=0)
    phase: Literal[
        "initial_search", "initial_selected", "improvement_search", "review_sealed"
    ] = "initial_search"
    pending_batch: SimulationBatch | None = None
    comparisons: tuple[SimulationBatch, ...] = Field(default=(), max_length=2)
    initial_selection_id: Identifier | None = None
    receipts: tuple[RequestReceipt, ...] = Field(min_length=1, max_length=12)
    review_seal: Digest | None = None
    context_digest: Digest
    improvement: ImprovementSearch | None = None
    conditional_improvement: ConditionalImprovement | None = None
    input_correction: InputCorrectionReceipt | None = None

    @model_serializer(mode="wrap")
    def _preserve_d1_payload(self, handler):
        payload = handler(self)
        if self.input_correction is None:
            payload.pop("input_correction", None)
        if self.capability == CAPABILITY:
            payload.pop("last_observed_at_ms", None)
            payload.pop("improvement", None)
            payload.pop("conditional_improvement", None)
        return payload

    @property
    def loop_type(self) -> str:
        return self.contracts[0].loop_type

    @property
    def plan(self) -> StageScoreContract:
        return next(
            c
            for c in self.contracts
            if c.profile_id == STAGE_PROFILES[self.loop_type][0]
        )

    def contract_for_batch(self, batch: SimulationBatch) -> StageScoreContract:
        if (
            batch.decision_point == "before-improvement"
            and self.loop_type == "implementation"
        ):
            return next(c for c in self.contracts if c.profile_id == "code-result-v1")
        return self.plan

    @property
    def current_contract(self) -> StageScoreContract:
        batch = self.pending_batch or (
            self.comparisons[-1] if self.comparisons else None
        )
        return self.contract_for_batch(batch) if batch is not None else self.plan

    @property
    def goal_contract(self):
        return self.plan.goal_contract

    @property
    def improvement_stop_reason(self) -> str | None:
        from ai_sdlc.core.loop_simulation_context import improvement_stop_reason

        return improvement_stop_reason(self)

    @property
    def selection(self) -> SimulationSelection | None:
        return next(
            (
                b.selection
                for b in reversed(self.comparisons)
                if b.outcome == "success" and b.decision_point != "before-improvement"
            ),
            None,
        )

    @property
    def selected_candidate(self) -> SimulatedCandidate | None:
        for batch in reversed(self.comparisons):
            if (
                batch.selection is not None
                and batch.decision_point != "before-improvement"
                and batch.selection.selected_id == self.initial_selection_id
            ):
                return next(
                    (
                        c
                        for c in batch.candidates
                        if c.candidate_id == self.initial_selection_id
                    ),
                    None,
                )
        return None


class SimulationPreparation(DecisionValue):
    status: Literal["preview", "prepared", "existing"]
    prepare_digest: Digest
    context: SimulationContext

    @property
    def selection(self):
        return self.context.selection
