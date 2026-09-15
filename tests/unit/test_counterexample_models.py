"""合同字段及原引用的边界；辅助构造同时供纯核测试使用。"""

import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from ai_sdlc.core.counterexample_models import (
    ArtifactRef,
    CounterexampleEvidenceRecord,
    CounterexamplePhaseContext,
    CounterexamplePlan,
    ResourceStateSnapshot,
    TypedValue,
    VerificationContract,
    canonical_decimal,
    counterexample_digest,
    obligation_task_owners,
    plan_obligations,
    source_digest_sha256,
    validate_contract_sources,
    validate_plan_contract,
    validate_witness_execution_bindings,
    witness_input_argument,
)

H = "a" * 64
SOURCE = b"## FR-SAVE\nSaved data must survive a new process.\n"
ENTRY = b"Saved data must survive a new process."


def resource_snapshot_data():
    return {
        "resource_id": "data",
        "directory_mode": 0o700,
        "files": [
            {"path": "nested", "kind": "directory", "sha256": None, "mode": 0o700},
            {"path": "result.json", "kind": "file", "sha256": H, "mode": 0o600},
        ],
    }


@pytest.mark.parametrize("mode", [0, 0o7777])
def test_resource_permission_boundaries_preserve_file_and_directory_modes(mode):
    data = resource_snapshot_data()
    data["directory_mode"] = mode
    for entry in data["files"]:
        entry["mode"] = mode
    assert ResourceStateSnapshot.model_validate(data).model_dump(mode="json") == data


@pytest.mark.parametrize("field", ["directory_mode", "mode"])
@pytest.mark.parametrize("value", ["missing", None, True, "384", 384.0, -1, 4096])
def test_resource_permission_requires_present_strict_bounded_integer(field, value):
    data = resource_snapshot_data()
    target = data if field == "directory_mode" else data["files"][1]
    if value == "missing":
        target.pop(field)
    else:
        target[field] = value
    with pytest.raises(ValidationError) as error:
        ResourceStateSnapshot.model_validate(data)
    expected = (field,) if field == "directory_mode" else ("files", 1, field)
    assert any(item["loc"] == expected for item in error.value.errors())


@pytest.mark.parametrize("entry", [False, True])
def test_resource_permission_fields_do_not_allow_unrecognized_state(entry):
    data = resource_snapshot_data()
    target = data["files"][0] if entry else data
    target["unrecognized_permission"] = 0o700
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ResourceStateSnapshot.model_validate(data)


def ref(path="evidence/input.json", data=b"input"):
    return {"path": path, "sha256": hashlib.sha256(data).hexdigest()}


def contract_data():
    return {
        "work_item_id": "sample",
        "sources": [
            {
                "id": "source-save",
                "namespace": "spec",
                "path": "specs/sample/spec.md",
                "sha256": hashlib.sha256(SOURCE).hexdigest(),
                "locator": "FR-SAVE",
                "entry_sha256": hashlib.sha256(ENTRY).hexdigest(),
            }
        ],
        "obligations": [
            {
                "id": "save",
                "source_id": "source-save",
                "required": True,
                "applicable": True,
                "selected": True,
                "applicability_reason": "独立进程可以读回保存结果。",
                "selection_reason": "原关键义务，尚无敏感性证据。",
                "producer_stage": "implementation",
                "consumer_stages": ["implementation", "local-pr-review"],
                "close_owner": "implementation",
                "oracle_spec": {
                    "assertion_id": "saved-state",
                    "source_ids": ["source-save"],
                    "basis": "explicit",
                    "legal_input_domain": {
                        "type": "string",
                        "allowed_values": [],
                        "required_fields": [],
                    },
                    "prerequisites": [],
                    "relation": "typed_equal",
                    "expected": {"type": "string", "value": "saved"},
                    "projection": [],
                    "allowed_changes": ["合法输入值"],
                    "observation_method": {
                        "kind": "process_json",
                        "location": "result.json",
                        "resource_id": "data",
                        "independent": True,
                    },
                    "verification_source_ids": ["source-save"],
                },
                "mechanisms": [
                    {
                        "key": "no-commit",
                        "hypothesis": "写入未提交导致独立读回为空。",
                        "source_ids": ["source-save"],
                    }
                ],
            }
        ],
        "resource_permissions": [
            {
                "id": "data",
                "kind": "directory",
                "scope": "scratch",
                "actions": ["read", "write", "execute", "cleanup"],
                "isolation": "owned_directory",
            }
        ],
        "budget_ref": ref("specs/sample/budget.md", b"existing budget"),
        "selection_policy": "original-criticality-then-unverified-then-id",
    }


