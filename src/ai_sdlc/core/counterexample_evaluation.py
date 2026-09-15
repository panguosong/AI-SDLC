"""从绑定的业务值和目标断言分别推导结果，不执行命令或解释自然语言。"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any, cast

from pydantic import ValidationError

from ai_sdlc.core.counterexample_models import (
    AcceptanceObservation,
    AcceptanceStatus,
    ArtifactRef,
    AttemptReceipt,
    BusinessObservation,
    CounterexampleAssessment,
    CounterexamplePlan,
    CounterexampleSubject,
    DimensionResult,
    Disposition,
    ExecutionStep,
    ObservationBundle,
    OracleSpec,
    ResourceStateEvidence,
    ResultStatus,
    SubjectAssessment,
    TypedValue,
    ValidityStatus,
    ValueDomain,
    VerificationContract,
    counterexample_digest,
    obligation_task_owners,
    required_execution_steps,
    validate_plan_contract,
)


def typed_equal(left: TypedValue, right: TypedValue) -> bool:
    if left.type != right.type:
        return False
    if left.type == "object":
        return left.value.keys() == right.value.keys() and all(
            typed_equal(value, right.value[key]) for key, value in left.value.items()
        )
    if left.type == "array":
        return len(left.value) == len(right.value) and all(
            typed_equal(a, b) for a, b in zip(left.value, right.value, strict=True)
        )
    return bool(left.value == right.value)


def domain_accepts(domain: ValueDomain, value: TypedValue) -> bool:
    return (
        value.type == domain.type
        and (
            not domain.required_fields
            or set(domain.required_fields) <= value.value.keys()
        )
        and (
            not domain.allowed_values
            or any(typed_equal(value, expected) for expected in domain.allowed_values)
        )
    )


def oracle_matches(oracle: OracleSpec, actual: TypedValue) -> bool | None:
    if oracle.basis == "semantic_only" or oracle.relation == "unsupported":
        return None
    if oracle.relation == "typed_equal":
        return typed_equal(actual, oracle.expected)
    if actual.type != "array":
        return False

    # 投影后按多重集合比较；缺字段是业务违约，不能通过去重掩盖重复行丢失。
    def project(value: TypedValue) -> Counter[str] | None:
        rows = []
        for item in value.value:
            if item.type != "object" or not set(oracle.projection) <= item.value.keys():
                return None
            rows.append(
                counterexample_digest(
                    {
                        key: item.value[key].model_dump(mode="json")
                        for key in oracle.projection
                    }
                )
            )
        return Counter(rows)

    observed, expected = project(actual), project(oracle.expected)
    return observed is not None and observed == expected


def _result(
    status: ResultStatus, reasons: Iterable[str] = (), refs: Iterable[ArtifactRef] = ()
) -> DimensionResult:
    return DimensionResult(
        status=status,
        reasons=tuple(dict.fromkeys(reasons)),
        evidence_refs=_deduplicate_refs(refs),
    )


def _deduplicate_refs(refs: Iterable[ArtifactRef]) -> tuple[ArtifactRef, ...]:
    return tuple({(ref.path, ref.sha256): ref for ref in refs}.values())


def _aggregate(results: list[DimensionResult]) -> DimensionResult:
    if not results:
        return _result("UNKNOWN", ("business-observation-missing",))
    status: ResultStatus = (
        "FAIL"
        if any(result.status == "FAIL" for result in results)
        else "UNKNOWN"
        if any(result.status == "UNKNOWN" for result in results)
        else "PASS"
    )
    return _result(
        status,
        tuple(reason for result in results for reason in result.reasons),
        tuple(ref for result in results for ref in result.evidence_refs),
    )


def _raw_payload(
    observation: BusinessObservation | AcceptanceObservation, bundle: ObservationBundle
) -> dict[str, Any] | None:
    matches = [
        raw
        for raw in bundle.raw_evidence
        if raw.ref == observation.raw_evidence_ref
        and raw.attempt_id == observation.attempt_id
    ]
    if len(matches) != 1:
        return None
    return _strict_payload(matches[0].content)


def _strict_payload(content: str) -> dict[str, Any] | None:
    try:

        def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate-json-key")
                result[key] = value
            return result

        payload = json.loads(
            content,
            object_pairs_hook=unique_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite-json")
            ),
        )
    except (ValueError, TypeError):
        return None
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
    ):
        return None
    return payload


def _resource_state_proof(
    plan: CounterexamplePlan,
    step: ExecutionStep,
    attempt: AttemptReceipt,
    bundle: ObservationBundle,
    *,
    plan_digest: str,
    binding_digests: Mapping[str, str],
) -> tuple[ResourceStateEvidence | None, list[str]]:
    """只消费原尝试目录的原始状态材料，摘要声明本身不能证明资源未变。"""
    path = attempt.attempt_ref.path.rsplit("/", 1)[0] + "/resource-state.json"
    rows = [raw for raw in bundle.raw_evidence if raw.ref.path == path]
    if (
        len(rows) != 1
        or rows[0].ref not in attempt.raw_evidence_refs
        or rows[0].attempt_id != attempt.attempt_id
        or rows[0].ownership_nonce != attempt.ownership_nonce
        or not rows[0].complete
    ):
        return None, ["resource-state-evidence-missing-or-not-owned"]
    try:
        proof = ResourceStateEvidence.model_validate(_strict_payload(rows[0].content))
    except (ValidationError, ValueError, TypeError):
        return None, ["resource-state-evidence-invalid"]
    if (
        proof.plan_digest != plan_digest
        or proof.step_id != step.id
        or proof.subject_id != step.subject_id
        or proof.attempt_id != attempt.attempt_id
        or proof.binding_digest != binding_digests[step.id]
        or proof.ownership_nonce != attempt.ownership_nonce
        or proof.resources != tuple(sorted(step.binding.resources, key=lambda r: r.id))
    ):
        return None, ["resource-state-evidence-binding-conflict"]
    if proof.before != proof.after:
        return proof, ["readonly-observation-or-acceptance-changed-resources"]
    return proof, []


def _binding_reasons(
    observation: BusinessObservation | AcceptanceObservation,
    subject: CounterexampleSubject,
    oracle: OracleSpec,
    plan: CounterexamplePlan,
    bundle: ObservationBundle,
    *,
    plan_digest: str,
    binding_digests: Mapping[str, str],
) -> list[str]:
    reasons = []
    if (
        observation.subject_id != subject.id
        or observation.subject_role != subject.role
        or observation.candidate_digest != subject.candidate_digest
        or observation.plan_digest != plan_digest
        or observation.assertion_id != oracle.assertion_id
    ):
        reasons.append("observation-identity-stale")
    steps = [step for step in plan.steps if step.id == observation.step_id]
    attempts = [
        attempt
        for attempt in bundle.attempts
        if attempt.attempt_id == observation.attempt_id
    ]
    if len(steps) != 1 or len(attempts) != 1:
        return reasons + ["observation-attempt-or-step-missing"]
    step, attempt = steps[0], attempts[0]
    if (
        step.subject_id != subject.id
        or attempt.step_id != step.id
        or attempt.subject_id != subject.id
        or attempt.plan_digest != plan_digest
        or attempt.contract_digest != plan.contract_digest
        or attempt.candidate_digest != subject.candidate_digest
        or attempt.binding_digest != binding_digests[step.id]
    ):
        reasons.append("attempt-identity-stale")
    if not attempt.normally_completed:
        reasons.append("attempt-incomplete-or-infrastructure-error")
    raw = [
        item
        for item in bundle.raw_evidence
        if item.ref == observation.raw_evidence_ref
        and item.attempt_id == attempt.attempt_id
    ]
    if (
        observation.raw_evidence_ref not in attempt.raw_evidence_refs
        or len(raw) != 1
        or not raw[0].complete
        or raw[0].ownership_nonce != attempt.ownership_nonce
    ):
        reasons.append("raw-evidence-not-owned-or-incomplete")
    if isinstance(observation, BusinessObservation):
        witness = next(w for w in plan.witnesses if w.id == subject.witness_id)
        if (
            step.kind != "observe"
            or observation.witness_ref != witness.input_ref
            or observation.collection_method != oracle.observation_method
        ):
            reasons.append("business-observation-binding-conflict")
        if attempt.exit_code != 0:
            reasons.append("business-observer-nonzero-exit")
    else:
        expected_digest = (
            plan.v0_digest if observation.acceptance_version == "V0" else plan.v1_digest
        )
        if (
            step.kind != "acceptance"
            or step.acceptance_version != observation.acceptance_version
            or observation.acceptance_digest != expected_digest
        ):
            reasons.append("acceptance-source-or-step-stale")
        if observation.assertion_result == "accepted" and attempt.exit_code != 0:
            reasons.append("acceptance-nonzero-without-target-rejection")
    _, state_reasons = _resource_state_proof(
        plan,
        step,
        attempt,
        bundle,
        plan_digest=plan_digest,
        binding_digests=binding_digests,
    )
    reasons.extend(state_reasons)
    by_id = {item.id: item for item in plan.steps}
    for dependency in step.depends_on:
        prior = [item for item in bundle.attempts if item.step_id == dependency]
        if (
            len(prior) != 1
            or not prior[0].normally_completed
            or prior[0].ended_at_ms is None
            or prior[0].ended_at_ms > attempt.started_at_ms
            or (by_id[dependency].kind != "acceptance" and prior[0].exit_code != 0)
        ):
            reasons.append("observation-dependency-not-completed-before-attempt")
    return reasons


def _business(
    subject: CounterexampleSubject,
    oracle: OracleSpec,
    plan: CounterexamplePlan,
    bundle: ObservationBundle,
    *,
    plan_digest: str,
    binding_digests: Mapping[str, str],
) -> DimensionResult:
    observations = [o for o in bundle.business if o.subject_id == subject.id]
    if not observations:
        return _result("UNKNOWN", ("business-observation-missing",))
    witness = next(w for w in plan.witnesses if w.id == subject.witness_id)
    legality = []
    if not domain_accepts(oracle.legal_input_domain, witness.input_value):
        legality.append("witness-outside-frozen-legal-domain")
    if any(
        p.id not in witness.premise_values
        or not typed_equal(p.expected, witness.premise_values[p.id])
        for p in oracle.prerequisites
    ):
        legality.append("frozen-prerequisites-not-established")
    results = []
    for observation in observations:
        refs = (
            observation.raw_evidence_ref,
            *(
                raw.ref
                for raw in bundle.raw_evidence
                if raw.attempt_id == observation.attempt_id
                and raw.ref.path.endswith("/resource-state.json")
            ),
        )
        reasons = legality + _binding_reasons(
            observation,
            subject,
            oracle,
            plan,
            bundle,
            plan_digest=plan_digest,
            binding_digests=binding_digests,
        )
        if observation.observation_status != "collected":
            reasons.append("business-" + observation.observation_status)
        payload = _raw_payload(observation, bundle)
        if observation.observation_status == "collected":
            try:
                actual = (
                    TypedValue.model_validate(payload["typed_actual"])
                    if payload is not None
                    else None
                )
                if (
                    actual is None
                    or observation.typed_actual is None
                    or not typed_equal(actual, observation.typed_actual)
                ):
                    reasons.append("business-raw-payload-mismatch")
            except (KeyError, ValidationError, TypeError, ValueError):
                reasons.append("business-observation-protocol-error")
        if reasons:
            results.append(_result("UNKNOWN", reasons, refs))
        else:
            assert observation.typed_actual is not None
            matched = oracle_matches(oracle, observation.typed_actual)
            results.append(
                _result(
                    "UNKNOWN" if matched is None else "PASS" if matched else "FAIL",
                    (
                        "oracle-unsupported"
                        if matched is None
                        else "frozen-business-relation-satisfied"
                        if matched
                        else "frozen-business-relation-violated",
                    ),
                    refs,
                )
            )
    return _aggregate(results)


def _acceptance(
    subject: CounterexampleSubject,
    oracle: OracleSpec,
    version: str,
    plan: CounterexamplePlan,
    bundle: ObservationBundle,
    *,
    plan_digest: str,
    binding_digests: Mapping[str, str],
) -> tuple[AcceptanceStatus, tuple[str, ...], tuple[ArtifactRef, ...]]:
    observations = [
        o
        for o in bundle.acceptance
        if o.subject_id == subject.id and o.acceptance_version == version
    ]
    if not observations:
        return "not_executed", (f"{version}-acceptance-not-executed",), ()
    states: list[AcceptanceStatus] = []
    reasons: list[str] = []
    refs: list[ArtifactRef] = []
    for observation in observations:
        refs.append(observation.raw_evidence_ref)
        refs.extend(
            raw.ref
            for raw in bundle.raw_evidence
            if raw.attempt_id == observation.attempt_id
            and raw.ref.path.endswith("/resource-state.json")
        )
        binding = _binding_reasons(
            observation,
            subject,
            oracle,
            plan,
            bundle,
            plan_digest=plan_digest,
            binding_digests=binding_digests,
        )
        step = next(
            (item for item in plan.steps if item.id == observation.step_id), None
        )
        if step is None:
            states.append("unknown")
            reasons.extend(binding)
            continue
        observed = [
            item
            for item in bundle.business
            if item.step_id == step.business_observation_step_id
        ]
        business = _result("UNKNOWN", ("bound-business-observation-unavailable",))
        if len(observed) != 1:
            binding.append("bound-business-observation-unavailable")
        else:
            # 每一次验收都匹配它实际依赖的读回，不能借用同对象另一状态的FAIL。
            business = _business(
                subject,
                oracle,
                plan,
                bundle.model_copy(update={"business": tuple(observed)}),
                plan_digest=plan_digest,
                binding_digests=binding_digests,
            )
            refs.extend(business.evidence_refs)
            observed_attempts = [
                item
                for item in bundle.attempts
                if item.attempt_id == observed[0].attempt_id
            ]
            accepted_attempts = [
                item
                for item in bundle.attempts
                if item.attempt_id == observation.attempt_id
            ]
            if len(observed_attempts) != 1 or len(accepted_attempts) != 1:
                binding.append("bound-business-attempt-unavailable")
            else:
                before, current = observed_attempts[0], accepted_attempts[0]
                observed_step = next(
                    item for item in plan.steps if item.id == observed[0].step_id
                )
                observed_state, invalid_observed = _resource_state_proof(
                    plan,
                    observed_step,
                    before,
                    bundle,
                    plan_digest=plan_digest,
                    binding_digests=binding_digests,
                )
                accepted_state, invalid_accepted = _resource_state_proof(
                    plan,
                    step,
                    current,
                    bundle,
                    plan_digest=plan_digest,
                    binding_digests=binding_digests,
                )
                binding.extend((*invalid_observed, *invalid_accepted))
                if (
                    observed_state is None
                    or accepted_state is None
                    or observed_state.after != accepted_state.before
                ):
                    binding.append("acceptance-resource-state-differs-from-business")
                if (
                    before.ended_at_ms is None
                    or before.ended_at_ms > current.started_at_ms
                ):
                    binding.append("acceptance-precedes-bound-business-observation")
                roots = {resource.root for resource in step.binding.resources}
                by_id = {item.id: item for item in plan.steps}
                if any(
                    by_id[item.step_id].kind in {"exercise", "reset", "cleanup"}
                    and roots.intersection(
                        r.root for r in by_id[item.step_id].binding.resources
                    )
                    and (
                        item.ended_at_ms is None
                        or item.ended_at_ms > before.started_at_ms
                    )
                    and item.started_at_ms
                    < (current.ended_at_ms or current.started_at_ms)
                    for item in bundle.attempts
                    if item.step_id in by_id
                ):
                    binding.append("acceptance-observed-state-invalidated-by-attempt")
        payload = _raw_payload(observation, bundle)
        fields = ("assertion_id", "reached", "assertion_result", "failure_reason")
        if payload is None or any(
            payload.get(field) != getattr(observation, field)
            or type(payload.get(field)) is not type(getattr(observation, field))
            for field in fields
        ):
            binding.append("acceptance-raw-payload-mismatch")
        if (
            not observation.reached
            or observation.failure_reason not in ("none", "target_assertion")
            or observation.assertion_result == "unknown"
        ):
            binding.append("target-assertion-not-proven")
        if (
            observation.assertion_result == "rejected"
            and observation.failure_reason != "target_assertion"
        ):
            binding.append("rejection-not-attributed-to-target")
        if (
            observation.assertion_result == "accepted"
            and observation.failure_reason != "none"
        ):
            binding.append("acceptance-reason-conflict")
        if binding or business.status == "UNKNOWN":
            states.append("unknown")
            reasons.extend(binding or ("business-validity-unknown",))
        elif business.status == "FAIL":
            states.append(
                "detected" if observation.assertion_result == "rejected" else "missed"
            )
        else:
            states.append(
                "false_rejection"
                if observation.assertion_result == "rejected"
                else "accepted"
            )
    unique = set(states)
    state: AcceptanceStatus
    if "false_rejection" in unique:
        state = "false_rejection"
    elif "unknown" in unique or len(unique) != 1:
        state = "unknown"
        if len(unique) != 1:
            reasons.append("acceptance-observations-conflict")
    else:
        state = states[0]
    return state, tuple(dict.fromkeys(reasons)), tuple(refs)


def _required_observation_failures(
    contract: VerificationContract,
    plan: CounterexamplePlan,
    bundle: ObservationBundle,
    required_steps: tuple[ExecutionStep, ...],
    *,
    plan_digest: str,
    binding_digests: Mapping[str, str],
) -> dict[str, tuple[str, ...]]:
    """逐次核验已启用步骤，不能让同一对象的另一条有效观测填补缺口。"""
    subjects = {subject.id: subject for subject in plan.subjects}
    oracles = {item.id: item.oracle_spec for item in contract.obligations}
    failures = {}
    for step in required_steps:
        if step.kind not in {"observe", "acceptance"}:
            continue
        rows = tuple(
            row
            for row in (
                bundle.business if step.kind == "observe" else bundle.acceptance
            )
            if row.step_id == step.id
        )
        attempts = [
            item.attempt_id for item in bundle.attempts if item.step_id == step.id
        ]
        if not attempts or Counter(row.attempt_id for row in rows) != Counter(attempts):
            failures[step.id] = ("required-observation-attempt-missing-or-conflicting",)
            continue
        subject = subjects[step.subject_id]
        oracle = oracles[subject.obligation_id]
        reasons = []
        for row in rows:
            single = bundle.model_copy(
                update={"business" if step.kind == "observe" else "acceptance": (row,)}
            )
            if step.kind == "observe":
                result = _business(
                    subject,
                    oracle,
                    plan,
                    single,
                    plan_digest=plan_digest,
                    binding_digests=binding_digests,
                )
                if result.status == "UNKNOWN":
                    reasons.extend(result.reasons)
            else:
                state, invalid, _ = _acceptance(
                    subject,
                    oracle,
                    step.acceptance_version,
                    plan,
                    single,
                    plan_digest=plan_digest,
                    binding_digests=binding_digests,
                )
                if state in {"unknown", "not_executed"}:
                    reasons.extend(invalid)
        if reasons:
            failures[step.id] = tuple(dict.fromkeys(reasons))
    return failures


def evaluate_counterexample(
    contract: VerificationContract,
    plan: CounterexamplePlan,
    observations: ObservationBundle,
    *,
    require_r2: bool = False,
) -> CounterexampleAssessment:
    """四维结果仅供原验证/评审消费；完整原始文件身份仍由统一 resolver 核验。"""
    validate_plan_contract(contract, plan)
    # 纯判定无回调或文件读取；仅在本次调用共享不变输入的摘要，逐条绑定仍分别比较。
    plan_digest = counterexample_digest(plan)
    binding_digests = {
        step.id: counterexample_digest(step.binding) for step in plan.steps
    }
    required_steps = required_execution_steps(plan, observations, require_r2=require_r2)
    observation_failures = _required_observation_failures(
        contract,
        plan,
        observations,
        required_steps,
        plan_digest=plan_digest,
        binding_digests=binding_digests,
    )
    phase = "r2" if any(step.phase == "r2" for step in required_steps) else "final"
    final_ids = {step.id for step in required_steps if step.phase == phase}
    enabled_ids = {step.id for step in required_steps}
    enabled_observations = observations.model_copy(
        update={
            "business": tuple(
                item for item in observations.business if item.step_id in enabled_ids
            ),
            "acceptance": tuple(
                item for item in observations.acceptance if item.step_id in enabled_ids
            ),
        }
    )
    final_observations = observations.model_copy(
        update={
            "business": tuple(
                item for item in observations.business if item.step_id in final_ids
            ),
            "acceptance": tuple(
                item for item in observations.acceptance if item.step_id in final_ids
            ),
        }
    )
    obligations = {obligation.id: obligation for obligation in contract.obligations}
    assessments = []
    for subject in plan.subjects:
        oracle = obligations[subject.obligation_id].oracle_spec
        business = _business(
            subject,
            oracle,
            plan,
            final_observations,
            plan_digest=plan_digest,
            binding_digests=binding_digests,
        )
        incomplete = [
            step
            for step in required_steps
            if step.id in final_ids
            and step.subject_id == subject.id
            and step.id in observation_failures
        ]
        missing_business = [step for step in incomplete if step.kind == "observe"]
        if missing_business:
            business = _result(
                "UNKNOWN",
                (
                    *business.reasons,
                    *(
                        reason
                        for step in missing_business
                        for reason in observation_failures[step.id]
                    ),
                ),
                business.evidence_refs,
            )
        v0, v0_reasons, v0_refs = _acceptance(
            subject,
            oracle,
            "V0",
            plan,
            final_observations,
            plan_digest=plan_digest,
            binding_digests=binding_digests,
        )
        v1, v1_reasons, v1_refs = _acceptance(
            subject,
            oracle,
            "V1",
            plan,
            final_observations,
            plan_digest=plan_digest,
            binding_digests=binding_digests,
        )
        if subject.role != "variant":
            # 同一冻结来源的早期误拒是仍存在的事实，末阶段成功不能将其抹去。
            for version in ("V0", "V1"):
                historical, history_reasons, history_refs = _acceptance(
                    subject,
                    oracle,
                    version,
                    plan,
                    enabled_observations,
                    plan_digest=plan_digest,
                    binding_digests=binding_digests,
                )
                if historical != "false_rejection":
                    continue
                retained = (
                    *history_reasons,
                    f"historical-legitimate-control-rejection-{version}",
                )
                if version == "V0":
                    v0 = "false_rejection"
                    v0_reasons = (*v0_reasons, *retained)
                    v0_refs = _deduplicate_refs((*v0_refs, *history_refs))
                else:
                    v1 = "false_rejection"
                    v1_reasons = (*v1_reasons, *retained)
                    v1_refs = _deduplicate_refs((*v1_refs, *history_refs))
        for step in incomplete:
            if step.kind == "acceptance" and step.acceptance_version == "V0":
                v0, v0_reasons = (
                    "false_rejection" if v0 == "false_rejection" else "unknown",
                    (
                        *v0_reasons,
                        *observation_failures[step.id],
                    ),
                )
            elif step.kind == "acceptance" and step.acceptance_version == "V1":
                v1, v1_reasons = (
                    "false_rejection" if v1 == "false_rejection" else "unknown",
                    (
                        *v1_reasons,
                        *observation_failures[step.id],
                    ),
                )
        validity: ValidityStatus
        if subject.role != "variant":
            validity = "not_applicable"
        elif any("stale" in reason for reason in business.reasons):
            validity = "stale"
        else:
            validity = cast(
                ValidityStatus,
                {"FAIL": "valid", "PASS": "invalid", "UNKNOWN": "unknown"}[
                    business.status
                ],
            )
        assessments.append(
            SubjectAssessment(
                subject_id=subject.id,
                obligation_id=subject.obligation_id,
                business=business,
                validity=validity,
                v0=v0,
                v1=v1,
                reasons=tuple(
                    dict.fromkeys((*business.reasons, *v0_reasons, *v1_reasons))
                ),
                evidence_refs=_deduplicate_refs(
                    (*business.evidence_refs, *v0_refs, *v1_refs)
                ),
            )
        )
    roles = {subject.id: subject.role for subject in plan.subjects}
    current = tuple(item for item in assessments if roles[item.subject_id] == "current")
    variants = tuple(
        item for item in assessments if roles[item.subject_id] == "variant"
    )
    controls = tuple(
        item for item in assessments if roles[item.subject_id] == "positive_control"
    )
    legal = (*current, *controls)
    reasons = []
    v1_valid = bool(
        plan.v1_digest
        and variants
        and controls
        and all(
            item.business.status == "PASS" and item.v1 == "accepted" for item in legal
        )
        and all(item.validity == "valid" and item.v1 == "detected" for item in variants)
    )
    new_evidence = any(
        item.v0 == "missed" and item.v1 == "detected" for item in variants
    ) or any(item.v0 == "false_rejection" and item.v1 == "accepted" for item in legal)
    disposition: Disposition = "adopt_v1" if v1_valid and new_evidence else "retain_v0"
    if plan.v1_digest and not v1_valid:
        reasons.append("v1-full-legal-and-target-validation-incomplete")
    if not new_evidence:
        reasons.append("no-new-validity-evidence-retain-v0")
    required = [
        item
        for item in assessments
        if obligations[item.obligation_id].selected
        and obligations[item.obligation_id].required
    ]
    required_subject_ids = {item.subject_id for item in required}
    mandatory_execution_incomplete = False
    step_ids = {step.id for step in plan.steps}
    by_id = {step.id: step for step in plan.steps}
    completed_steps = {
        attempt.step_id
        for attempt in observations.attempts
        if attempt.normally_completed
        and attempt.step_id in by_id
        and (by_id[attempt.step_id].kind == "acceptance" or attempt.exit_code == 0)
    }
    missing_steps = [step for step in required_steps if step.id not in completed_steps]
    if missing_steps:
        mandatory = any(
            step.subject_id in required_subject_ids for step in missing_steps
        )
        reasons.append(
            "required-execution-step-incomplete"
            if mandatory
            else "optional-execution-step-incomplete"
        )
        mandatory_execution_incomplete |= mandatory
        disposition = "retain_v0"
    if observation_failures:
        mandatory = any(
            by_id[step_id].subject_id in required_subject_ids
            for step_id in observation_failures
        )
        prefix = "required" if mandatory else "optional"
        reasons.append(f"{prefix}-observation-step-incomplete")
        reasons.extend(
            f"{prefix}-observation-unavailable:{step_id}"
            for step_id in observation_failures
        )
        mandatory_execution_incomplete |= mandatory
        # 可选项尚未执行可以缺省；已经执行却缺原始读回不能降成覆盖建议。
        if any(a.step_id in observation_failures for a in observations.attempts):
            reasons.append("attempt-observation-evidence-incomplete-or-conflicting")
            mandatory_execution_incomplete = True
        disposition = "retain_v0"
    if (
        len({a.attempt_id for a in observations.attempts}) != len(observations.attempts)
        or any(
            a.step_id not in step_ids
            or a.plan_digest != plan_digest
            or a.cleanup_status != "complete"
            or a.status != "completed"
            for a in observations.attempts
        )
        or len(observations.attempts) > plan.max_execution_attempts
    ):
        reasons.append("attempt-history-incomplete-or-conflicting")
        mandatory_execution_incomplete = True
        disposition = "retain_v0"
    chosen = "v1" if disposition == "adopt_v1" else "v0"
    complete = (
        bool(assessments)
        and not mandatory_execution_incomplete
        and all(
            (item.validity == "valid" and getattr(item, chosen) == "detected")
            if roles[item.subject_id] == "variant"
            else (
                item.business.status == "PASS" and getattr(item, chosen) == "accepted"
            )
            for item in required
        )
    )
    if not complete:
        reasons.append("required-counterexample-evidence-incomplete")
    current_result = _aggregate([item.business for item in current])
    historical_failures = [
        _business(
            subject,
            obligations[subject.obligation_id].oracle_spec,
            plan,
            observations,
            plan_digest=plan_digest,
            binding_digests=binding_digests,
        )
        for subject in plan.subjects
        if subject.role == "current"
    ]
    current_result = _aggregate(
        [
            current_result,
            *(result for result in historical_failures if result.status == "FAIL"),
        ]
    )
    if current_result.status == "FAIL":
        reasons.append("current-business-failure-requires-original-repair")
        complete = False
    # 顶层模型冻结不等于嵌套字典冻结；本次求值途中输入变化必须拒绝。
    if counterexample_digest(plan) != plan_digest:
        raise ValueError("counterexample-plan-changed-during-evaluation")
    return CounterexampleAssessment(
        contract_digest=plan.contract_digest,
        plan_digest=plan_digest,
        candidate_digest=plan.candidate_digest,
        current_result=current_result,
        variants=variants,
        positive_controls=controls,
        current_subjects=current,
        required_complete=complete,
        v1_disposition=disposition,
        reasons=tuple(reasons),
        unselected_obligation_ids=tuple(
            o.id for o in contract.obligations if not o.selected
        ),
        repairs=observations.repairs,
    )


def aggregate_task_assessments(
    contract: VerificationContract,
    plan_assessments: Iterable[tuple[CounterexamplePlan, CounterexampleAssessment]],
    *,
    task_ids: Iterable[str],
) -> tuple[Disposition, bool, tuple[str, ...]]:
    """在完整任务集合上选择同一验收版本；历史债和原始观察由执行层先核验。"""
    owners = obligation_task_owners(contract, task_ids=task_ids)
    mandatory_tasks = {
        owners[obligation.id]
        for obligation in contract.obligations
        if obligation.selected and obligation.required
    }
    pairs = tuple(plan_assessments)
    expected_tasks = set(owners.values())
    actual_tasks = [plan.task_id for plan, _ in pairs]
    if len(actual_tasks) != len(set(actual_tasks)):
        raise ValueError("counterexample-duplicate-task-assessment")
    if set(actual_tasks) - expected_tasks:
        raise ValueError("counterexample-task-assessment-owner-mismatch")
    if not mandatory_tasks <= set(actual_tasks):
        return "retain_v0", False, ("counterexample-task-evidence-coverage-incomplete",)
    if not pairs:
        return "retain_v0", True, ("counterexample-optional-task-evidence-missing",)
    identities = {
        (plan.loop_id, plan.contract_digest, plan.candidate_digest, plan.v0_digest)
        for plan, _ in pairs
    }
    if len(identities) != 1:
        raise ValueError("counterexample-task-assessment-loop-identity-conflict")
    v1_digests = {plan.v1_digest for plan, _ in pairs if plan.v1_digest is not None}
    if len(v1_digests) > 1:
        raise ValueError("counterexample-loop-reinforcement-version-conflict")
    obligations = {obligation.id: obligation for obligation in contract.obligations}
    rows: list[tuple[str, SubjectAssessment]] = []
    reasons: list[str] = []
    execution_incomplete = False
    mandatory_execution_incomplete = False
    for plan, assessment in pairs:
        validate_plan_contract(contract, plan)
        if (
            assessment.plan_digest != counterexample_digest(plan)
            or assessment.contract_digest != plan.contract_digest
            or assessment.candidate_digest != plan.candidate_digest
        ):
            raise ValueError("counterexample-task-assessment-binding-conflict")
        subjects = {subject.id: subject for subject in plan.subjects}
        grouped = (
            ("current", assessment.current_subjects),
            ("variant", assessment.variants),
            ("positive_control", assessment.positive_controls),
        )
        actual_subjects = [item.subject_id for _, items in grouped for item in items]
        if len(actual_subjects) != len(set(actual_subjects)) or set(
            actual_subjects
        ) != set(subjects):
            raise ValueError("counterexample-task-assessment-subject-coverage-conflict")
        for role, items in grouped:
            for item in items:
                subject = subjects[item.subject_id]
                if subject.role != role or subject.obligation_id != item.obligation_id:
                    raise ValueError(
                        "counterexample-task-assessment-subject-binding-conflict"
                    )
                rows.append((role, item))
        # 局部版本选择不直接沿用；可选步骤缺失只阻止推广，未知历史仍硬拒绝。
        if any(
            reason
            in {
                "required-execution-step-incomplete",
                "required-observation-step-incomplete",
                "optional-execution-step-incomplete",
                "optional-observation-step-incomplete",
                "attempt-observation-evidence-incomplete-or-conflicting",
                "attempt-history-incomplete-or-conflicting",
                "current-business-failure-requires-original-repair",
            }
            for reason in assessment.reasons
        ):
            execution_incomplete = True
            reasons.append(f"counterexample-task-execution-incomplete:{plan.task_id}")
            if any(
                reason
                in {
                    "required-execution-step-incomplete",
                    "required-observation-step-incomplete",
                    "attempt-observation-evidence-incomplete-or-conflicting",
                    "attempt-history-incomplete-or-conflicting",
                    "current-business-failure-requires-original-repair",
                }
                for reason in assessment.reasons
            ):
                mandatory_execution_incomplete = True
    variants = [item for role, item in rows if role == "variant"]
    legal = [item for role, item in rows if role != "variant"]
    v1_valid = bool(
        set(actual_tasks) == expected_tasks
        and all(plan.v1_digest is not None for plan, _ in pairs)
        and variants
        and legal
        and not execution_incomplete
        and all(
            item.business.status == "PASS" and item.v1 == "accepted" for item in legal
        )
        and all(item.validity == "valid" and item.v1 == "detected" for item in variants)
    )
    new_evidence = any(
        item.v0 == "missed" and item.v1 == "detected" for item in variants
    ) or any(item.v0 == "false_rejection" and item.v1 == "accepted" for item in legal)
    disposition: Disposition = "adopt_v1" if v1_valid and new_evidence else "retain_v0"
    chosen = "v1" if disposition == "adopt_v1" else "v0"
    required = [
        (role, item) for role, item in rows if obligations[item.obligation_id].required
    ]
    complete = not mandatory_execution_incomplete and all(
        (item.validity == "valid" and getattr(item, chosen) == "detected")
        if role == "variant"
        else (item.business.status == "PASS" and getattr(item, chosen) == "accepted")
        for role, item in required
    )
    if v1_digests and not v1_valid:
        reasons.append("v1-full-legal-and-target-validation-incomplete")
    if not new_evidence:
        reasons.append("no-new-validity-evidence-retain-v0")
    if not complete:
        reasons.append("required-counterexample-evidence-incomplete")
    return disposition, complete, tuple(dict.fromkeys(reasons))
