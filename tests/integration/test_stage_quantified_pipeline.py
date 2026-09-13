"""多阶段真实CLI协议；合成独立判断只证明接线，不证明模型商业质量。"""

import hashlib
import json

import pytest

from ai_sdlc.cli.loop_stage_cmd import resolve_stage_decision_host
from ai_sdlc.core.loop_simulation_context import SimulationPrepareRequest
from ai_sdlc.core.loop_simulation_models import STAGE_PROFILES
from ai_sdlc.core.loop_stage_decision_service import prepare_stage_simulation_decision
from tests.integration.test_quantified_implementation import (
    _cli,
    _complete_task,
    _payload,
    _ready_project,
)
from tests.unit.test_loop_simulation import assessment_data, candidate_data
from tests.unit.test_loop_simulation_models import contract_data

CAPABILITY = "stage-simulation-v1"
LOOP = "stage-pipeline"
WORK_ITEM = "specs/demo-implementation-loop"


def stage_start(root, stage):
    _ready_project(root)
    if stage == "requirement":
        args = [
            "start",
            "--idea",
            "拒绝越权请求并保留实际审查",
            "--acceptance",
            "拒绝越权请求",
            "--work-item-id",
            "stage-requirement",
        ]
    elif stage == "design-contract":
        args = ["check", "--wi", WORK_ITEM]
    else:
        args = ["start", "--wi", WORK_ITEM]
    return _payload(
        _cli(
            root,
            "loop",
            stage,
            *args,
            "--loop-id",
            LOOP,
            "--decision-mode",
            "adaptive-quantified",
            "--decision-capability",
            CAPABILITY,
            "--json",
        )
    )


def stage_apply(root, stage, data):
    request = SimulationPrepareRequest.model_validate(data)
    kwargs = {"host_resolver": lambda: resolve_stage_decision_host(root, stage, LOOP)}
    preview = prepare_stage_simulation_decision(root, stage, LOOP, request, **kwargs)
    return prepare_stage_simulation_decision(
        root,
        stage,
        LOOP,
        request,
        dry_run=False,
        expected_digest=preview.prepare_digest,
        **kwargs,
    ).context


def stage_selected(root, stage, *, start=True, repair_fact=False):
    if start:
        stage_start(root, stage)
    source = root / WORK_ITEM / "spec.md"
    fact = root / "repair-fact.md"
    if repair_fact:
        fact.write_text("R1 前的实际材料", encoding="utf-8")
    contracts = [
        {
            **contract_data(),
            "capability": CAPABILITY,
            "loop_type": stage,
            "profile_id": profile,
        }
        for profile in STAGE_PROFILES[stage]
    ]
    context = stage_apply(
        root,
        stage,
        {
            "operation": "begin",
            "request_id": "begin",
            "contracts": contracts,
            "sources": [
                {
                    "id": "spec",
                    "path": source.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "locator": "all",
                    "claim": "冻结目标与约束",
                },
                *(
                    [
                        {
                            "id": "repair-fact",
                            "path": fact.name,
                            "sha256": hashlib.sha256(fact.read_bytes()).hexdigest(),
                            "locator": "all",
                            "claim": "待核对的原始材料",
                        }
                    ]
                    if repair_fact
                    else []
                ),
            ],
        },
    )
    candidates = [
        candidate_data(context.plan, "baseline"),
        candidate_data(context.plan, "better"),
    ]
    frozen = stage_apply(
        root,
        stage,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze",
            "candidates": candidates,
        },
    )
    selected = stage_apply(
        root,
        stage,
        {
            "operation": "record-comparison",
            "request_id": "judge",
            "judgement": {
                "judge_input_digest": frozen.pending_batch.judge_input_digest,
                "assessments": [
                    assessment_data("baseline", 2, 2),
                    assessment_data("better", 3, 3),
                ],
            },
        },
    )
    assert selected.initial_selection_id == "better"
    if stage == "implementation":
        _complete_task(root, loop_id=LOOP)
    return selected


def actual_record(root, stage, reviewed, *, status="PASS"):
    destination = root / ".ai-sdlc/state/stage-test-experts"
    destination.mkdir(parents=True, exist_ok=True)
    paths = []
    source = next(path for path in reviewed["artifact_paths"] if path.endswith(".md"))
    for index, role in enumerate(reviewed["expert_roles"]):
        data = {
            "execution": {
                "status": "completed",
                "roles": [role],
                "role_reasons": {role: reviewed["expert_reasons"][role]},
                "findings": [],
            },
            "assessment": {
                "input_digest": reviewed["input_digest"],
                "context_digest": reviewed["context_digest"],
                "selected_route_id": reviewed["selected_route_id"],
                "results": [
                    {
                        "id": "o0",
                        "status": status,
                        "evidence_refs": [] if status == "UNKNOWN" else ["actual"],
                        "reason": "核对实际阶段材料",
                    }
                ],
                "evidence": [
                    {
                        "id": "actual",
                        "path": source,
                        "sha256": reviewed["evidence_manifest"][source],
                        "locator": "all",
                        "claim": "当前阶段实际工件",
                    }
                ],
                "repair_readiness": {
                    "authorization": "PASS",
                    "facts": "PASS",
                    "verification": "PASS",
                    "evidence_refs": ["actual"],
                    "reason": "原范围内必要修复",
                },
            },
        }
        path = destination / f"{stage}-{index}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        paths += ["--result", str(path)]
    return _payload(
        _cli(
            root,
            "loop",
            "review-record",
            "--type",
            stage,
            "--loop-id",
            LOOP,
            "--expect-digest",
            reviewed["input_digest"],
            *paths,
            "--json",
        )
    )