def make_contract():
    return VerificationContract.model_validate(contract_data())


def plan_data(contract=None, v1=True):
    contract = contract or make_contract()
    environment = {"LANG": "C.UTF-8"}
    subjects = []
    steps = []
    for role in ("current", "variant", "positive_control"):
        variant = role == "variant"
        subjects.append(
            {
                "id": role,
                "role": role,
                "obligation_id": "save",
                "witness_id": "input",
                "candidate_digest": "b" * 64 if variant else H,
                "parent_candidate_digest": H,
                "snapshot": ref(f"evidence/{role}/snapshot.json"),
                "patch": ref("evidence/variant.patch") if variant else None,
                "modified_paths": ["src/save.py"] if variant else [],
                "mechanism_key": "no-commit" if variant else "",
                "positive_control_refs": ["positive_control"] if variant else [],
            }
        )
        for kind, version in (
            ("observe", "none"),
            ("acceptance", "V0"),
            *(([("acceptance", "V1")]) if v1 else []),
        ):
            step_id = f"{role}-{version}"
            steps.append(
                {
                    "id": step_id,
                    "subject_id": role,
                    "assertion_id": "saved-state",
                    "acceptance_version": version,
                    "kind": kind,
                    "business_observation_step_id": f"{role}-none"
                    if kind == "acceptance"
                    else None,
                    "phase": "final",
                    "binding": {
                        "project_root": f"/private/tmp/counterexample/{role}",
                        "cwd": ".",
                        "argv": ["python", "observe.py", "request"],
                        "witness_input": {
                            "witness_id": "input",
                            "argv_index": 2,
                            "encoding": "text",
                        }
                        if kind == "observe"
                        else None,
                        "effective_environment": environment,
                        "environment_digest": counterexample_digest(environment),
                        "resources": [
                            {
                                "id": "data",
                                "permission_id": "data",
                                "kind": "directory",
                                "root": f"/private/tmp/counterexample/{role}/scratch",
                                "initial_state": ref("evidence/initial-state.json"),
                                "observation_endpoint": f"/private/tmp/counterexample/{role}/scratch/result.json",
                                "cleanup_method": "owned_directory",
                            }
                        ],
                        "timeout_seconds": 5,
                        "max_output_bytes": 4096,
                    },
                    "reservation_seconds": 30,
                    "depends_on": [f"{role}-none"] if kind == "acceptance" else [],
                }
            )
        cleanup = copy.deepcopy(steps[-1])
        cleanup.update(
            id=f"{role}-cleanup",
            kind="cleanup",
            acceptance_version="none",
            business_observation_step_id=None,
            depends_on=[step["id"] for step in steps if step["subject_id"] == role],
        )
        steps.append(cleanup)
    captured_bytes = json.dumps(contract.model_dump(mode="json"), indent=2).encode()
    return {
        "id": "plan-1",
        "work_item_id": "sample",
        "task_id": "T01",
        "loop_id": "sample-implementation",
        "contract_digest": hashlib.sha256(captured_bytes).hexdigest(),
        "contract_model_digest": counterexample_digest(contract),
        "candidate_digest": H,
        "v0_digest": "c" * 64,
        "v1_digest": "d" * 64 if v1 else None,
        "budget_ref": contract.budget_ref.model_dump(),
        "allowed_modified_paths": ["src/save.py"],
        "protected_paths": ["tests/acceptance.py"],
        "witnesses": [
            {
                "id": "input",
                "input_ref": ref(),
                "input_value": {"type": "string", "value": "request"},
                "premise_values": {},
            }
        ],
        "subjects": subjects,
        "steps": steps,
        "max_execution_attempts": len(steps) * 2,
        "generation_batch": 1,
        "reinforcement_batch": 1 if v1 else 0,
        "required_reserve_seconds": 300,
    }


def make_plan(contract=None, v1=True):
    return CounterexamplePlan.model_validate(plan_data(contract, v1))


def task_owned_contract_data():
    data = contract_data()
    original = copy.deepcopy(data["obligations"][0])
    data["obligations"] = []
    for index, task_id in enumerate(("T11", "T21")):
        source = copy.deepcopy(data["sources"][0])
        source.update(
            id=f"task-{task_id}",
            namespace="task",
            task_id=task_id,
            path="specs/sample/tasks.md",
            locator=f"{task_id}/acceptance/1",
        )
        data["sources"].append(source)
        obligation = copy.deepcopy(original)
        obligation["id"] = "save" if index == 0 else "save-other"
        obligation["oracle_spec"]["assertion_id"] = f"saved-state-{task_id}"
        obligation["oracle_spec"]["verification_source_ids"] = [source["id"]]
        data["obligations"].append(obligation)
    return data


