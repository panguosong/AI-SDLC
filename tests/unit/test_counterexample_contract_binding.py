"""上游原合同捕获和继承边界；临时项目不迁移当前开发 Loop。"""

import hashlib
import json
from dataclasses import replace

import pytest

from ai_sdlc.core.counterexample_models import (
    VerificationContract,
    validate_contract_sources,
)
from ai_sdlc.core.design_contract_loop import check_design_contract_loop
from ai_sdlc.core.design_contract_models import (
    DesignContractCheckOptions,
    DesignContractClose,
    DesignContractInput,
)
from ai_sdlc.core.design_contract_store import (
    _verification_spec_entry,
    _verification_task_entry,
    build_contract_input,
    design_contract_artifacts,
    design_contract_input_digest,
    read_verification_contract,
    verification_binding,
)
from ai_sdlc.core.implementation_models import ImplementationInput
from ai_sdlc.core.implementation_store import (
    implementation_input_digest,
    validate_implementation_verification_contract,
)
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_models import LoopRun
from ai_sdlc.core.loop_stage_input import (
    stage_input_identity,
    validate_stage_material_update,
)
from tests.unit.test_counterexample_models import contract_data
from tests.unit.test_design_contract_loop import _write_work_item


@pytest.mark.parametrize("intro", ["", "Payment must remain durable.\n\n"])
@pytest.mark.parametrize("boundary", ["## Refund", "# Other requirements"])
def test_heading_source_includes_descendants_until_same_or_higher_heading(
    intro, boundary
):
    descendants = (
        "### Retry rules\nRetry a failed payment.\n\n"
        "#### Limit\nAllow at most three retries.\n\n"
        "### Receipt\nKeep the payment receipt."
    )
    source = (
        "# Requirements\nUnrelated introduction.\n\n"
        f"## Payment\n{intro}{descendants}\n\n"
        f"{boundary}\nOutside the payment requirement.\n"
    ).encode()

    assert _verification_spec_entry(source, "Payment") == (
        intro + descendants
    ).encode()


def test_heading_source_subsection_change_updates_entry_digest():
    original = b"## Payment\nKeep payments durable.\n### Retry\nAllow three retries.\n"
    changed = original.replace(b"three retries", b"one retry")

    before = hashlib.sha256(_verification_spec_entry(original, "Payment")).hexdigest()
    after = hashlib.sha256(_verification_spec_entry(changed, "Payment")).hexdigest()

    assert before != after


def test_heading_source_rejects_changed_subsection_with_stale_entry_digest():
    original = b"## Payment\nKeep payments durable.\n### Retry\nAllow three retries.\n"
    changed = original.replace(b"three retries", b"one retry")
    original_entry = _verification_spec_entry(original, "Payment")
    data = contract_data()
    data["sources"][0].update(
        locator="Payment",
        sha256=hashlib.sha256(original).hexdigest(),
        entry_sha256=hashlib.sha256(original_entry).hexdigest(),
    )
    contract = VerificationContract.model_validate(data)
    path = contract.sources[0].path
    validate_contract_sources(
        contract, {path: original}, {path: {"Payment": original_entry}}
    )

    # 即使文件摘要已更新，子要求变化也必须使旧条目摘要失效。
    data["sources"][0]["sha256"] = hashlib.sha256(changed).hexdigest()
    stale_entry = VerificationContract.model_validate(data)
    with pytest.raises(ValueError, match="original-entry-missing-or-stale"):
        validate_contract_sources(
            stale_entry,
            {path: changed},
            {path: {"Payment": _verification_spec_entry(changed, "Payment")}},
        )


def _add_task_owner(root, work, data, task_id="T11"):
    content = (work / "tasks.md").read_bytes()
    locator = f"{task_id}/acceptance/1"
    source_id = f"owner-{task_id}"
    data["sources"].append(
        {
            "id": source_id,
            "namespace": "task",
            "task_id": task_id,
            "path": (work / "tasks.md").relative_to(root).as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "locator": locator,
            "entry_sha256": hashlib.sha256(
                _verification_task_entry(content, task_id, locator)
            ).hexdigest(),
        }
    )
    data["obligations"][0]["oracle_spec"]["verification_source_ids"].append(source_id)


