"""从真实格式的原始材料验证四维纯判定，不能把测试成绩当产品收益。"""

import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from ai_sdlc.core.counterexample_evaluation import (
    aggregate_task_assessments,
    evaluate_counterexample,
    oracle_matches,
    typed_equal,
)
from ai_sdlc.core.counterexample_models import (
    CounterexamplePlan,
    ObservationBundle,
    OracleSpec,
    TypedValue,
    VerificationContract,
    counterexample_digest,
    required_execution_steps,
)
from tests.unit.test_counterexample_models import (
    contract_data,
    make_contract,
    make_plan,
    plan_data,
    ref,
    task_owned_contract_data,
    task_owned_plan,
)


def bundle_data(
    contract,
    plan,
    *,
    current="saved",
    variant="missing",
    control="saved",
    v0_variant="accepted",
    v1_variant="rejected",
    v0_control="accepted",
    v1_control="accepted",
):
    business, acceptance, attempts, raw = [], [], [], []
    subjects = {subject.id: subject for subject in plan.subjects}
    plan_digest = counterexample_digest(plan)
    for index, step in enumerate(plan.steps):
        subject = subjects[step.subject_id]
        attempt_id = "attempt-" + step.id
        observation = {
            "assertion_id": step.assertion_id,
            "subject_id": subject.id,
            "subject_role": subject.role,
            "candidate_digest": subject.candidate_digest,
            "plan_digest": plan_digest,
            "attempt_id": attempt_id,
            "step_id": step.id,
        }
        if step.kind == "observe":
            actual = TypedValue.from_python(
                {"current": current, "variant": variant, "positive_control": control}[
                    subject.role
                ]
            ).model_dump(mode="json")
            payload = {"schema_version": 1, "typed_actual": actual}
            observation.update(
                witness_ref=plan.witnesses[0].input_ref.model_dump(),
                typed_actual=actual,
                collection_method=contract.obligations[
                    0
                ].oracle_spec.observation_method.model_dump(),
                observation_status="collected",
                reason="",
            )
        elif step.kind == "acceptance":
            state = "accepted"
            if subject.role == "variant":
                state = v0_variant if step.acceptance_version == "V0" else v1_variant
            if subject.role == "positive_control":
                state = v0_control if step.acceptance_version == "V0" else v1_control
            payload = {
                "schema_version": 1,
                "assertion_id": step.assertion_id,
                "reached": True,
                "assertion_result": state,
                "failure_reason": "target_assertion" if state == "rejected" else "none",
            }
            observation.update(
                {
                    key: value
                    for key, value in payload.items()
                    if key != "schema_version"
                }
            )
            observation.update(
                acceptance_version=step.acceptance_version,
                acceptance_digest=plan.v0_digest
                if step.acceptance_version == "V0"
                else plan.v1_digest,
            )
        else:
            payload = {"schema_version": 1}
        content = json.dumps(payload, ensure_ascii=False)
        raw_ref = ref(f"evidence/{attempt_id}/raw.json", content.encode())
        observation["raw_evidence_ref"] = raw_ref
        raw.append(
            {
                "ref": raw_ref,
                "attempt_id": attempt_id,
                "ownership_nonce": "owner-" + attempt_id,
                "complete": True,
                "content": content,
            }
        )
        attempts.append(
            {
                "attempt_id": attempt_id,
                "attempt_ref": ref(f"evidence/{attempt_id}/intent.json"),
                "plan_digest": plan_digest,
                "contract_digest": plan.contract_digest,
                "candidate_digest": subject.candidate_digest,
                "step_id": step.id,
                "subject_id": subject.id,
                "binding_digest": counterexample_digest(step.binding),
                "ownership_nonce": "owner-" + attempt_id,
                "status": "completed",
                "started_at_ms": 1000 + index * 100,
                "ended_at_ms": 1050 + index * 100,
                "exit_code": 1 if payload.get("assertion_result") == "rejected" else 0,
                "timed_out": False,
                "output_truncated": False,
                "cleanup_status": "complete",
                "raw_evidence_refs": [raw_ref],
            }
        )
        if step.kind in {"observe", "acceptance"}:
            value = {
                "current": current,
                "variant": variant,
                "positive_control": control,
            }[subject.role]
            resources = [
                item.model_dump(mode="json")
                for item in sorted(step.binding.resources, key=lambda item: item.id)
            ]
            snapshots = [
                {
                    "resource_id": resource["id"],
                    "directory_mode": 0o700,
                    "files": [
                        {
                            "path": "result.json",
                            "kind": "file",
                            "mode": 0o600,
                            "sha256": hashlib.sha256(
                                json.dumps(value).encode()
                            ).hexdigest(),
                        }
                    ],
                }
                for resource in resources
            ]
            state_content = json.dumps(
                {
                    "schema_version": 1,
                    "plan_digest": plan_digest,
                    "step_id": step.id,
                    "subject_id": subject.id,
                    "attempt_id": attempt_id,
                    "binding_digest": counterexample_digest(step.binding),
                    "ownership_nonce": "owner-" + attempt_id,
                    "resources": resources,
                    "before": snapshots,
                    "after": copy.deepcopy(snapshots),
                },
                ensure_ascii=False,
            )
            state_ref = ref(
                f"evidence/{attempt_id}/resource-state.json", state_content.encode()
            )
            raw.append(
                {
                    "ref": state_ref,
                    "attempt_id": attempt_id,
                    "ownership_nonce": "owner-" + attempt_id,
                    "complete": True,
                    "content": state_content,
                }
            )
            attempts[-1]["raw_evidence_refs"].append(state_ref)
        if step.kind in {"observe", "acceptance"}:
            (business if step.kind == "observe" else acceptance).append(observation)
    return {
        "business": business,
        "acceptance": acceptance,
        "attempts": attempts,
        "raw_evidence": raw,
        "repairs": [],
    }


