"""第三批判断材料的摘要必须覆盖真实历史及来源说明。"""

import pytest

from ai_sdlc.core.loop_simulation import judge_input_digest
from ai_sdlc.core.loop_simulation_context import (
    _digest,
    _stamp,
    comparison_judge_input,
    validate_simulation_context,
)
from tests.unit.test_instance_comparison_authorization import (
    authorization_request,
    rejected_history,
    third_candidates,
)
from tests.unit.test_loop_simulation import assessment_data
from tests.unit.test_quantified_input_correction import advance


def freeze_third(old, *, source_claim=None):
    request = authorization_request(old)
    if source_claim is not None:
        request["sources"][1]["claim"] = source_claim
    context = advance(old, now=11_000_000, **request)
    return advance(
        context,
        "freeze-comparison",
        request_id="freeze-third",
        now=11_001_000,
        candidates=third_candidates(context),
    )


def test_third_digest_changes_when_actual_prior_judgement_changes():
    first = freeze_third(rejected_history(cost_reason="先前判断指出实现成本遗漏"))
    second = freeze_third(rejected_history(cost_reason="先前判断指出下游交付成本遗漏"))
    assert first.pending_batch.candidates == second.pending_batch.candidates
    assert first.pending_batch.source_manifest == second.pending_batch.source_manifest
    assert first.plan == second.plan
    assert first.comparisons != second.comparisons
    assert first.pending_batch.judge_input_digest != second.pending_batch.judge_input_digest


def test_third_digest_changes_when_frozen_source_claim_changes():
    old = rejected_history()
    first = freeze_third(old, source_claim="新增完整浏览器运行回执")
    second = freeze_third(old, source_claim="新增浏览器运行与下游交付证据")
    assert first.pending_batch.candidates == second.pending_batch.candidates
    assert first.pending_batch.source_manifest == second.pending_batch.source_manifest
    assert first.plan == second.plan
    assert first.sources != second.sources
    assert first.pending_batch.judge_input_digest != second.pending_batch.judge_input_digest


@pytest.mark.parametrize("completed", [False, True])
def test_unbound_third_digest_cannot_be_replayed_as_bound(completed):
    frozen = freeze_third(rejected_history())
    batch = frozen.pending_batch
    weak = judge_input_digest(frozen.plan, batch.candidates, batch.source_manifest)
    context = frozen
    if completed:
        context = advance(
            frozen, "record-comparison", now=11_002_000,
            judgement=dict(
                judge_input_digest=batch.judge_input_digest,
                assessments=[assessment_data(name) for name in ("A", "B")],
            ),
        )
    payload = context.model_dump(mode="json")
    target = payload["comparisons"][-1] if completed else payload["pending_batch"]
    target["judge_input_digest"] = weak
    if completed:
        target["judgement"]["judge_input_digest"] = weak
    with pytest.raises(ValueError, match="simulation-judge-input-drift"):
        validate_simulation_context(_stamp(payload))


def test_different_history_result_cannot_record_and_retry_preserves_material():
    first = freeze_third(rejected_history(cost_reason="缺少实现成本"))
    second = freeze_third(rejected_history(cost_reason="缺少下游成本"))
    with pytest.raises(ValueError, match="simulation-judgement-input-mismatch"):
        advance(
            second, "record-comparison", now=11_002_000,
            judgement=dict(
                judge_input_digest=first.pending_batch.judge_input_digest,
                assessments=[assessment_data(name) for name in ("A", "B")],
            ),
        )
    material = comparison_judge_input(second, second.pending_batch)
    assert material["judge_input_digest"] == _digest(
        {key: value for key, value in material.items() if key != "judge_input_digest"}
    )
    retried = advance(
        second, "record-comparison", now=11_002_000,
        request_id="technical-transmission",
        failure=dict(stage="judging", reason="原始调用已退出但未完整输出", retry=True,
                     prior_call_terminated=True),
    )
    assert comparison_judge_input(retried, retried.pending_batch) == material
    result = advance(
        retried, "record-comparison", now=11_003_000,
        judgement=dict(judge_input_digest=material["judge_input_digest"],
                       assessments=[assessment_data(name) for name in ("A", "B")]),
    )
    assert result.initial_selection_id == "A"
    assert result.comparisons[:2] == second.comparisons
    assert validate_simulation_context(result) == result