def _contract(root, namespace="spec"):
    work = _write_work_item(root)
    data = contract_data()
    data["work_item_id"] = work.name
    path = work / ("tasks.md" if namespace == "task" else "spec.md")
    content = path.read_bytes()
    locator = "T11/acceptance/1" if namespace == "task" else "FR-DEMO-001"
    entry = (
        _verification_task_entry(content, "T11", locator)
        if namespace == "task"
        else _verification_spec_entry(content, locator)
    )
    data["sources"][0].update(
        namespace=namespace,
        path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(content).hexdigest(),
        locator=locator,
        entry_sha256=hashlib.sha256(entry).hexdigest(),
    )
    if namespace == "task":
        data["sources"][0]["task_id"] = "T11"
    data["budget_ref"] = {
        "path": "specs/demo-contract/spec.md",
        "sha256": hashlib.sha256((work / "spec.md").read_bytes()).hexdigest(),
    }
    raw = json.dumps(data, ensure_ascii=False, indent=3).encode()
    (work / "verification.json").write_bytes(raw)
    options = DesignContractCheckOptions(
        root=root,
        work_item="specs/demo-contract",
        loop_id="design",
        decision_mode="adaptive-quantified",
        decision_capability="stage-simulation-v1",
        verification_contract="specs/demo-contract/verification.json",
    )
    return work, raw, options


@pytest.mark.parametrize("stage", ["implementation", "design-contract"])
@pytest.mark.parametrize("spelling", ["lower", "upper", "mixed"])
def test_design_budget_rejects_future_stage_path_case_aliases(
    root_tmp_path, stage, spelling
):
    root = root_tmp_path
    work, _, options = _contract(root)
    path = f".ai-sdlc/loops/{stage}/sample/budget.md"
    if spelling == "upper":
        path = path.upper()
    elif spelling == "mixed":
        path = f".AI-SDLC/Loops/{stage.title()}/sample/budget.md"
    budget = b"Existing operation budget.\n"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(budget)
    data = json.loads((work / "verification.json").read_bytes())
    data["budget_ref"] = {"path": path, "sha256": hashlib.sha256(budget).hexdigest()}
    (work / "verification.json").write_text(json.dumps(data), encoding="utf-8")

    result = check_design_contract_loop(options)

    assert result.status == "blocked", result
    assert "future-stage-budget-forbidden" in result.blocker
    assert not design_contract_artifacts(root, "design").input_path.exists()
    assert target.read_bytes() == budget


@pytest.mark.parametrize(
    "path",
    [
        "SPECS/Original/Budget.md",
        "specs/implementation/budget.md",
        "specs/design-contract/budget.md",
        "specs/loops/implementation/budget.md",
        "specs/Loops/Design-Contract/budget.md",
        "docs/.ai-sdlc/loops/implementation/budget.md",
        "docs/.ai-sdlc/loops/design-contract/budget.md",
    ],
)
def test_design_budget_keeps_ordinary_path_spelling(root_tmp_path, path):
    root = root_tmp_path
    work, _, options = _contract(root)
    budget = b"Existing operation budget.\n"
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(budget)
    data = json.loads((work / "verification.json").read_bytes())
    data["budget_ref"] = {"path": path, "sha256": hashlib.sha256(budget).hexdigest()}
    (work / "verification.json").write_text(json.dumps(data), encoding="utf-8")

    result = check_design_contract_loop(options)

    assert result.status == "ready", result
    frozen = DesignContractInput.model_validate_json(
        design_contract_artifacts(root, "design").input_path.read_bytes()
    )
    contract, material = read_verification_contract(root, frozen)
    assert contract.budget_ref.path == path
    assert material[path] == budget


@pytest.mark.parametrize("namespace", ["spec", "task"])
def test_first_check_captures_exact_contract_then_reuses_it(root_tmp_path, namespace):
    root = root_tmp_path
    work, raw, options = _contract(root, namespace)
    assert (
        check_design_contract_loop(replace(options, dry_run=True)).status == "dry_run"
    )
    assert not design_contract_artifacts(root, "design").input_path.exists()
    result = check_design_contract_loop(options)
    assert result.status == "ready", result
    path = design_contract_artifacts(root, "design").input_path
    frozen = DesignContractInput.model_validate_json(path.read_bytes())
    assert (root / frozen.verification_contract_ref).read_bytes() == raw
    assert frozen.verification_contract_digest == hashlib.sha256(raw).hexdigest()
    (work / "verification.json").unlink()
    result = check_design_contract_loop(replace(options, verification_contract=""))
    assert result.status == "ready", result
    fresh = DesignContractInput.model_validate_json(path.read_bytes())
    assert verification_binding(fresh) == verification_binding(frozen)
    contract, material = read_verification_contract(root, fresh)
    assert contract is not None
    assert fresh.verification_contract_ref in material
    assert fresh.tasks_path in material
    assert read_verification_contract(root, fresh, material)[0] == contract
    with pytest.raises(ValueError, match="captured-source-missing"):
        read_verification_contract(root, fresh, {})