def task_owned_plan(contract, task_id):
    obligation = plan_obligations(contract, task_id)[0]
    data = plan_data(contract)
    data.update(id=f"plan-{task_id}", task_id=task_id)
    data["allowed_modified_paths"] = [f"src/save-{task_id}.py"]
    for subject in data["subjects"]:
        subject["obligation_id"] = obligation.id
        if subject["role"] == "variant":
            subject["modified_paths"] = list(data["allowed_modified_paths"])
    for step in data["steps"]:
        step["assertion_id"] = obligation.oracle_spec.assertion_id
    return CounterexamplePlan.model_validate(data)


def test_task_owners_come_from_verification_sources_and_filter_complete_plans():
    contract = VerificationContract.model_validate(task_owned_contract_data())
    assert obligation_task_owners(contract, task_ids=("T11", "T21")) == {
        "save": "T11",
        "save-other": "T21",
    }
    assert all(source.namespace == "spec" for source in contract.sources[:1])
    for task_id in ("T11", "T21"):
        plan = task_owned_plan(contract, task_id)
        validate_plan_contract(contract, plan)
        assert {item.obligation_id for item in plan.subjects} == {
            item.id for item in plan_obligations(contract, task_id)
        }
    stolen = task_owned_plan(contract, "T11").model_copy(update={"task_id": "T21"})
    with pytest.raises(ValueError, match="subject-obligation-not-selected"):
        validate_plan_contract(contract, stolen)


@pytest.mark.parametrize("damage", ["ambiguous", "missing"])
def test_selected_task_owner_is_unique_and_never_partially_inferred(damage):
    data = task_owned_contract_data()
    data["obligations"][0]["oracle_spec"]["verification_source_ids"] = (
        ["task-T11", "task-T21"] if damage == "ambiguous" else ["source-save"]
    )
    with pytest.raises(ValidationError, match="task-owner"):
        VerificationContract.model_validate(data)


def test_reference_in_mechanism_does_not_silently_change_task_owner():
    data = task_owned_contract_data()
    data["obligations"][0]["mechanisms"][0]["source_ids"].append("task-T21")
    contract = VerificationContract.model_validate(data)
    assert obligation_task_owners(contract)["save"] == "T11"
    with pytest.raises(ValueError, match="not-in-frozen-tasks"):
        obligation_task_owners(contract, task_ids=("T11",))


@pytest.mark.parametrize("task_ids", [(), ("T11", "T21"), ("T11", "T11")])
def test_legacy_unowned_contract_requires_an_actual_unique_task(task_ids):
    contract = make_contract()
    original = contract.model_dump_json()
    assert obligation_task_owners(contract) == {"save": None}
    assert obligation_task_owners(contract, task_ids=("T11",)) == {"save": "T11"}
    with pytest.raises(ValueError, match="task"):
        obligation_task_owners(contract, task_ids=task_ids)
    assert contract.model_dump_json() == original


def test_legacy_record_bytes_and_digest_omit_absent_phase_context():
    payload = {
        "schema_version": 1,
        "loop_id": "sample-implementation",
        "task_id": "T11",
        "contract_ref": ref("evidence/contract.json"),
        "plan_ref": ref("evidence/plan.json"),
        "observations_ref": ref("evidence/observations.json"),
        "assessment_ref": ref("evidence/assessment.json"),
        "source_digest_before": H,
        "source_digest_after": H,
        "recorded_at_ms": 1000,
    }
    record = CounterexampleEvidenceRecord.model_validate(payload)
    assert record.model_dump(mode="json") == payload
    assert counterexample_digest(record) == counterexample_digest(payload)
    phased = record.model_copy(
        update={
            "phase_context": CounterexamplePhaseContext(
                require_r2=True,
                observed_at_ms=900,
                review_ref=ref("evidence/r1.json"),
                snapshot_ref=None,
            )
        }
    )
    assert counterexample_digest(phased) != counterexample_digest(record)
    assert (
        CounterexampleEvidenceRecord.model_validate_json(phased.model_dump_json())
        == phased
    )