def make_bundle(contract, plan, **options):
    return ObservationBundle.model_validate(bundle_data(contract, plan, **options))


def change_raw(data, observation, **changes):
    raw = next(
        raw
        for raw in data["raw_evidence"]
        if raw["attempt_id"] == observation["attempt_id"]
    )
    payload = json.loads(raw["content"])
    payload.update(changes)
    raw["content"] = json.dumps(payload, ensure_ascii=False)
    raw["ref"]["sha256"] = hashlib.sha256(raw["content"].encode()).hexdigest()


def change_resource_state(data, step_id, update):
    """只构造纯核故障材料；真实状态采集由原生执行测试验证。"""
    raw = next(
        item
        for item in data["raw_evidence"]
        if item["attempt_id"] == "attempt-" + step_id
        and item["ref"]["path"].endswith("/resource-state.json")
    )
    payload = json.loads(raw["content"])
    update(payload)
    raw["content"] = json.dumps(payload, ensure_ascii=False)
    raw["ref"]["sha256"] = hashlib.sha256(raw["content"].encode()).hexdigest()
    return raw


def test_valid_counterexample_and_all_legal_controls_support_v1():
    contract, plan = make_contract(), make_plan()
    assessment = evaluate_counterexample(contract, plan, make_bundle(contract, plan))
    assert assessment.current_result.status == "PASS"
    assert assessment.variants[0].validity == "valid"
    assert (assessment.variants[0].v0, assessment.variants[0].v1) == (
        "missed",
        "detected",
    )
    assert assessment.positive_controls[0].v1 == "accepted"
    assert assessment.v1_disposition == "adopt_v1"
    assert assessment.required_complete


def test_evaluation_digests_are_local_and_nested_changes_are_recomputed(monkeypatch):
    from ai_sdlc.core import counterexample_evaluation as evaluation

    contract, plan = make_contract(), make_plan()
    bundle = make_bundle(contract, plan)
    original_digest = evaluation.counterexample_digest
    plan_reads = []
    binding_reads = []

    def digest(value):
        if value is plan:
            plan_reads.append(original_digest(value))
        if any(value is step.binding for step in plan.steps):
            binding_reads.append(value)
        return original_digest(value)

    monkeypatch.setattr(evaluation, "counterexample_digest", digest)
    first = evaluate_counterexample(contract, plan, bundle)
    assert first.required_complete
    assert len(plan_reads) == 2
    assert len(binding_reads) == len(plan.steps)
    assert plan_reads == [first.plan_digest, first.plan_digest]

    # 顶层 frozen 模型仍有可变嵌套数据；下一次调用不能继承旧摘要或旧通过。
    plan.witnesses[0].premise_values["additional"] = TypedValue.from_python("new")
    plan_reads.clear()
    binding_reads.clear()
    second = evaluate_counterexample(contract, plan, bundle)
    assert not second.required_complete
    assert second.plan_digest != first.plan_digest
    assert plan_reads == [second.plan_digest, second.plan_digest]
    assert len(binding_reads) == len(plan.steps)


def test_evaluation_rejects_nested_plan_change_during_calculation(monkeypatch):
    from ai_sdlc.core import counterexample_evaluation as evaluation

    contract, plan = make_contract(), make_plan()
    bundle = make_bundle(contract, plan)
    original = evaluation._required_observation_failures

    def change_after_validation(*args, **kwargs):
        result = original(*args, **kwargs)
        plan.witnesses[0].premise_values["concurrent"] = TypedValue.from_python("new")
        return result

    monkeypatch.setattr(
        evaluation, "_required_observation_failures", change_after_validation
    )
    with pytest.raises(ValueError, match="plan-changed-during-evaluation"):
        evaluate_counterexample(contract, plan, bundle)


def _task_assessments(*, second_options=None):
    contract = VerificationContract.model_validate(task_owned_contract_data())
    plans = [task_owned_plan(contract, task_id) for task_id in ("T11", "T21")]
    results = [
        evaluate_counterexample(contract, plans[0], make_bundle(contract, plans[0])),
        evaluate_counterexample(
            contract,
            plans[1],
            make_bundle(contract, plans[1], **(second_options or {})),
        ),
    ]
    return contract, list(zip(plans, results, strict=True))


def test_loop_selects_one_v1_when_another_task_needs_no_local_improvement():
    contract, pairs = _task_assessments(second_options={"v0_variant": "rejected"})
    assert [result.v1_disposition for _, result in pairs] == ["adopt_v1", "retain_v0"]
    disposition, complete, _ = aggregate_task_assessments(
        contract, pairs, task_ids=("T11", "T21")
    )
    assert (disposition, complete) == ("adopt_v1", True)


def test_other_task_legal_control_rejection_blocks_loop_wide_adoption():
    contract, pairs = _task_assessments(
        second_options={"v0_variant": "rejected", "v1_control": "rejected"}
    )
    assert pairs[0][1].v1_disposition == "adopt_v1"
    disposition, complete, reasons = aggregate_task_assessments(
        contract, pairs, task_ids=("T11", "T21")
    )
    assert (disposition, complete) == ("retain_v0", False)
    assert "v1-full-legal-and-target-validation-incomplete" in reasons


def test_complete_task_cannot_replace_missing_owner_task():
    contract, pairs = _task_assessments()
    assert aggregate_task_assessments(contract, pairs[:1], task_ids=("T11", "T21")) == (
        "retain_v0",
        False,
        ("counterexample-task-evidence-coverage-incomplete",),
    )
    with pytest.raises(ValueError, match="duplicate-task-assessment"):
        aggregate_task_assessments(contract, pairs + pairs[:1], task_ids=("T11", "T21"))


def test_loop_acceptance_does_not_merge_distinct_v1_versions():
    contract, pairs = _task_assessments()
    second = pairs[1][0].model_copy(update={"v1_digest": "e" * 64})
    pairs[1] = (
        second,
        evaluate_counterexample(contract, second, make_bundle(contract, second)),
    )
    with pytest.raises(ValueError, match="reinforcement-version-conflict"):
        aggregate_task_assessments(contract, pairs, task_ids=("T11", "T21"))