@pytest.mark.parametrize("explicit_owner", [False, True])
def test_frozen_full_task_set_requires_owner_even_when_other_task_is_optional(
    root_tmp_path, explicit_owner
):
    root = root_tmp_path
    work, _, options = _contract(root)
    with (work / "tasks.md").open("a", encoding="utf-8") as stream:
        stream.write(
            "\n### Task 2: Documentation\n\n- **任务编号**：T21\n- **优先级**：P2\n- **验收标准**：Describe usage.\n- **验证**：python -m pytest tests/\n"
        )
    data = json.loads((work / "verification.json").read_bytes())
    if explicit_owner:
        _add_task_owner(root, work, data)
        (work / "verification.json").write_text(json.dumps(data), encoding="utf-8")
    result = check_design_contract_loop(options)
    if not explicit_owner:
        assert result.status == "blocked"
        assert "task-owner-required" in result.blocker
        assert not design_contract_artifacts(root, "design").input_path.exists()
    else:
        assert result.status == "ready", result
        frozen = DesignContractInput.model_validate_json(
            design_contract_artifacts(root, "design").input_path.read_bytes()
        )
        contract, material = read_verification_contract(root, frozen)
        assert contract is not None
        assert material[frozen.tasks_path] == (work / "tasks.md").read_bytes()


def test_captured_contract_cannot_omit_full_task_set_or_read_changed_tasks(
    root_tmp_path,
):
    root = root_tmp_path
    work, _, options = _contract(root)
    assert check_design_contract_loop(options).status == "ready"
    frozen = DesignContractInput.model_validate_json(
        design_contract_artifacts(root, "design").input_path.read_bytes()
    )
    _, material = read_verification_contract(root, frozen)
    incomplete = {
        path: raw for path, raw in material.items() if path != frozen.tasks_path
    }
    with pytest.raises(ValueError, match="captured-source-missing"):
        read_verification_contract(root, frozen, incomplete)
    with (work / "tasks.md").open("a", encoding="utf-8") as stream:
        stream.write("\n### Task 2: Changed\n- **任务编号**：T21\n")
    with pytest.raises(ValueError, match="tasks-digest-mismatch"):
        read_verification_contract(root, frozen)


@pytest.fixture
def root_tmp_path(tmp_path):
    return tmp_path.resolve()


def test_contract_replacement_or_field_addition_cannot_refresh_identity(root_tmp_path):
    root = root_tmp_path
    work, _, options = _contract(root)
    assert check_design_contract_loop(options).status == "ready"
    artifacts = design_contract_artifacts(root, "design")
    before = artifacts.input_path.read_bytes()
    payload = json.loads((work / "verification.json").read_bytes())
    payload["obligations"][0]["selection_reason"] = "不同合同"
    (work / "verification.json").write_text(json.dumps(payload))
    result = check_design_contract_loop(options)
    assert result.status == "blocked"
    assert "input-identity-change" in result.blocker
    assert artifacts.input_path.read_bytes() == before
    original = DesignContractInput.model_validate_json(before)
    old = original.model_copy(
        update={key: None for key in verification_binding(original)}
    )
    assert stage_input_identity("design-contract", old) != stage_input_identity(
        "design-contract", original
    )
    with pytest.raises(ValueError, match="input-identity-change"):
        validate_stage_material_update(
            root, "design-contract", artifacts.loop_dir, old, original
        )


@pytest.mark.parametrize(
    "mode,capability", [("legacy", None), ("adaptive-quantified", None)]
)
def test_unsupported_modes_reject_requested_contract_without_writes(
    root_tmp_path, mode, capability
):
    root = root_tmp_path
    _, _, options = _contract(root)
    result = check_design_contract_loop(
        replace(options, decision_mode=mode, decision_capability=capability)
    )
    assert result.status == "blocked"
    assert not design_contract_artifacts(root, "design").input_path.exists()


