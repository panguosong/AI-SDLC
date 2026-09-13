"""模拟初始择优的有限值对象；草案与独立判断分开，不授予执行权限。"""

from __future__ import annotations

from fractions import Fraction
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, StrictBool, model_validator

from ai_sdlc.core.loop_decision_models import (
    DecisionValue,
    Digest,
    ExecutionPrecondition,
    GoalContract,
    Identifier,
    NonnegativeNumber,
    PositiveNumber,
    Text,
    TimeEstimate,
)

Level = Annotated[int, Field(strict=True, ge=0, le=4)]
Count = Annotated[int, Field(strict=True, ge=0)]
References = Annotated[tuple[Identifier, ...], Field(max_length=64)]
SimulationCapability = Literal["implementation-simulation-v1", "stage-simulation-v1"]
StageKind = Literal[
    "requirement",
    "design-contract",
    "implementation",
    "frontend-evidence",
    "local-pr-review",
]
StageProfile = Literal[
    "requirement-analysis-v1",
    "design-contract-v1",
    "implementation-plan-v1",
    "code-result-v1",
    "frontend-evidence-v1",
    "delivery-readiness-v1",
]
STAGE_PROFILES = {
    "requirement": ("requirement-analysis-v1",),
    "design-contract": ("design-contract-v1",),
    "implementation": ("implementation-plan-v1", "code-result-v1"),
    "frontend-evidence": ("frontend-evidence-v1",),
    "local-pr-review": ("delivery-readiness-v1",),
}


def _unique(values: tuple[object, ...], label: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"simulation-{label}-duplicate")