def test_subject_success_does_not_hide_incomplete_task_execution():
    contract, pairs = _task_assessments()
    plan = pairs[1][0]
    data = bundle_data(contract, plan)
    cleanup = next(step.id for step in plan.steps if step.kind == "cleanup")
    data["attempts"] = [item for item in data["attempts"] if item["step_id"] != cleanup]
    assessment = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert all(item.v1 == "accepted" for item in assessment.positive_controls)
    assert "required-execution-step-incomplete" in assessment.reasons
    pairs[1] = (plan, assessment)
    disposition, complete, reasons = aggregate_task_assessments(
        contract, pairs, task_ids=("T11", "T21")
    )
    assert (disposition, complete) == ("retain_v0", False)
    assert "counterexample-task-execution-incomplete:T21" in reasons


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("optional_state", ["missing", "unstarted", "unknown"])
def test_optional_owner_does_not_upgrade_required_v0_coverage(required, optional_state):
    data = task_owned_contract_data()
    data["obligations"][1]["required"] = required
    contract = VerificationContract.model_validate(data)
    first, second = [task_owned_plan(contract, task) for task in ("T11", "T21")]
    pairs = [
        (
            first,
            evaluate_counterexample(
                contract, first, make_bundle(contract, first, v0_variant="rejected")
            ),
        )
    ]
    if optional_state != "missing":
        bundle = bundle_data(contract, second)
        if optional_state == "unstarted":
            bundle = {
                **bundle,
                "attempts": [],
                "business": [],
                "acceptance": [],
                "raw_evidence": [],
            }
        else:
            bundle["attempts"][0]["status"] = "execution_unknown"
        pairs.append(
            (
                second,
                evaluate_counterexample(
                    contract, second, ObservationBundle.model_validate(bundle)
                ),
            )
        )
    disposition, complete, _ = aggregate_task_assessments(
        contract, pairs, task_ids=("T11", "T21")
    )
    assert disposition == "retain_v0"
    assert complete is (not required and optional_state != "unknown")


def mixed_optional_plan():
    data = task_owned_contract_data()
    data["sources"][-1].update(task_id="T11", locator="T11/acceptance/2")
    data["obligations"][1]["required"] = False
    contract = VerificationContract.model_validate(data)
    data = plan_data(contract)
    data["task_id"] = "T11"
    for step in data["steps"]:
        step["assertion_id"] = "saved-state-T11"
    subjects, steps = copy.deepcopy(data["subjects"]), copy.deepcopy(data["steps"])
    for subject in subjects:
        subject["id"] += "-optional"
        subject["obligation_id"] = "save-other"
        subject["positive_control_refs"] = [
            value + "-optional" for value in subject["positive_control_refs"]
        ]
    for step in steps:
        step["id"] += "-optional"
        step["subject_id"] += "-optional"
        step["assertion_id"] = "saved-state-T21"
        step["depends_on"] = [value + "-optional" for value in step["depends_on"]]
        if step["business_observation_step_id"]:
            step["business_observation_step_id"] += "-optional"
        binding = step["binding"]
        old_root = binding["project_root"]
        binding["project_root"] += "-optional"
        for resource in binding["resources"]:
            for key in ("root", "observation_endpoint"):
                resource[key] = resource[key].replace(old_root, old_root + "-optional")
    data["subjects"].extend(subjects)
    data["steps"].extend(steps)
    data["max_execution_attempts"] = len(data["steps"]) * 2
    return contract, CounterexamplePlan.model_validate(data)


@pytest.mark.parametrize(
    "optional_state", ["unstarted", "unknown", "bad_raw", "missing_observation"]
)
def test_optional_steps_share_required_owner_without_becoming_mandatory(optional_state):
    contract, plan = mixed_optional_plan()
    data = bundle_data(contract, plan, v0_variant="rejected")
    optional = {
        item["attempt_id"]
        for item in data["attempts"]
        if item["subject_id"].endswith("-optional")
    }
    if optional_state == "unstarted":
        for key in ("business", "acceptance", "attempts", "raw_evidence"):
            data[key] = [
                item for item in data[key] if item["attempt_id"] not in optional
            ]
    elif optional_state == "unknown":
        next(item for item in data["attempts"] if item["attempt_id"] in optional)[
            "status"
        ] = "execution_unknown"
    elif optional_state == "missing_observation":
        data["business"] = [
            item for item in data["business"] if item["attempt_id"] not in optional
        ]
    else:
        next(item for item in data["raw_evidence"] if item["attempt_id"] in optional)[
            "content"
        ] = "{}"
        with pytest.raises(ValidationError, match="raw-content-digest-mismatch"):
            ObservationBundle.model_validate(data)
        return
    assessment = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    disposition, complete, _ = aggregate_task_assessments(
        contract, [(plan, assessment)], task_ids=("T11",)
    )
    assert disposition == "retain_v0"
    assert assessment.required_complete is (optional_state == "unstarted")
    assert complete is (optional_state == "unstarted")


@pytest.mark.parametrize("replay", ["complete", "missing_cleanup", "unknown"])
def test_optional_complete_replay_can_resolve_original_control_rejection(replay):
    from types import SimpleNamespace

    from ai_sdlc.core.counterexample_execution import _unresolved_control_rejection

    data = contract_data()
    data["obligations"][0]["required"] = False
    contract = VerificationContract.model_validate(data)
    plan = make_plan(contract)
    data = bundle_data(contract, plan, v0_variant="rejected")
    if replay == "missing_cleanup":
        data["attempts"] = [
            item
            for item in data["attempts"]
            if item["step_id"] != "positive_control-cleanup"
        ]
    elif replay == "unknown":
        data["attempts"][0]["status"] = "execution_unknown"
    assessment = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    previous = evaluate_counterexample(
        contract, plan, make_bundle(contract, plan, v0_control="rejected")
    )
    old = SimpleNamespace(
        plan=plan.model_copy(update={"candidate_digest": "e" * 64}), assessment=previous
    )
    latest = SimpleNamespace(plan=plan, assessment=assessment)
    assert assessment.required_complete is (replay != "unknown")
    assert _unresolved_control_rejection(
        [(1, "old", old)], latest, assessment, "v0", plan.v0_digest
    ) is (replay != "complete")


