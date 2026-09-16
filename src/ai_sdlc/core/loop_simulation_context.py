"""版本化阶段的有限准备协议；纯状态转换，不读文件、不调用模型。"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import PurePosixPath

from ai_sdlc.core.loop_decision import contract_digest
from ai_sdlc.core.loop_simulation import (
    assess_current_staged_tree,
    judge_input_digest,
    select_simulated_candidates,
    select_simulated_improvement,
    stage_contract_digest,
)
from ai_sdlc.core.loop_simulation_context_models import (
    CAPABILITY as CAPABILITY,
)
from ai_sdlc.core.loop_simulation_context_models import (
    STAGE_CAPABILITY as STAGE_CAPABILITY,
)
from ai_sdlc.core.loop_simulation_context_models import (
    InputCorrectionReceipt,
    RequestReceipt,
    SimulationBatch,
)
from ai_sdlc.core.loop_simulation_context_models import (
    SimulationContext as SimulationContext,
)
from ai_sdlc.core.loop_simulation_context_models import (
    SimulationFailure as SimulationFailure,
)
from ai_sdlc.core.loop_simulation_context_models import (
    SimulationPreparation as SimulationPreparation,
)
from ai_sdlc.core.loop_simulation_context_models import (
    SimulationPrepareRequest as SimulationPrepareRequest,
)
from ai_sdlc.core.loop_simulation_models import (
    STAGE_PROFILES,
    ConditionalImprovement,
    StageScoreContract,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def comparison_judge_input(context, batch, *, candidates=None, sources=None, manifest=None):
    """冻结、送审和重放共用同一材料；第三批不能在摘要之后追加决定依据。"""
    candidates = batch.candidates if candidates is None else candidates
    sources = context.sources if sources is None else sources
    manifest = batch.source_manifest if manifest is None else manifest
    contract = context.contract_for_batch(batch)
    material = {
        "contract": contract.model_dump(mode="json"),
        "candidates": [
            candidate.model_dump(mode="json")
            for candidate in sorted(candidates, key=lambda item: item.candidate_id)
        ],
        "sources": [source.model_dump(mode="json") for source in sources],
        "source_manifest": manifest,
    }
    if batch.number == 3 and context.comparison_extension is not None:
        revision = context.comparison_extension
        material.update(
            prior_comparisons=[
                previous.model_dump(mode="json")
                for previous in context.comparisons[:2]
            ],
            prior_contracts=[
                context.contract_for_batch(previous).model_dump(mode="json")
                for previous in context.comparisons[:2]
            ],
            comparison_authorization=revision.revision_request["comparison_authorization"],
            authorization_reason=revision.revision_request["reason"],
            authorized_at_ms=revision.corrected_at_ms,
            original_started_at_ms=context.started_at_ms,
            baseline_digest=batch.base_input_digest,
        )
        # 第一、二批保留原协议；未发布第三批的弱摘要不降级为可执行凭据。
        digest = _digest(material)
    else:
        digest = judge_input_digest(contract, candidates, manifest)
    return {"judge_input_digest": digest, **material}


def request_digest(request: SimulationPrepareRequest) -> str:
    return _digest(request.model_dump(mode="json", exclude_unset=True))


def has_receipt(context: SimulationContext, request: SimulationPrepareRequest) -> bool:
    for receipt in context.receipts:
        if receipt.request_id == request.request_id:
            if receipt.request_digest != request_digest(request):
                raise ValueError("simulation-request-id-conflict")
            return True
    return False


def _stamp(payload: dict) -> SimulationContext:
    payload.pop("context_digest", None)
    # 先规范化默认字段，再签当前载荷；输入解析不能偷偷补字段改变已存摘要。
    normalized = SimulationContext.model_validate(
        {**payload, "context_digest": "0" * 64}
    ).model_dump(mode="json", exclude={"context_digest"})
    return SimulationContext.model_validate(
        {**normalized, "context_digest": _digest(normalized)}
    )


def _check_bundle(contracts: tuple[StageScoreContract, ...]) -> None:
    if (
        not contracts
        or len({c.capability for c in contracts}) != 1
        or len({c.loop_type for c in contracts}) != 1
    ):
        raise ValueError("simulation-profile-bundle-identity-mismatch")
    if len(contracts) != len(STAGE_PROFILES[contracts[0].loop_type]) or {
        c.profile_id for c in contracts
    } != set(STAGE_PROFILES[contracts[0].loop_type]):
        raise ValueError("simulation-profile-bundle-invalid")
    if len({contract_digest(c.goal_contract) for c in contracts}) != 1 or any(
        c.time_plan != contracts[0].time_plan for c in contracts
    ):
        raise ValueError("simulation-profile-bundle-contract-mismatch")


def _check_sources(contracts, sources, candidates=()) -> None:
    ids = {s.id for s in sources}
    if len(ids) != len(sources):
        raise ValueError("simulation-source-ids-duplicate")
    refs = set()
    for c in contracts:
        refs.update(c.source_refs)
        refs.update(c.time_plan.basis_refs)
        refs.update(g.source_ref for g in c.goal_contract.goals)
        refs.update(o.source_ref for o in c.goal_contract.obligations)
        for row in c.criteria:
            refs.update(row.source_refs)
    for candidate in candidates:
        refs.update(b.source_ref for b in candidate.basis if b.kind == "project_fact")
    if not refs <= ids:
        raise ValueError("simulation-external-source-unbound")


def _check_candidate_time_conditions(contract, batch, candidates) -> None:
    for candidate in candidates:
        for field, expected, actual in (
            (
                "cost_decision_point",
                batch.decision_point,
                candidate.cost_decision_point,
            ),
            (
                "future_cost_estimate.scope",
                contract.time_plan.scope,
                candidate.future_cost_estimate.scope
                if candidate.future_cost_estimate is not None
                else contract.time_plan.scope,
            ),
        ):
            if actual != expected:
                raise ValueError(
                    "simulation-time-conditions-mismatch: "
                    f"candidate={candidate.candidate_id}; field={field}; "
                    f"expected={expected!r}; actual={actual!r}"
                )


def input_correction_available(context: SimulationContext) -> bool:
    """只识别可用的原剩余批次；执行状态仍由原生宿主另行核验。"""
    if (
        context.capability != STAGE_CAPABILITY
        or context.loop_type == "local-pr-review"
        or context.phase != "initial_selected"
        or context.pending_batch is not None
        or len(context.comparisons) != 1
        or context.initial_selection_id is not None
        or context.review_seal is not None
        or context.improvement is not None
        or context.conditional_improvement is not None
        or context.input_correction is not None
    ):
        return False
    batch = context.comparisons[0]
    selection = batch.selection
    if not (
        batch.number == 1
        and batch.decision_point == "before-execution"
        and batch.outcome == "success"
        and batch.candidates
        and batch.elapsed_seconds is not None
        and selection is not None
        and selection.selected_id is None
        and {item.candidate_id for item in selection.excluded}
        == {candidate.candidate_id for candidate in batch.candidates}
    ):
        return False
    reasons = {item.reason for item in selection.excluded}
    if reasons == {"time_plan_exceeded"}:
        # 这里只开放原剩余批次；新成本事实、原窗口和独立判断仍须逐层通过。
        return True
    # 机器时点错误可能遮住时间分支，旧入口仍要求原预测本来就能放入窗口。
    return reasons == {"time_conditions_mismatch"} and all(
        candidate.future_cost_estimate is not None
        and Fraction(batch.elapsed_seconds)
        + Fraction(candidate.future_cost_estimate.upper_seconds)
        <= Fraction(context.plan.time_plan.window_seconds)
        for candidate in batch.candidates
    )


def _cost_rejection_correction(context) -> bool:
    return bool(
        context.input_correction is not None
        and context.input_correction.old_contracts is None
        and {item.reason for item in context.comparisons[0].selection.excluded}
        == {"time_plan_exceeded"}
    )


def time_plan_revision_available(context: SimulationContext) -> bool:
    """同一 Implementation 的纯规划失准只可使用原剩余第二批。"""
    return (
        context.loop_type == "implementation"
        and input_correction_available(context)
        and {item.reason for item in context.comparisons[0].selection.excluded}
        == {"time_plan_exceeded"}
    )


def _check_independent_cost_sources(prior_sources, new_sources):
    prior_paths = {s.path.casefold() for s in prior_sources}
    prior_hashes = {s.sha256 for s in prior_sources}
    prior_ids = {s.id for s in prior_sources}
    for source in new_sources:
        path = PurePosixPath(source.path).as_posix().casefold()
        if (
            source.id in prior_ids
            or path in prior_paths
            or source.sha256 in prior_hashes
            or PurePosixPath(path).parts[:1] == (".ai-sdlc",)
        ):
            raise ValueError("simulation-correction-cost-independent-source-required")


def _check_time_plan_revision(context, request):
    if not time_plan_revision_available(context):
        raise ValueError("simulation-time-revision-unavailable")
    _check_time_only_contracts(context, request)


def _check_time_only_contracts(
    context, request, *, allow_same_window=False, required_time_basis_refs=None
):
    _check_bundle(request.contracts)
    originals = {c.profile_id: c for c in context.contracts}
    if {c.profile_id for c in request.contracts} != set(originals):
        raise ValueError("simulation-time-revision-contract-changed")
    for contract in request.contracts:
        old = originals[contract.profile_id]
        if (
            contract.model_dump(exclude={"time_plan"})
            != old.model_dump(exclude={"time_plan"})
            or contract.time_plan.scope != old.time_plan.scope
            or contract.time_plan.window_seconds < old.time_plan.window_seconds
            or not allow_same_window
            and contract.time_plan.window_seconds == old.time_plan.window_seconds
            or not set(old.time_plan.basis_refs) <= set(contract.time_plan.basis_refs)
        ):
            raise ValueError("simulation-time-revision-contract-changed")
    _check_independent_cost_sources(context.sources, request.sources)
    _check_sources(request.contracts, (*context.sources, *request.sources))
    required_refs = (
        {s.id for s in request.sources}
        if required_time_basis_refs is None
        else set(required_time_basis_refs)
    )
    if not required_refs <= set(request.contracts[0].time_plan.basis_refs):
        raise ValueError("simulation-time-revision-source-unbound")


def _check_completed_cost_sources(context, original, candidate, additions, sources):
    correction = context.input_correction
    assert correction is not None
    prior_sources = context.sources[: correction.source_count]
    new_sources = {s.id: s for s in sources[correction.source_count :]}
    cost = candidate.future_cost_estimate
    old_cost = original.future_cost_estimate
    if (
        _cost_rejection_correction(context)
        and cost.upper_seconds >= old_cost.upper_seconds
    ) or not set(old_cost.basis_refs) <= set(cost.basis_refs):
        raise ValueError("simulation-correction-cost-not-reduced")
    _check_independent_cost_sources(prior_sources, new_sources.values())
    if not additions or not all(
        fact.source_ref in new_sources and fact.id in cost.basis_refs
        for fact in additions
    ):
        raise ValueError("simulation-correction-cost-new-completed-fact-required")


def _check_corrected_candidates(context, candidates, sources) -> None:
    originals = {c.candidate_id: c for c in context.comparisons[0].candidates}
    if len(candidates) != len(originals) or {c.candidate_id for c in candidates} != set(
        originals
    ):
        raise ValueError("simulation-correction-candidates-changed")
    added_source_refs = set()
    for candidate in candidates:
        original = originals[candidate.candidate_id]
        fixed = {"cost_decision_point", "future_cost_estimate", "basis"}
        if context.input_correction.old_contracts is not None:
            fixed.add("contract_digest")
            if candidate.contract_digest != stage_contract_digest(context.plan):
                raise ValueError("simulation-correction-contract-digest-mismatch")
        if candidate.model_dump(exclude=fixed) != original.model_dump(exclude=fixed):
            raise ValueError("simulation-correction-candidate-changed")
        if candidate.basis[: len(original.basis)] != original.basis:
            raise ValueError("simulation-correction-basis-changed")
        additions = candidate.basis[len(original.basis) :]
        if any(item.kind != "project_fact" for item in additions):
            raise ValueError("simulation-correction-cost-fact-required")
        cost = candidate.future_cost_estimate
        original_cost = original.future_cost_estimate
        if cost is None or original_cost is None:
            raise ValueError("simulation-correction-cost-required")
        corrected_cost = original_cost.model_copy(
            update={"scope": context.plan.time_plan.scope}
        )
        if cost != corrected_cost and (
            not additions
            or not {item.id for item in additions}.intersection(cost.basis_refs)
            or not set(original_cost.basis_refs) <= set(cost.basis_refs)
        ):
            raise ValueError("simulation-correction-cost-fact-required")
        if (
            _cost_rejection_correction(context)
            or context.input_correction.old_contracts is not None
        ):
            _check_completed_cost_sources(
                context, original, candidate, additions, sources
            )
        added_source_refs.update(item.source_ref for item in additions)
    correction = context.input_correction
    assert correction is not None
    if not {s.id for s in sources[correction.source_count :]}.issubset(
        added_source_refs
    ):
        raise ValueError("simulation-correction-source-unbound")


def _validate_input_correction(context, batches) -> None:
    correction = context.input_correction
    if correction is None:
        return
    if (
        context.capability != STAGE_CAPABILITY
        or context.loop_type == "local-pr-review"
        or len(batches) != 2
        or not context.comparisons
        or context.improvement is not None
        or context.conditional_improvement is not None
        or correction.source_count > len(context.sources)
        or correction.source_receipt_count >= len(context.receipts)
        or not context.started_at_ms
        <= correction.source_observed_at_ms
        <= correction.corrected_at_ms
        <= context.last_observed_at_ms
    ):
        raise ValueError("simulation-correction-reference-invalid")
    first, second = batches
    if (
        correction.source_batch_digest != _digest(first.model_dump(mode="json"))
        or second.number != 2
        or second.decision_point != "before-execution"
        or second.base_input_digest != correction.corrected_base_input_digest
        or second.incumbent_id is not None
        or second.judge_input_digest is not None
        and second.judge_input_digest == first.judge_input_digest
        or Fraction(correction.corrected_at_ms - context.started_at_ms, 1000)
        >= Fraction(context.plan.time_plan.window_seconds)
    ):
        raise ValueError("simulation-correction-batch-invalid")
    revised = correction.old_contracts is not None
    request = (
        SimulationPrepareRequest.model_validate(correction.revision_request)
        if revised
        else SimulationPrepareRequest(
            operation="correct-input", request_id=correction.request_id
        )
    )
    if request.request_id != correction.request_id or (
        revised and request.operation != "revise-time-plan"
    ):
        raise ValueError("simulation-correction-receipt-unbound")
    if context.receipts[correction.source_receipt_count] != RequestReceipt(
        request_id=request.request_id, request_digest=request_digest(request)
    ):
        raise ValueError("simulation-correction-receipt-unbound")
    # 用仍保留的原件重建旧摘要，不能只接受一个可随状态改写的恢复布尔值。
    original = context.model_copy(
        update={
            "sources": context.sources[: correction.source_count],
            "contracts": correction.old_contracts if revised else context.contracts,
            "receipts": context.receipts[: correction.source_receipt_count],
            "last_observed_at_ms": correction.source_observed_at_ms,
            "phase": "initial_selected",
            "pending_batch": None,
            "comparisons": (first,),
            "initial_selection_id": None,
            "review_seal": None,
            "input_correction": None,
            "context_digest": correction.source_context_digest,
        }
    )
    validate_simulation_context(original)
    if not input_correction_available(original):
        raise ValueError("simulation-correction-source-ineligible")
    if revised:
        _check_time_plan_revision(original, request)
        if (
            request.contracts != context.contracts
            or context.sources[
                correction.source_count : correction.source_count + len(request.sources)
            ]
            != request.sources
        ):
            raise ValueError("simulation-time-revision-request-unbound")
    if second.candidates:
        _check_candidate_time_conditions(context.plan, second, second.candidates)
        _check_corrected_candidates(context, second.candidates, context.sources)
    elif len(context.sources) != correction.source_count + (
        len(request.sources) if revised else 0
    ):
        raise ValueError("simulation-correction-source-unbound")


def _continuation_stop_reason(context, batch, selection, judgement, elapsed):
    """第二批也要有独立提案及完整成本，不能只因作者想继续就预留。"""
    if batch.number >= 2 or selection.selected_id is None:
        return "opportunities_exhausted_or_no_route"
    proposal = judgement.initial_search_continuation
    if proposal is None:
        return "no_supported_improvement"
    contract = context.contract_for_batch(batch)
    criteria = {c.id: c for c in contract.criteria if c.applicability == "applicable"}
    if not set(proposal.criterion_ids) <= criteria.keys():
        return "continuation_criterion_unbound"
    assessment = next(
        a for a in judgement.assessments if a.candidate_id == selection.selected_id
    )
    levels = {a.criterion_id: a.lower for a in assessment.criterion_assessments}
    if not any(
        levels[key] < criteria[key].target_level or criteria[key].improvement_direction
        for key in proposal.criterion_ids
    ):
        return "simulated_targets_met"
    candidate = next(
        c for c in batch.candidates if c.candidate_id == selection.selected_id
    )
    cost = proposal.future_cost_estimate
    references = {b.id for b in candidate.basis} | {s.id for s in context.sources}
    if cost.scope != contract.time_plan.scope or not set(cost.basis_refs) <= references:
        return "continuation_cost_unbound"
    if (
        candidate.future_cost_estimate is None
        or cost.upper_seconds < candidate.future_cost_estimate.upper_seconds
    ):
        return "continuation_cost_incomplete"
    if elapsed + cost.upper_seconds > contract.time_plan.window_seconds:
        return "model_plan_not_feasible"
    return None


def _batch_selection(context, batch, judgement, elapsed):
    contract = context.contract_for_batch(batch)
    if context.loop_type == "local-pr-review":
        return assess_current_staged_tree(
            contract, batch.candidates, judgement.assessments
        )
    return select_simulated_candidates(
        contract,
        batch.candidates,
        judgement.assessments,
        decision_point=batch.decision_point,
        elapsed_seconds=elapsed,
        incumbent_id=batch.incumbent_id,
    )


def _improvement_result(context, batch, judgement, elapsed):
    assert context.improvement is not None and batch.incumbent_id is not None
    return select_simulated_improvement(
        context.contract_for_batch(batch),
        batch.candidates,
        judgement.assessments,
        decision_point=batch.decision_point,
        elapsed_seconds=elapsed,
        incumbent_id=batch.incumbent_id,
        criterion_ids=context.improvement.criterion_ids,
    )


def _conditional_proposal(context, batch, result, now_ms):
    if result.selected_id is None:
        return None
    candidate = next(
        c for c in batch.candidates if c.candidate_id == result.selected_id
    )
    assert (
        context.improvement is not None and candidate.future_cost_estimate is not None
    )
    return ConditionalImprovement(
        contract_digest=stage_contract_digest(context.contract_for_batch(batch)),
        baseline_digest=context.improvement.baseline_digest,
        incumbent_id=context.improvement.incumbent.candidate_id,
        selected_id=result.selected_id,
        changed_scope=candidate.changed_scope,
        criterion_ids=result.criterion_ids,
        future_cost_estimate=candidate.future_cost_estimate,
        proposed_at_ms=now_ms,
        expires_at_ms=context.started_at_ms
        + int(Fraction(context.plan.time_plan.window_seconds) * 1000),
    )


def improvement_stop_reason(context: SimulationContext) -> str | None:
    """报告已完成改善比较的停止原因，不从未比较的分项推断达标。"""
    if context.improvement is None:
        return None
    if context.pending_batch is not None:
        return "comparison_pending"
    batch = context.comparisons[-1]
    if batch.outcome != "success":
        return "no_supported_improvement"
    return _improvement_result(
        context, batch, batch.judgement, batch.elapsed_seconds
    ).stop_reason


def conditional_improvement_admission(
    context: SimulationContext,
    *,
    source_digest: str,
    now_ms: int | None,
) -> str | None:
    """只复核已封存提案的预测准入；调用者必须先通过当前实际 R1 门禁。"""
    context = validate_simulation_context(context)
    proposal = context.conditional_improvement
    if context.phase != "review_sealed" or proposal is None:
        return "no_supported_improvement"
    if source_digest != proposal.baseline_digest:
        return "simulation-improvement-baseline-drift"
    if type(now_ms) is not int or now_ms < max(
        proposal.proposed_at_ms, context.last_observed_at_ms or context.started_at_ms
    ):
        return "simulation-clock-unavailable"
    remaining = Fraction(proposal.future_cost_estimate.upper_seconds) * 1000
    if now_ms + remaining > proposal.expires_at_ms:
        return "model_plan_not_feasible"
    return None


def validate_simulation_context(context: SimulationContext) -> SimulationContext:
    if context.capability == CAPABILITY and (
        context.improvement is not None or context.conditional_improvement is not None
    ):
        raise ValueError("simulation-d1-improvement-unsupported")
    context = SimulationContext.model_validate(context.model_dump())
    if context.context_digest != _digest(
        context.model_dump(mode="json", exclude={"context_digest"})
    ):
        raise ValueError("simulation-context-digest-mismatch")
    _check_bundle(context.contracts)
    if context.capability != context.schema_version or any(
        c.capability != context.capability for c in context.contracts
    ):
        raise ValueError("simulation-context-identity-mismatch")
    batches = (
        *context.comparisons,
        *((context.pending_batch,) if context.pending_batch else ()),
    )
    if context.capability == STAGE_CAPABILITY:
        observed = context.last_observed_at_ms
        if observed is None or observed < context.started_at_ms:
            raise ValueError("simulation-clock-unavailable")
        observed_elapsed = (observed - context.started_at_ms + 999) // 1000
        previous_elapsed = 0
        for batch in batches:
            elapsed = batch.elapsed_seconds
            if elapsed is not None:
                if not previous_elapsed <= elapsed <= observed_elapsed:
                    raise ValueError("simulation-clock-moved-backwards")
                previous_elapsed = elapsed
        if (
            context.conditional_improvement is not None
            and context.conditional_improvement.proposed_at_ms > observed
        ):
            raise ValueError("simulation-clock-moved-backwards")
    if (
        tuple(b.number for b in batches) != tuple(range(1, len(batches) + 1))
        or not 1 <= len(batches) <= context.effective_max_batches
    ):
        raise ValueError("simulation-batch-sequence-invalid")
    if len({r.request_id for r in context.receipts}) != len(context.receipts):
        raise ValueError("simulation-request-ids-duplicate")
    if context.comparison_extension is None:
        if len(context.receipts) > 12:
            raise ValueError("simulation-receipt-limit")
        _validate_input_correction(context, batches)
    else:
        from ai_sdlc.core.loop_comparison_authorization import (
            validate_comparison_extension,
        )

        validate_comparison_extension(context, batches)
    winner = None
    for batch in batches:
        improving = batch.decision_point == "before-improvement"
        incumbent = (
            context.improvement.incumbent.candidate_id
            if improving and context.improvement
            else winner
        )
        if batch.incumbent_id != incumbent:
            raise ValueError("simulation-incumbent-unbound")
        if improving and (
            context.capability != STAGE_CAPABILITY
            or context.improvement is None
            or batch.number != 2
            or context.loop_type == "local-pr-review"
            or batch.base_input_digest != context.improvement.baseline_digest
        ):
            raise ValueError("simulation-improvement-baseline-unbound")
        _check_sources(context.contracts, context.sources, batch.candidates)
        if batch.candidates:
            expected = comparison_judge_input(context, batch)["judge_input_digest"]
            if batch.judge_input_digest != expected:
                raise ValueError("simulation-judge-input-drift")
        elif batch.judge_input_digest is not None or batch.judgement is not None:
            raise ValueError("simulation-candidates-missing")
        if len(batch.failures) == 2 and batch.failures[-1].retry:
            raise ValueError("simulation-retry-limit")
        if batch.outcome == "success":
            if (
                batch.judgement is None
                or batch.elapsed_seconds is None
                or batch.judgement.judge_input_digest != batch.judge_input_digest
            ):
                raise ValueError("simulation-judgement-unbound")
            expected_selection = _batch_selection(
                context, batch, batch.judgement, batch.elapsed_seconds
            )
            if expected_selection != batch.selection:
                raise ValueError("simulation-selection-invalid")
            reason = (
                _continuation_stop_reason(
                    context,
                    batch,
                    batch.selection,
                    batch.judgement,
                    batch.elapsed_seconds,
                )
                if batch.continuation_requested
                else None
            )
            if batch.continuation_stop_reason != reason:
                raise ValueError("simulation-continuation-invalid")
            if improving:
                result = _improvement_result(
                    context, batch, batch.judgement, batch.elapsed_seconds
                )
                proposed_at = (
                    context.conditional_improvement.proposed_at_ms
                    if context.conditional_improvement
                    else context.started_at_ms
                )
                if context.conditional_improvement is not None and (
                    proposed_at < context.started_at_ms
                    or (proposed_at - context.started_at_ms + 999) // 1000
                    != batch.elapsed_seconds
                ):
                    raise ValueError("simulation-proposal-time-unbound")
                if (
                    _conditional_proposal(context, batch, result, proposed_at)
                    != context.conditional_improvement
                ):
                    raise ValueError("simulation-conditional-proposal-invalid")
            else:
                winner = batch.selection.selected_id
        elif batch.judgement is not None or batch.selection is not None:
            raise ValueError("simulation-failure-cannot-score")
    for previous in context.comparisons:
        has_next = previous.number < len(batches)
        admitted = (
            previous.continuation_requested
            and previous.outcome == "success"
            and previous.continuation_stop_reason is None
        )
        improvement_next = (
            has_next and batches[previous.number].decision_point == "before-improvement"
        )
        correction_next = (
            has_next and previous.number == 1 and context.input_correction is not None
        )
        authorized_next = (
            has_next
            and previous.number == 2
            and context.comparison_extension is not None
        )
        if has_next != (
            admitted or improvement_next or correction_next or authorized_next
        ):
            raise ValueError("simulation-continuation-sequence-invalid")
    if context.initial_selection_id != winner:
        raise ValueError("simulation-initial-selection-invalid")
    if (context.phase in {"initial_search", "improvement_search"}) != (
        context.pending_batch is not None
    ):
        raise ValueError("simulation-phase-invalid")
    if context.improvement is not None:
        if not any(b.decision_point == "before-improvement" for b in batches) or (
            context.pending_batch is not None and context.phase != "improvement_search"
        ):
            raise ValueError("simulation-improvement-phase-invalid")
    elif (
        context.conditional_improvement is not None
        or context.phase == "improvement_search"
    ):
        raise ValueError("simulation-improvement-missing")
    if context.conditional_improvement is not None and not any(
        b.decision_point == "before-improvement" and b.outcome == "success"
        for b in context.comparisons
    ):
        raise ValueError("simulation-conditional-proposal-without-judgement")
    if context.phase == "review_sealed":
        if winner is None or context.review_seal != _digest(
            context.model_dump(mode="json", exclude={"review_seal", "context_digest"})
        ):
            raise ValueError("simulation-review-seal-invalid")
    elif context.review_seal is not None:
        raise ValueError("simulation-unsealed-has-seal")
    return context


def transition_simulation(
    context: SimulationContext | None,
    request: SimulationPrepareRequest,
    *,
    loop_id: str,
    input_digest: str,
    source_digest: str,
    source_manifest: dict[str, str],
    now_ms: int,
    actual_ready: bool = False,
    review_started: bool = False,
    execution_started: bool | None = None,
) -> SimulationContext:
    if type(now_ms) is not int or now_ms < 0:
        raise ValueError("simulation-clock-unavailable")
    receipt = RequestReceipt(
        request_id=request.request_id, request_digest=request_digest(request)
    ).model_dump(mode="json")
    if context is None:
        if request.operation != "begin":
            raise ValueError("simulation-begin-required")
        _check_bundle(request.contracts)
        _check_sources(request.contracts, request.sources)
        if request.contracts[0].capability == STAGE_CAPABILITY and review_started:
            raise ValueError("simulation-review-already-started")
        return _stamp(
            {
                "schema_version": request.contracts[0].capability,
                "capability": request.contracts[0].capability,
                "loop_id": loop_id,
                "implementation_input_digest": input_digest,
                "contracts": [c.model_dump(mode="json") for c in request.contracts],
                "sources": [s.model_dump(mode="json") for s in request.sources],
                "started_at_ms": now_ms,
                "last_observed_at_ms": now_ms
                if request.contracts[0].capability == STAGE_CAPABILITY
                else None,
                "pending_batch": SimulationBatch(
                    number=1,
                    decision_point="before-execution",
                    base_input_digest=source_digest,
                ).model_dump(mode="json"),
                "receipts": [receipt],
            }
        )
    context = validate_simulation_context(context)
    if context.capability == STAGE_CAPABILITY and (
        context.loop_id != loop_id
        or context.implementation_input_digest != input_digest
    ):
        raise ValueError("simulation-context-owner-mismatch")
    if has_receipt(context, request):
        return context
    if context.phase == "review_sealed":
        raise ValueError("simulation-review-sealed")
    if request.operation == "begin":
        raise ValueError("simulation-already-started")
    if now_ms < max(
        context.started_at_ms,
        context.last_observed_at_ms
        if context.capability == STAGE_CAPABILITY
        else context.started_at_ms,
    ):
        raise ValueError("simulation-clock-moved-backwards")
    payload = context.model_dump(mode="json")
    if context.capability == STAGE_CAPABILITY:
        # 封存、失败及恢复也已消耗时间；尚未得到成功评分不能返还这部分预算。
        payload["last_observed_at_ms"] = now_ms
    payload["receipts"].append(receipt)
    if context.capability == STAGE_CAPABILITY and review_started:
        raise ValueError("simulation-review-already-started")
    if request.operation == "authorize-comparison":
        from ai_sdlc.core.loop_comparison_authorization import authorize_comparison

        return authorize_comparison(
            context,
            request,
            payload,
            source_digest,
            now_ms,
            execution_started=execution_started,
        )
    if request.operation in {"correct-input", "revise-time-plan"}:
        if execution_started is not False:
            raise ValueError("simulation-correction-before-execution-required")
        if not input_correction_available(context):
            raise ValueError("simulation-input-correction-unavailable")
        revised = request.operation == "revise-time-plan"
        if revised:
            _check_time_plan_revision(context, request)
        if Fraction(now_ms - context.started_at_ms, 1000) >= Fraction(
            request.contracts[0].time_plan.window_seconds
            if revised
            else context.plan.time_plan.window_seconds
        ):
            raise ValueError("simulation-model-plan-not-feasible")
        payload.update(
            input_correction=InputCorrectionReceipt(
                source_context_digest=context.context_digest,
                source_batch_digest=_digest(
                    context.comparisons[0].model_dump(mode="json")
                ),
                source_count=len(context.sources),
                source_receipt_count=len(context.receipts),
                source_observed_at_ms=context.last_observed_at_ms,
                corrected_at_ms=now_ms,
                request_id=request.request_id,
                corrected_base_input_digest=source_digest,
                old_contracts=context.contracts if revised else None,
                # 保留实际请求字段集；补默认字段会破坏请求形状与原摘要。
                revision_request=request.model_dump(mode="json", exclude_unset=True)
                if revised
                else None,
            ).model_dump(mode="json"),
            phase="initial_search",
            pending_batch=SimulationBatch(
                number=2,
                decision_point="before-execution",
                base_input_digest=source_digest,
            ).model_dump(mode="json"),
        )
        if revised:
            payload["contracts"] = [
                c.model_dump(mode="json") for c in request.contracts
            ]
            payload["sources"] = [
                s.model_dump(mode="json") for s in (*context.sources, *request.sources)
            ]
        return _stamp(payload)
    if request.operation == "begin-improvement":
        if context.capability != STAGE_CAPABILITY:
            raise ValueError("simulation-d1-improvement-unsupported")
        if context.loop_type == "local-pr-review":
            raise ValueError("simulation-local-pr-improvement-unsupported")
        if (
            context.pending_batch is not None
            or len(context.comparisons) != 1
            or context.initial_selection_id is None
        ):
            raise ValueError("simulation-improvement-opportunities-exhausted")
        if not actual_ready:
            raise ValueError("simulation-improvement-actual-evidence-required")
        improvement = request.improvement
        assert improvement is not None
        if improvement.baseline_digest != source_digest:
            raise ValueError("simulation-improvement-baseline-drift")
        batch = SimulationBatch(
            number=2,
            decision_point="before-improvement",
            base_input_digest=source_digest,
            incumbent_id=improvement.incumbent.candidate_id,
        )
        contract = context.contract_for_batch(batch)
        candidate = improvement.incumbent
        if (
            candidate.contract_digest != stage_contract_digest(contract)
            or candidate.cost_decision_point != batch.decision_point
        ):
            raise ValueError("simulation-improvement-incumbent-view-mismatch")
        criteria = {c.id for c in contract.criteria if c.applicability == "applicable"}
        if not set(improvement.criterion_ids) <= criteria:
            raise ValueError("simulation-improvement-criterion-unbound")
        cost = improvement.future_cost_estimate
        if cost.scope != contract.time_plan.scope or not set(cost.basis_refs) <= (
            {b.id for b in candidate.basis} | {s.id for s in context.sources}
        ):
            raise ValueError("simulation-improvement-cost-unbound")
        if (
            candidate.future_cost_estimate is None
            or cost.upper_seconds < candidate.future_cost_estimate.upper_seconds
        ):
            raise ValueError("simulation-improvement-cost-incomplete")
        if Fraction(now_ms - context.started_at_ms, 1000) + Fraction(
            cost.upper_seconds
        ) > Fraction(contract.time_plan.window_seconds):
            raise ValueError("simulation-model-plan-not-feasible")
        payload.update(
            phase="improvement_search",
            improvement=improvement.model_dump(mode="json"),
            pending_batch=batch.model_dump(mode="json"),
        )
        return _stamp(payload)
    if request.operation == "seal-for-review":
        if context.pending_batch is not None:
            raise ValueError("simulation-comparison-pending")
        if context.initial_selection_id is None:
            raise ValueError("simulation-initial-selection-missing")
        if (
            context.improvement is not None
            and context.improvement.baseline_digest != source_digest
        ):
            raise ValueError("simulation-improvement-baseline-drift")
        payload["phase"] = "review_sealed"
        payload["review_seal"] = _digest(
            {
                k: v
                for k, v in payload.items()
                if k not in {"review_seal", "context_digest"}
            }
        )
        return _stamp(payload)
    batch = context.pending_batch
    if batch is None:
        raise ValueError("simulation-no-pending-comparison")
    if source_digest != batch.base_input_digest:
        raise ValueError("simulation-source-drift")
    if request.operation == "freeze-comparison":
        if batch.candidates:
            raise ValueError("simulation-comparison-already-frozen")
        _check_candidate_time_conditions(
            context.contract_for_batch(batch), batch, request.candidates
        )
        sources = {s.id: s for s in context.sources}
        for source in request.sources:
            if source.id in sources and source != sources[source.id]:
                raise ValueError("simulation-source-id-conflict")
            sources[source.id] = source
        _check_sources(context.contracts, tuple(sources.values()), request.candidates)
        if batch.number == 3 and context.comparison_extension is not None:
            from ai_sdlc.core.loop_comparison_authorization import (
                check_authorized_candidates,
            )

            check_authorized_candidates(
                context, request.candidates, tuple(sources.values())
            )
            if any(
                Fraction(now_ms - context.started_at_ms, 1000)
                + Fraction(candidate.future_cost_estimate.upper_seconds)
                > Fraction(context.plan.time_plan.window_seconds)
                for candidate in request.candidates
            ):
                raise ValueError("simulation-authorized-cost-not-feasible")
        elif context.input_correction is not None:
            _check_corrected_candidates(
                context, request.candidates, tuple(sources.values())
            )
            # 只在新冻结时检查现在的剩余窗口；历史回读不能把晚到的否定判断变成损坏。
            if (
                _cost_rejection_correction(context)
                or context.input_correction.old_contracts is not None
            ) and any(
                Fraction(now_ms - context.started_at_ms, 1000)
                + Fraction(candidate.future_cost_estimate.upper_seconds)
                > Fraction(context.plan.time_plan.window_seconds)
                for candidate in request.candidates
            ):
                raise ValueError("simulation-correction-cost-not-feasible")
        if batch.incumbent_id is not None:
            old = (
                context.improvement.incumbent
                if batch.decision_point == "before-improvement" and context.improvement
                else context.selected_candidate
            )
            new = next(
                (c for c in request.candidates if c.candidate_id == batch.incumbent_id),
                None,
            )
            if (
                old is None
                or new is None
                or any(
                    getattr(old, field) != getattr(new, field)
                    for field in ("artifact_sketch", "mechanism", "changed_scope")
                )
            ):
                raise ValueError("simulation-incumbent-changed")
            if batch.decision_point == "before-improvement" and (
                new != old or len(request.candidates) < 2
            ):
                raise ValueError("simulation-improvement-incumbent-changed")
        if context.loop_type == "local-pr-review" and (
            len(request.candidates) != 1
            or request.candidates[0].candidate_id != "current-staged-tree"
        ):
            raise ValueError("simulation-local-pr-only-current-staged-tree")
        if (
            len(
                json.dumps(
                    [c.model_dump(mode="json") for c in request.candidates],
                    ensure_ascii=False,
                ).encode()
            )
            > 65536
        ):
            raise ValueError("simulation-candidate-payload-too-large")
        digest = comparison_judge_input(
            context, batch, candidates=request.candidates,
            sources=tuple(sources.values()), manifest=source_manifest,
        )["judge_input_digest"]
        payload["sources"] = [s.model_dump(mode="json") for s in sources.values()]
        payload["pending_batch"] = {
            **batch.model_dump(mode="json"),
            "candidates": [c.model_dump(mode="json") for c in request.candidates],
            "source_manifest": source_manifest,
            "judge_input_digest": digest,
        }
    else:
        if request.continue_search and (
            batch.decision_point == "before-improvement"
            or context.loop_type == "local-pr-review"
        ):
            raise ValueError("simulation-continuation-unsupported")
        completed = batch.model_dump(mode="json")
        continue_admitted = False
        if request.failure is not None:
            f = request.failure
            if (f.stage == "judging") != bool(batch.candidates):
                raise ValueError("simulation-failure-stage-invalid")
            if f.retry and (batch.failures or not f.prior_call_terminated):
                raise ValueError("simulation-retry-unavailable")
            completed["failures"].append(f.model_dump(mode="json"))
            if f.retry:
                payload["pending_batch"] = completed
                return _stamp(payload)
            completed["outcome"] = "technical_failure"
        else:
            j = request.judgement
            if (
                not batch.candidates
                or j is None
                or j.judge_input_digest != batch.judge_input_digest
            ):
                raise ValueError("simulation-judgement-input-mismatch")
            elapsed = (now_ms - context.started_at_ms + 999) // 1000
            selected = _batch_selection(context, batch, j, elapsed)
            completed.update(
                outcome="success",
                judgement=j.model_dump(mode="json"),
                elapsed_seconds=elapsed,
                selection=selected.model_dump(mode="json"),
            )
            # 新判断的否定结果覆盖旧赢家；只有技术失败可保留先前有效选择。
            if batch.decision_point == "before-improvement":
                result = _improvement_result(context, batch, j, elapsed)
                proposal = _conditional_proposal(context, batch, result, now_ms)
                payload["conditional_improvement"] = (
                    proposal.model_dump(mode="json") if proposal else None
                )
            else:
                payload["initial_selection_id"] = selected.selected_id
            if request.continue_search:
                reason = _continuation_stop_reason(context, batch, selected, j, elapsed)
                completed.update(
                    continuation_requested=True, continuation_stop_reason=reason
                )
                continue_admitted = reason is None
        payload["comparisons"].append(completed)
        payload["pending_batch"] = None
        payload["phase"] = "initial_selected"
        if continue_admitted:
            payload["pending_batch"] = SimulationBatch(
                number=2,
                decision_point="before-execution",
                base_input_digest=source_digest,
                incumbent_id=payload["initial_selection_id"],
            ).model_dump(mode="json")
            payload["phase"] = "initial_search"
    return _stamp(payload)
