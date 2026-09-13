"""真实 D2 本地链路；合成专家判断验证协议，不作为模型效果实证。"""

import json

import pytest

from ai_sdlc.cli.loop_review_cmd import validate_review_input_for_close
from ai_sdlc.cli.loop_stage_cmd import resolve_stage_decision_host
from ai_sdlc.core.loop_review_service import read_verified_implementation_close
from ai_sdlc.core.loop_stage_decision_service import stage_material_digest
from tests.integration.test_quantified_implementation import (
    _cli,
    _close_args,
    _complete_task,
    _payload,
)
from tests.integration.test_stage_quantified_pipeline import (
    LOOP,
    actual_record,
    stage_apply,
    stage_selected,
)
from tests.unit.test_loop_simulation import assessment_data, candidate_data

STAGE = "implementation"
ACTUAL_SOURCE = "src/ai_sdlc/core/implementation_loop.py"


def conditional_proposal(root):
    context = stage_selected(root, STAGE)
    contract = next(
        item for item in context.contracts if item.profile_id == "code-result-v1"
    )
    incumbent = candidate_data(contract, "keep-current-code")
    incumbent.update(
        cost_decision_point="before-improvement", changed_scope=[ACTUAL_SOURCE]
    )
    proposal = stage_apply(
        root,
        STAGE,
        {
            "operation": "begin-improvement",
            "request_id": "begin-improvement",
            "improvement": {
                "incumbent": incumbent,
                "baseline_digest": stage_material_digest(
                    root, resolve_stage_decision_host(root, STAGE, LOOP)
                ),
                "criterion_ids": ["coverage"],
                "hypothesis": "在原实现任务范围内增加明确的失败分支覆盖",
                "future_cost_estimate": incumbent["future_cost_estimate"],
            },
        },
    )
    challenger = candidate_data(contract, "clarify-failure-branch")
    challenger.update(
        cost_decision_point="before-improvement", changed_scope=[ACTUAL_SOURCE]
    )
    frozen = stage_apply(
        root,
        STAGE,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze-improvement",
            "candidates": [incumbent, challenger],
        },
    )
    context = stage_apply(
        root,
        STAGE,
        {
            "operation": "record-comparison",
            "request_id": "judge-improvement",
            "judgement": {
                "judge_input_digest": frozen.pending_batch.judge_input_digest,
                "assessments": [
                    assessment_data("keep-current-code", 2, 2),
                    assessment_data("clarify-failure-branch", 3, 3),
                ],
            },
        },
    )
    assert proposal.pending_batch.number == 2
    assert context.initial_selection_id == "better"
    assert context.conditional_improvement.selected_id == "clarify-failure-branch"
    assert {
        name: score.s_low
        for name, score in context.comparisons[-1].selection.scores.items()
    } == {"keep-current-code": 50, "clarify-failure-branch": 75}
    return stage_apply(
        root, STAGE, {"operation": "seal-for-review", "request_id": "seal"}
    )


def review(root):
    return _payload(
        _cli(root, "loop", "review", "--type", STAGE, "--loop-id", LOOP, "--json")
    )


@pytest.mark.parametrize(
    "first_status,action", [("PASS", "improve"), ("FAIL", "repair")]
)
def test_native_stage_improvement_and_required_repair_keep_unique_r2_and_original_close(
    initialized_project_dir, first_status, action
):
    root = initialized_project_dir
    context = conditional_proposal(root)
    directory = root / ".ai-sdlc/loops" / STAGE / LOOP
    sealed_bytes = (directory / "decision-context.json").read_bytes()
    first = review(root)
    assert first["round_number"] == 1
    assert (
        actual_record(root, STAGE, first, status=first_status)["status"] == "needs_fix"
    )
    first_path = directory / "review-outcome-round-1.json"
    original = first_path.read_bytes()
    data = json.loads(original)["simulation"]
    assert data["decision"]["action"] == action
    assert data["evaluation"]["h"] == (0 if first_status == "PASS" else 1)
    premature = _cli(root, *_close_args(first))
    assert premature.returncode != 0
    assert not (directory / "implementation-close.json").exists()

    # 先经原生准入记录开始，再修改唯一实际路线；不伪造第二轮 finding。
    started = _payload(
        _cli(
            root,
            "loop",
            STAGE,
            "record",
            "--loop-id",
            LOOP,
            "--task-id",
            "T11",
            "--status",
            "in_progress",
            "--json",
        )
    )
    assert started["status"] != "blocked"
    (root / ACTUAL_SOURCE).write_text("VALUE = 2\n", encoding="utf-8")
    _complete_task(root, loop_id=LOOP)
    second = review(root)
    assert second["round_number"] == 2
    assert second["input_digest"] != first["input_digest"]
    assert actual_record(root, STAGE, second)["status"] == "passed"
    saved = json.loads((directory / "review-outcome-round-2.json").read_bytes())[
        "simulation"
    ]
    assert saved["decision"]["action"] == "stop"
    assert saved["evaluation"]["h"] == 0
    assert saved["source_digest"] != data["source_digest"]
    assert saved["context_digest"] == context.context_digest
    # R2 Close 多次复验真实 Git 快照；仅给此命令留出跨平台完成时间。
    closed = _cli(root, *_close_args(second), timeout=60)
    assert closed.returncode == 0, closed.stdout + closed.stderr
    assert (directory / "implementation-close.json").is_file()
    assert (
        read_verified_implementation_close(
            root, LOOP, review_input_validator=validate_review_input_for_close
        ).loop_id
        == LOOP
    )
    assert first_path.read_bytes() == original
    assert (directory / "decision-context.json").read_bytes() == sealed_bytes
    assert not (directory / "review-outcome-round-3.json").exists()