def test_current_failure_is_not_overwritten_by_variant_detection():
    contract, plan = make_contract(), make_plan()
    result = evaluate_counterexample(
        contract, plan, make_bundle(contract, plan, current="lost")
    )
    assert result.current_result.status == "FAIL"
    assert result.variants[0].v1 == "detected"
    assert not result.required_complete
    assert result.v1_disposition == "retain_v0"
    assert result.current_result.evidence_refs


def test_v0_false_rejection_can_be_repaired_even_when_v0_detected_variant():
    contract, plan = make_contract(), make_plan()
    result = evaluate_counterexample(
        contract,
        plan,
        make_bundle(contract, plan, v0_variant="rejected", v0_control="rejected"),
    )
    assert result.positive_controls[0].v0 == "false_rejection"
    assert result.positive_controls[0].v1 == "accepted"
    assert result.v1_disposition == "adopt_v1"
    assert result.required_complete


def test_v1_false_rejection_and_absent_new_evidence_preserve_v0():
    contract, plan = make_contract(), make_plan()
    rejected = evaluate_counterexample(
        contract, plan, make_bundle(contract, plan, v1_control="rejected")
    )
    assert rejected.positive_controls[0].v1 == "false_rejection"
    assert rejected.v1_disposition == "retain_v0"
    assert not rejected.required_complete
    unchanged = evaluate_counterexample(
        contract, plan, make_bundle(contract, plan, v0_variant="rejected")
    )
    assert unchanged.v1_disposition == "retain_v0"
    assert unchanged.required_complete


def test_invalid_variant_is_not_detection_and_does_not_fail_current_business():
    contract, plan = make_contract(), make_plan()
    result = evaluate_counterexample(
        contract, plan, make_bundle(contract, plan, variant="saved")
    )
    assert result.variants[0].validity == "invalid"
    assert result.variants[0].v1 == "false_rejection"
    assert result.current_result.status == "PASS"
    assert not result.required_complete


@pytest.mark.parametrize("actual", [1, True, {"missing": "required"}, None])
def test_legally_collected_wrong_business_types_are_real_violations(actual):
    contract, plan = make_contract(), make_plan()
    result = evaluate_counterexample(
        contract, plan, make_bundle(contract, plan, variant=actual)
    )
    assert result.variants[0].business.status == "FAIL"
    assert result.variants[0].validity == "valid"
    assert result.variants[0].v1 == "detected"


@pytest.mark.parametrize(
    "failure_reason",
    [
        "compile_error",
        "unrelated_assertion",
        "infrastructure_error",
        "protocol_error",
        "not_reached",
    ],
)
def test_non_business_failure_never_counts_as_detection(failure_reason):
    contract, plan = make_contract(), make_plan()
    data = bundle_data(contract, plan)
    observation = next(
        o
        for o in data["acceptance"]
        if o["subject_id"] == "variant" and o["acceptance_version"] == "V1"
    )
    observation.update(
        failure_reason=failure_reason, reached=failure_reason != "not_reached"
    )
    change_raw(
        data, observation, failure_reason=failure_reason, reached=observation["reached"]
    )
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert result.variants[0].validity == "valid"
    assert result.variants[0].v1 == "unknown"
    assert not result.required_complete


def test_forged_cooked_assertion_does_not_override_original_acceptance():
    contract, plan = make_contract(), make_plan()
    data = bundle_data(contract, plan, v1_variant="accepted")
    observation = next(
        o
        for o in data["acceptance"]
        if o["subject_id"] == "variant" and o["acceptance_version"] == "V1"
    )
    observation.update(assertion_result="rejected", failure_reason="target_assertion")
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert result.variants[0].v1 == "unknown"
    assert "acceptance-raw-payload-mismatch" in result.variants[0].reasons


def test_forged_business_value_and_protocol_corruption_are_unknown():
    contract, plan = make_contract(), make_plan()
    data = bundle_data(contract, plan)
    observation = data["business"][1]
    observation["typed_actual"] = {"type": "string", "value": "saved"}
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert result.variants[0].business.status == "UNKNOWN"
    change_raw(data, observation, typed_actual={"type": "integer", "value": True})
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert result.variants[0].validity == "unknown"
    assert result.variants[0].v1 == "unknown"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["raw_evidence"].clear(),
        lambda data: data["attempts"].clear(),
        lambda data: _variant_attempt(data).update(cleanup_status="unknown"),
        lambda data: _variant_attempt(data).update(status="execution_unknown"),
        lambda data: _variant_attempt(data).update(output_truncated=True),
        lambda data: _variant_attempt(data).update(timed_out=True),
        lambda data: _variant_attempt(data).update(ownership_nonce="wrong-owner"),
        lambda data: _variant_attempt(data).update(candidate_digest="f" * 64),
        lambda data: data["business"][1].update(candidate_digest="f" * 64),
        lambda data: data["acceptance"][3].update(acceptance_digest="f" * 64),
    ],
)
def test_missing_stale_or_incomplete_evidence_cannot_pass(mutation):
    contract, plan = make_contract(), make_plan()
    data = bundle_data(contract, plan)
    mutation(data)
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert not result.required_complete
    assert result.v1_disposition == "retain_v0"


def _variant_attempt(data):
    return next(row for row in data["attempts"] if row["step_id"] == "variant-none")