@pytest.mark.parametrize("require_r2,snapshot", [(True, None), (False, ref())])
def test_phase_context_cannot_replace_native_review_with_author_boolean(
    require_r2, snapshot
):
    with pytest.raises(ValidationError, match="phase-review-origin-required"):
        CounterexamplePhaseContext(
            require_r2=require_r2,
            observed_at_ms=1000,
            review_ref=None,
            snapshot_ref=snapshot,
        )


def test_contract_and_plan_roundtrip_preserve_captured_and_model_hashes():
    contract, plan = make_contract(), make_plan()
    assert (
        VerificationContract.model_validate_json(contract.model_dump_json()) == contract
    )
    assert CounterexamplePlan.model_validate_json(plan.model_dump_json()) == plan
    assert plan.contract_digest != plan.contract_model_digest
    validate_plan_contract(contract, plan)
    validate_contract_sources(
        contract,
        {"specs/sample/spec.md": SOURCE},
        {"specs/sample/spec.md": {"FR-SAVE": ENTRY}},
    )


@pytest.mark.parametrize("version", [True, 1.0, "1", 2])
@pytest.mark.parametrize(
    "factory,model",
    [(contract_data, VerificationContract), (plan_data, CounterexamplePlan)],
)
def test_versions_are_strict_integers(version, factory, model):
    data = factory()
    data["schema_version"] = version
    with pytest.raises(ValidationError, match="schema-version"):
        model.model_validate(data)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["sources"].append(data["sources"][0].copy()),
        lambda data: data["obligations"].append(data["obligations"][0].copy()),
        lambda data: data["obligations"][0].pop("required"),
        lambda data: data["obligations"][0].update(source_id="missing"),
        lambda data: data["obligations"][0]["oracle_spec"].update(
            verification_source_ids=["missing"]
        ),
        lambda data: data["obligations"][0].update(consumer_stages=["requirement"]),
        lambda data: data["obligations"][0].update(close_owner="frontend-evidence"),
        lambda data: data["obligations"][0].update(applicable=False),
        lambda data: data["obligations"][0]["oracle_spec"]["observation_method"].update(
            independent=False
        ),
        lambda data: data["sources"][0].update(namespace="task"),
        lambda data: data["sources"][0].update(path="../spec.md"),
        lambda data: data["sources"][0].update(id="../../escape"),
    ],
)
def test_invalid_contracts_do_not_get_default_success(mutation):
    data = contract_data()
    mutation(data)
    with pytest.raises(ValidationError):
        VerificationContract.model_validate(data)


@pytest.mark.parametrize("stage", ["implementation", "design-contract"])
@pytest.mark.parametrize("spelling", ["lower", "upper", "mixed"])
def test_requirement_source_rejects_future_stage_path_case_aliases(stage, spelling):
    data = contract_data()
    path = f".ai-sdlc/loops/{stage}/sample/report.json"
    if spelling == "upper":
        path = path.upper()
    elif spelling == "mixed":
        path = f".AI-SDLC/Loops/{stage.title()}/sample/report.json"
    data["sources"][0].update(
        namespace="requirement", path=path, loop_id="requirement",
        profile_id="profile", goal_id="goal", obligation_id="obligation",
    )
    with pytest.raises(ValidationError, match="future-stage-source-forbidden"):
        VerificationContract.model_validate(data)


@pytest.mark.parametrize(
    "path",
    [
        "SPECS/Original/Requirements.md",
        "specs/implementation/spec.md",
        "specs/design-contract/spec.md",
        "specs/IMPLEMENTATION/spec.md",
        "specs/Design-Contract/spec.md",
        "specs/loops/implementation/spec.md",
        "specs/loops/design-contract/spec.md",
        "docs/.ai-sdlc/loops/implementation/spec.md",
        "docs/.ai-sdlc/loops/design-contract/spec.md",
    ],
)
def test_requirement_source_keeps_ordinary_path_spelling(path):
    data = contract_data()
    data["sources"][0].update(
        namespace="requirement", path=path, loop_id="requirement",
        profile_id="profile", goal_id="goal", obligation_id="obligation",
    )
    contract = VerificationContract.model_validate(data)
    assert contract.sources[0].path == path
    assert contract.sources[0].sha256 == data["sources"][0]["sha256"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["subjects"][1].update(positive_control_refs=[]),
        lambda data: data["subjects"][1].update(positive_control_refs=["current"]),
        lambda data: data["subjects"][1].update(modified_paths=["tests/acceptance.py"]),
        lambda data: data["subjects"][1].update(parent_candidate_digest="e" * 64),
        lambda data: data["subjects"][1].update(witness_id="missing"),
        lambda data: data["steps"][0].update(depends_on=[data["steps"][-1]["id"]]),
        lambda data: data["steps"][0]["binding"].update(environment_digest="e" * 64),
        lambda data: data.update(max_execution_attempts=1),
    ],
)
def test_plan_identity_scope_and_execution_table_rejections(mutation):
    data = plan_data()
    mutation(data)
    with pytest.raises(ValidationError):
        CounterexamplePlan.model_validate(data)