def _exact_fraction(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("simulation-fraction-requires-exact-input")
    return value


ExactFraction = Annotated[Fraction, BeforeValidator(_exact_fraction)]


class GoalShare(DecisionValue):
    goal_id: Identifier
    share: Annotated[PositiveNumber, Field(le=1)]


class ScoreAnchor(DecisionValue):
    level: Level
    statement: Text
    required_supported_ids: tuple[Identifier, ...] = Field(default=(), max_length=2048)


class ScoreCriterion(DecisionValue):
    id: Identifier
    goal_shares: tuple[GoalShare, ...] = Field(max_length=128)
    statement: Text
    source_refs: References = Field(min_length=1)
    applicability: Literal["applicable", "not_applicable"] = "applicable"
    applicability_reason: Text | None = None
    anchors: tuple[ScoreAnchor, ...] = Field(min_length=5, max_length=5)
    target_level: Level = 3
    protected: StrictBool = False
    critical_floor: Level | None = None
    improvement_direction: Text | None = None
    count_scope: Literal["rubric", "contract", "candidate"] = "rubric"
    item_ids: tuple[Identifier, ...] = Field(default=(), max_length=2048)

    @model_validator(mode="after")
    def _check_criterion(self) -> ScoreCriterion:
        _unique(tuple(row.goal_id for row in self.goal_shares), "goal-share")
        _unique(self.source_refs, "source-reference")
        _unique(self.item_ids, "count-item")
        if {row.level for row in self.anchors} != set(range(5)):
            raise ValueError("simulation-anchors-must-cover-zero-through-four")
        if self.applicability == "not_applicable":
            if self.goal_shares or not self.applicability_reason:
                raise ValueError("simulation-inapplicable-requires-reason-and-no-share")
        elif not self.goal_shares:
            raise ValueError("simulation-applicable-requires-goal-share")
        if (self.count_scope == "contract") != bool(self.item_ids):
            raise ValueError("simulation-contract-count-requires-frozen-items")
        for anchor in self.anchors:
            _unique(anchor.required_supported_ids, "anchor-item")
            if not set(anchor.required_supported_ids) <= set(self.item_ids):
                raise ValueError("simulation-anchor-references-unfrozen-item")
        return self


class StageTimePlan(DecisionValue):
    window_seconds: PositiveNumber
    scope: Text
    basis_refs: References = Field(min_length=1)
    assumptions: tuple[Text, ...] = Field(min_length=1, max_length=64)
    work_breakdown: tuple[Text, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _check_refs(self) -> StageTimePlan:
        _unique(self.basis_refs, "time-basis")
        return self


class SimulationPolicy(DecisionValue):
    max_batches: Annotated[int, Field(strict=True, ge=2, le=2)] = 2
    max_candidates: Annotated[int, Field(strict=True, ge=3, le=3)] = 3
    max_judges: Annotated[int, Field(strict=True, ge=1, le=1)] = 1


class StageScoreContract(DecisionValue):
    schema_version: Literal["1"] = "1"
    capability: SimulationCapability = "implementation-simulation-v1"
    loop_type: StageKind = "implementation"
    profile_id: StageProfile = "implementation-plan-v1"
    profile_version: Literal["1"] = "1"
    goal_contract: GoalContract
    criteria: tuple[ScoreCriterion, ...] = Field(min_length=1, max_length=16)
    time_plan: StageTimePlan
    simulation_policy: SimulationPolicy = Field(default_factory=SimulationPolicy)
    source_refs: References = Field(min_length=1)

    @model_validator(mode="after")
    def _check_shares(self) -> StageScoreContract:
        if self.capability == "stage-simulation-v1":
            if not {"loop_type", "profile_id"} <= self.model_fields_set:
                raise ValueError("simulation-stage-identity-must-be-explicit")
        elif self.loop_type != "implementation":
            raise ValueError("simulation-d1-only-supports-implementation")
        if self.profile_id not in STAGE_PROFILES[self.loop_type]:
            raise ValueError("simulation-stage-profile-mismatch")
        _unique(tuple(row.id for row in self.criteria), "criterion")
        _unique(self.source_refs, "source-reference")
        shares = dict.fromkeys(
            (goal.id for goal in self.goal_contract.goals), Fraction(0)
        )
        for criterion in self.criteria:
            for row in criterion.goal_shares:
                if row.goal_id not in shares:
                    raise ValueError("simulation-share-references-unknown-goal")
                shares[row.goal_id] += Fraction(row.share)
        if any(value != 1 for value in shares.values()):
            raise ValueError("simulation-goal-shares-must-total-one")
        return self


class SimulationBasis(DecisionValue):
    id: Identifier
    kind: Literal["project_fact", "design_assumption", "logical_projection"]
    source_ref: Identifier | None = None
    locator: Text
    statement: Text

    @model_validator(mode="after")
    def _fact_needs_source(self) -> SimulationBasis:
        if self.kind == "project_fact" and self.source_ref is None:
            raise ValueError("simulation-project-fact-needs-source")
        return self


class ArtifactSketch(DecisionValue):
    goal_mapping: tuple[Text, ...] = Field(min_length=1, max_length=128)
    key_structure: tuple[Text, ...] = Field(min_length=1, max_length=64)
    representative_scenarios: tuple[Text, ...] = Field(min_length=1, max_length=64)
    failure_paths: tuple[Text, ...] = Field(min_length=1, max_length=64)
    downstream_impacts: tuple[Text, ...] = Field(min_length=1, max_length=64)
    excluded_scope: tuple[Text, ...] = Field(max_length=64)


class CandidateItems(DecisionValue):
    criterion_id: Identifier
    item_ids: tuple[Identifier, ...] = Field(max_length=2048)

    @model_validator(mode="after")
    def _check_items(self) -> CandidateItems:
        _unique(self.item_ids, "candidate-item")
        return self


class SimulatedCandidate(DecisionValue):
    """评分前封存的草案；作者不能夹带派生分数或独立判断。"""

    candidate_id: Identifier
    contract_digest: Digest
    origin: Literal["simulation"] = "simulation"
    mechanism: Text
    changed_scope: tuple[Text, ...] = Field(min_length=1, max_length=64)
    assumptions: tuple[Text, ...] = Field(min_length=1, max_length=64)
    artifact_sketch: ArtifactSketch
    basis: tuple[SimulationBasis, ...] = Field(min_length=1, max_length=64)
    execution_preconditions: tuple[ExecutionPrecondition, ...] = Field(
        min_length=3, max_length=3
    )
    cost_decision_point: Text
    future_cost_estimate: TimeEstimate | None = None
    candidate_items: tuple[CandidateItems, ...] = Field(default=(), max_length=16)
    known_required_conflicts: tuple[Identifier, ...] = Field(
        default=(), max_length=2048
    )

    @model_validator(mode="after")
    def _check_candidate(self) -> SimulatedCandidate:
        _unique(tuple(row.id for row in self.basis), "basis")
        _unique(
            tuple(row.criterion_id for row in self.candidate_items), "candidate-count"
        )
        _unique(self.known_required_conflicts, "required-conflict")
        if {row.kind for row in self.execution_preconditions} != {
            "authorization",
            "safety",
            "mandatory_constraints",
        }:
            raise ValueError("simulation-requires-three-distinct-preconditions")
        refs = {
            ref for row in self.execution_preconditions for ref in row.evidence_refs
        }
        if self.future_cost_estimate is not None:
            refs.update(self.future_cost_estimate.basis_refs)
        if not refs <= {row.id for row in self.basis}:
            raise ValueError("simulation-candidate-basis-reference-missing")
        return self


class CountItem(DecisionValue):
    id: Identifier
    status: Literal["supported", "contradicted", "unknown"]
    basis_refs: References = ()

    @model_validator(mode="after")
    def _check_basis(self) -> CountItem:
        _unique(self.basis_refs, "count-basis")
        if self.status != "unknown" and not self.basis_refs:
            raise ValueError("simulation-known-count-requires-basis")
        return self


class CriterionAssessment(DecisionValue):
    criterion_id: Identifier
    lower: Level
    upper: Level
    basis_refs: References = Field(min_length=1)
    reason: Text
    items: tuple[CountItem, ...] = Field(default=(), max_length=2048)

    @model_validator(mode="after")
    def _check_assessment(self) -> CriterionAssessment:
        if self.lower > self.upper:
            raise ValueError("simulation-level-interval-reversed")
        _unique(self.basis_refs, "assessment-basis")
        _unique(tuple(row.id for row in self.items), "count-item")
        return self


class CandidateAssessment(DecisionValue):
    candidate_id: Identifier
    cost_check: Literal["supported", "incomplete", "unknown"] = "unknown"
    cost_reason: Text | None = None
    criterion_assessments: tuple[CriterionAssessment, ...] = Field(
        min_length=1, max_length=16
    )
    known_required_conflicts: tuple[Identifier, ...] = Field(
        default=(), max_length=2048
    )

    @model_validator(mode="after")
    def _check_ids(self) -> CandidateAssessment:
        if self.cost_check != "unknown" and self.cost_reason is None:
            raise ValueError("simulation-cost-check-requires-reason")
        _unique(
            tuple(row.criterion_id for row in self.criterion_assessments),
            "assessment-criterion",
        )
        _unique(self.known_required_conflicts, "required-conflict")
        return self


class InitialSearchContinuation(DecisionValue):
    """独立判断提出的初始续搜假设；并非修改或扩展时间窗口的授权。"""

    criterion_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=16)
    hypothesis: Text
    future_cost_estimate: TimeEstimate

    @model_validator(mode="after")
    def _check_references(self) -> InitialSearchContinuation:
        _unique(self.criterion_ids, "continuation-criterion")
        _unique(self.future_cost_estimate.basis_refs, "continuation-time-basis")
        return self


class SimulationJudgement(DecisionValue):
    judge_input_digest: Digest
    assessments: tuple[CandidateAssessment, ...] = Field(min_length=1, max_length=3)
    initial_search_continuation: InitialSearchContinuation | None = None

    @model_validator(mode="after")
    def _check_candidates(self) -> SimulationJudgement:
        _unique(
            tuple(row.candidate_id for row in self.assessments), "assessed-candidate"
        )
        return self


class CountSummary(DecisionValue):
    criterion_id: Identifier
    total: Count
    supported: Count
    contradicted: Count
    unknown: Count
    coverage_low: ExactFraction | None = None
    coverage_high: ExactFraction | None = None

    @model_validator(mode="after")
    def _check_partition(self) -> CountSummary:
        if self.supported + self.contradicted + self.unknown != self.total:
            raise ValueError("simulation-count-partition-invalid")
        return self


class SimulationScore(DecisionValue):
    candidate_id: Identifier
    contract_digest: Digest
    s_low: Annotated[ExactFraction, Field(ge=0, le=100)]
    s_high: Annotated[ExactFraction, Field(ge=0, le=100)]
    u_sim: Annotated[ExactFraction, Field(ge=0, le=1)]
    targets_met: StrictBool
    floor_failures: tuple[Identifier, ...] = ()
    floor_unknowns: tuple[Identifier, ...] = ()
    counts: tuple[CountSummary, ...] = ()

    @model_validator(mode="after")
    def _check_interval(self) -> SimulationScore:
        if self.s_low > self.s_high:
            raise ValueError("simulation-score-interval-reversed")
        return self


SimulationExclusionReason = Literal[
    "precondition_not_passed",
    "known_required_conflict",
    "cost_incomplete",
    "cost_unknown",
    "time_plan_exceeded",
    "time_unknown",
    "time_conditions_mismatch",
    "critical_floor",
    "protected_regression",
    "quality_preference",
    "future_time_preference",
]
SimulationChoiceBasis = Literal["forecast_preference", "unresolved_tie_fallback"]
SimulationSelectionReason = Literal[
    "no_safe_route",
    "model_plan_not_feasible",
    "single_feasible_route",
    "quality_preference",
    "future_time_preference",
    "incumbent_tie_break",
    "stable_id_tie_break",
]


class SimulationExclusion(DecisionValue):
    candidate_id: Identifier
    reason: SimulationExclusionReason
    criterion_id: Identifier | None = None
    witness_id: Identifier | None = None


class SimulationSelection(DecisionValue):
    contract_digest: Digest
    selected_id: Identifier | None
    basis: SimulationChoiceBasis | None
    reason: SimulationSelectionReason
    scores: dict[Identifier, SimulationScore]
    eligible_ids: tuple[Identifier, ...]
    quality_survivors: tuple[Identifier, ...]
    remaining_ids: tuple[Identifier, ...]
    excluded: tuple[SimulationExclusion, ...] = ()
    limitations: tuple[Text, ...] = ()

    @property
    def candidate_scores(self) -> dict[str, SimulationScore]:
        return self.scores


class SimulationComparison(DecisionValue):
    contract: StageScoreContract
    candidates: tuple[SimulatedCandidate, ...] = Field(min_length=1, max_length=3)
    assessments: tuple[CandidateAssessment, ...] = Field(min_length=1, max_length=3)
    decision_point: Text
    elapsed_seconds: NonnegativeNumber | None
    incumbent_id: Identifier | None = None


class ImprovementSearch(DecisionValue):
    """当前实际材料的模拟基线；实际就绪与正式轮次仍由宿主校验。"""

    incumbent: SimulatedCandidate
    baseline_digest: Digest
    criterion_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=16)
    hypothesis: Text
    future_cost_estimate: TimeEstimate

    @model_validator(mode="after")
    def _check_ids(self) -> ImprovementSearch:
        _unique(self.criterion_ids, "improvement-criterion")
        return self


class ConditionalImprovement(DecisionValue):
    contract_digest: Digest
    baseline_digest: Digest
    incumbent_id: Identifier
    selected_id: Identifier
    changed_scope: tuple[Text, ...] = Field(min_length=1, max_length=64)
    criterion_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=16)
    future_cost_estimate: TimeEstimate
    proposed_at_ms: Annotated[int, Field(strict=True, ge=0)]
    expires_at_ms: Annotated[int, Field(strict=True, ge=0)]


class SimulationImprovementDecision(DecisionValue):
    selection: SimulationSelection
    selected_id: Identifier | None
    criterion_ids: tuple[Identifier, ...] = ()
    stop_reason: (
        Literal[
            "simulated_targets_met",
            "no_supported_improvement",
            "comparison_inconclusive",
            "model_plan_not_feasible",
        ]
        | None
    ) = None