def test_raw_content_hash_is_checked_before_business_evaluation():
    contract, plan = make_contract(), make_plan()
    data = bundle_data(contract, plan)
    data["raw_evidence"][0]["content"] = "replaced"
    with pytest.raises(ValidationError, match="raw-content-digest-mismatch"):
        ObservationBundle.model_validate(data)


def test_optional_failure_is_not_a_business_defect():
    data = contract_data()
    data["obligations"][0]["required"] = False
    contract = VerificationContract.model_validate(data)
    plan = make_plan(contract)
    result = evaluate_counterexample(
        contract, plan, make_bundle(contract, plan, variant="saved")
    )
    assert result.current_result.status == "PASS"
    assert result.v1_disposition == "retain_v0"


def test_unsupported_semantic_claim_stays_unknown():
    data = contract_data()
    data["obligations"][0]["oracle_spec"]["basis"] = "semantic_only"
    contract = VerificationContract.model_validate(data)
    plan = make_plan(contract)
    result = evaluate_counterexample(contract, plan, make_bundle(contract, plan))
    assert result.current_result.status == "UNKNOWN"
    assert result.variants[0].v1 == "unknown"
    assert not result.required_complete


def test_wrong_input_domain_is_not_valid_counterexample():
    contract, plan = make_contract(), make_plan()
    data = plan.model_dump(mode="json")
    data["witnesses"][0]["input_value"] = {"type": "integer", "value": 1}
    for step in data["steps"]:
        binding = step["binding"].get("witness_input")
        if binding:
            binding["encoding"] = "json"
            step["binding"]["argv"][binding["argv_index"]] = "1"
    plan = type(plan).model_validate(data)
    result = evaluate_counterexample(contract, plan, make_bundle(contract, plan))
    assert result.variants[0].validity == "unknown"
    assert result.variants[0].v1 == "unknown"


def test_value_comparison_preserves_types_array_order_and_object_values():
    assert not typed_equal(TypedValue.from_python(True), TypedValue.from_python(1))
    assert not typed_equal(TypedValue.from_python(1), TypedValue.from_python("1"))
    assert not typed_equal(TypedValue.from_python("1"), TypedValue.from_python(" 1"))
    assert not typed_equal(
        TypedValue.from_python([1, 2]), TypedValue.from_python([2, 1])
    )
    assert typed_equal(
        TypedValue.from_python({"a": 1, "b": [True]}),
        TypedValue.from_python({"b": [True], "a": 1}),
    )
    assert not typed_equal(
        TypedValue(type="decimal", value="1"), TypedValue.from_python(1)
    )
    assert not typed_equal(
        TypedValue(type="decimal", value="0.000000000000000000001"),
        TypedValue(type="decimal", value="0.000000000000000000002"),
    )


def test_frozen_projection_preserves_multiset_duplicates_and_missing_fields():
    data = make_contract().obligations[0].oracle_spec.model_dump(mode="json")
    data.update(
        relation="projected_multiset_equal",
        projection=["id"],
        expected=TypedValue.from_python(
            [
                {"id": 1, "display": "a"},
                {"id": 1, "display": "a"},
                {"id": 2, "display": "b"},
            ]
        ).model_dump(mode="json"),
    )
    oracle = OracleSpec.model_validate(data)
    assert oracle_matches(
        oracle, TypedValue.from_python([{"id": 2}, {"id": 1, "extra": True}, {"id": 1}])
    )
    assert not oracle_matches(oracle, TypedValue.from_python([{"id": 2}, {"id": 1}]))
    assert not oracle_matches(
        oracle, TypedValue.from_python([{"id": 2}, {"id": 1}, {"missing": 1}])
    )
    assert not oracle_matches(
        oracle, TypedValue.from_python([{"id": "2"}, {"id": 1}, {"id": 1}])
    )


@pytest.mark.parametrize(
    ("exit_code", "normally_completed"),
    [(-9, False), (-15, False), (0, True), (1, True), (247, True),
     (255, True), (256, False), (0x40000015, False),
     (0x80000001, False), (0xC0000005, False), (0xC0000602, False),
     (0xFFFFFFFF, False)],
)
def test_rejection_requires_normal_target_completion(exit_code, normally_completed):
    contract, plan = make_contract(), make_plan()
    data = bundle_data(contract, plan)
    attempt = next(item for item in data["attempts"] if item["step_id"] == "variant-V1")
    attempt["exit_code"] = exit_code
    # 旧格式原件仍可解析，读取平台不能改变相同退出事实的业务含义。
    original = json.dumps(data).encode()
    bundle = ObservationBundle.model_validate_json(original)
    parsed = next(item for item in bundle.attempts if item.step_id == "variant-V1")
    result = evaluate_counterexample(contract, plan, bundle)
    assert json.dumps(data).encode() == original
    assert parsed.exit_code == exit_code
    assert parsed.status == "completed"
    assert parsed.normally_completed is normally_completed
    if not normally_completed:
        assert result.variants[0].v1 == "unknown"
        assert not result.required_complete
        assert result.v1_disposition == "retain_v0"
        assert "required-execution-step-incomplete" in result.reasons
    else:
        assert result.variants[0].v1 == "detected"
        assert result.required_complete
        assert result.v1_disposition == "adopt_v1"


@pytest.mark.parametrize("kind", ["exercise", "reset", "cleanup"])
def test_missing_planned_operational_step_cannot_be_complete(kind):
    contract, plan = make_contract(), make_plan()
    data = plan.model_dump(mode="json")
    step = dict(data["steps"][0], id="required-operation", kind=kind)
    index = next(
        i for i, item in enumerate(data["steps"]) if item["id"] == "current-cleanup"
    )
    data["steps"].insert(index, step)
    data["steps"][index + 1]["depends_on"].append(step["id"])
    plan = CounterexamplePlan.model_validate(data)
    data = bundle_data(contract, plan)
    data["acceptance"] = [
        item for item in data["acceptance"] if item["step_id"] != "required-operation"
    ]
    data["attempts"] = [
        item for item in data["attempts"] if item["step_id"] != "required-operation"
    ]
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert not result.required_complete
    assert result.v1_disposition == "retain_v0"