def test_missing_actual_source_and_entry_are_rejected():
    contract = make_contract()
    with pytest.raises(ValueError, match="source-missing"):
        validate_contract_sources(contract, {}, {})
    with pytest.raises(ValueError, match="entry-missing"):
        validate_contract_sources(contract, {"specs/sample/spec.md": SOURCE}, {})


def test_unfrozen_mechanism_and_incomplete_execution_table_are_rejected():
    contract = make_contract()
    data = plan_data(contract)
    data["subjects"][1]["mechanism_key"] = "unfrozen"
    with pytest.raises(ValueError, match="unfrozen-mechanism"):
        validate_plan_contract(contract, CounterexamplePlan.model_validate(data))
    data = plan_data(contract)
    data["steps"] = [
        step for step in data["steps"] if step["acceptance_version"] != "V1"
    ]
    for step in data["steps"]:
        step["depends_on"] = [
            identifier
            for identifier in step["depends_on"]
            if not identifier.endswith("-V1")
        ]
    with pytest.raises(ValueError, match="acceptance-table-incomplete"):
        validate_plan_contract(contract, CounterexamplePlan.model_validate(data))


def test_business_values_preserve_whitespace_and_unbounded_exact_digits():
    assert TypedValue.from_python(" \n ").value == " \n "
    integer = 10**100 + 1
    assert (
        TypedValue.model_validate_json(
            TypedValue.from_python(integer).model_dump_json()
        ).value
        == integer
    )
    decimal = "123456789012345678901234567890.000000000000000000001"
    assert TypedValue(type="decimal", value=decimal).value == decimal
    assert canonical_decimal("-0001.23000") == "-1.23"
    assert canonical_decimal("-000.000") == "0"


@pytest.mark.parametrize("value", [1.1, float("nan"), {"bad": 1.1}])
def test_binary_floats_are_not_business_exact_values(value):
    with pytest.raises(ValueError):
        TypedValue.from_python(value)


@pytest.mark.parametrize(
    "kind,value",
    [
        ("integer", True),
        ("integer", "1"),
        ("string", 1),
        ("decimal", 1.1),
        ("decimal", "1.0"),
        ("object", {"x": 1}),
    ],
)
def test_typed_payloads_cannot_coerce_business_values(kind, value):
    with pytest.raises(ValidationError):
        TypedValue(type=kind, value=value)


def test_quality_digest_conversion_is_explicit():
    assert source_digest_sha256("sha256:" + H) == H
    with pytest.raises(ValueError):
        source_digest_sha256(H)
    with pytest.raises(ValidationError):
        ArtifactRef(path="evidence/result", sha256="sha256:" + H)


def test_final_phase_is_required_and_resources_cannot_change_between_steps():
    contract = make_contract()
    data = plan_data(contract)
    for step in data["steps"]:
        step["phase"] = "exploration"
    with pytest.raises(ValueError, match="execution-table-incomplete"):
        validate_plan_contract(contract, CounterexamplePlan.model_validate(data))
    data = plan_data(contract)
    data["steps"][1]["binding"]["resources"][0]["initial_state"] = ref(
        "evidence/other-state.json"
    )
    with pytest.raises(ValueError, match="acceptance-business-binding-conflict"):
        validate_plan_contract(contract, CounterexamplePlan.model_validate(data))


def test_step_action_and_protected_scope_cannot_bypass_frozen_permissions():
    data = contract_data()
    data["resource_permissions"][0]["actions"] = ["read"]
    contract = VerificationContract.model_validate(data)
    with pytest.raises(ValueError, match="step-action-not-authorized"):
        validate_plan_contract(contract, make_plan(contract))
    data = plan_data()
    data["allowed_modified_paths"].append("tests/acceptance.py")
    with pytest.raises(ValidationError, match="protected-scope-conflict"):
        CounterexamplePlan.model_validate(data)