@pytest.mark.parametrize("stage", ["requirement", "design-contract", "implementation"])
@pytest.mark.parametrize("status", ["PASS", "UNKNOWN"])
def test_stage_forecast_and_actual_review_feed_original_close(
    initialized_project_dir, stage, status
):
    root = initialized_project_dir
    selected = stage_selected(root, stage)
    assert len(selected.comparisons) == 1
    unsealed = _cli(
        root, "loop", "review", "--type", stage, "--loop-id", LOOP, "--json"
    )
    assert unsealed.returncode == 1 and "sealed" in unsealed.stdout
    stage_apply(root, stage, {"operation": "seal-for-review", "request_id": "seal"})
    reviewed = _payload(
        _cli(root, "loop", "review", "--type", stage, "--loop-id", LOOP, "--json")
    )
    assert reviewed["decision_capability"] == CAPABILITY
    assert reviewed["decision_context"]["contracts"][0]["loop_type"] == stage
    assert reviewed["snapshot_read_command"][4] == stage
    recorded = actual_record(root, stage, reviewed, status=status)
    directory = root / ".ai-sdlc/loops" / stage / LOOP
    saved = json.loads((directory / "review-outcome-round-1.json").read_bytes())
    assert saved["simulation"]["evaluation"]["h"] == (0 if status == "PASS" else 1)
    assert "b1" not in saved
    close = _cli(
        root,
        "loop",
        stage,
        "freeze" if stage == "requirement" else "close",
        "--loop-id",
        LOOP,
        "--expect-review-digest",
        reviewed["input_digest"],
        "--yes",
        "--json",
    )
    if status == "PASS":
        assert recorded["status"] == "passed"
        assert close.returncode == 0, close.stdout + close.stderr
        state = _payload(_cli(root, "loop", stage, "status", "--json"))
        assert state["current_loop"]["status"] == "closed"
    else:
        assert recorded["status"] == "needs_fix"
        assert close.returncode == 1
        assert not (directory / f"{stage}-close.json").exists()


def test_new_schema_keeps_d1_operations_separate(initialized_project_dir):
    root = initialized_project_dir
    for capability in (CAPABILITY, "implementation-simulation-v1"):
        schema = _payload(
            _cli(
                root,
                "loop",
                "decision-prepare",
                "--schema",
                "--capability",
                capability,
                "--json",
            )
        )
        assert ("begin-improvement" in schema["properties"]["operation"]["enum"]) == (
            capability == CAPABILITY
        )
    assert len(schema["x-guidance"]["stage_profiles"]["profiles"]) == 6


@pytest.mark.parametrize("stage", ["requirement", "design-contract"])
def test_stage_necessary_repair_preserves_contract_and_stops_at_r2(
    initialized_project_dir, stage
):
    root = initialized_project_dir
    stage_selected(root, stage, repair_fact=True)
    stage_apply(root, stage, {"operation": "seal-for-review", "request_id": "seal"})
    reviewed = _payload(
        _cli(root, "loop", "review", "--type", stage, "--loop-id", LOOP, "--json")
    )
    assert (
        actual_record(root, stage, reviewed, status="UNKNOWN")["status"] == "needs_fix"
    )
    directory = root / ".ai-sdlc/loops" / stage / LOOP
    original = {
        name: (directory / name).read_bytes()
        for name in ("decision-context.json", "review-outcome-round-1.json")
    }
    (root / "repair-fact.md").write_text(
        "R1 后原范围内修正的实际材料", encoding="utf-8"
    )
    if stage == "requirement":
        args = [
            "start",
            "--idea",
            "拒绝越权请求并保留实际审查",
            "--work-item-id",
            "stage-requirement",
            "--acceptance",
            "拒绝越权请求并给出明确错误，附实际材料",
        ]
    else:
        plan = root / WORK_ITEM / "plan.md"
        plan.write_text(
            plan.read_text() + "\n原范围内补充失败处理的实际说明。\n", encoding="utf-8"
        )
        args = ["check", "--wi", WORK_ITEM]
    result = _payload(
        _cli(
            root,
            "loop",
            stage,
            *args,
            "--loop-id",
            LOOP,
            "--decision-mode",
            "adaptive-quantified",
            "--decision-capability",
            CAPABILITY,
            "--json",
        )
    )
    assert result["status"] == "ready"
    for name, content in original.items():
        assert (directory / name).read_bytes() == content
    second = _payload(
        _cli(root, "loop", "review", "--type", stage, "--loop-id", LOOP, "--json")
    )
    assert second["round_number"] == 2
    assert actual_record(root, stage, second)["status"] == "passed"
    closed = _cli(
        root,
        "loop",
        stage,
        "freeze" if stage == "requirement" else "close",
        "--loop-id",
        LOOP,
        "--expect-review-digest",
        second["input_digest"],
        "--yes",
        "--json",
    )
    assert closed.returncode == 0, closed.stdout + closed.stderr
    forbidden = _cli(
        root,
        "loop",
        stage,
        *args,
        "--loop-id",
        LOOP,
        "--decision-mode",
        "adaptive-quantified",
        "--decision-capability",
        CAPABILITY,
        "--json",
    )
    assert (
        forbidden.returncode == 1 or json.loads(forbidden.stdout).get("frozen") is True
    )
    assert not (directory / "review-outcome-round-3.json").exists()


