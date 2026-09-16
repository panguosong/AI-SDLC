"""单实例第三次比较授权保留两次原判断；默认上限仍为两批。"""

from copy import deepcopy

import pytest

from ai_sdlc.core.loop_simulation import stage_contract_digest
from ai_sdlc.core.loop_simulation_context import (
    SimulationContext,
    _digest,
    validate_simulation_context,
)
from tests.unit.test_loop_simulation import assessment_data
from tests.unit.test_quantified_input_correction import (
    _cost_rejected_history,
    advance,
    time_revision_candidates,
    time_revision_request,
)


def rejected_history(*, selected=False, technical=False, cost_reason="合成成本判断，不表示业务验收"):
    original = _cost_rejected_history()
    revised = advance(original, now=4_000_000, **time_revision_request(original))
    frozen = advance(
        revised,
        "freeze-comparison",
        request_id="freeze-second",
        now=4_001_000,
        candidates=time_revision_candidates(original, revised),
    )
    failed = advance(
        frozen,
        "record-comparison",
        request_id="technical-second",
        now=4_001_500,
        failure=dict(
            stage="judging",
            reason="已终止的调用没有完整结果",
            retry=True,
            prior_call_terminated=True,
        ),
    )
    if technical:
        return advance(
            failed,
            "record-comparison",
            request_id="terminal-second",
            now=4_002_000,
            failure=dict(stage="judging", reason="技术恢复仍没有有效判断", retry=False),
        )
    return advance(
        failed,
        "record-comparison",
        request_id="judge-second",
        now=4_002_000 if selected else 6_000_000,
        judgement={
            "judge_input_digest": frozen.pending_batch.judge_input_digest,
            "assessments": [
                {
                    **assessment_data(name),
                    "cost_check": "supported" if selected else "incomplete",
                    "cost_reason": cost_reason,
                }
                for name in ("A", "B")
            ],
        },
    )


def authorization_request(old):
    sources = [
        dict(
            id="instance-authorization",
            path="authorization.md",
            sha256="c" * 64,
            locator="explicit user direction",
            claim="仅本实例有效模拟比较上限2改3",
        ),
        dict(
            id="prepared-facts",
            path="preparation.md",
            sha256="d" * 64,
            locator="receipts and remaining cost",
            claim="新准备证据与完整剩余工作",
        ),
    ]
    contracts = [contract.model_dump(mode="json") for contract in old.contracts]
    for contract in contracts:
        contract["time_plan"].update(
            window_seconds=20000,
            basis_refs=[*old.plan.time_plan.basis_refs, "prepared-facts"],
            work_breakdown=["保留原起点累计历时", "准备、比较、执行、验证及原 Close"],
        )
    return dict(
        operation="authorize-comparison",
        request_id="authorize-third",
        comparison_authorization=dict(
            loop_id=old.loop_id,
            source_context_digest=old.context_digest,
            max_batches=3,
            authorization_ref="instance-authorization",
            fact_refs=["prepared-facts"],
        ),
        contracts=contracts,
        sources=sources,
        reason="明确实例授权与新准备事实",
    )


def third_candidates(context):
    candidates = [c.model_dump(mode="json") for c in context.comparisons[1].candidates]
    for candidate in candidates:
        candidate["contract_digest"] = stage_contract_digest(context.plan)
        candidate["basis"].append(
            dict(
                id="third-preparation",
                kind="project_fact",
                source_ref="prepared-facts",
                locator="complete receipt",
                statement="新增准备结果及完整剩余成本依据",
            )
        )
        candidate["future_cost_estimate"].update(
            lower_seconds=4000,
            upper_seconds=6000,
            basis_refs=[
                *candidate["future_cost_estimate"]["basis_refs"],
                "third-preparation",
            ],
        )
    return candidates


def authorized(old=None):
    old = old or rejected_history()
    return advance(old, now=11_000_000, **authorization_request(old))