@pytest.mark.parametrize(
    "damage", ["missing", "r2-only", "use-after-cleanup", "unordered"]
)
def test_each_enabled_resource_path_must_end_with_ordered_cleanup(damage):
    data = plan_data()
    if damage == "missing":
        data["steps"] = [s for s in data["steps"] if s["kind"] != "cleanup"]
    elif damage == "r2-only":
        for step in data["steps"]:
            if step["kind"] == "cleanup":
                step["phase"] = "r2"
        # 完整 R2 观察表仍存在，不能拿未启用的 R2 清理替代 final。
        repeated = copy.deepcopy([s for s in data["steps"] if s["kind"] != "cleanup"])
        for step in repeated:
            step.update(
                id="r2-" + step["id"],
                phase="r2",
                depends_on=["r2-" + item for item in step["depends_on"]],
                business_observation_step_id=(
                    "r2-" + step["business_observation_step_id"]
                    if step["business_observation_step_id"]
                    else None
                ),
            )
        data["steps"].extend(repeated)
        data["max_execution_attempts"] = 100
    elif damage == "use-after-cleanup":
        extra = copy.deepcopy(data["steps"][0])
        extra.update(id="late-observe", depends_on=["current-cleanup"])
        data["steps"].append(extra)
        for accepted in data["steps"][1:3]:
            linked = copy.deepcopy(accepted)
            linked.update(
                id="late-" + accepted["id"],
                business_observation_step_id=extra["id"],
                depends_on=[extra["id"]],
            )
            data["steps"].append(linked)
    else:
        next(s for s in data["steps"] if s["kind"] == "cleanup")["depends_on"] = []
    with pytest.raises(ValueError, match="resource-.*cleanup"):
        validate_plan_contract(make_contract(), CounterexamplePlan.model_validate(data))


def test_independent_cleanup_phase_preserves_valid_resource_completion():
    data = plan_data()
    for step in data["steps"]:
        if step["kind"] == "cleanup":
            step["phase"] = "cleanup"
    validate_plan_contract(make_contract(), CounterexamplePlan.model_validate(data))


@pytest.mark.parametrize(
    "value,encoding,argument",
    [
        ({"type": "string", "value": " \n "}, "text", " \n "),
        ({"type": "string", "value": "保存"}, "json", '"保存"'),
        ({"type": "boolean", "value": True}, "json", "true"),
        ({"type": "null", "value": None}, "json", "null"),
        ({"type": "integer", "value": 10**100 + 1}, "json", str(10**100 + 1)),
        (
            {"type": "decimal", "value": "0.000000000000000000001"},
            "json",
            "0.000000000000000000001",
        ),
        (
            {
                "type": "object",
                "value": {
                    "b": {"type": "string", "value": "1"},
                    "a": {
                        "type": "array",
                        "value": [
                            {"type": "integer", "value": 1},
                            {"type": "boolean", "value": False},
                        ],
                    },
                },
            },
            "json",
            '{"a":[1,false],"b":"1"}',
        ),
    ],
)
def test_witness_argument_preserves_exact_business_input(value, encoding, argument):
    typed = TypedValue.model_validate(value)
    assert witness_input_argument(typed, encoding) == argument
    data = plan_data()
    data["witnesses"][0]["input_value"] = value
    for step in data["steps"]:
        if step["binding"]["witness_input"] is not None:
            step["binding"]["witness_input"]["encoding"] = encoding
            step["binding"]["argv"][2] = argument
    validate_witness_execution_bindings(CounterexamplePlan.model_validate(data))


@pytest.mark.parametrize(
    "damage,reason",
    [
        ("different-input", "argv-input-mismatch"),
        ("different-witness", "binding-identity-mismatch"),
        ("missing-binding", "observation-witness-binding-required"),
        ("outside-argv", "argv-input-mismatch"),
        ("executable-index", "greater_than_equal"),
        ("boolean-index", "int_type"),
        ("different-encoding", "argv-input-mismatch"),
        ("unbound-exercise", "exercise-witness-binding-required"),
    ],
)
def test_declared_witness_cannot_hide_different_actual_input(damage, reason):
    data = plan_data()
    step = data["steps"][0]
    binding = step["binding"]
    if damage == "different-input":
        binding["argv"][2] = "other-request"
    elif damage == "different-witness":
        binding["witness_input"]["witness_id"] = "another-witness"
    elif damage == "missing-binding":
        binding["witness_input"] = None
    elif damage == "outside-argv":
        binding["witness_input"]["argv_index"] = len(binding["argv"])
    elif damage == "executable-index":
        binding["witness_input"]["argv_index"] = 0
    elif damage == "boolean-index":
        binding["witness_input"]["argv_index"] = True
    elif damage == "different-encoding":
        binding["witness_input"]["encoding"] = "json"
    else:
        step["kind"] = "exercise"
        binding["witness_input"] = None
    with pytest.raises(ValidationError, match=reason):
        CounterexamplePlan.model_validate(data)