def test_early_observations_cannot_replace_final_execution():
    contract, plan = make_contract(), make_plan()
    data = plan.model_dump(mode="json")
    early = [
        dict(
            step,
            id="early-" + step["id"],
            phase="exploration",
            depends_on=["early-" + identifier for identifier in step["depends_on"]],
            business_observation_step_id=(
                "early-" + step["business_observation_step_id"]
                if step["business_observation_step_id"]
                else None
            ),
        )
        for step in data["steps"]
    ]
    data["steps"] = early + data["steps"]
    plan = CounterexamplePlan.model_validate(data)
    data = bundle_data(contract, plan)
    data["business"] = [
        item for item in data["business"] if item["step_id"].startswith("early-")
    ]
    data["acceptance"] = [
        item for item in data["acceptance"] if item["step_id"].startswith("early-")
    ]
    data["attempts"] = [
        item for item in data["attempts"] if item["step_id"].startswith("early-")
    ]
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert not result.required_complete
    assert result.current_result.status == "UNKNOWN"


def r2_fixture():
    contract, plan = make_contract(), make_plan()
    data = plan.model_dump(mode="json")
    data["steps"] += [
        dict(
            step,
            id="r2-" + step["id"],
            phase="r2",
            depends_on=["r2-" + identifier for identifier in step["depends_on"]],
            business_observation_step_id=(
                "r2-" + step["business_observation_step_id"]
                if step["business_observation_step_id"]
                else None
            ),
        )
        for step in data["steps"]
    ]
    plan = CounterexamplePlan.model_validate(data)
    return contract, plan, bundle_data(contract, plan)


def only_attempts(data, keep):
    data["business"] = [item for item in data["business"] if keep(item["step_id"])]
    data["acceptance"] = [item for item in data["acceptance"] if keep(item["step_id"])]
    data["attempts"] = [item for item in data["attempts"] if keep(item["step_id"])]
    ids = {item["attempt_id"] for item in data["attempts"]}
    data["raw_evidence"] = [
        item for item in data["raw_evidence"] if item["attempt_id"] in ids
    ]
    return ObservationBundle.model_validate(data)


def test_unstarted_r2_reservation_does_not_execute_future_review_round():
    contract, plan, data = r2_fixture()
    bundle = only_attempts(data, lambda step_id: not step_id.startswith("r2-"))
    assert all(step.phase != "r2" for step in required_execution_steps(plan, bundle))
    assert evaluate_counterexample(contract, plan, bundle).required_complete
    assert not evaluate_counterexample(
        contract, plan, bundle, require_r2=True
    ).required_complete


def test_started_r2_requires_whole_group_and_cannot_reuse_final_observations():
    contract, plan, data = r2_fixture()
    bundle = only_attempts(
        data,
        lambda step_id: not step_id.startswith("r2-") or step_id == "r2-current-none",
    )
    assert len(required_execution_steps(plan, bundle)) == len(plan.steps)
    result = evaluate_counterexample(contract, plan, bundle)
    assert not result.required_complete
    assert result.variants[0].v1 == "unknown"


def test_complete_r2_supplies_its_own_current_evidence():
    contract, plan, data = r2_fixture()
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data), require_r2=True
    )
    assert result.required_complete
    assert all("attempt-r2-" in item.path for item in result.variants[0].evidence_refs)


def test_native_r2_requirement_cannot_be_inferred_away_when_plan_has_no_r2():
    contract, plan = make_contract(), make_plan()
    with pytest.raises(ValueError, match="required-r2-execution-table-missing"):
        evaluate_counterexample(
            contract, plan, make_bundle(contract, plan), require_r2=True
        )


def repeated_observation_fixture(subject_id, version, *, phase="final"):
    contract, plan = make_contract(), make_plan()
    data = plan.model_dump(mode="json")
    if phase != "final":
        data["steps"] += [
            dict(
                step,
                id=phase + "-" + step["id"],
                phase=phase,
                depends_on=[phase + "-" + item for item in step["depends_on"]],
                business_observation_step_id=(
                    phase + "-" + step["business_observation_step_id"]
                    if step["business_observation_step_id"]
                    else None
                ),
            )
            for step in data["steps"]
        ]
    original = next(
        step
        for step in data["steps"]
        if step["subject_id"] == subject_id
        and step["acceptance_version"] == version
        and step["kind"] in {"observe", "acceptance"}
        and step["phase"] == phase
    )
    extra = copy.deepcopy(original)
    extra["id"] = "second-" + original["id"]
    cleanup_index = next(
        index
        for index, step in enumerate(data["steps"])
        if step["subject_id"] == subject_id
        and step["phase"] == phase
        and step["kind"] == "cleanup"
    )
    data["steps"][cleanup_index]["depends_on"].append(extra["id"])
    additional = [extra]
    if extra["kind"] == "observe":
        for accepted in ("V0", "V1") if plan.v1_digest else ("V0",):
            linked = copy.deepcopy(
                next(
                    step
                    for step in data["steps"]
                    if step["subject_id"] == subject_id
                    and step["phase"] == phase
                    and step["acceptance_version"] == accepted
                )
            )
            linked.update(
                id="second-" + linked["id"],
                business_observation_step_id=extra["id"],
                depends_on=[extra["id"]],
            )
            additional.append(linked)
            data["steps"][cleanup_index]["depends_on"].append(linked["id"])
    data["steps"][cleanup_index:cleanup_index] = additional
    data["max_execution_attempts"] = len(data["steps"]) * 2
    plan = CounterexamplePlan.model_validate(data)
    return contract, plan, bundle_data(contract, plan), extra["id"]