def test_old_inputs_omit_fields_and_keep_digests(root_tmp_path):
    root = root_tmp_path
    work = _write_work_item(root)
    original = build_contract_input(
        root=root,
        loop_id="legacy",
        work_item_dir=work,
        requirement_loop_id="req-current",
    )
    before = original.model_dump(mode="json")
    assert not any(key.startswith("verification_") for key in before)
    old = DesignContractInput.model_validate(before)
    assert old.model_dump_json() == original.model_dump_json()
    assert design_contract_input_digest(old) == design_contract_input_digest(original)
    impl = ImplementationInput(
        loop_id="impl",
        work_item_id=work.name,
        work_item_path="specs/demo-contract",
        spec_path="specs/demo-contract/spec.md",
        plan_path="specs/demo-contract/plan.md",
        tasks_path="specs/demo-contract/tasks.md",
        design_contract_loop_id="legacy",
    )
    assert not any(
        key.startswith("verification_") for key in impl.model_dump(mode="json")
    )
    assert implementation_input_digest(
        ImplementationInput.model_validate_json(impl.model_dump_json())
    ) == implementation_input_digest(impl)


def _closed_upstream(root):
    _, _, options = _contract(root)
    assert check_design_contract_loop(options).status == "ready"
    artifacts = design_contract_artifacts(root, "design")
    frozen = DesignContractInput.model_validate_json(artifacts.input_path.read_bytes())
    store = LoopArtifactStore(root)
    run = LoopRun.model_validate_json(artifacts.loop_run_path.read_bytes())
    # 该 fixture 仅隔离存储边界；正式 R1/Close 路径由生命周期集成覆盖。
    store.write_json_artifact(
        artifacts.loop_run_path, run.model_copy(update={"status": "closed"})
    )
    store.write_json_artifact(
        artifacts.close_path,
        DesignContractClose(
            loop_id="design",
            report_path=artifacts.report_json_path.relative_to(root).as_posix(),
        ),
    )
    impl = ImplementationInput(
        loop_id="impl",
        work_item_id=frozen.work_item_id,
        work_item_path=frozen.work_item_path,
        spec_path=frozen.spec_path,
        plan_path=frozen.plan_path,
        tasks_path=frozen.tasks_path,
        design_contract_loop_id="design",
        decision_mode="adaptive-quantified",
        decision_capability="stage-simulation-v1",
        **verification_binding(frozen),
    )
    return frozen, impl, artifacts


def test_implementation_inherits_whole_contract_and_rejects_removal(root_tmp_path):
    root = root_tmp_path
    frozen, impl, artifacts = _closed_upstream(root)
    contract, material = validate_implementation_verification_contract(root, impl)
    assert contract.work_item_id == impl.work_item_id
    assert (
        validate_implementation_verification_contract(root, impl, material)[0]
        == contract
    )
    removed = impl.model_copy(update={key: None for key in verification_binding(impl)})
    with pytest.raises(ValueError, match="upstream-verification-identity-mismatch"):
        validate_implementation_verification_contract(root, removed)
    artifacts.close_path.unlink()
    with pytest.raises((ValueError, OSError)):
        validate_implementation_verification_contract(root, impl)
    (root / frozen.verification_contract_ref).unlink()
    with pytest.raises((ValueError, OSError)):
        read_verification_contract(root, frozen)


@pytest.mark.parametrize(
    "fault", ["contract", "source", "entry", "traversal", "symlink", "capability"]
)
def test_original_content_and_paths_fail_closed(root_tmp_path, fault):
    root = root_tmp_path
    work, _, options = _contract(root)
    path = work / "verification.json"
    data = json.loads(path.read_bytes())
    if fault == "contract":
        data["work_item_id"] = "different"
    elif fault == "source":
        data["sources"][0]["sha256"] = "a" * 64
    elif fault == "entry":
        data["sources"][0]["entry_sha256"] = "a" * 64
    elif fault == "traversal":
        data["sources"][0]["path"] = "../spec.md"
    elif fault == "symlink":
        spec = work / "spec.md"
        moved = work / "saved-spec.md"
        spec.rename(moved)
        spec.symlink_to(moved)
    else:
        data["verification_capability"] = "unknown"
    path.write_text(json.dumps(data))
    result = check_design_contract_loop(options)
    assert result.status == "blocked", result
    assert not design_contract_artifacts(root, "design").input_path.exists()