def test_explicit_instance_authorization_keeps_two_histories_and_one_third_batch():
    old = rejected_history()
    old_bytes = old.model_dump_json()
    request = authorization_request(old)
    context = authorized(old)
    assert context.started_at_ms == old.started_at_ms
    assert context.comparisons == old.comparisons
    assert context.input_correction == old.input_correction
    assert context.receipts[: len(old.receipts)] == old.receipts
    assert context.sources[: len(old.sources)] == old.sources
    assert context.comparison_extension.source_context_digest == old.context_digest
    assert context.comparison_extension.old_contracts == old.contracts
    assert context.pending_batch.number == 3
    assert context.contract_for_batch(old.comparisons[0]) == old.contract_for_batch(
        old.comparisons[0]
    )
    assert context.contract_for_batch(old.comparisons[1]) == old.contract_for_batch(
        old.comparisons[1]
    )
    assert context.current_contract.time_plan.window_seconds == 20000
    assert advance(context, now=11_001_000, **request) == context
    assert old.model_dump_json() == old_bytes
    frozen = advance(
        context,
        "freeze-comparison",
        request_id="freeze-third",
        now=11_001_000,
        candidates=third_candidates(context),
    )
    assert frozen.pending_batch.judge_input_digest not in {
        batch.judge_input_digest for batch in old.comparisons
    }
    with pytest.raises(ValueError, match="judgement-input"):
        advance(
            frozen,
            "record-comparison",
            request_id="stale-judge",
            now=11_002_000,
            judgement=old.comparisons[-1].judgement.model_dump(mode="json"),
        )
    selected = advance(
        frozen,
        "record-comparison",
        request_id="judge-third",
        now=11_002_000,
        judgement=dict(
            judge_input_digest=frozen.pending_batch.judge_input_digest,
            assessments=[assessment_data(n) for n in ("A", "B")],
        ),
    )
    assert selected.initial_selection_id == "A"
    assert selected.comparisons[-1].elapsed_seconds == 11001
    assert selected.comparisons[:2] == old.comparisons
    assert len(selected.comparisons[1].failures) == 1
    for operation, kwargs in (
        (
            "authorize-comparison",
            {k: v for k, v in request.items() if k not in {"operation", "request_id"}},
        ),
        ("freeze-comparison", dict(candidates=third_candidates(context))),
        ("correct-input", {}),
    ):
        with pytest.raises(ValueError):
            advance(
                selected,
                operation,
                request_id="fourth-" + operation,
                now=11_003_000,
                **kwargs,
            )
    assert (
        validate_simulation_context(
            SimulationContext.model_validate_json(selected.model_dump_json())
        )
        == selected
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-authorization",
        "wrong-loop",
        "wrong-context",
        "four",
        "old-source",
        "metadata",
        "alias",
        "same-content",
        "unbound-fact",
        "authorization-as-fact",
        "changed-goal",
        "changed-scope",
        "short-window",
    ],
)
def test_authorization_cannot_expand_scope_or_reuse_unbound_material(mutation):
    old = rejected_history()
    request = authorization_request(old)
    auth = request["comparison_authorization"]
    if mutation == "missing-authorization":
        del request["comparison_authorization"]
    elif mutation == "wrong-loop":
        auth["loop_id"] = "another-instance"
    elif mutation == "wrong-context":
        auth["source_context_digest"] = "0" * 64
    elif mutation == "four":
        auth["max_batches"] = 4
    elif mutation == "old-source":
        auth["authorization_ref"] = old.sources[0].id
    elif mutation == "metadata":
        request["sources"][0]["path"] = ".ai-sdlc/loops/implementation/old/report.json"
    elif mutation == "alias":
        request["sources"][0]["path"] = old.sources[0].path.upper()
    elif mutation == "same-content":
        request["sources"][0]["sha256"] = old.sources[0].sha256
    elif mutation == "unbound-fact":
        auth["fact_refs"] = ["missing"]
    elif mutation == "authorization-as-fact":
        auth["fact_refs"] = [auth["authorization_ref"]]
    elif mutation == "changed-goal":
        request["contracts"][0]["criteria"][0]["statement"] = "放宽质量锚点"
    elif mutation == "changed-scope":
        for contract in request["contracts"]:
            contract["time_plan"]["scope"] = "skip-close"
    else:
        for contract in request["contracts"]:
            contract["time_plan"]["window_seconds"] = 10000
    with pytest.raises(ValueError):
        advance(old, now=11_000_000, **request)


@pytest.mark.parametrize(
    "flags",
    [
        {"execution_started": True},
        {"execution_started": None},
        {"review_started": True},
    ],
)
def test_authorization_requires_native_preexecution_state(flags):
    old = rejected_history()
    with pytest.raises(ValueError):
        advance(old, now=11_000_000, **authorization_request(old), **flags)


@pytest.mark.parametrize(
    "mutation",
    [
        "first",
        "second",
        "failure",
        "start",
        "old-contract",
        "receipt",
        "remove-extension",
    ],
)
def test_third_batch_rejects_history_tampering_even_with_recomputed_outer_digest(
    mutation,
):
    context = authorized()
    payload = deepcopy(context.model_dump(mode="json"))
    if mutation in {"first", "second"}:
        payload["comparisons"][0 if mutation == "first" else 1]["elapsed_seconds"] += 1
    elif mutation == "failure":
        payload["comparisons"][1]["failures"] = []
    elif mutation == "start":
        payload["started_at_ms"] += 1
    elif mutation == "old-contract":
        old_plan = payload["comparison_extension"]["old_contracts"][0]["time_plan"]
        old_plan["window_seconds"] = int(old_plan["window_seconds"]) + 1
    elif mutation == "receipt":
        payload["receipts"][0]["request_digest"] = "f" * 64
    else:
        del payload["comparison_extension"]
    payload["context_digest"] = _digest(
        {k: v for k, v in payload.items() if k != "context_digest"}
    )
    with pytest.raises(ValueError):
        validate_simulation_context(SimulationContext.model_validate(payload))


