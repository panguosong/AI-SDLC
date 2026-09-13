"""Local PR 新能力的正常 Next；保持当前树验证及原交付终态。"""

from __future__ import annotations

import re
from pathlib import Path

from ai_sdlc.core.loop_decision_service import DecisionPreparationError
from ai_sdlc.core.loop_review_service import outcome_path
from ai_sdlc.core.loop_status import LoopNextActionGuidance, LoopNextActionSafety
from ai_sdlc.core.pr_review_decision import (
    pr_review_context_path,
    validate_pr_review_context,
)
from ai_sdlc.core.pr_review_service import (
    _delivery_commit_state,
    _load_current_review_run,
    _load_review_pack,
    _load_verification_evidence,
    _precommit_staged_source_blocker,
    _verification_evidence_blocker,
)
from ai_sdlc.core.stable_file_read import _stable_regular_file_exists


def pr_simulation_status_guidance(
    root: Path, loop_id: str
) -> LoopNextActionGuidance | None:
    """只在正式评审前补宿主准备动作；封存后交回既有 Review/Commit/Close。"""
    try:
        return _pr_simulation_status_guidance(root, loop_id)
    except (OSError, ValueError) as exc:
        raise DecisionPreparationError(str(exc)) from exc


def _pr_simulation_status_guidance(root, loop_id):
    root = root.resolve(strict=True)
    run, path = _load_current_review_run(root)
    if run.loop_id != loop_id:
        raise ValueError("pr-decision-current-review-identity-mismatch")
    context_path = pr_review_context_path(root, run)
    exists = _stable_regular_file_exists(root, context_path)
    if run.decision_mode == "legacy":
        validate_pr_review_context(root, run)
        return None
    if not exists and (
        run.status == "closed"
        or run.decision_started_at_ms is not None
        or any(
            _stable_regular_file_exists(root, outcome_path(path.parent, number))
            for number in (1, 2)
        )
    ):
        raise ValueError("pr-decision-context-missing-after-begin-or-review")
    context = validate_pr_review_context(root, run) if exists else None
    if run.status in {"blocked", "needs_user", "needs_fix"}:
        return LoopNextActionGuidance(
            command="ai-sdlc pr-review fix"
            if run.status == "needs_fix"
            else "ai-sdlc pr-review status --json",
            reason=run.next_action
            or "Resolve the original independent reviewer findings before preparing current-tree quantification.",
            writes_artifacts=run.status == "needs_fix",
            safety=LoopNextActionSafety.BLOCKED
            if run.status == "blocked"
            else LoopNextActionSafety.WRITES_REVIEW_ARTIFACTS
            if run.status == "needs_fix"
            else LoopNextActionSafety.SAFE_READ_ONLY,
            evidence=[path.relative_to(root).as_posix()],
        )
    if not exists:
        return LoopNextActionGuidance(
            command="ai-sdlc pr-review decision-prepare --schema --json",
            reason="Host agent: instantiate delivery-readiness-v1 from the current staged tree and upstream obligations, then begin. Users do not fill scores or budgets; do not select an implementation route.",
            requires_model=True,
            safety=LoopNextActionSafety.SAFE_READ_ONLY,
            evidence=[path.relative_to(root).as_posix()],
        )
    assert context is not None
    if context.phase == "review_sealed":
        return None
    blocker = _precommit_staged_source_blocker(
        root, run, _load_review_pack(root, run.review_pack_path)
    )
    if blocker:
        raise ValueError(f"pr-decision-current-tree-mismatch: {blocker}")
    command = (
        f"ai-sdlc pr-review decision-prepare --review-id {run.review_id} "
        "--input <host-generated-input> --dry-run --json"
    )
    evidence = [context_path.relative_to(root).as_posix()]
    if context.pending_batch is not None:
        batch = context.pending_batch
        reason = (
            "Host agent: freeze-comparison for the only current-staged-tree risk/counterexample analysis."
            if not batch.candidates
            else "Host agent: give the frozen judge_input to an independent read-only context, then record-comparison with the exact digest or a truthful bounded failure receipt."
        )
        return LoopNextActionGuidance(
            command=command, reason=reason, requires_model=True, evidence=evidence
        )
    if context.initial_selection_id is None:
        return LoopNextActionGuidance(
            reason="Current-tree independent analysis did not complete; preserve the existing context and failure receipts. Do not reset the review or manufacture PASS.",
            safety=LoopNextActionSafety.BLOCKED,
            evidence=evidence,
        )
    blocker = _verification_evidence_blocker(
        run, _load_verification_evidence(root, run)
    )
    if blocker:
        return LoopNextActionGuidance(
            command="ai-sdlc pr-review verify --cwd . -- <argv...>",
            reason="Execute the existing real quality command on the exact current staged tree before sealing. Forecast scores are not actual verification.",
            writes_artifacts=True,
            writes_code=False,
            safety=LoopNextActionSafety.WRITES_REVIEW_ARTIFACTS,
            evidence=evidence,
        )
    return LoopNextActionGuidance(
        command=command,
        reason="Host agent: submit operation=seal-for-review, then follow the original independent actual review, exact-tree commit and Close. No product route or optional improvement is authorized.",
        evidence=evidence,
    )


def pr_delivery_status_guidance(
    root: Path, loop_id: str, review_digest: str
) -> LoopNextActionGuidance | None:
    """调用方已证明实际评审 passed 后，再映射原精确树 Commit/Close。"""
    root = root.resolve(strict=True)
    run, run_path = _load_current_review_run(root)
    if run.decision_mode == "legacy" or run.status == "closed":
        return None
    if run.loop_id != loop_id or not re.fullmatch(r"[0-9a-f]{64}", review_digest):
        raise DecisionPreparationError("pr-decision-delivery-identity-mismatch")
    validate_pr_review_context(root, run, purpose="review")
    pack = _load_review_pack(root, run.review_pack_path)
    blocker = _verification_evidence_blocker(
        run, _load_verification_evidence(root, run)
    )
    if blocker:
        raise DecisionPreparationError(blocker)
    delivery_blocker, commit, _ = _delivery_commit_state(root, run, pack)
    if commit == run.head_commit:
        blocker = _precommit_staged_source_blocker(root, run, pack)
        if blocker:
            raise DecisionPreparationError(blocker)
        command = 'ai-sdlc pr-review commit --message "<reviewed change>"'
        reason = "Actual independent review passed; commit the unchanged reviewed staged tree with the original Git hooks, then complete Close."
    else:
        if delivery_blocker:
            raise DecisionPreparationError(delivery_blocker)
        command = (
            f"ai-sdlc pr-review close --review-id {run.review_id} --loop-id {run.loop_id} "
            f"--expect-review-digest {review_digest}"
        )
        reason = "The exact reviewed tree is committed; complete the original Local PR Close with the current independent review digest."
    return LoopNextActionGuidance(
        command=command,
        reason=reason,
        writes_artifacts=True,
        writes_code=False,
        safety=LoopNextActionSafety.WRITES_PROJECT_ARTIFACTS,
        evidence=[run_path.relative_to(root).as_posix()],
    )
