"""阶段成果可以更新，但同 ID 不得换目标、身份或重置已有决策。"""

import json
import time

import pytest

from ai_sdlc.core.requirement_loop import (
    RequirementStartOptions,
    start_requirement_loop,
)


def requirement_start(root, **changes):
    values = dict(
        root=root,
        loop_id="stage-req",
        idea="仅在既有范围内验证访问授权与异常路径。",
        acceptance=("合法用户可读取",),
        decision_mode="adaptive-quantified",
        decision_capability="stage-simulation-v1",
    )
    values.update(changes)
    return start_requirement_loop(RequirementStartOptions(**values))


def test_explicit_requirement_source_cannot_overwrite_stage_with_legacy(tmp_path):
    assert requirement_start(tmp_path).status == "ready"
    directory = tmp_path / ".ai-sdlc/loops/requirement/stage-req"
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    result = requirement_start(
        tmp_path, decision_mode="legacy", decision_capability=None
    )
    assert result.status == "blocked"
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before


def test_same_requirement_id_cannot_change_original_goal(tmp_path):
    assert requirement_start(tmp_path).status == "ready"
    result = requirement_start(tmp_path, idea="新增其他业务与无限范围")
    assert result.status == "blocked"
    assert "identity" in result.blocker


def test_requirement_output_update_keeps_original_run_identity(tmp_path):
    assert requirement_start(tmp_path).status == "ready"
    path = tmp_path / ".ai-sdlc/loops/requirement/stage-req/loop-run.json"
    payload = json.loads(path.read_text())
    payload["created_at"] = "2020-01-01T00:00:00Z"
    path.write_text(json.dumps(payload))
    result = requirement_start(tmp_path, idea="", acceptance=("异常路径拒绝",))
    assert result.status == "ready"
    assert json.loads(path.read_text())["created_at"] == "2020-01-01T00:00:00Z"


def test_design_recheck_cannot_replace_frozen_spec_goal(tmp_path):
    from ai_sdlc.core.design_contract_loop import (
        DesignContractCheckOptions,
        check_design_contract_loop,
    )
    from tests.unit.test_design_contract_loop import _write_work_item

    work_item = _write_work_item(tmp_path)
    options = DesignContractCheckOptions(
        root=tmp_path,
        work_item="specs/demo-contract",
        loop_id="stage-design",
        decision_mode="adaptive-quantified",
        decision_capability="stage-simulation-v1",
    )
    assert check_design_contract_loop(options).status == "ready"
    (work_item / "spec.md").write_text(
        (work_item / "spec.md").read_text() + "\n改变原目标。\n"
    )
    result = check_design_contract_loop(options)
    assert result.status == "blocked"
    assert "identity" in result.blocker


def test_design_schema_fix_without_ce_cannot_replace_frozen_spec(tmp_path):
    from ai_sdlc.core.design_contract_loop import (
        DesignContractCheckOptions,
        check_design_contract_loop,
    )
    from tests.unit.test_design_contract_loop import _write_work_item

    work_item = _write_work_item(tmp_path)
    plan = work_item / "plan.md"
    original_plan = plan.read_bytes()
    plan.write_text("# 实施计划\n", encoding="utf-8")
    options = DesignContractCheckOptions(
        root=tmp_path,
        work_item="specs/demo-contract",
        loop_id="stage-design",
        decision_mode="adaptive-quantified",
        decision_capability="stage-simulation-v1",
    )
    assert check_design_contract_loop(options).status == "needs_fix"
    directory = tmp_path / ".ai-sdlc/loops/design-contract/stage-design"
    before = {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }
    plan.write_bytes(original_plan)
    spec = work_item / "spec.md"
    spec.write_text(spec.read_text() + "\n改变原目标。\n", encoding="utf-8")

    result = check_design_contract_loop(options)

    assert result.status == "blocked"
    assert "identity" in result.blocker
    assert {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    } == before


@pytest.mark.parametrize("initially_ready", [False, True])
def test_design_without_ce_can_update_plan_and_tasks_with_same_spec(
    tmp_path, initially_ready
):
    from ai_sdlc.core.design_contract_loop import (
        DesignContractCheckOptions,
        check_design_contract_loop,
    )
    from tests.unit.test_design_contract_loop import _write_work_item

    work_item = _write_work_item(tmp_path)
    spec = (work_item / "spec.md").read_bytes()
    plan = work_item / "plan.md"
    original_plan = plan.read_bytes()
    if not initially_ready:
        plan.write_text("# 实施计划\n", encoding="utf-8")
    options = DesignContractCheckOptions(
        root=tmp_path,
        work_item="specs/demo-contract",
        loop_id="stage-design",
        decision_mode="adaptive-quantified",
        decision_capability="stage-simulation-v1",
    )
    first = check_design_contract_loop(options)
    assert first.status == ("ready" if initially_ready else "needs_fix")
    directory = tmp_path / ".ai-sdlc/loops/design-contract/stage-design"
    before = json.loads((directory / "loop-run.json").read_bytes())
    assert before.get("decision_started_at_ms") is None
    plan.write_bytes(original_plan + "\n实施说明补齐。\n".encode())
    tasks = work_item / "tasks.md"
    tasks.write_text(tasks.read_text() + "\n任务实施说明补齐。\n", encoding="utf-8")

    result = check_design_contract_loop(options)

    assert result.status == "ready", result
    after = json.loads((directory / "loop-run.json").read_bytes())
    assert (work_item / "spec.md").read_bytes() == spec
    assert after["created_at"] == before["created_at"]
    assert after["current_round"] == before["current_round"] == 1
    assert len(after["rounds"]) == len(before["rounds"]) == 1
    assert after.get("decision_started_at_ms") is None
    assert not (directory / "decision-context.json").exists()
    assert not list(directory.glob("review-outcome-*.json"))