@pytest.mark.parametrize("subject_id", ["current", "variant", "positive_control"])
@pytest.mark.parametrize("version", ["none", "V0", "V1"])
def test_every_repeated_observation_can_supply_its_own_evidence(subject_id, version):
    contract, plan, data, _ = repeated_observation_fixture(subject_id, version)
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert result.required_complete
    assert result.v1_disposition == "adopt_v1"


@pytest.mark.parametrize("subject_id", ["current", "variant", "positive_control"])
@pytest.mark.parametrize("version", ["none", "V0", "V1"])
@pytest.mark.parametrize("missing", ["observation", "uncollected_raw", "invalid_json"])
def test_other_observations_cannot_hide_required_output_loss(
    subject_id, version, missing
):
    contract, plan, data, step_id = repeated_observation_fixture(subject_id, version)
    key = "business" if version == "none" else "acceptance"
    row = next(item for item in data[key] if item["step_id"] == step_id)
    if missing == "invalid_json":
        raw = next(
            item
            for item in data["raw_evidence"]
            if item["attempt_id"] == row["attempt_id"]
        )
        raw["content"] = "{"
        raw["ref"]["sha256"] = hashlib.sha256(b"{").hexdigest()
    else:
        data[key].remove(row)
        if missing == "uncollected_raw":
            # 不可解码的 stdout 仍有原始引用，但收集器不能生成文本观测。
            data["raw_evidence"] = [
                item
                for item in data["raw_evidence"]
                if item["attempt_id"] != row["attempt_id"]
            ]
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    subject = next(
        item
        for item in (
            *result.current_subjects,
            *result.variants,
            *result.positive_controls,
        )
        if item.subject_id == subject_id
    )
    if version == "none":
        assert subject.business.status == "UNKNOWN"
    else:
        assert getattr(subject, version.lower()) == "unknown"
    assert not result.required_complete
    assert result.v1_disposition == "retain_v0"
    assert f"required-observation-unavailable:{step_id}" in result.reasons


@pytest.mark.parametrize("version", ["none", "V0", "V1"])
def test_repeated_valid_row_cannot_replace_another_steps_missing_observation(version):
    contract, plan, data, step_id = repeated_observation_fixture("variant", version)
    key = "business" if version == "none" else "acceptance"
    data[key] = [item for item in data[key] if item["step_id"] != step_id]
    row = next(
        item
        for item in data[key]
        if item["subject_id"] == "variant"
        and (version == "none" or item["acceptance_version"] == version)
    )
    data[key].append(copy.deepcopy(row))
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert not result.required_complete
    assert "required-observation-step-incomplete" in result.reasons


@pytest.mark.parametrize("phase", ["exploration", "r2"])
@pytest.mark.parametrize("version", ["none", "V0", "V1"])
def test_completed_later_phase_cannot_hide_earlier_required_observation_loss(
    phase, version
):
    contract, plan, data, _ = repeated_observation_fixture(
        "variant", version, phase=phase
    )
    missing_id = (
        ("exploration-" if phase == "exploration" else "") + "variant-" + version
    )
    key = "business" if version == "none" else "acceptance"
    data[key] = [item for item in data[key] if item["step_id"] != missing_id]
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert not result.required_complete
    assert f"required-observation-unavailable:{missing_id}" in result.reasons


@pytest.mark.parametrize("step_id", ["variant-none", "variant-V0", "variant-V1"])
@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "duplicate",
        "nonce",
        "binding",
        "plan",
        "resources",
        "before-after",
        "directory-mode",
        "file-mode",
        "missing-directory-mode",
        "missing-file-mode",
        "omitted-resource",
        "directory-kind",
        "duplicate-path",
    ],
)
def test_resource_state_must_be_owned_complete_and_unchanged(step_id, damage):
    contract, plan = make_contract(), make_plan()
    data = bundle_data(contract, plan)
    raw = change_resource_state(data, step_id, lambda payload: None)
    if damage == "missing":
        data["raw_evidence"].remove(raw)
    elif damage == "duplicate":
        data["raw_evidence"].append(copy.deepcopy(raw))
    else:

        def alter(payload):
            if damage == "nonce":
                payload["ownership_nonce"] = "another-owner"
            elif damage == "binding":
                payload["binding_digest"] = "e" * 64
            elif damage == "plan":
                payload["plan_digest"] = "e" * 64
            elif damage == "resources":
                payload["resources"][0]["root"] += "-other"
            elif damage == "before-after":
                payload["after"][0]["files"][0]["sha256"] = "e" * 64
            elif damage == "directory-mode":
                payload["after"][0]["directory_mode"] ^= 0o040
            elif damage == "file-mode":
                payload["after"][0]["files"][0]["mode"] ^= 0o040
            elif damage == "missing-directory-mode":
                payload["after"][0].pop("directory_mode")
            elif damage == "missing-file-mode":
                payload["after"][0]["files"][0].pop("mode")
            elif damage == "omitted-resource":
                payload["after"] = []
            elif damage == "directory-kind":
                payload["before"][0]["files"][0]["kind"] = "directory"
            else:
                payload["after"][0]["files"].append(
                    copy.deepcopy(payload["after"][0]["files"][0])
                )

        change_resource_state(data, step_id, alter)
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert not result.required_complete
    assert result.v1_disposition == "retain_v0"
    assert f"required-observation-unavailable:{step_id}" in result.reasons
    if damage in {"directory-mode", "file-mode"}:
        assert (
            "readonly-observation-or-acceptance-changed-resources"
            in result.variants[0].reasons
        )
    elif damage in {"missing-directory-mode", "missing-file-mode"}:
        assert "resource-state-evidence-invalid" in result.variants[0].reasons


