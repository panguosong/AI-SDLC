"""Implementation 单实例追加比较；复用原回执、来源和历史重算协议。"""

from fractions import Fraction

from ai_sdlc.core.loop_simulation import stage_contract_digest
from ai_sdlc.core.loop_simulation_context_models import (
    STAGE_CAPABILITY,
    InputCorrectionReceipt,
    RequestReceipt,
    SimulationBatch,
    SimulationPrepareRequest,
)


def comparison_authorization_available(context) -> bool:
    return (
        context.capability == STAGE_CAPABILITY
        and context.loop_type == "implementation"
        and context.phase == "initial_selected"
        and context.pending_batch is None
        and len(context.comparisons) == 2
        and context.initial_selection_id is None
        and context.review_seal is None
        and context.improvement is None
        and context.conditional_improvement is None
        and context.comparison_extension is None
        and all(
            b.outcome == "success"
            and b.decision_point == "before-execution"
            and b.judgement is not None
            and b.selection is not None
            for b in context.comparisons
        )
    )


def _check_authorization(context, request):
    from ai_sdlc.core.loop_simulation_context import _check_time_only_contracts

    if not comparison_authorization_available(context):
        raise ValueError("simulation-comparison-authorization-unavailable")
    authorization = request.comparison_authorization
    if (
        authorization is None
        or authorization.loop_id != context.loop_id
        or authorization.source_context_digest != context.context_digest
        or {authorization.authorization_ref, *authorization.fact_refs}
        != {s.id for s in request.sources}
    ):
        raise ValueError("simulation-comparison-authorization-unbound")
    _check_time_only_contracts(
        context,
        request,
        allow_same_window=True,
        required_time_basis_refs=authorization.fact_refs,
    )
    # 授权与事实须各有原件，不能把同一文件重复声明为两种独立来源。
    if len({s.path.casefold() for s in request.sources}) != len(request.sources) or len(
        {s.sha256 for s in request.sources}
    ) != len(request.sources):
        raise ValueError("simulation-comparison-authorization-sources-duplicate")


def authorize_comparison(
    context, request, payload, source_digest, now_ms, *, execution_started
):
    from ai_sdlc.core.loop_simulation_context import _digest, _stamp

    if execution_started is not False:
        raise ValueError("simulation-authorization-before-execution-required")
    _check_authorization(context, request)
    if Fraction(now_ms - context.started_at_ms, 1000) >= Fraction(
        request.contracts[0].time_plan.window_seconds
    ):
        raise ValueError("simulation-model-plan-not-feasible")
    payload.update(
        comparison_extension=InputCorrectionReceipt(
            source_context_digest=context.context_digest,
            source_batch_digest=_digest(
                [b.model_dump(mode="json") for b in context.comparisons]
            ),
            source_count=len(context.sources),
            source_receipt_count=len(context.receipts),
            source_observed_at_ms=context.last_observed_at_ms,
            corrected_at_ms=now_ms,
            request_id=request.request_id,
            corrected_base_input_digest=source_digest,
            old_contracts=context.contracts,
            revision_request=request.model_dump(mode="json", exclude_unset=True),
        ).model_dump(mode="json"),
        contracts=[c.model_dump(mode="json") for c in request.contracts],
        sources=[
            s.model_dump(mode="json") for s in (*context.sources, *request.sources)
        ],
        phase="initial_search",
        pending_batch=SimulationBatch(
            number=3,
            decision_point="before-execution",
            base_input_digest=source_digest,
        ).model_dump(mode="json"),
    )
    return _stamp(payload)


