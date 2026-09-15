"""反例合同与实际观察的值对象；不执行命令、不创建预算或 Close。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

VERIFICATION_CAPABILITY: Literal["counterexample-acceptance-v1"] = (
    "counterexample-acceptance-v1"
)
Identifier = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Text = Annotated[StrictStr, Field(min_length=1)]
Digest = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
Stage = Literal[
    "requirement",
    "design-contract",
    "implementation",
    "frontend-evidence",
    "local-pr-review",
]
SubjectRole = Literal["current", "variant", "positive_control"]
ValueKind = Literal[
    "null", "boolean", "integer", "decimal", "string", "array", "object"
]
ObservationStatus = Literal[
    "collected", "unavailable", "parse_error", "infrastructure_error"
]
Phase = Literal["exploration", "repair", "final", "r2", "cleanup"]
ResultStatus = Literal["PASS", "FAIL", "UNKNOWN"]
ValidityStatus = Literal["valid", "invalid", "unknown", "stale", "not_applicable"]
AcceptanceStatus = Literal[
    "detected", "missed", "accepted", "false_rejection", "unknown", "not_executed"
]
Disposition = Literal["adopt_v1", "retain_v0"]
STAGE_ORDER = {
    name: index
    for index, name in enumerate(
        (
            "requirement",
            "design-contract",
            "implementation",
            "frontend-evidence",
            "local-pr-review",
        )
    )
}


class CounterexampleValue(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, revalidate_instances="always"
    )

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def _strict_schema(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("counterexample-schema-version-unsupported")
        return value


def project_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or ":" in value
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise ValueError("counterexample-path-must-be-project-relative")
    return value


def counterexample_digest(value: BaseModel | Mapping[str, Any]) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def source_digest_sha256(value: str) -> str:
    """原质量命令使用带算法前缀的摘要；新工件字段统一保存裸 SHA256。"""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("counterexample-quality-source-digest-invalid")
    return value.removeprefix("sha256:")


def canonical_decimal(value: str) -> str:
    """只规整十进制文本，避免 Decimal 上下文精度和 float 造成舍入。"""
    if not isinstance(value, str) or not re.fullmatch(
        r"-?\d+(?:\.\d+)?", value, flags=re.ASCII
    ):
        raise ValueError("counterexample-decimal-requires-exact-text")
    sign = "-" if value.startswith("-") else ""
    integer, _, fraction = value.lstrip("-").partition(".")
    integer, fraction = integer.lstrip("0") or "0", fraction.rstrip("0")
    if integer == "0" and not fraction:
        sign = ""
    return sign + integer + ("." + fraction if fraction else "")


class TypedValue(CounterexampleValue):
    type: ValueKind
    value: Any

    @model_validator(mode="after")
    def _typed_value(self) -> Self:
        actual = self.value
        valid = {
            "null": actual is None,
            "boolean": type(actual) is bool,
            "integer": type(actual) is int,
            "string": type(actual) is str,
        }
        if self.type in valid and not valid[self.type]:
            raise ValueError("counterexample-business-value-type-mismatch")
        if self.type == "decimal" and (
            type(actual) is not str or canonical_decimal(actual) != actual
        ):
            raise ValueError("counterexample-decimal-not-canonical")
        if self.type == "array":
            if type(actual) not in (list, tuple):
                raise ValueError("counterexample-array-required")
            object.__setattr__(
                self, "value", tuple(TypedValue.model_validate(item) for item in actual)
            )
        if self.type == "object":
            if type(actual) is not dict or any(type(key) is not str for key in actual):
                raise ValueError("counterexample-object-required")
            object.__setattr__(
                self,
                "value",
                {key: TypedValue.model_validate(item) for key, item in actual.items()},
            )
        return self

    @classmethod
    def from_python(cls, value: Any) -> TypedValue:
        kinds: dict[type, ValueKind] = {
            type(None): "null",
            bool: "boolean",
            int: "integer",
            str: "string",
            list: "array",
            tuple: "array",
            dict: "object",
        }
        kind = kinds.get(type(value))
        if kind is None:
            raise ValueError("counterexample-value-requires-explicit-exact-type")
        if kind == "array":
            value = [cls.from_python(item) for item in value]
        elif kind == "object":
            value = {key: cls.from_python(item) for key, item in value.items()}
        return cls(type=kind, value=value)


class ArtifactRef(CounterexampleValue):
    path: Text
    sha256: Digest

    _path = field_validator("path")(project_relative_path)


class SourceReference(ArtifactRef):
    id: Identifier
    namespace: Literal["requirement", "spec", "task"]
    locator: Text
    entry_sha256: Digest
    loop_id: str = ""
    profile_id: str = ""
    goal_id: str = ""
    obligation_id: str = ""
    criterion_id: str = ""
    task_id: str = ""

    @model_validator(mode="after")
    def _namespace(self) -> Self:
        if self.namespace == "requirement":
            if (
                not all(
                    (self.loop_id, self.profile_id, self.goal_id, self.obligation_id)
                )
                or self.task_id
            ):
                raise ValueError(
                    "counterexample-original-requirement-identity-required"
                )
            # 跨平台拒绝阶段目录的大小写别名，保留原路径用于身份绑定。
            source_path = self.path.casefold()
            if "/implementation/" in source_path or "/design-contract/" in source_path:
                raise ValueError("counterexample-future-stage-source-forbidden")
        elif any(
            (
                self.loop_id,
                self.profile_id,
                self.goal_id,
                self.obligation_id,
                self.criterion_id,
            )
        ):
            raise ValueError("counterexample-source-namespace-conflict")
        if self.namespace == "task" and not self.task_id:
            raise ValueError("counterexample-original-task-id-required")
        return self


class ValueDomain(CounterexampleValue):
    type: ValueKind
    allowed_values: tuple[TypedValue, ...]
    required_fields: tuple[Text, ...]

    @model_validator(mode="after")
    def _domain(self) -> Self:
        if self.required_fields and self.type != "object":
            raise ValueError("counterexample-domain-fields-require-object")
        if any(item.type != self.type for item in self.allowed_values):
            raise ValueError("counterexample-domain-value-type-conflict")
        _unique(self.required_fields, "domain-fields")
        return self


class OraclePremise(CounterexampleValue):
    id: Identifier
    source_id: Identifier
    expected: TypedValue


class ObservationMethod(CounterexampleValue):
    kind: Literal["file_json", "process_json", "sqlite_json"]
    location: Text
    resource_id: Identifier
    independent: StrictBool

    @model_validator(mode="after")
    def _independent(self) -> Self:
        if not self.independent:
            raise ValueError("counterexample-independent-observation-required")
        return self


class OracleSpec(CounterexampleValue):
    assertion_id: Identifier
    source_ids: tuple[Identifier, ...] = Field(min_length=1)
    basis: Literal["explicit", "relation", "reference", "semantic_only"]
    legal_input_domain: ValueDomain
    prerequisites: tuple[OraclePremise, ...]
    relation: Literal["typed_equal", "projected_multiset_equal", "unsupported"]
    expected: TypedValue
    projection: tuple[Text, ...]
    allowed_changes: tuple[Text, ...]
    observation_method: ObservationMethod
    verification_source_ids: tuple[Identifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _oracle(self) -> Self:
        _unique((p.id for p in self.prerequisites), "premises")
        _unique(self.source_ids, "oracle-sources")
        _unique(self.verification_source_ids, "verification-sources")
        _unique(self.projection, "projection-fields")
        if self.relation == "projected_multiset_equal":
            if not self.projection or self.expected.type != "array":
                raise ValueError(
                    "counterexample-multiset-requires-array-and-projection"
                )
            if any(
                item.type != "object" or not set(self.projection) <= item.value.keys()
                for item in self.expected.value
            ):
                raise ValueError("counterexample-expected-projection-incomplete")
        elif self.projection:
            raise ValueError("counterexample-projection-only-for-multiset")
        return self


class MechanismSpec(CounterexampleValue):
    key: Identifier
    hypothesis: Text
    source_ids: tuple[Identifier, ...] = Field(min_length=1)


class VerificationObligation(CounterexampleValue):
    id: Identifier
    source_id: Identifier
    required: StrictBool
    applicable: StrictBool
    selected: StrictBool
    applicability_reason: Text
    selection_reason: Text
    producer_stage: Stage
    consumer_stages: tuple[Stage, ...] = Field(min_length=1)
    close_owner: Stage
    oracle_spec: OracleSpec
    mechanisms: tuple[MechanismSpec, ...] = Field(max_length=2)

    @model_validator(mode="after")
    def _stages(self) -> Self:
        if self.selected and not self.applicable:
            raise ValueError("counterexample-selected-obligation-not-applicable")
        if self.selected != bool(self.mechanisms):
            raise ValueError("counterexample-selected-mechanisms-required")
        _unique((item.key for item in self.mechanisms), "mechanisms")
        _unique(self.consumer_stages, "consumer-stages")
        producer = STAGE_ORDER[self.producer_stage]
        if (
            self.producer_stage != "implementation"
            or self.close_owner != "implementation"
            or any(STAGE_ORDER[stage] < producer for stage in self.consumer_stages)
        ):
            raise ValueError(
                "counterexample-stage-dependency-cycle-or-unsupported-owner"
            )
        if self.close_owner not in self.consumer_stages:
            raise ValueError("counterexample-close-owner-must-consume")
        return self


class ResourcePermission(CounterexampleValue):
    id: Identifier
    kind: Literal["directory", "file", "sqlite", "service"]
    scope: Text
    actions: tuple[Literal["read", "write", "execute", "cleanup"], ...] = Field(
        min_length=1
    )
    isolation: Literal["owned_directory", "owned_namespace"]

    _scope = field_validator("scope")(project_relative_path)


class VerificationContract(CounterexampleValue):
    schema_version: Literal[1] = 1
    verification_capability: Literal["counterexample-acceptance-v1"] = (
        VERIFICATION_CAPABILITY
    )
    work_item_id: Identifier
    sources: tuple[SourceReference, ...] = Field(min_length=1)
    obligations: tuple[VerificationObligation, ...] = Field(min_length=1)
    resource_permissions: tuple[ResourcePermission, ...] = Field(min_length=1)
    budget_ref: ArtifactRef
    selection_policy: Literal["original-criticality-then-unverified-then-id"]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("counterexample-schema-version-unsupported")
        return value

    @model_validator(mode="after")
    def _references(self) -> Self:
        _unique((s.id for s in self.sources), "sources")
        _unique((o.id for o in self.obligations), "obligations")
        _unique((o.oracle_spec.assertion_id for o in self.obligations), "assertions")
        _unique((r.id for r in self.resource_permissions), "resource-permissions")
        if sum(o.selected for o in self.obligations) > 3:
            raise ValueError("counterexample-selected-obligation-limit")
        ids = {s.id for s in self.sources}
        for obligation in self.obligations:
            oracle = obligation.oracle_spec
            references = {
                obligation.source_id,
                *oracle.source_ids,
                *oracle.verification_source_ids,
                *(p.source_id for p in oracle.prerequisites),
                *(
                    source
                    for mechanism in obligation.mechanisms
                    for source in mechanism.source_ids
                ),
            }
            if not references <= ids:
                raise ValueError("counterexample-source-reference-not-closed")
            if obligation.required and not obligation.selected:
                raise ValueError("counterexample-required-obligation-must-be-selected")
            if oracle.observation_method.resource_id not in {
                r.id for r in self.resource_permissions
            }:
                raise ValueError("counterexample-observation-resource-not-authorized")
        obligation_task_owners(self)
        return self


def obligation_task_owners(
    contract: VerificationContract, *, task_ids: Iterable[str] | None = None
) -> dict[str, str | None]:
    """验收负责人的唯一来源是原任务条目；无归属旧合同仅兼容真实单任务。"""
    sources = {source.id: source for source in contract.sources}
    owners: dict[str, str | None] = {}
    for obligation in contract.obligations:
        if not obligation.selected:
            continue
        declared = {
            sources[source_id].task_id
            for source_id in obligation.oracle_spec.verification_source_ids
            if sources[source_id].namespace == "task"
        }
        if len(declared) > 1:
            raise ValueError("counterexample-obligation-task-owner-ambiguous")
        owners[obligation.id] = next(iter(declared), None)
    if any(owners.values()) and any(owner is None for owner in owners.values()):
        raise ValueError("counterexample-obligation-task-owner-missing")
    if task_ids is None:
        # 纯值校验保留旧序列化；生产入口必须另传原冻结的完整任务集合。
        return owners
    known_tasks = tuple(task_ids)
    _unique(known_tasks, "frozen-task-ids")
    if not known_tasks or any(not task_id.strip() for task_id in known_tasks):
        raise ValueError("counterexample-original-task-set-missing")
    if owners and not any(owners.values()):
        if len(known_tasks) != 1:
            raise ValueError("counterexample-obligation-task-owner-required")
        return {obligation_id: known_tasks[0] for obligation_id in owners}
    if any(owner not in known_tasks for owner in owners.values()):
        raise ValueError("counterexample-obligation-task-owner-not-in-frozen-tasks")
    return owners


def plan_obligations(
    contract: VerificationContract,
    task_id: str,
    *,
    task_ids: Iterable[str] | None = None,
) -> tuple[VerificationObligation, ...]:
    """只取本任务的冻结义务；未绑定旧纯值仍按全表做结构检查。"""
    owners = obligation_task_owners(contract, task_ids=task_ids)
    return tuple(
        obligation
        for obligation in contract.obligations
        if obligation.selected and owners[obligation.id] in {None, task_id}
    )


class Witness(CounterexampleValue):
    id: Identifier
    input_ref: ArtifactRef
    input_value: TypedValue
    premise_values: dict[str, TypedValue]


class ResourceBinding(CounterexampleValue):
    id: Identifier
    permission_id: Identifier
    kind: Literal["directory", "file", "sqlite", "service"]
    root: Text
    initial_state: ArtifactRef
    observation_endpoint: Text
    cleanup_method: Literal["owned_directory", "owned_namespace"]


class WitnessInputBinding(CounterexampleValue):
    witness_id: Identifier
    argv_index: Annotated[StrictInt, Field(ge=1)]
    encoding: Literal["text", "json"]


class ExecutionBinding(CounterexampleValue):
    project_root: Text
    cwd: Text
    argv: tuple[StrictStr, ...] = Field(min_length=1)
    witness_input: WitnessInputBinding | None = None
    effective_environment: dict[StrictStr, StrictStr]
    environment_digest: Digest
    resources: tuple[ResourceBinding, ...] = Field(min_length=1)
    timeout_seconds: Annotated[StrictInt, Field(gt=0)]
    max_output_bytes: Annotated[StrictInt, Field(gt=0)]

    @model_validator(mode="after")
    def _binding(self) -> Self:
        if not self.argv[0] or any(
            "\x00" in value
            for value in (
                *self.argv,
                *self.effective_environment.keys(),
                *self.effective_environment.values(),
            )
        ):
            raise ValueError("counterexample-command-invalid")
        if counterexample_digest(self.effective_environment) != self.environment_digest:
            raise ValueError("counterexample-environment-digest-mismatch")
        _unique((r.id for r in self.resources), "bound-resources")
        return self


class CounterexampleSubject(CounterexampleValue):
    id: Identifier
    role: SubjectRole
    obligation_id: Identifier
    witness_id: Identifier
    candidate_digest: Digest
    parent_candidate_digest: Digest
    snapshot: ArtifactRef
    patch: ArtifactRef | None
    modified_paths: tuple[Text, ...]
    mechanism_key: str
    positive_control_refs: tuple[Identifier, ...]

    @model_validator(mode="after")
    def _role(self) -> Self:
        for path in self.modified_paths:
            project_relative_path(path)
        if self.role == "variant":
            if (
                not self.mechanism_key
                or self.patch is None
                or not self.modified_paths
                or not self.positive_control_refs
                or self.candidate_digest == self.parent_candidate_digest
            ):
                raise ValueError("counterexample-variant-binding-incomplete")
        elif self.mechanism_key or self.positive_control_refs:
            raise ValueError("counterexample-subject-role-conflict")
        if self.role == "current" and (
            self.patch is not None
            or self.modified_paths
            or self.candidate_digest != self.parent_candidate_digest
        ):
            raise ValueError("counterexample-current-candidate-conflict")
        if (self.patch is None) != (not self.modified_paths):
            raise ValueError("counterexample-patch-paths-must-be-paired")
        return self


class ExecutionStep(CounterexampleValue):
    id: Identifier
    subject_id: Identifier
    assertion_id: Identifier
    acceptance_version: Literal["V0", "V1", "none"]
    kind: Literal["exercise", "observe", "acceptance", "reset", "cleanup"]
    phase: Phase
    binding: ExecutionBinding
    reservation_seconds: Annotated[StrictInt, Field(ge=0)]
    depends_on: tuple[Identifier, ...]
    business_observation_step_id: Identifier | None = None

    @model_validator(mode="after")
    def _acceptance(self) -> Self:
        if (self.kind == "acceptance") != (self.acceptance_version != "none"):
            raise ValueError("counterexample-step-acceptance-identity-conflict")
        if (self.kind == "acceptance") != (
            self.business_observation_step_id is not None
        ):
            raise ValueError("counterexample-acceptance-business-binding-required")
        return self


class CounterexamplePlan(CounterexampleValue):
    schema_version: Literal[1] = 1
    id: Identifier
    work_item_id: Identifier
    task_id: Identifier
    loop_id: Identifier
    contract_digest: Digest
    contract_model_digest: Digest
    candidate_digest: Digest
    v0_digest: Digest
    v1_digest: Digest | None
    budget_ref: ArtifactRef
    allowed_modified_paths: tuple[Text, ...]
    protected_paths: tuple[Text, ...] = Field(min_length=1)
    witnesses: tuple[Witness, ...] = Field(min_length=1)
    subjects: tuple[CounterexampleSubject, ...] = Field(min_length=1)
    steps: tuple[ExecutionStep, ...] = Field(min_length=1)
    max_execution_attempts: Annotated[StrictInt, Field(gt=0)]
    generation_batch: Literal[1]
    reinforcement_batch: Literal[0, 1]
    required_reserve_seconds: Annotated[StrictInt, Field(ge=0)]

    @field_validator("generation_batch", "reinforcement_batch", mode="before")
    @classmethod
    def _strict_batch(cls, value: Any) -> int:
        if type(value) is not int:
            raise ValueError("counterexample-batch-requires-integer")
        return value

    @model_validator(mode="after")
    def _plan(self) -> Self:
        _unique((w.id for w in self.witnesses), "witnesses")
        _unique((s.id for s in self.subjects), "subjects")
        _unique((s.id for s in self.steps), "steps")
        _unique(self.allowed_modified_paths, "allowed-paths")
        _unique(self.protected_paths, "protected-paths")
        for path in (*self.allowed_modified_paths, *self.protected_paths):
            project_relative_path(path)
        if set(self.allowed_modified_paths) & set(self.protected_paths):
            raise ValueError("counterexample-allowed-and-protected-scope-conflict")
        if self.max_execution_attempts < len(self.steps):
            raise ValueError("counterexample-execution-table-exceeds-attempt-limit")
        if self.reinforcement_batch != int(self.v1_digest is not None):
            raise ValueError("counterexample-reinforcement-identity-conflict")
        if sum(s.role == "variant" for s in self.subjects) > 6:
            raise ValueError("counterexample-variant-limit")
        subjects = {s.id: s for s in self.subjects}
        for subject in self.subjects:
            if (
                subject.witness_id not in {w.id for w in self.witnesses}
                or subject.parent_candidate_digest != self.candidate_digest
            ):
                raise ValueError("counterexample-subject-input-or-candidate-conflict")
            if any(
                path not in self.allowed_modified_paths or path in self.protected_paths
                for path in subject.modified_paths
            ):
                raise ValueError("counterexample-patch-outside-allowed-scope")
            for control_id in subject.positive_control_refs:
                control = subjects.get(control_id)
                if (
                    control is None
                    or control.role != "positive_control"
                    or control.obligation_id != subject.obligation_id
                ):
                    raise ValueError(
                        "counterexample-positive-control-reference-invalid"
                    )
        seen: set[str] = set()
        for step in self.steps:
            if step.subject_id not in subjects or not set(step.depends_on) <= seen:
                raise ValueError("counterexample-step-reference-or-order-invalid")
            if step.acceptance_version == "V1" and self.v1_digest is None:
                raise ValueError("counterexample-step-v1-missing")
            seen.add(step.id)
        validate_witness_execution_bindings(self)
        validate_business_observation_bindings(self)
        return self


class AttemptReceipt(CounterexampleValue):
    schema_version: Literal[1] = 1
    attempt_id: Identifier
    attempt_ref: ArtifactRef  # 指向启动前意图，不能引用含自身摘要的完成回执。
    plan_digest: Digest
    contract_digest: Digest
    candidate_digest: Digest
    step_id: Identifier
    subject_id: Identifier
    binding_digest: Digest
    ownership_nonce: Text
    status: Literal["completed", "execution_unknown", "infrastructure_error"]
    started_at_ms: Annotated[StrictInt, Field(ge=0)]
    ended_at_ms: Annotated[StrictInt, Field(ge=0)] | None
    exit_code: StrictInt | None
    timed_out: StrictBool
    output_truncated: StrictBool
    cleanup_status: Literal["complete", "incomplete", "unknown"]
    raw_evidence_refs: tuple[ArtifactRef, ...]

    @property
    def normally_completed(self) -> bool:
        """完成原件不等于正常业务完成；异常终止不得计为目标断言拒绝。"""
        return (
            self.status == "completed"
            and self.ended_at_ms is not None
            and self.ended_at_ms >= self.started_at_ms
            and self.exit_code is not None
            # 反例只接纳跨平台的 8 位正常退出码；其余原码保留为未知执行。
            # 原件可跨平台读取，不能按读取主机的 os.name 重新解释这项事实。
            and 0 <= self.exit_code <= 255
            and not self.timed_out
            and not self.output_truncated
            and self.cleanup_status == "complete"
        )


class ResourceStateEntry(CounterexampleValue):
    path: Text
    kind: Literal["file", "directory"]
    sha256: Digest | None
    mode: Annotated[StrictInt, Field(ge=0, le=0o7777)]

    @model_validator(mode="after")
    def _entry(self) -> Self:
        project_relative_path(self.path)
        if ".ai-sdlc-owner.json" in self.path.split("/"):
            raise ValueError("counterexample-resource-state-owner-entry-forbidden")
        if (self.kind == "file") != (self.sha256 is not None):
            raise ValueError("counterexample-resource-state-entry-kind-conflict")
        return self


class ResourceStateSnapshot(CounterexampleValue):
    resource_id: Identifier
    directory_mode: Annotated[StrictInt, Field(ge=0, le=0o7777)]
    files: tuple[ResourceStateEntry, ...]

    @model_validator(mode="after")
    def _entries(self) -> Self:
        paths = [entry.path for entry in self.files]
        if paths != sorted(set(paths)):
            raise ValueError("counterexample-resource-state-entries-not-canonical")
        return self


class ResourceStateEvidence(CounterexampleValue):
    schema_version: Literal[1] = 1
    plan_digest: Digest
    step_id: Identifier
    subject_id: Identifier
    attempt_id: Identifier
    binding_digest: Digest
    ownership_nonce: Text
    resources: tuple[ResourceBinding, ...]
    before: tuple[ResourceStateSnapshot, ...]
    after: tuple[ResourceStateSnapshot, ...]

    @model_validator(mode="after")
    def _resources(self) -> Self:
        identifiers = [resource.id for resource in self.resources]
        if identifiers != sorted(set(identifiers)) or not identifiers:
            raise ValueError("counterexample-resource-state-bindings-not-canonical")
        for snapshots in (self.before, self.after):
            if [snapshot.resource_id for snapshot in snapshots] != identifiers:
                raise ValueError("counterexample-resource-state-table-incomplete")
        return self


class OwnedRawEvidence(CounterexampleValue):
    ref: ArtifactRef
    attempt_id: Identifier
    ownership_nonce: Text
    complete: StrictBool
    # 内容由统一 resolver 读取并验摘要；纯核仍重新核对这些实际字节。
    content: StrictStr

    @model_validator(mode="after")
    def _digest(self) -> Self:
        if hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.ref.sha256:
            raise ValueError("counterexample-raw-content-digest-mismatch")
        return self


class BusinessObservation(CounterexampleValue):
    schema_version: Literal[1] = 1
    assertion_id: Identifier
    subject_id: Identifier
    subject_role: SubjectRole
    candidate_digest: Digest
    plan_digest: Digest
    attempt_id: Identifier
    step_id: Identifier
    witness_ref: ArtifactRef
    typed_actual: TypedValue | None
    raw_evidence_ref: ArtifactRef
    collection_method: ObservationMethod
    observation_status: ObservationStatus
    reason: str

    @model_validator(mode="after")
    def _collected(self) -> Self:
        if (self.observation_status == "collected") != (self.typed_actual is not None):
            raise ValueError("counterexample-collected-value-required-only-on-success")
        if self.observation_status != "collected" and not self.reason:
            raise ValueError("counterexample-unavailable-reason-required")
        return self


class AcceptanceObservation(CounterexampleValue):
    schema_version: Literal[1] = 1
    acceptance_version: Literal["V0", "V1"]
    acceptance_digest: Digest
    subject_id: Identifier
    subject_role: SubjectRole
    candidate_digest: Digest
    plan_digest: Digest
    attempt_id: Identifier
    step_id: Identifier
    assertion_id: Identifier
    reached: StrictBool
    assertion_result: Literal["accepted", "rejected", "unknown"]
    failure_reason: Literal[
        "none",
        "target_assertion",
        "unrelated_assertion",
        "compile_error",
        "infrastructure_error",
        "protocol_error",
        "not_reached",
    ]
    raw_evidence_ref: ArtifactRef


class RepairEvidence(CounterexampleValue):
    before_observation_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    repair_ref: ArtifactRef
    after_observation_refs: tuple[ArtifactRef, ...] = Field(min_length=1)


class ObservationBundle(CounterexampleValue):
    business: tuple[BusinessObservation, ...]
    acceptance: tuple[AcceptanceObservation, ...]
    attempts: tuple[AttemptReceipt, ...]
    raw_evidence: tuple[OwnedRawEvidence, ...]
    repairs: tuple[RepairEvidence, ...] = ()


class DimensionResult(CounterexampleValue):
    status: ResultStatus
    reasons: tuple[Text, ...]
    evidence_refs: tuple[ArtifactRef, ...]


class SubjectAssessment(CounterexampleValue):
    subject_id: Identifier
    obligation_id: Identifier
    business: DimensionResult
    validity: ValidityStatus
    v0: AcceptanceStatus
    v1: AcceptanceStatus
    reasons: tuple[Text, ...]
    evidence_refs: tuple[ArtifactRef, ...]


class CounterexampleAssessment(CounterexampleValue):
    schema_version: Literal[1] = 1
    contract_digest: Digest
    plan_digest: Digest
    candidate_digest: Digest
    current_result: DimensionResult
    variants: tuple[SubjectAssessment, ...]
    positive_controls: tuple[SubjectAssessment, ...]
    current_subjects: tuple[SubjectAssessment, ...]
    required_complete: StrictBool
    v1_disposition: Disposition
    reasons: tuple[Text, ...]
    unselected_obligation_ids: tuple[Identifier, ...]
    repairs: tuple[RepairEvidence, ...]


class BoundEvidence(CounterexampleValue):
    contract: VerificationContract
    plan: CounterexamplePlan
    observations: ObservationBundle
    assessment: CounterexampleAssessment
    artifact_refs: tuple[ArtifactRef, ...]
    missing: tuple[Text, ...]


class CounterexamplePhaseContext(CounterexampleValue):
    require_r2: StrictBool
    observed_at_ms: Annotated[StrictInt, Field(ge=0)]
    review_ref: ArtifactRef | None
    snapshot_ref: ArtifactRef | None

    @model_validator(mode="after")
    def _review_origin(self) -> Self:
        if (
            self.require_r2 or self.snapshot_ref is not None
        ) and self.review_ref is None:
            raise ValueError("counterexample-phase-review-origin-required")
        return self


class CounterexampleEvidenceRecord(CounterexampleValue):
    schema_version: Literal[1] = 1
    loop_id: Identifier
    task_id: Identifier
    contract_ref: ArtifactRef
    plan_ref: ArtifactRef
    observations_ref: ArtifactRef
    assessment_ref: ArtifactRef
    source_digest_before: Digest
    source_digest_after: Digest
    recorded_at_ms: Annotated[StrictInt, Field(ge=0)]
    phase_context: CounterexamplePhaseContext | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


def _unique(values: Iterable[str], label: str) -> None:
    values = tuple(values)
    if len(set(values)) != len(values):
        raise ValueError(f"counterexample-duplicate-{label}")


def validate_contract_sources(
    contract: VerificationContract,
    source_bytes: Mapping[str, bytes],
    source_entries: Mapping[str, Mapping[str, bytes]],
) -> None:
    """调用方持有稳定原文及按原生解析器定位的条目；此处只核验闭合身份。"""
    for source in contract.sources:
        raw = source_bytes.get(source.path)
        entry = source_entries.get(source.path, {}).get(source.locator)
        if raw is None or hashlib.sha256(raw).hexdigest() != source.sha256:
            raise ValueError("counterexample-original-source-missing-or-stale")
        if entry is None or hashlib.sha256(entry).hexdigest() != source.entry_sha256:
            raise ValueError("counterexample-original-entry-missing-or-stale")


def validate_plan_contract(
    contract: VerificationContract, plan: CounterexamplePlan
) -> None:
    validate_witness_execution_bindings(plan)
    validate_business_observation_bindings(plan)
    if (
        plan.work_item_id != contract.work_item_id
        or plan.contract_model_digest != counterexample_digest(contract)
        or plan.budget_ref != contract.budget_ref
    ):
        raise ValueError("counterexample-plan-contract-identity-mismatch")
    obligations = {o.id: o for o in plan_obligations(contract, plan.task_id)}
    permissions = {r.id: r for r in contract.resource_permissions}
    subjects = {s.id: s for s in plan.subjects}
    for subject in plan.subjects:
        obligation = obligations.get(subject.obligation_id)
        if obligation is None:
            raise ValueError("counterexample-subject-obligation-not-selected")
        if subject.role == "variant" and subject.mechanism_key not in {
            m.key for m in obligation.mechanisms
        }:
            raise ValueError("counterexample-unfrozen-mechanism")
    for obligation in obligations.values():
        group = [s for s in plan.subjects if s.obligation_id == obligation.id]
        if len([s for s in group if s.role == "current"]) != 1 or not any(
            s.role == "positive_control" for s in group
        ):
            raise ValueError("counterexample-current-and-positive-control-required")
        mechanisms = [s.mechanism_key for s in group if s.role == "variant"]
        if sorted(mechanisms) != sorted(m.key for m in obligation.mechanisms):
            raise ValueError("counterexample-mechanism-execution-table-incomplete")
    for step in plan.steps:
        obligation = obligations[subjects[step.subject_id].obligation_id]
        if step.assertion_id != obligation.oracle_spec.assertion_id:
            raise ValueError("counterexample-step-assertion-conflict")
        for resource in step.binding.resources:
            permission = permissions.get(resource.permission_id)
            if (
                permission is None
                or permission.kind != resource.kind
                or permission.isolation != resource.cleanup_method
            ):
                raise ValueError("counterexample-resource-permission-mismatch")
            action = {
                "observe": "read",
                "cleanup": "cleanup",
                "reset": "write",
                "exercise": "execute",
                "acceptance": "execute",
            }[step.kind]
            if action not in permission.actions:
                raise ValueError("counterexample-step-action-not-authorized")
        if obligation.oracle_spec.observation_method.resource_id not in {
            r.permission_id for r in step.binding.resources
        }:
            raise ValueError("counterexample-step-observation-resource-missing")
    for subject in plan.subjects:
        phases = (
            ("final", "r2")
            if any(step.phase == "r2" for step in plan.steps)
            else ("final",)
        )
        for phase in phases:
            steps = [
                step
                for step in plan.steps
                if step.subject_id == subject.id and step.phase == phase
            ]
            if (
                not any(step.kind == "observe" for step in steps)
                or not any(step.acceptance_version == "V0" for step in steps)
                or (
                    plan.v1_digest is not None
                    and not any(step.acceptance_version == "V1" for step in steps)
                )
            ):
                raise ValueError("counterexample-subject-execution-table-incomplete")
        # 每种条件组组合都需真实收尾；独立 cleanup 阶段也可以覆盖其依赖的使用。
        for active in (set(), {"repair"}, {"r2"}, {"repair", "r2"}):
            enabled = [
                step
                for step in plan.steps
                if step.phase not in {"repair", "r2"} or step.phase in active
            ]
            steps = [step for step in enabled if step.subject_id == subject.id]
            cleanups = [step for step in steps if step.kind == "cleanup"]
            if not cleanups:
                raise ValueError("counterexample-resource-terminal-cleanup-required")
            by_id = {step.id: step for step in plan.steps}
            pending = [
                identifier for cleanup in cleanups for identifier in cleanup.depends_on
            ]
            ordered = set()
            while pending:
                identifier = pending.pop()
                if identifier not in ordered:
                    ordered.add(identifier)
                    pending.extend(by_id[identifier].depends_on)
            if not {step.id for step in steps if step.kind != "cleanup"} <= ordered:
                raise ValueError("counterexample-resource-cleanup-order-incomplete")
            if not ordered <= {step.id for step in enabled}:
                raise ValueError(
                    "counterexample-resource-cleanup-condition-unavailable"
                )
        # 同一对象的运行、独立读回和验收必须指向同一组输入初态及资源。
        bindings = [
            step.binding.resources
            for step in plan.steps
            if step.subject_id == subject.id
        ]
        baseline = {resource.id: resource for resource in bindings[0]}
        if any(
            {resource.id: resource for resource in resources} != baseline
            for resources in bindings
        ):
            raise ValueError("counterexample-subject-resource-initial-state-conflict")


def witness_input_argument(value: TypedValue, encoding: Literal["text", "json"]) -> str:
    """绑定传给进程的完整参数；JSON 十进制不经过浮点，文本保留全部空白。"""
    if encoding == "text":
        if value.type != "string":
            raise ValueError("counterexample-witness-text-requires-string")
        return value.value
    if encoding != "json":
        raise ValueError("counterexample-witness-encoding-unsupported")
    if value.type == "decimal":
        return canonical_decimal(value.value)
    if value.type == "array":
        return (
            "[" + ",".join(witness_input_argument(v, "json") for v in value.value) + "]"
        )
    if value.type == "object":
        return (
            "{"
            + ",".join(
                json.dumps(key, ensure_ascii=False)
                + ":"
                + witness_input_argument(value.value[key], "json")
                for key in sorted(value.value)
            )
            + "}"
        )
    return json.dumps(value.value, ensure_ascii=False, allow_nan=False)


def validate_witness_execution_bindings(plan: CounterexamplePlan) -> None:
    """只读核对见证与实际 argv；参数传入不等于证明被测程序消费了参数。"""
    subjects = {subject.id: subject for subject in plan.subjects}
    witnesses = {witness.id: witness for witness in plan.witnesses}
    by_id = {step.id: step for step in plan.steps}
    positions = {step.id: index for index, step in enumerate(plan.steps)}
    for step in plan.steps:
        binding = step.binding.witness_input
        if binding is not None:
            witness = witnesses[subjects[step.subject_id].witness_id]
            if binding.witness_id != witness.id:
                raise ValueError("counterexample-witness-binding-identity-mismatch")
            if binding.argv_index >= len(step.binding.argv) or step.binding.argv[
                binding.argv_index
            ] != witness_input_argument(witness.input_value, binding.encoding):
                raise ValueError("counterexample-witness-argv-input-mismatch")
        if step.kind == "exercise" and binding is None:
            raise ValueError("counterexample-exercise-witness-binding-required")
        if step.kind != "observe" or binding is not None:
            continue
        # 读回可以沿同一对象、同一阶段的执行依赖继承输入，不能只凭计划标签归因。
        pending = list(step.depends_on)
        ancestors: set[str] = set()
        while pending:
            identifier = pending.pop()
            if identifier not in ancestors:
                ancestors.add(identifier)
                pending.extend(by_id[identifier].depends_on)
        exercises = [
            by_id[identifier]
            for identifier in ancestors
            if by_id[identifier].kind == "exercise"
            and by_id[identifier].subject_id == step.subject_id
            and by_id[identifier].phase == step.phase
            and by_id[identifier].binding.witness_input is not None
        ]
        # 每份观察资源分别沿最近的有效执行归因，无关资源的动作不遮蔽既有见证。
        for root in {resource.root for resource in step.binding.resources}:
            relevant = [
                exercise
                for exercise in exercises
                if root in {resource.root for resource in exercise.binding.resources}
            ]
            if not relevant:
                raise ValueError("counterexample-observation-witness-binding-required")
            exercised = max(relevant, key=lambda candidate: positions[candidate.id])
            # 中间实际执行的改写会切断归因，即使它不是本次读回的依赖祖先。
            if any(
                middle.kind in {"exercise", "reset", "cleanup"}
                and root in {resource.root for resource in middle.binding.resources}
                for middle in plan.steps[
                    positions[exercised.id] + 1 : positions[step.id]
                ]
            ):
                raise ValueError("counterexample-observation-witness-state-invalidated")


def validate_business_observation_bindings(plan: CounterexamplePlan) -> None:
    """验收关联精确读回步骤；实际资源是否只读仍须由运行原件证明。"""
    by_id = {step.id: step for step in plan.steps}
    positions = {step.id: index for index, step in enumerate(plan.steps)}
    for step in plan.steps:
        if step.kind != "acceptance":
            if step.business_observation_step_id is not None:
                raise ValueError("counterexample-nonacceptance-business-binding")
            continue
        observed = by_id.get(step.business_observation_step_id)
        if (
            observed is None
            or observed.kind != "observe"
            or observed.subject_id != step.subject_id
            or observed.assertion_id != step.assertion_id
            or observed.phase != step.phase
            or {r.id: r for r in observed.binding.resources}
            != {r.id: r for r in step.binding.resources}
            or positions[observed.id] >= positions[step.id]
        ):
            raise ValueError("counterexample-acceptance-business-binding-conflict")
        ancestors: set[str] = set()
        pending = list(step.depends_on)
        while pending:
            identifier = pending.pop()
            if identifier not in ancestors:
                if identifier not in by_id:
                    raise ValueError("counterexample-step-reference-or-order-invalid")
                ancestors.add(identifier)
                pending.extend(by_id[identifier].depends_on)
        if observed.id not in ancestors:
            raise ValueError(
                "counterexample-acceptance-observation-dependency-required"
            )
        roots = {resource.root for resource in step.binding.resources}
        if any(
            middle.kind in {"exercise", "reset", "cleanup"}
            and roots.intersection(
                resource.root for resource in middle.binding.resources
            )
            for middle in plan.steps[positions[observed.id] + 1 : positions[step.id]]
        ):
            raise ValueError("counterexample-acceptance-observed-state-invalidated")
    versions = {"V0", "V1"} if plan.v1_digest is not None else {"V0"}
    for observed in (step for step in plan.steps if step.kind == "observe"):
        if not versions <= {
            step.acceptance_version
            for step in plan.steps
            if step.business_observation_step_id == observed.id
        }:
            raise ValueError("counterexample-observation-acceptance-table-incomplete")


def required_execution_steps(
    plan: CounterexamplePlan,
    observations: ObservationBundle,
    *,
    require_r2: bool = False,
) -> tuple[ExecutionStep, ...]:
    """原流程决定是否进入 R2；条件组一旦实际启动，不能只消费其中有利步骤。"""
    steps = {step.id: step for step in plan.steps}
    started = {
        steps[attempt.step_id].phase
        for attempt in observations.attempts
        if attempt.step_id in steps
    }
    if require_r2:
        if not any(step.phase == "r2" for step in plan.steps):
            raise ValueError("counterexample-required-r2-execution-table-missing")
        started.add("r2")
    return tuple(
        step
        for step in plan.steps
        if step.phase not in ("repair", "r2") or step.phase in started
    )