def _exercise_then_observe_data():
    data = plan_data()
    observation = data["steps"][0]
    exercise = copy.deepcopy(observation)
    exercise.update(id="current-exercise", kind="exercise")
    observation["binding"]["witness_input"] = None
    observation["depends_on"] = [exercise["id"]]
    data["steps"].insert(0, exercise)
    return data


def test_independent_readback_inherits_same_subject_and_phase_witness():
    data = _exercise_then_observe_data()
    plan = CounterexamplePlan.model_validate(data)
    validate_plan_contract(make_contract(), plan)
    direct = make_plan()
    assert counterexample_digest(plan.steps[0].binding) != counterexample_digest(
        plan.steps[1].binding
    )
    assert direct.steps[0].binding.witness_input is not None


@pytest.mark.parametrize("damage", ["different-subject", "different-phase", "no-edge"])
def test_readback_cannot_borrow_unrelated_execution_witness(damage):
    data = _exercise_then_observe_data()
    if damage == "different-subject":
        data["steps"][0]["subject_id"] = "positive_control"
    elif damage == "different-phase":
        data["steps"][0]["phase"] = "exploration"
    else:
        data["steps"][1]["depends_on"] = []
    with pytest.raises(ValidationError, match="observation-witness-binding-required"):
        CounterexamplePlan.model_validate(data)


def test_readonly_witness_validation_rechecks_mutable_typed_payloads():
    plan = make_plan()
    value = plan.witnesses[0].input_value
    object.__setattr__(value, "value", "changed-after-construction")
    with pytest.raises(ValueError, match="argv-input-mismatch"):
        validate_witness_execution_bindings(plan)


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "nonacceptance",
        "subject",
        "phase",
        "no-edge",
        "before-observe",
        "resource",
        "reset",
        "exercise",
        "cleanup",
    ],
)
def test_acceptance_must_bind_an_unchanged_preceding_business_observation(damage):
    data = plan_data()
    observed, accepted = data["steps"][0:2]
    if damage == "missing":
        accepted.pop("business_observation_step_id")
    elif damage == "nonacceptance":
        observed["business_observation_step_id"] = observed["id"]
    elif damage == "subject":
        accepted["business_observation_step_id"] = "variant-none"
    elif damage == "phase":
        observed["phase"] = "exploration"
    elif damage == "no-edge":
        accepted["depends_on"] = []
    elif damage == "before-observe":
        accepted["depends_on"] = []
        data["steps"][0:2] = [accepted, observed]
    elif damage == "resource":
        accepted["binding"]["resources"][0]["observation_endpoint"] += ".other"
    else:
        changed = copy.deepcopy(observed)
        changed.update(id="state-change", kind=damage)
        data["steps"].insert(1, changed)
    with pytest.raises(ValidationError, match="acceptance|nonacceptance"):
        CounterexamplePlan.model_validate(data)


def test_transitive_observation_dependency_is_valid_and_rechecked_at_dispatch():
    data = plan_data()
    data["steps"][2]["depends_on"] = [data["steps"][1]["id"]]
    plan = CounterexamplePlan.model_validate(data)
    validate_plan_contract(make_contract(), plan)
    object.__setattr__(plan.steps[2], "business_observation_step_id", "variant-none")
    with pytest.raises(ValueError, match="acceptance-business-binding-conflict"):
        validate_plan_contract(make_contract(), plan)


@pytest.mark.parametrize("change_kind", ["reset", "cleanup", "exercise"])
@pytest.mark.parametrize("dependency", ["direct", "indirect", "not-ancestor"])
@pytest.mark.parametrize("entry", ["construction", "contract-readback"])
def test_inherited_witness_rejects_intervening_shared_state_change(
    change_kind, dependency, entry
):
    data = _exercise_then_observe_data()
    valid = CounterexamplePlan.model_validate(data)
    exercise, observed = data["steps"][:2]
    change = copy.deepcopy(exercise)
    change.update(id="state-change", kind=change_kind, depends_on=[exercise["id"]])
    if change_kind == "exercise":
        change["phase"] = "exploration"
    if dependency == "direct":
        observed["depends_on"] = [change["id"]]
    elif dependency == "indirect":
        middle = copy.deepcopy(change)
        middle.update(id="indirect-change", depends_on=[change["id"]])
        observed["depends_on"] = [middle["id"]]
        data["steps"].insert(1, middle)
    data["steps"].insert(1, change)
    for cleanup in data["steps"]:
        if cleanup["kind"] == "cleanup" and cleanup["id"] == "current-cleanup":
            cleanup["depends_on"].extend(
                s["id"]
                for s in data["steps"]
                if s["id"] in {"state-change", "indirect-change"}
            )
    with pytest.raises(ValueError, match="observation-witness-state-invalidated"):
        if entry == "construction":
            CounterexamplePlan.model_validate(data)
        else:
            altered = valid.model_copy(
                update={
                    "steps": tuple(
                        type(valid.steps[0]).model_validate(s) for s in data["steps"]
                    )
                }
            )
            validate_plan_contract(make_contract(), altered)