def test_requirement_references_use_closed_original_ids_and_business_source(
    initialized_project_dir,
):
    from tests.integration.test_quantified_implementation import (
        _cli,
        _payload,
        _ready_project,
    )
    from tests.integration.test_stage_quantified_pipeline import (
        LOOP,
        WORK_ITEM,
        actual_record,
        stage_apply,
        stage_selected,
    )

    root = initialized_project_dir.resolve()
    _ready_project(root)
    result = _cli(
        root,
        "loop",
        "requirement",
        "start",
        "--idea",
        "原任务规范",
        "--acceptance",
        "满足原任务规范",
        "--work-item-id",
        "demo-implementation-loop",
        "--design-scope-family",
        "implementation",
        "--loop-id",
        LOOP,
        "--decision-mode",
        "adaptive-quantified",
        "--decision-capability",
        "stage-simulation-v1",
        "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    context = stage_selected(root, "requirement", start=False)
    stage_apply(
        root, "requirement", {"operation": "seal-for-review", "request_id": "seal"}
    )
    reviewed = _payload(
        _cli(
            root, "loop", "review", "--type", "requirement", "--loop-id", LOOP, "--json"
        )
    )
    assert actual_record(root, "requirement", reviewed)["status"] == "passed"
    result = _cli(
        root,
        "loop",
        "requirement",
        "freeze",
        "--loop-id",
        LOOP,
        "--expect-review-digest",
        reviewed["input_digest"],
        "--yes",
        "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    source_path = WORK_ITEM + "/spec.md"
    source = (root / source_path).read_bytes()
    data = contract_data()
    data["work_item_id"] = "demo-implementation-loop"
    data["sources"][0].update(
        namespace="requirement",
        path=source_path,
        sha256=hashlib.sha256(source).hexdigest(),
        locator="FR-IMPL-001",
        entry_sha256=hashlib.sha256(
            _verification_spec_entry(source, "FR-IMPL-001")
        ).hexdigest(),
        loop_id=LOOP,
        profile_id=context.contracts[0].profile_id,
        goal_id="g0",
        obligation_id="o0",
        criterion_id="coverage",
    )
    data["budget_ref"] = {
        "path": source_path,
        "sha256": hashlib.sha256(source).hexdigest(),
    }
    _add_task_owner(root, root / WORK_ITEM, data)
    contract_path = root / WORK_ITEM / "verification.json"
    contract_path.write_text(json.dumps(data))
    options = DesignContractCheckOptions(
        root=root,
        work_item=WORK_ITEM,
        loop_id="new-design",
        decision_mode="adaptive-quantified",
        decision_capability="stage-simulation-v1",
        verification_contract=contract_path.relative_to(root).as_posix(),
    )
    result = check_design_contract_loop(options)
    assert result.status == "ready", result
    frozen = DesignContractInput.model_validate_json(
        design_contract_artifacts(root, "new-design").input_path.read_bytes()
    )
    contract, material = read_verification_contract(root, frozen)
    assert contract.sources[0].path == source_path
    assert f".ai-sdlc/loops/requirement/{LOOP}/decision-context.json" in material
    assert read_verification_contract(root, frozen, material)[0] == contract
    for field in ("loop_id", "profile_id", "goal_id", "obligation_id", "criterion_id"):
        altered = json.loads(json.dumps(data))
        altered["sources"][0][field] = "missing"
        contract_path.write_text(json.dumps(altered))
        result = check_design_contract_loop(replace(options, loop_id=f"bad-{field}"))
        assert result.status == "blocked", (field, result)
        assert not design_contract_artifacts(root, f"bad-{field}").input_path.exists()


def test_capture_write_failure_leaves_no_referencing_input(root_tmp_path, monkeypatch):
    root = root_tmp_path
    _, _, options = _contract(root)

    def unavailable(*args, **kwargs):
        raise PermissionError("contract capture unavailable")

    monkeypatch.setattr(LoopArtifactStore, "write_bytes_artifact", unavailable)
    result = check_design_contract_loop(options)
    assert result.status == "blocked"
    assert "capture unavailable" in result.blocker
    assert not design_contract_artifacts(root, "design").input_path.exists()