def test_legacy_design_recheck_keeps_existing_spec_edit_behavior(tmp_path):
    from ai_sdlc.core.design_contract_loop import (
        DesignContractCheckOptions,
        check_design_contract_loop,
    )
    from tests.unit.test_design_contract_loop import _write_work_item

    work_item = _write_work_item(tmp_path)
    options = DesignContractCheckOptions(
        root=tmp_path, work_item="specs/demo-contract", loop_id="legacy-design"
    )
    assert check_design_contract_loop(options).status == "ready"
    spec = work_item / "spec.md"
    spec.write_text(spec.read_text() + "\n补充原有合同说明。\n", encoding="utf-8")

    assert check_design_contract_loop(options).status == "ready"


def bind_requirement_context(root, *, selected=False, sealed=False):
    from ai_sdlc.core.loop_simulation_context import (
        SimulationPrepareRequest,
        transition_simulation,
    )
    from ai_sdlc.core.loop_stage_input import stage_input_identity
    from ai_sdlc.core.requirement_loop import RequirementIntake
    from tests.unit.test_loop_simulation import assessment_data, candidate_data
    from tests.unit.test_loop_simulation_models import contract_data

    directory = root / ".ai-sdlc/loops/requirement/stage-req"
    intake = RequirementIntake.model_validate_json(
        (directory / "requirement-intake.json").read_bytes()
    )
    contract = {
        **contract_data(),
        "capability": "stage-simulation-v1",
        "loop_type": "requirement",
        "profile_id": "requirement-analysis-v1",
    }
    now = time.time_ns() // 1_000_000
    context = None

    def step(payload):
        nonlocal context
        context = transition_simulation(
            context,
            SimulationPrepareRequest.model_validate(payload),
            loop_id="stage-req",
            input_digest=stage_input_identity("requirement", intake),
            source_digest="1" * 64,
            source_manifest={"source.md": "2" * 64},
            now_ms=now,
        )

    step(
        {
            "operation": "begin",
            "request_id": "begin",
            "contracts": [contract],
            "sources": [
                {
                    "id": "spec",
                    "path": "source.md",
                    "sha256": "2" * 64,
                    "locator": "all",
                    "claim": "冻结目标",
                }
            ],
        }
    )
    if selected:
        step(
            {
                "operation": "freeze-comparison",
                "request_id": "freeze",
                "candidates": [candidate_data(context.plan)],
            }
        )
        step(
            {
                "operation": "record-comparison",
                "request_id": "record",
                "judgement": {
                    "judge_input_digest": context.pending_batch.judge_input_digest,
                    "assessments": [assessment_data()],
                },
            }
        )
    if sealed:
        step({"operation": "seal-for-review", "request_id": "seal"})
    path = directory / "decision-context.json"
    path.write_text(context.model_dump_json(), encoding="utf-8")
    return path, context


def test_pending_requirement_comparison_cannot_be_restarted_or_refunded(tmp_path):
    assert requirement_start(tmp_path).status == "ready"
    path, context = bind_requirement_context(tmp_path)
    before = path.read_bytes()
    result = requirement_start(tmp_path, idea="", acceptance=("增加具体成果",))
    assert result.status == "blocked"
    assert "comparison-pending" in result.blocker
    assert path.read_bytes() == before
    assert context.pending_batch.number == 1


def test_selected_requirement_output_update_preserves_context_and_first_start(tmp_path):
    assert requirement_start(tmp_path).status == "ready"
    path, context = bind_requirement_context(tmp_path, selected=True)
    before = path.read_bytes()
    result = requirement_start(tmp_path, idea="", acceptance=("异常路径拒绝",))
    assert result.status == "ready", result.blocker
    assert path.read_bytes() == before
    assert context.initial_selection_id == "A"


def test_sealed_requirement_cannot_reopen_before_native_r1(tmp_path):
    assert requirement_start(tmp_path).status == "ready"
    path, _ = bind_requirement_context(tmp_path, selected=True, sealed=True)
    before = path.read_bytes()
    result = requirement_start(tmp_path, idea="", acceptance=("R1前改写已封存成果",))
    assert result.status == "blocked"
    assert "revision-unavailable" in result.blocker
    assert path.read_bytes() == before