@pytest.mark.parametrize(
    "mutation",
    [
        "mechanism",
        "scope",
        "sketch",
        "assumptions",
        "missing-fact",
        "cheap-no-basis",
        "expired",
    ],
)
def test_third_candidate_keeps_route_and_requires_bound_complete_cost(mutation):
    context = authorized()
    candidates = third_candidates(context)
    if mutation == "mechanism":
        candidates[0]["mechanism"] = "另一实现机制"
    elif mutation == "scope":
        candidates[0]["changed_scope"] = ["another-component"]
    elif mutation == "sketch":
        candidates[0]["artifact_sketch"]["key_structure"] = ["另一结构"]
    elif mutation == "assumptions":
        candidates[0]["assumptions"] = ["去掉原约束"]
    elif mutation == "missing-fact":
        candidates[0]["basis"][-1]["source_ref"] = "spec"
    elif mutation == "cheap-no-basis":
        candidates[0]["future_cost_estimate"].update(
            lower_seconds=1, upper_seconds=2, basis_refs=["b"]
        )
    with pytest.raises(ValueError):
        advance(
            context,
            "freeze-comparison",
            request_id="freeze-third",
            candidates=candidates,
            now=19_000_000 if mutation == "expired" else 11_001_000,
        )


def test_unextended_default_history_has_no_new_fields_and_cannot_get_third_batch():
    old = rejected_history()
    assert "comparison_extension" not in old.model_dump(mode="json")
    with pytest.raises(ValueError):
        advance(
            old,
            "freeze-comparison",
            request_id="default-third",
            now=6_001_000,
            candidates=[
                c.model_dump(mode="json") for c in old.comparisons[-1].candidates
            ],
        )


@pytest.mark.parametrize("kind", ["selected", "technical", "only-first", "requirement"])
def test_only_two_valid_unselected_implementation_comparisons_are_eligible(kind):
    if kind == "only-first":
        old = _cost_rejected_history()
    elif kind == "requirement":
        from tests.unit.test_quantified_input_correction import (
            corrected_candidates,
            history,
        )

        first = history()
        pending = advance(first, "correct-input")
        frozen = advance(
            pending,
            "freeze-comparison",
            now=5000,
            candidates=corrected_candidates(first),
        )
        old = advance(
            frozen,
            "record-comparison",
            now=6000,
            judgement=dict(
                judge_input_digest=frozen.pending_batch.judge_input_digest,
                assessments=[
                    {
                        **assessment_data(n),
                        "cost_check": "incomplete",
                        "cost_reason": "未完整",
                    }
                    for n in ("A", "B")
                ],
            ),
        )
        assert old.loop_type == "requirement" and len(old.comparisons) == 2
    else:
        old = rejected_history(
            selected=kind == "selected", technical=kind == "technical"
        )
    with pytest.raises(ValueError, match="authorization-unavailable"):
        advance(old, now=11_000_000, **authorization_request(old))


def test_third_batch_retains_only_the_existing_single_technical_retry():
    context = authorized()
    frozen = advance(
        context,
        "freeze-comparison",
        request_id="freeze-third",
        now=11_001_000,
        candidates=third_candidates(context),
    )
    failure = dict(
        stage="judging",
        reason="完整结果缺失且旧调用已结束",
        retry=True,
        prior_call_terminated=True,
    )
    recovered = advance(
        frozen,
        "record-comparison",
        request_id="third-retry",
        now=11_002_000,
        failure=failure,
    )
    assert recovered.pending_batch.number == 3
    assert (
        recovered.pending_batch.judge_input_digest
        == frozen.pending_batch.judge_input_digest
    )
    with pytest.raises(ValueError, match="retry-unavailable"):
        advance(
            recovered,
            "record-comparison",
            request_id="third-retry-again",
            now=11_003_000,
            failure=failure,
        )
    stopped = advance(
        recovered,
        "record-comparison",
        request_id="third-negative",
        now=11_003_000,
        judgement=dict(
            judge_input_digest=frozen.pending_batch.judge_input_digest,
            assessments=[
                {**assessment_data(n), "cost_check": "unknown"} for n in ("A", "B")
            ],
        ),
    )
    assert stopped.initial_selection_id is None
    with pytest.raises(ValueError, match="no-pending-comparison"):
        advance(
            stopped,
            "record-comparison",
            request_id="negative-is-not-technical",
            now=11_004_000,
            failure=failure,
        )
