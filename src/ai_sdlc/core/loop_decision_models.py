"""量化合同的纯值对象；不计费、不执行，也不授予 Close 权限。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)

Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)
]
Identifier = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
OracleKind = Literal["deterministic", "observed", "rubric"]
ResultStatus = Literal["PASS", "FAIL", "UNKNOWN"]


def _decimal_input(value: object) -> Decimal:
    # 已经经过 float 的输入无法恢复原始十进制，必须明确拒绝。
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("use an integer, Decimal, or exact decimal string, not float")
    if len(str(value)) > 128:
        raise ValueError("decimal input is too long")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal input") from exc
    if not number.is_finite():
        raise ValueError("decimal input must be finite")
    if not number:
        return Decimal(0)
    # 只处理系数与指数，不能 normalize() 后再检查，否则低精度会放过非法值。
    _sign, coefficient, exponent = number.as_tuple()
    digits = list(coefficient)
    assert isinstance(exponent, int)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    places = max(-exponent, 0)
    total_digits = max(len(digits) + max(exponent, 0), places)
    if total_digits > 18 or places > 9:
        raise ValueError("decimal input exceeds 18 digits or 9 decimal places")
    return number


Number = Annotated[
    Decimal,
    BeforeValidator(_decimal_input),
    Field(allow_inf_nan=False),
]
PositiveNumber = Annotated[Number, Field(gt=0)]
NonnegativeNumber = Annotated[Number, Field(ge=0)]


class DecisionValue(BaseModel):
    """继承原生 JSON 校验；十进制小数须用字符串，整数可用数值 token。"""

    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always"
    )


class Goal(DecisionValue):
    id: Identifier
    source_ref: Text
    weight: PositiveNumber = Decimal(1)


class Obligation(DecisionValue):
    id: Identifier
    goal_id: Identifier
    statement: Text
    required: StrictBool
    source_ref: Text
    oracle_kind: OracleKind
    pass_rule: Text
    fail_rule: Text
    unknown_rule: Text
    weight_share: Annotated[PositiveNumber, Field(le=1)]


class GoalContract(DecisionValue):
    goals: tuple[Goal, ...] = Field(min_length=1, max_length=128)
    obligations: tuple[Obligation, ...] = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def _validate_group_weights(self) -> GoalContract:
        goal_ids = {goal.id for goal in self.goals}
        if len(goal_ids) != len(self.goals):
            raise ValueError("goal ids must be unique")
        if len({item.id for item in self.obligations}) != len(self.obligations):
            raise ValueError("obligation ids must be unique")
        shares = dict.fromkeys(goal_ids, Fraction(0))
        for item in self.obligations:
            if item.goal_id not in shares:
                raise ValueError("obligation references an unknown goal")
            shares[item.goal_id] += Fraction(item.weight_share)
        if any(total != 1 for total in shares.values()):
            raise ValueError("every goal must have obligation shares totaling one")
        return self


class ObligationResult(DecisionValue):
    id: Identifier
    status: ResultStatus
    evidence_refs: tuple[Text, ...] = Field(default=(), max_length=64)
    reason: Text

    @model_validator(mode="after")
    def _require_evidence(self) -> ObligationResult:
        if self.status != "UNKNOWN" and not self.evidence_refs:
            raise ValueError("PASS and FAIL require evidence references")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("evidence references must be unique")
        return self


class EvaluationInput(DecisionValue):
    """已判定的原始材料；引用的真实性仍由外围评审核对。"""

    contract: GoalContract
    artifact_digest: Digest
    results: tuple[ObligationResult, ...] = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def _require_exact_coverage(self) -> EvaluationInput:
        result_ids = {result.id for result in self.results}
        if len(result_ids) != len(self.results):
            raise ValueError("result ids must be unique")
        if result_ids != {item.id for item in self.contract.obligations}:
            raise ValueError("results must cover exactly the contract obligations")
        return self


class TimeEstimate(DecisionValue):
    """同一决策时点到路线验收完成的预测，不是实际用量。"""

    lower_seconds: NonnegativeNumber
    upper_seconds: NonnegativeNumber
    scope: Text
    basis_refs: tuple[Text, ...] = Field(min_length=1, max_length=64)
    assumptions: tuple[Text, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _ordered_interval(self) -> TimeEstimate:
        if self.lower_seconds > self.upper_seconds:
            raise ValueError("time estimate lower bound exceeds upper bound")
        return self


class Evaluation(DecisionValue):
    """精确聚合结果；Fraction 序列化保留分数，不充当通过凭据。"""

    contract_digest: Digest
    artifact_digest: Digest
    baseline_artifact_digest: Digest | None = None
    results: tuple[ObligationResult, ...]
    h: int = Field(ge=0)
    q: Fraction
    u: Fraction
    f: Fraction
    j: Fraction
    rf: Fraction | None = None
    ru: Fraction | None = None
    delta_q: Fraction | None = None

    @field_validator("h", "q", "u", "f", "j", "rf", "ru", "delta_q", mode="before")
    @classmethod
    def _reject_inexact_numbers(cls, value: object) -> object:
        # bool 不是计数；经过 float 的值也无法恢复原始十进制精度。
        if isinstance(value, (bool, float)):
            raise ValueError(
                "boolean and float values are not exact evaluation numbers"
            )
        return value


class ImplementationDecisionInput(DecisionValue):
    """B1 只消费原始义务；修复就绪须由外围独立判断，不缺省授予。"""

    current: EvaluationInput
    round_number: int = Field(strict=True, ge=1, le=2)
    has_actionable_findings: StrictBool
    repair_readiness: ResultStatus = "UNKNOWN"


class ImplementationDecision(DecisionValue):
    """停止修改或准入一次必要修复的建议，不是既有 Close 的替代。"""

    action: Literal["stop", "repair", "blocked"]
    reason: Literal[
        "requirements-satisfied",
        "review-round-limit",
        "repair-unavailable",
        "required-gap",
        "actionable-findings",
    ]


class MeasurementConditions(DecisionValue):
    """同单位还不够；总体、测量程序和环境也必须一致。"""

    unit: Text
    population: Text
    procedure: Text
    environment: Text


class NativeMetric(DecisionValue):
    id: Identifier
    goal_id: Identifier
    conditions: MeasurementConditions
    direction: Literal["maximize", "minimize"]
    material_difference: NonnegativeNumber | None = None
    source_refs: tuple[Text, ...] = Field(min_length=1, max_length=64)

    @field_validator("source_refs")
    @classmethod
    def _unique_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("source references must be unique")
        return value


class RouteSelectionContract(DecisionValue):
    """只组合本次比较语义，不改变旧聚合合同，也不持久化执行状态。"""

    goal_contract: GoalContract
    metrics: tuple[NativeMetric, ...] = Field(default=(), max_length=16)
    ordered_metric_ids: tuple[Identifier, ...] = Field(default=(), max_length=16)
    decision_point: Text
    time_scope: Text
    source_refs: tuple[Text, ...] = Field(min_length=1, max_length=64)
    baseline_id: Identifier | None = None

    @model_validator(mode="after")
    def _validate_references(self) -> RouteSelectionContract:
        metric_ids = {metric.id for metric in self.metrics}
        if len(metric_ids) != len(self.metrics):
            raise ValueError("metric ids must be unique")
        if set(self.ordered_metric_ids) != metric_ids or len(
            self.ordered_metric_ids
        ) != len(metric_ids):
            raise ValueError("metric order must cover exactly the contract metrics")
        goals = {goal.id for goal in self.goal_contract.goals}
        if any(metric.goal_id not in goals for metric in self.metrics):
            raise ValueError("metric references an unknown goal")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("source references must be unique")
        return self


class MetricEstimate(DecisionValue):
    """预测与实测分开；未知不能携带伪装成测量的数值。"""

    id: Identifier
    kind: Literal["actual", "forecast", "unknown"]
    conditions: MeasurementConditions
    lower: Number | None = None
    upper: Number | None = None
    evidence_refs: tuple[Text, ...] = Field(default=(), max_length=64)
    reason: Text
    assumptions: tuple[Text, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def _validate_measurement(self) -> MetricEstimate:
        if self.kind == "unknown":
            if self.lower is not None or self.upper is not None:
                raise ValueError("unknown metrics cannot contain measured values")
        elif (
            self.lower is None
            or self.upper is None
            or self.lower > self.upper
            or not self.evidence_refs
        ):
            raise ValueError("known metrics require ordered bounds and evidence")
        if self.kind == "forecast" and not self.assumptions:
            raise ValueError("forecasts require explicit assumptions")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("evidence references must be unique")
        return self


class ExecutionPrecondition(DecisionValue):
    """校验已提供的前提声明；声明与选择结果本身都不授予执行权限。"""

    kind: Literal["authorization", "safety", "mandatory_constraints"]
    status: ResultStatus
    evidence_refs: tuple[Text, ...] = Field(default=(), max_length=64)
    reason: Text

    @model_validator(mode="after")
    def _require_evidence(self) -> ExecutionPrecondition:
        if self.status != "UNKNOWN" and not self.evidence_refs:
            raise ValueError("PASS and FAIL preconditions require evidence")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("evidence references must be unique")
        return self


class ObligationChange(DecisionValue):
    id: Identifier
    expectation: Text


class RouteCandidate(DecisionValue):
    id: Identifier
    contract_digest: Digest
    mechanism: Text
    changed_scope: tuple[Text, ...] = Field(min_length=1, max_length=64)
    expected_obligation_changes: tuple[ObligationChange, ...] = Field(
        default=(), max_length=2048
    )
    evidence_refs: tuple[Text, ...] = Field(min_length=1, max_length=64)
    assumptions: tuple[Text, ...] = Field(min_length=1, max_length=64)
    execution_preconditions: tuple[ExecutionPrecondition, ...] = Field(
        min_length=3, max_length=3
    )
    metric_estimates: tuple[MetricEstimate, ...] = Field(default=(), max_length=16)
    cost_decision_point: Text
    future_cost_estimate: TimeEstimate | None = None

    @model_validator(mode="after")
    def _unique_candidate_parts(self) -> RouteCandidate:
        expected = {"authorization", "safety", "mandatory_constraints"}
        if {row.kind for row in self.execution_preconditions} != expected:
            raise ValueError("all three distinct execution preconditions are required")
        for rows in (self.metric_estimates, self.expected_obligation_changes):
            if len({row.id for row in rows}) != len(rows):
                raise ValueError("candidate result and obligation ids must be unique")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("evidence references must be unique")
        return self


class RouteExclusion(DecisionValue):
    candidate_id: Identifier
    reason: Literal[
        "precondition_not_passed",
        "dominated",
        "quality_preference",
        "future_time_preference",
    ]
    witness_id: Identifier | None = None
    metric_id: Identifier | None = None


class Selection(DecisionValue):
    """当前合同下的路线推荐，不是实际交付结果或 Close 凭据。"""

    contract_digest: Digest
    selected_id: Identifier | None
    basis: (
        Literal["evidence_advantage", "forecast_preference", "unresolved_tie_fallback"]
        | None
    )
    reason: Literal[
        "no_safe_route",
        "single_feasible_route",
        "quality_preference",
        "future_time_preference",
        "baseline_tie_break",
        "stable_id_tie_break",
    ]
    eligible_ids: tuple[Identifier, ...]
    quality_survivors: tuple[Identifier, ...]
    remaining_ids: tuple[Identifier, ...]
    excluded: tuple[RouteExclusion, ...]
    limitations: tuple[Text, ...] = ()


class DecisionSource(DecisionValue):
    """来源字节与语义定位；摘要不能替代独立专家的命题核对。"""

    id: Identifier
    path: Text
    sha256: Digest
    locator: Text
    claim: Text

    @field_validator("path")
    @classmethod
    def _canonical_relative_path(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or ":" in value
            or any(part in {"", ".", "..", ".git"} for part in value.split("/"))
        ):
            raise ValueError("decision-source-path-invalid")
        return value


class DecisionPrepareInput(DecisionValue):
    route_contract: RouteSelectionContract
    candidates: tuple[RouteCandidate, ...] = Field(min_length=1, max_length=3)
    sources: tuple[DecisionSource, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _complete_sources(self) -> DecisionPrepareInput:
        source_ids = {source.id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("decision-source-ids-duplicate")

        def references(value: object) -> set[str]:
            found: set[str] = set()
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "source_ref":
                        found.add(item)
                    elif key in {"source_refs", "evidence_refs", "basis_refs"}:
                        found.update(item)
                    else:
                        found.update(references(item))
            elif isinstance(value, (tuple, list)):
                for item in value:
                    found.update(references(item))
            return found

        used = references(self.route_contract.model_dump())
        used.update(references([item.model_dump() for item in self.candidates]))
        if used != source_ids:
            raise ValueError("decision-sources-must-cover-exactly-references")
        return self


class DecisionContext(DecisionPrepareInput):
    schema_version: Literal["implementation-b1"] = "implementation-b1"
    capability: Literal["implementation-b1"] = "implementation-b1"
    loop_id: Identifier
    implementation_input_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    selection: Selection
    context_digest: Digest


class DecisionPreparation(DecisionValue):
    status: Literal["preview", "prepared", "existing", "no-safe-route"]
    prepare_digest: Digest
    context: DecisionContext | None
    selection: Selection


class B1RepairReadiness(DecisionValue):
    """三项缺一不可；只有独立专家的同次证据能支持必要修复。"""

    authorization: ResultStatus = "UNKNOWN"
    facts: ResultStatus = "UNKNOWN"
    verification: ResultStatus = "UNKNOWN"
    evidence_refs: tuple[Text, ...] = Field(default=(), max_length=64)
    reason: Text

    @model_validator(mode="after")
    def _require_readiness_evidence(self) -> B1RepairReadiness:
        if (
            any(
                value != "UNKNOWN"
                for value in (self.authorization, self.facts, self.verification)
            )
            and not self.evidence_refs
        ):
            raise ValueError("decision-repair-readiness-evidence-missing")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("decision-repair-readiness-refs-duplicate")
        return self


class B1Assessment(DecisionValue):
    """单位专家的逐义务实际判断；不接受作者聚合分或权限覆盖。"""

    input_digest: Digest
    context_digest: Digest
    selected_route_id: Identifier
    results: tuple[ObligationResult, ...] = Field(min_length=1, max_length=2048)
    evidence: tuple[DecisionSource, ...] = ()
    repair_readiness: B1RepairReadiness

    @model_validator(mode="after")
    def _validate_local_references(self) -> B1Assessment:
        if len({result.id for result in self.results}) != len(self.results):
            raise ValueError("decision-assessment-obligations-duplicate")
        evidence_ids = {source.id for source in self.evidence}
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("decision-assessment-evidence-duplicate")
        references = set(self.repair_readiness.evidence_refs)
        for result in self.results:
            references.update(result.evidence_refs)
        if references != evidence_ids:
            raise ValueError("decision-assessment-evidence-coverage")
        return self


class B1ReviewData(DecisionValue):
    """既有 outcome 内的可复算量化区；历史索引不冒充外部署名证明。"""

    input_digest: Digest
    context_digest: Digest
    selected_route_id: Identifier
    manifest: dict[str, Digest] = Field(min_length=1)
    assessments: dict[str, B1Assessment] = Field(min_length=1, max_length=2)
    evaluation: Evaluation
    decision: ImplementationDecision

    @field_validator("manifest")
    @classmethod
    def _safe_manifest_paths(cls, value: dict[str, str]) -> dict[str, str]:
        for path in value:
            if DecisionSource._canonical_relative_path(path) != path:
                raise ValueError("decision-manifest-path-invalid")
        return value

    @field_validator("assessments")
    @classmethod
    def _exact_roles(cls, value: dict[str, B1Assessment]) -> dict[str, B1Assessment]:
        if any(not role or role.strip() != role for role in value):
            raise ValueError("decision-assessment-role-invalid")
        return value
