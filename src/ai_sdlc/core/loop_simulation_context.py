"""版本化阶段的有限准备协议；纯状态转换，不读文件、不调用模型。"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction

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


def _continuation_stop_reason(context, batch, selection, judgement, elapsed):
    """第二批也要有独立提案及完整成本，不能只因作者想继续就预留。"""
    if batch.number >= 2 or selection.selected_id is None:
        return "opportunities_exhausted_or_no_route"
    proposal = judgement.initial_search_continuation
    if proposal is None:
        return "no_supported_improvement"
    criteria = {
        c.id: c for c in context.plan.criteria if c.applicability == "applicable"
    }
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
    if (
        cost.scope != context.plan.time_plan.scope
        or not set(cost.basis_refs) <= references
    ):
        return "continuation_cost_unbound"
    if (
        candidate.future_cost_estimate is None
        or cost.upper_seconds < candidate.future_cost_estimate.upper_seconds
    ):
        return "continuation_cost_incomplete"
    if elapsed + cost.upper_seconds > context.plan.time_plan.window_seconds:
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
        or not 1 <= len(batches) <= 2
    ):
        raise ValueError("simulation-batch-sequence-invalid")
    if len({r.request_id for r in context.receipts}) != len(context.receipts):
        raise ValueError("simulation-request-ids-duplicate")
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
            expected = judge_input_digest(
                context.contract_for_batch(batch),
                batch.candidates,
                batch.source_manifest,
            )
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
        if has_next != (admitted or improvement_next):
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
        sources = {s.id: s for s in context.sources}
        for source in request.sources:
            if source.id in sources and source != sources[source.id]:
                raise ValueError("simulation-source-id-conflict")
            sources[source.id] = source
        _check_sources(context.contracts, tuple(sources.values()), request.candidates)
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
        digest = judge_input_digest(
            context.contract_for_batch(batch), request.candidates, source_manifest
        )
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