def test_normal_run_preserves_quantified_predecessor_chain(initialized_project_dir):
    from tests.integration.test_quantified_implementation import _git
    from tests.unit.test_implementation_loop import _write_ready_work_item

    root = initialized_project_dir
    _write_ready_work_item(root)
    source = root / "src/ai_sdlc/core/implementation_loop.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Stage Integration")
    _git(root, "config", "user.email", "stage@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "original goal")
    for stage in ("requirement", "design-contract", "implementation"):
        if stage == "requirement":
            args = [
                "start",
                "--idea",
                "系统必须记录实现任务证据",
                "--acceptance",
                "完成任务后可关闭",
                "--design-scope-family",
                "implementation",
                "--work-item-id",
                "demo-implementation-loop",
            ]
        elif stage == "design-contract":
            args = ["check", "--wi", WORK_ITEM, "--requirement-loop-id", LOOP]
        else:
            args = ["start", "--wi", WORK_ITEM, "--design-contract-loop-id", LOOP]
        _payload(
            _cli(
                root,
                "loop",
                stage,
                *args,
                "--loop-id",
                LOOP,
                "--decision-mode",
                "adaptive-quantified",
                "--decision-capability",
                CAPABILITY,
                "--json",
            )
        )
        routed = _payload(_cli(root, "run", "--json"))
        assert routed["current_loop"]["loop_type"] == stage, routed
        assert "begin" in routed["next_action"], routed
        stage_selected(root, stage, start=False)
        routed = _payload(_cli(root, "run", "--json"))
        assert "seal-for-review" in routed["next_action"], routed
        stage_apply(root, stage, {"operation": "seal-for-review", "request_id": "seal"})
        reviewed = _payload(
            _cli(root, "loop", "review", "--type", stage, "--loop-id", LOOP, "--json")
        )
        if stage != "requirement":
            assert any(
                path.endswith("decision-context.json")
                for path in reviewed["upstream_context_paths"]
            )
            assert any(
                "review-outcome-round-1.json" in path
                for path in reviewed["upstream_context_paths"]
            )
        assert actual_record(root, stage, reviewed)["status"] == "passed"
        _payload(
            _cli(
                root,
                "loop",
                stage,
                "freeze" if stage == "requirement" else "close",
                "--loop-id",
                LOOP,
                "--expect-review-digest",
                reviewed["input_digest"],
                "--yes",
                "--json",
            )
        )
        status = _payload(_cli(root, "loop", stage, "status", "--json"))
        assert status["current_loop"]["status"] == "closed"


@pytest.mark.parametrize("stage", ["requirement", "design-contract"])
@pytest.mark.parametrize("missing", ["digest", "validator"])
def test_stage_core_close_requires_actual_review(
    initialized_project_dir, stage, missing
):
    from ai_sdlc.cli.loop_review_cmd import validate_review_input_for_close
    from ai_sdlc.core.design_contract_loop import (
        DesignContractCloseOptions,
        close_design_contract_loop,
    )
    from ai_sdlc.core.requirement_loop import (
        RequirementFreezeOptions,
        freeze_requirement_loop,
    )

    root = initialized_project_dir
    stage_start(root, stage)
    directory = root / ".ai-sdlc/loops" / stage / LOOP
    before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    option_type, operation = (
        (RequirementFreezeOptions, freeze_requirement_loop)
        if stage == "requirement"
        else (DesignContractCloseOptions, close_design_contract_loop)
    )
    result = operation(
        option_type(
            root=root,
            loop_id=LOOP,
            yes=True,
            expected_review_digest="" if missing == "digest" else "a" * 64,
        ),
        review_input_validator=(
            validate_review_input_for_close if missing == "digest" else None
        ),
    )
    assert result.status == "blocked"
    assert "quantified-stage-close-review-required" in result.blocker
    assert {
        p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()
    } == before