def validate_comparison_extension(context, batches):
    from ai_sdlc.core.loop_simulation_context import (
        _digest,
        request_digest,
        validate_simulation_context,
    )

    receipt = context.comparison_extension
    if (
        context.capability != STAGE_CAPABILITY
        or context.loop_type != "implementation"
        or len(batches) != 3
        or len(context.comparisons) < 2
        or context.improvement is not None
        or context.conditional_improvement is not None
        or receipt.old_contracts is None
        or receipt.revision_request is None
        or receipt.source_count >= len(context.sources)
        or receipt.source_receipt_count >= len(context.receipts)
        or not context.started_at_ms
        <= receipt.source_observed_at_ms
        <= receipt.corrected_at_ms
        <= context.last_observed_at_ms
    ):
        raise ValueError("simulation-comparison-extension-reference-invalid")
    first_two = context.comparisons[:2]
    third = batches[2]
    if (
        receipt.source_batch_digest
        != _digest([b.model_dump(mode="json") for b in first_two])
        or third.number != 3
        or third.decision_point != "before-execution"
        or third.base_input_digest != receipt.corrected_base_input_digest
        or third.incumbent_id is not None
        or third.judge_input_digest is not None
        and third.judge_input_digest in {b.judge_input_digest for b in first_two}
        or Fraction(receipt.corrected_at_ms - context.started_at_ms, 1000)
        >= Fraction(context.plan.time_plan.window_seconds)
    ):
        raise ValueError("simulation-comparison-extension-batch-invalid")
    request = SimulationPrepareRequest.model_validate(receipt.revision_request)
    if (
        request.operation != "authorize-comparison"
        or request.request_id != receipt.request_id
        or context.receipts[receipt.source_receipt_count]
        != RequestReceipt(
            request_id=request.request_id,
            request_digest=request_digest(request),
        )
    ):
        raise ValueError("simulation-comparison-extension-receipt-unbound")
    # 先回放完整原上下文；旧 input_correction 仍只解释原第二批。
    original = context.model_copy(
        update=dict(
            contracts=receipt.old_contracts,
            sources=context.sources[: receipt.source_count],
            receipts=context.receipts[: receipt.source_receipt_count],
            last_observed_at_ms=receipt.source_observed_at_ms,
            phase="initial_selected",
            pending_batch=None,
            comparisons=first_two,
            initial_selection_id=None,
            review_seal=None,
            comparison_extension=None,
            context_digest=receipt.source_context_digest,
        )
    )
    validate_simulation_context(original)
    _check_authorization(original, request)
    if (
        request.contracts != context.contracts
        or context.sources[receipt.source_count :] != request.sources
    ):
        raise ValueError("simulation-comparison-extension-request-unbound")
    if third.candidates:
        check_authorized_candidates(context, third.candidates, context.sources)


def check_authorized_candidates(context, candidates, sources):
    request = SimulationPrepareRequest.model_validate(
        context.comparison_extension.revision_request
    )
    if tuple(sources) != context.sources:
        raise ValueError("simulation-authorized-source-unbound")
    originals = {c.candidate_id: c for c in context.comparisons[1].candidates}
    if len(candidates) != len(originals) or {c.candidate_id for c in candidates} != set(
        originals
    ):
        raise ValueError("simulation-authorized-candidates-changed")
    fact_refs = set(request.comparison_authorization.fact_refs)
    referenced_facts = set()
    for candidate in candidates:
        original = originals[candidate.candidate_id]
        if (
            candidate.model_dump(
                exclude={"contract_digest", "basis", "future_cost_estimate"}
            )
            != original.model_dump(
                exclude={"contract_digest", "basis", "future_cost_estimate"}
            )
            or candidate.contract_digest != stage_contract_digest(context.plan)
            or candidate.basis[: len(original.basis)] != original.basis
        ):
            raise ValueError("simulation-authorized-candidate-changed")
        additions = candidate.basis[len(original.basis) :]
        cost = candidate.future_cost_estimate
        if (
            not additions
            or cost is None
            or cost.scope != context.plan.time_plan.scope
            or original.future_cost_estimate is not None
            and not set(original.future_cost_estimate.basis_refs)
            <= set(cost.basis_refs)
            or not all(
                fact.kind == "project_fact"
                and fact.source_ref in fact_refs
                and fact.id in cost.basis_refs
                for fact in additions
            )
        ):
            raise ValueError("simulation-authorized-cost-fact-required")
        referenced_facts.update(fact.source_ref for fact in additions)
    if referenced_facts != fact_refs:
        raise ValueError("simulation-authorized-facts-unbound")