@pytest.mark.parametrize("field", ["sha256", "mode", "directory_mode"])
def test_unchanged_acceptance_on_a_different_state_cannot_claim_detection(field):
    contract, plan = make_contract(), make_plan()
    data = bundle_data(contract, plan)

    def changed(payload):
        for boundary in ("before", "after"):
            snapshot = payload[boundary][0]
            target = snapshot if field == "directory_mode" else snapshot["files"][0]
            target[field] = "e" * 64 if field == "sha256" else target[field] ^ 0o040

    change_resource_state(data, "variant-V1", changed)
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert result.variants[0].v1 == "unknown"
    assert (
        "acceptance-resource-state-differs-from-business" in result.variants[0].reasons
    )
    assert not result.required_complete


@pytest.mark.parametrize("damage", ["before-observation", "overlapping-observation"])
def test_actual_acceptance_receipt_cannot_precede_its_bound_business_state(damage):
    contract, plan = make_contract(), make_plan()
    data = bundle_data(contract, plan)
    observed = next(
        item for item in data["attempts"] if item["step_id"] == "variant-none"
    )
    accepted = next(
        item for item in data["attempts"] if item["step_id"] == "variant-V1"
    )
    accepted["started_at_ms"] = (
        observed["started_at_ms"] - 1
        if damage == "before-observation"
        else observed["ended_at_ms"] - 1
    )
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert result.variants[0].v1 == "unknown"
    assert not result.required_complete


@pytest.mark.parametrize("kind", ["exercise", "reset", "cleanup"])
@pytest.mark.parametrize("position", ["before", "after"])
def test_nonzero_required_operation_never_counts_as_completed(kind, position):
    contract, plan = make_contract(), make_plan()
    data = plan.model_dump(mode="json")
    operation = copy.deepcopy(data["steps"][0])
    operation.update(id="nonzero-operation", kind=kind)
    index = (
        0
        if position == "before"
        else next(
            i for i, item in enumerate(data["steps"]) if item["id"] == "current-cleanup"
        )
    )
    data["steps"].insert(index, operation)
    cleanup = next(item for item in data["steps"] if item["id"] == "current-cleanup")
    cleanup["depends_on"].append(operation["id"])
    plan = CounterexamplePlan.model_validate(data)
    data = bundle_data(contract, plan)
    next(item for item in data["attempts"] if item["step_id"] == operation["id"])[
        "exit_code"
    ] = 7
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert not result.required_complete
    assert result.v1_disposition == "retain_v0"
    assert "required-execution-step-incomplete" in result.reasons


@pytest.mark.parametrize("transition", ["exploration-final", "final-r2"])
@pytest.mark.parametrize("version", ["V0", "V1"])
def test_earlier_legitimate_control_rejection_is_not_erased_by_later_success(
    transition, version
):
    if transition == "final-r2":
        contract, plan, data = r2_fixture()
        prefix = ""
    else:
        contract, original = make_contract(), make_plan()
        model = original.model_dump(mode="json")
        early = copy.deepcopy(model["steps"])
        for step in early:
            step.update(
                id="early-" + step["id"],
                phase="exploration",
                depends_on=["early-" + identifier for identifier in step["depends_on"]],
                business_observation_step_id=(
                    "early-" + step["business_observation_step_id"]
                    if step["business_observation_step_id"]
                    else None
                ),
            )
        model["steps"] = early + model["steps"]
        plan = CounterexamplePlan.model_validate(model)
        data = bundle_data(contract, plan)
        prefix = "early-"
    old = next(
        item
        for item in data["acceptance"]
        if item["step_id"] == prefix + "positive_control-" + version
    )
    old.update(assertion_result="rejected", failure_reason="target_assertion")
    change_raw(
        data, old, assertion_result="rejected", failure_reason="target_assertion"
    )
    next(item for item in data["attempts"] if item["attempt_id"] == old["attempt_id"])[
        "exit_code"
    ] = 1
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    control = result.positive_controls[0]
    assert getattr(control, version.lower()) == "false_rejection"
    assert f"historical-legitimate-control-rejection-{version}" in control.reasons
    assert old["raw_evidence_ref"]["path"] in {
        item.path for item in control.evidence_refs
    }
    if version == "V1":
        assert not result.required_complete
        assert result.v1_disposition == "retain_v0"
    else:
        assert result.required_complete
        assert result.v1_disposition == "adopt_v1"


def test_each_repeated_observation_has_explicit_acceptance_and_cannot_borrow_rejection():
    contract, plan, data, repeated = repeated_observation_fixture("variant", "none")
    bound = [
        step for step in plan.steps if step.business_observation_step_id == repeated
    ]
    assert {step.acceptance_version for step in bound} == {"V0", "V1"}
    accepted = next(
        item for item in data["acceptance"] if item["step_id"] == "second-variant-V1"
    )
    accepted.update(assertion_result="accepted", failure_reason="none")
    change_raw(data, accepted, assertion_result="accepted", failure_reason="none")
    next(
        item
        for item in data["attempts"]
        if item["attempt_id"] == accepted["attempt_id"]
    )["exit_code"] = 0
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert result.variants[0].v1 == "unknown"
    assert not result.required_complete
    assert result.v1_disposition == "retain_v0"


def test_earlier_false_rejection_survives_incomplete_last_phase():
    contract, plan, data = r2_fixture()
    earlier = next(
        item for item in data["acceptance"] if item["step_id"] == "positive_control-V1"
    )
    earlier.update(assertion_result="rejected", failure_reason="target_assertion")
    change_raw(
        data, earlier, assertion_result="rejected", failure_reason="target_assertion"
    )
    next(
        item for item in data["attempts"] if item["attempt_id"] == earlier["attempt_id"]
    )["exit_code"] = 1
    data["business"] = [
        item
        for item in data["business"]
        if item["step_id"] != "r2-positive_control-none"
    ]
    result = evaluate_counterexample(
        contract, plan, ObservationBundle.model_validate(data)
    )
    assert result.positive_controls[0].business.status == "UNKNOWN"
    assert result.positive_controls[0].v1 == "false_rejection"
    assert not result.required_complete
    assert earlier["raw_evidence_ref"]["path"] in {
        ref.path for ref in result.positive_controls[0].evidence_refs
    }