def test_inherited_witness_requires_exercise_covering_observed_resources():
    data = _exercise_then_observe_data()
    data["steps"][0]["binding"]["resources"][0]["root"] += "-other"
    with pytest.raises(ValueError, match="observation-witness-binding-required"):
        CounterexamplePlan.model_validate(data)


@pytest.mark.parametrize(
    "control", ["new-exercise", "unrelated-reset", "direct-observe"]
)
def test_inherited_witness_state_controls_remain_legal(control):
    data = _exercise_then_observe_data()
    exercise, observed = data["steps"][:2]
    reset = copy.deepcopy(exercise)
    reset.update(id="state-reset", kind="reset", depends_on=[exercise["id"]])
    observed["depends_on"] = [reset["id"]]
    data["steps"].insert(1, reset)
    if control == "new-exercise":
        fresh = copy.deepcopy(exercise)
        fresh.update(id="fresh-exercise", depends_on=[reset["id"]])
        observed["depends_on"] = [fresh["id"]]
        data["steps"].insert(2, fresh)
    elif control == "unrelated-reset":
        other = next(s for s in data["steps"] if s["id"] == "positive_control-cleanup")
        reset["subject_id"] = "positive_control"
        reset["binding"] = copy.deepcopy(other["binding"])
        other["depends_on"].append(reset["id"])
    else:
        observed["binding"]["witness_input"] = copy.deepcopy(
            exercise["binding"]["witness_input"]
        )
    plan = CounterexamplePlan.model_validate(data)
    validate_plan_contract(make_contract(), plan)


@pytest.mark.parametrize("damage", [False, True])
def test_model_witness_attributes_each_observed_resource_to_its_latest_exercise(damage):
    # 此处只核模型的见证归因；合同仍保留每个对象资源组一致的原有约束。
    data = _exercise_then_observe_data()
    exercise, observed = data["steps"][:2]
    other_resource = copy.deepcopy(exercise["binding"]["resources"][0])
    other_resource.update(
        id="other-data",
        root="/private/tmp/counterexample/current/other",
        observation_endpoint="/private/tmp/counterexample/current/other/result.json",
    )
    other = copy.deepcopy(exercise)
    other.update(id="other-exercise", depends_on=[exercise["id"]])
    other["binding"]["resources"] = [other_resource]
    observed["binding"]["resources"].append(copy.deepcopy(other_resource))
    for accepted in data["steps"]:
        if accepted["business_observation_step_id"] == observed["id"]:
            accepted["binding"]["resources"] = copy.deepcopy(
                observed["binding"]["resources"]
            )
    observed["depends_on"] = [other["id"]]
    data["steps"].insert(1, other)
    if damage:
        reset = copy.deepcopy(exercise)
        reset.update(id="reset-first-resource", kind="reset", depends_on=[other["id"]])
        observed["depends_on"] = [reset["id"]]
        data["steps"].insert(2, reset)
        with pytest.raises(ValueError, match="observation-witness-state-invalidated"):
            CounterexamplePlan.model_validate(data)
    else:
        validate_witness_execution_bindings(CounterexamplePlan.model_validate(data))


def test_model_unrelated_exercise_does_not_hide_prior_resource_witness():
    data = _exercise_then_observe_data()
    exercise, observed = data["steps"][:2]
    other = copy.deepcopy(exercise)
    other.update(id="unrelated-exercise", depends_on=[exercise["id"]])
    resource = other["binding"]["resources"][0]
    resource.update(
        id="other-data",
        root="/private/tmp/counterexample/current/other",
        observation_endpoint="/private/tmp/counterexample/current/other/result.json",
    )
    observed["depends_on"] = [other["id"]]
    data["steps"].insert(1, other)
    validate_witness_execution_bindings(CounterexamplePlan.model_validate(data))
