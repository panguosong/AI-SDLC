"""当前暂存树量化不选产品路线，也不替代真实验证和独立评审。"""

from __future__ import annotations

import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from typer.testing import CliRunner

from ai_sdlc.cli.main import app
from ai_sdlc.core.loop_simulation_context import SimulationPrepareRequest
from ai_sdlc.core.loop_simulation_models import StageScoreContract
from ai_sdlc.core.pr_review_decision import (
    prepare_pr_review_decision,
    validate_pr_review_context,
)
from ai_sdlc.core.pr_review_models import ReviewRun
from ai_sdlc.core.pr_review_service import (
    PRReviewStartOptions,
    _load_current_review_run,
    close_pr_review,
    commit_pr_review,
    rerun_pr_review,
    start_pr_review,
    verify_pr_review_command,
)
from tests.unit.test_loop_simulation import assessment_data, candidate_data
from tests.unit.test_loop_simulation_models import contract_data
from tests.unit.test_pr_review_service import _git, _init_repo


@pytest.fixture
def current_tree(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text(
        "# Test\nCurrent authorization boundary.\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "README.md")
    options = PRReviewStartOptions(
        root=tmp_path,
        base_ref="HEAD",
        diff_source="local-staged",
        provider_id="mock-reviewer",
        review_id="quantified-pr",
        decision_mode="adaptive-quantified",
        decision_capability="stage-simulation-v1",
    )
    result = start_pr_review(options)
    assert result.status == "started", result
    run, _ = _load_current_review_run(tmp_path)
    return tmp_path, run, options


def _apply(root, payload):
    request = SimulationPrepareRequest.model_validate(payload)
    preview = prepare_pr_review_decision(root, "quantified-pr", request)
    return prepare_pr_review_decision(
        root,
        "quantified-pr",
        request,
        dry_run=False,
        expected_digest=preview.prepare_digest,
    ).context


def _begin_request(root):
    data = contract_data()
    data.update(
        capability="stage-simulation-v1",
        loop_type="local-pr-review",
        profile_id="delivery-readiness-v1",
    )
    data["time_plan"].update(
        scope="local-pr-review-close",
        work_breakdown=["当前树风险分析", "真实验证与独立评审", "原 Close"],
    )
    return {
        "operation": "begin",
        "request_id": "begin",
        "contracts": [data],
        "sources": [
            {
                "id": "spec",
                "path": "README.md",
                "sha256": hashlib.sha256((root / "README.md").read_bytes()).hexdigest(),
                "locator": "authorization boundary",
                "claim": "当前交付义务",
            }
        ],
    }


def _begin(root):
    return _apply(root, _begin_request(root))


def _scored(root, *, lower=4, upper=4):
    context = _begin(root)
    candidate = candidate_data(context.plan, "current-staged-tree", seconds=None)
    candidate["mechanism"] = "当前暂存树风险与反例分析"
    context = _apply(
        root,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze",
            "candidates": [candidate],
        },
    )
    return _apply(
        root,
        {
            "operation": "record-comparison",
            "request_id": "record",
            "judgement": {
                "judge_input_digest": context.pending_batch.judge_input_digest,
                "assessments": [assessment_data("current-staged-tree", lower, upper)],
            },
        },
    )


def test_explicit_pr_identity_and_legacy_serialization(current_tree):
    _, run, _ = current_tree
    assert run.decision_staged_tree_oid == run.staged_tree_oid
    assert run.decision_capability == "stage-simulation-v1"
    legacy = ReviewRun(review_id="old", loop_id="old-loop")
    assert not set(legacy.model_dump()) & {
        "decision_mode",
        "decision_capability",
        "decision_staged_tree_oid",
        "decision_started_at_ms",
        "decision_begin_pending_digest",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"decision_capability": "future-unknown"},
        {"decision_mode": "legacy"},
        {"decision_staged_tree_oid": ""},
        {"decision_begin_pending_digest": "a" * 64},
        {"decision_started_at_ms": 1, "decision_begin_pending_digest": "not-a-digest"},
    ],
)
def test_unknown_or_partial_identity_is_not_legacy(current_tree, mutation):
    _, run, _ = current_tree
    with pytest.raises(ValueError):
        ReviewRun.model_validate({**run.model_dump(), **mutation})


def test_quantified_pr_rejects_nonstaged_and_identity_restarts(current_tree):
    root, _, options = current_tree
    assert (
        "requires-current-staged-tree"
        in start_pr_review(replace(options, diff_source="local-git-range")).blocker
    )
    assert "requires-rerun" in start_pr_review(options).blocker
    assert (
        "identity-mismatch"
        in start_pr_review(
            replace(options, decision_mode="legacy", decision_capability=None)
        ).blocker
    )
    assert not (root / ".ai-sdlc/loops/local-pr-review").exists()


def test_rerun_serializes_begin_and_preserves_its_start_marker(
    current_tree, monkeypatch
):
    from ai_sdlc.core import pr_review_service

    root, _, _ = current_tree
    provider_entered = Event()
    release_provider = Event()
    begin_entered = Event()
    original_provider = pr_review_service._run_provider

    def paused_provider(*args, **kwargs):
        provider_entered.set()
        assert release_provider.wait(30)
        return original_provider(*args, **kwargs)

    def begin_after_provider():
        begin_entered.set()
        return _begin(root)

    monkeypatch.setattr(pr_review_service, "_run_provider", paused_provider)
    with ThreadPoolExecutor(max_workers=2) as executor:
        rerun = executor.submit(rerun_pr_review, root)
        try:
            assert provider_entered.wait(10)
            beginning = executor.submit(begin_after_provider)
            assert begin_entered.wait(10)
            # 真实 begin 与 rerun 并发；缺锁时旧 run 会覆盖刚保存的开始标记。
            try:
                beginning.result(timeout=5)
                began_during_rerun = True
            except TimeoutError:
                began_during_rerun = False
        finally:
            release_provider.set()
        assert rerun.result(timeout=10).status == "started"
        # 锁释放后的真实快照允许 Windows I/O 延迟；上方 5 秒互斥观察不变。
        context = beginning.result(timeout=60)

    saved, _ = _load_current_review_run(root)
    assert saved.decision_started_at_ms == context.started_at_ms
    assert not began_during_rerun
    assert validate_pr_review_context(root, saved) == context


@pytest.mark.parametrize("begun", [False, True])
def test_rerun_waits_for_shared_stage_lock_before_mutating(
    current_tree, monkeypatch, begun
):
    from ai_sdlc.core import pr_review_service
    from ai_sdlc.core.loop_resource_lock import _stage_write_guard

    root, run, _ = current_tree
    if begun:
        _begin(root)
    directory = root / ".ai-sdlc/reviews/pr" / run.review_id

    def snapshot():
        return {
            str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }

    before = snapshot()
    entered = Event()
    original_load = pr_review_service._load_current_review_run

    def loaded(*args, **kwargs):
        result = original_load(*args, **kwargs)
        entered.set()
        return result

    monkeypatch.setattr(pr_review_service, "_load_current_review_run", loaded)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with _stage_write_guard(root, "local-pr-review", run.review_id):
            rerun = executor.submit(rerun_pr_review, root)
            assert entered.wait(10)
            try:
                early_result = rerun.result(timeout=5)
            except TimeoutError:
                early_result = None
            unchanged_while_locked = snapshot() == before
        assert rerun.result(timeout=10).status == "started"
    assert early_result is None, early_result
    assert unchanged_while_locked


def _interrupted_begin(root, monkeypatch, failure_point):
    from ai_sdlc.core import pr_review_decision
    from ai_sdlc.core.loop_artifacts import LoopArtifactStore

    request = SimulationPrepareRequest.model_validate(_begin_request(root))
    preview = prepare_pr_review_decision(root, "quantified-pr", request)
    original_write = LoopArtifactStore.write_json_artifact
    writes = 0

    def interrupted_context(*args, **kwargs):
        raise OSError("injected context write failure")

    def interrupted_finalize(store, path, payload, *args, **kwargs):
        nonlocal writes
        if path.name == "review-run.json":
            writes += 1
            if writes == 2:
                raise OSError("injected finalize write failure")
        return original_write(store, path, payload, *args, **kwargs)

    with monkeypatch.context() as failure:
        if failure_point == "context":
            failure.setattr(pr_review_decision, "_write_context", interrupted_context)
        else:
            failure.setattr(
                LoopArtifactStore, "write_json_artifact", interrupted_finalize
            )
        with pytest.raises(OSError, match=f"injected {failure_point} write failure"):
            prepare_pr_review_decision(
                root,
                "quantified-pr",
                request,
                dry_run=False,
                expected_digest=preview.prepare_digest,
            )
    return request


@pytest.mark.parametrize("failure_point", ["context", "finalize"])
def test_interrupted_begin_recovers_only_original_context_and_clock(
    current_tree, monkeypatch, failure_point
):
    from types import SimpleNamespace

    from ai_sdlc.core import pr_review_decision

    root, _, _ = current_tree
    request = _interrupted_begin(root, monkeypatch, failure_point)
    pending, run_path = _load_current_review_run(root)
    frozen_run = run_path.read_bytes()
    frozen_digest = getattr(pending, "decision_begin_pending_digest", None)
    assert frozen_digest is not None
    context_path = run_path.with_name("decision-context.json")
    assert context_path.exists() == (failure_point == "finalize")
    with pytest.raises(ValueError, match="begin-pending"):
        validate_pr_review_context(root, pending)
    rejected = rerun_pr_review(root)
    assert rejected.status == "blocked"
    assert "begin-pending" in rejected.blocker
    with pytest.raises(ValueError, match="begin-pending"):
        prepare_pr_review_decision(
            root,
            "quantified-pr",
            SimulationPrepareRequest(operation="seal-for-review", request_id="skip"),
        )
    assert run_path.read_bytes() == frozen_run

    monkeypatch.setattr(
        pr_review_decision,
        "time",
        SimpleNamespace(
            time_ns=lambda: (pending.decision_started_at_ms + 60_000) * 1_000_000
        ),
    )
    preview = prepare_pr_review_decision(root, "quantified-pr", request)
    assert preview.context.context_digest == frozen_digest
    assert preview.context.started_at_ms == pending.decision_started_at_ms
    assert run_path.read_bytes() == frozen_run
    recovered = prepare_pr_review_decision(
        root,
        "quantified-pr",
        request,
        dry_run=False,
        expected_digest=preview.prepare_digest,
    )
    saved, _ = _load_current_review_run(root)
    assert "decision_begin_pending_digest" not in saved.model_dump()
    assert validate_pr_review_context(root, saved) == recovered.context
    assert recovered.context.context_digest == frozen_digest
    assert saved.decision_started_at_ms == pending.decision_started_at_ms
    context_path.unlink()
    with pytest.raises(ValueError, match="context-missing-after-begin"):
        prepare_pr_review_decision(root, "quantified-pr", request)


@pytest.mark.parametrize("failure_point", ["context", "finalize"])
@pytest.mark.parametrize("mutation", ["request", "source", "contract", "clock"])
def test_pending_begin_rejects_changed_identity_without_mutation(
    current_tree, monkeypatch, failure_point, mutation
):
    from types import SimpleNamespace

    from ai_sdlc.core import pr_review_decision

    root, _, _ = current_tree
    request = _interrupted_begin(root, monkeypatch, failure_point)
    pending, run_path = _load_current_review_run(root)
    assert getattr(pending, "decision_begin_pending_digest", None) is not None
    frozen_run = run_path.read_bytes()
    changed = request.model_dump(mode="json", exclude_unset=True)
    if mutation == "request":
        changed["request_id"] = "different-begin"
    elif mutation == "source":
        (root / "README.md").write_text(
            "different authorized source\n", encoding="utf-8"
        )
        _git(root, "add", "README.md")
        changed["sources"][0]["sha256"] = hashlib.sha256(
            (root / "README.md").read_bytes()
        ).hexdigest()
    elif mutation == "contract":
        changed["contracts"][0]["goal_contract"]["obligations"][0]["statement"] = (
            "changed objective"
        )
    else:
        monkeypatch.setattr(
            pr_review_decision,
            "time",
            SimpleNamespace(
                time_ns=lambda: (pending.decision_started_at_ms - 1) * 1_000_000
            ),
        )
    changed_request = SimulationPrepareRequest.model_validate(changed)
    with pytest.raises(
        ValueError,
        match="simulation-clock-moved-backwards" if mutation == "clock" else None,
    ):
        prepare_pr_review_decision(root, "quantified-pr", changed_request)
    assert run_path.read_bytes() == frozen_run


def test_missing_context_blocks_instead_of_downgrade(current_tree):
    root, run, _ = current_tree
    with pytest.raises(ValueError, match="context-missing"):
        validate_pr_review_context(root, run, purpose="review")


def test_single_tree_unknown_analysis_is_not_actual_q(current_tree):
    root, _, _ = current_tree
    context = _scored(root, lower=0, upper=4)
    assert context.initial_selection_id == "current-staged-tree"
    assert context.selection.scores["current-staged-tree"].u_sim == 1
    assert "evaluation" not in context.model_dump()
    assert not context.selection.scores["current-staged-tree"].targets_met


def test_high_prediction_cannot_seal_without_staged_verification(current_tree):
    root, _, _ = current_tree
    _scored(root)
    with pytest.raises(ValueError, match="actual-verification-required"):
        _apply(root, {"operation": "seal-for-review", "request_id": "seal"})
    assert commit_pr_review(root, message="must not commit").status == "blocked"


def test_verified_seal_still_requires_actual_independent_review(current_tree):
    root, run, _ = current_tree
    _scored(root)
    result = verify_pr_review_command(
        root, cwd=".", argv=(sys.executable, "-c", "print('verified')")
    )
    assert result.status == "ready", result
    context = _apply(root, {"operation": "seal-for-review", "request_id": "seal"})
    assert context.phase == "review_sealed"
    run, _ = _load_current_review_run(root)
    assert validate_pr_review_context(root, run, purpose="review") == context
    assert commit_pr_review(root, message="must not commit").status == "blocked"
    assert not (
        root / ".ai-sdlc/reviews/pr/quantified-pr/review-outcome-round-1.json"
    ).exists()


@pytest.mark.parametrize("supplied_noop_validator", [False, True])
def test_core_pr_close_cannot_skip_actual_review(current_tree, supplied_noop_validator):
    from ai_sdlc.cli.loop_review_cmd import prepare_current_loop_review

    root, run, _ = current_tree
    _scored(root)
    assert (
        verify_pr_review_command(
            root, cwd=".", argv=(sys.executable, "-c", "print('verified')")
        ).status
        == "ready"
    )
    _apply(root, {"operation": "seal-for-review", "request_id": "seal"})
    assert commit_pr_review(root, message="must remain uncommitted").status == "blocked"
    assert _git(root, "rev-parse", "HEAD") == run.head_commit
    prepared, _ = prepare_current_loop_review(root, "local-pr-review", run.loop_id)
    assert prepared.status == "review_missing"
    if supplied_noop_validator:
        # 手工同树提交只构造 Close 反例，不代表通过了实际独立评审。
        _git(root, "commit", "-m", "test exact-tree close bypass")
    result = close_pr_review(
        root,
        expected_review_digest=prepared.review_input.input_digest
        if supplied_noop_validator
        else "",
        review_input_validator=(lambda *args, **kwargs: None)
        if supplied_noop_validator
        else None,
    )
    assert result.status == "blocked", result
    assert (
        "expert review is not current and clean"
        if supplied_noop_validator
        else "pr-decision-guarded-close-required"
    ) in result.blocker
    assert not (root / ".ai-sdlc/reviews/pr/quantified-pr/final-report.md").exists()
    current, _ = _load_current_review_run(root)
    assert current.status != "closed"


def test_tree_drift_rejected_and_context_preserved(current_tree):
    root, _, _ = current_tree
    context = _begin(root)
    path = root / ".ai-sdlc/reviews/pr/quantified-pr/decision-context.json"
    before = path.read_bytes()
    (root / "README.md").write_text("different tree\n", encoding="utf-8")
    _git(root, "add", "README.md")
    with pytest.raises(ValueError, match="mismatch|drift"):
        _apply(
            root,
            {
                "operation": "freeze-comparison",
                "request_id": "freeze",
                "candidates": [candidate_data(context.plan, "current-staged-tree")],
            },
        )
    assert path.read_bytes() == before


def test_pr_does_not_accept_product_routes_or_optional_improvement(current_tree):
    root, _, _ = current_tree
    context = _begin(root)
    with pytest.raises(ValueError, match="current-tree-only"):
        _apply(
            root,
            {
                "operation": "freeze-comparison",
                "request_id": "product-route",
                "candidates": [candidate_data(context.plan, "implement-new-route")],
            },
        )
    with pytest.raises(ValueError, match="current-tree-only"):
        _apply(
            root,
            {
                "operation": "record-comparison",
                "request_id": "continue",
                "continue_search": True,
                "reason": "more score",
                "judgement": {
                    "judge_input_digest": "1" * 64,
                    "assessments": [assessment_data("current-staged-tree")],
                },
            },
        )


def test_pr_schema_exposes_host_contract_and_current_tree_only():
    result = CliRunner().invoke(
        app, ["pr-review", "decision-prepare", "--schema", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["x-guidance"]["profile_id"] == "delivery-readiness-v1"
    assert payload["x-guidance"]["candidate_id"] == "current-staged-tree"
    assert "begin-improvement" not in payload["x-guidance"]["allowed_operations"]
    assert StageScoreContract.model_json_schema()


def test_prepare_digest_conflict_and_repeat_preserve_clock(current_tree):
    root, _, _ = current_tree
    context = _begin(root)
    request = SimulationPrepareRequest(
        operation="freeze-comparison",
        request_id="freeze",
        candidates=[candidate_data(context.plan, "current-staged-tree")],
    )
    preview = prepare_pr_review_decision(root, "quantified-pr", request)
    with pytest.raises(ValueError, match="prepare-digest-mismatch"):
        prepare_pr_review_decision(
            root, "quantified-pr", request, dry_run=False, expected_digest="0" * 64
        )
    applied = prepare_pr_review_decision(
        root,
        "quantified-pr",
        request,
        dry_run=False,
        expected_digest=preview.prepare_digest,
    )
    repeated = prepare_pr_review_decision(
        root,
        "quantified-pr",
        request,
        dry_run=False,
        expected_digest=preview.prepare_digest,
    )
    assert repeated.status == "existing"
    assert repeated.context == applied.context
    assert repeated.context.started_at_ms == context.started_at_ms
    assert len(repeated.context.receipts) == 2


def test_deleted_context_cannot_restart_original_time_window(current_tree):
    from ai_sdlc.cli.loop_pr_stage_cmd import pr_simulation_status_guidance

    root, _, _ = current_tree
    context = _begin(root)
    run, run_path = _load_current_review_run(root)
    assert run.decision_started_at_ms == context.started_at_ms
    original_run = run_path.read_bytes()
    (run_path.parent / "decision-context.json").unlink()
    with pytest.raises(ValueError, match="context-missing-after-begin"):
        _begin(root)
    with pytest.raises(ValueError, match="context-missing-after-begin"):
        validate_pr_review_context(root, run)
    with pytest.raises(ValueError, match="context-missing-after-begin"):
        pr_simulation_status_guidance(root, run.loop_id)
    verified = verify_pr_review_command(
        root, cwd=".", argv=(sys.executable, "-c", "raise RuntimeError('must not run')")
    )
    assert verified.status == "blocked"
    assert "context-missing-after-begin" in verified.blocker
    restarted = rerun_pr_review(root)
    assert restarted.status == "blocked"
    assert "context-missing-after-begin" in restarted.blocker
    assert run_path.read_bytes() == original_run


def test_context_requires_matching_saved_start_marker(current_tree):
    from ai_sdlc.cli.loop_pr_stage_cmd import pr_simulation_status_guidance
    from ai_sdlc.core.pr_review_decision import validate_captured_pr_review_context

    root, _, _ = current_tree
    context = _begin(root)
    run, run_path = _load_current_review_run(root)
    for marker in (None, context.started_at_ms + 1):
        changed = run.model_copy(update={"decision_started_at_ms": marker})
        with pytest.raises(ValueError, match="identity-mismatch"):
            validate_captured_pr_review_context(changed, context)
        run_path.write_text(changed.model_dump_json(), encoding="utf-8")
        with pytest.raises(ValueError, match="marker-missing|identity-mismatch"):
            pr_simulation_status_guidance(root, run.loop_id)


def test_exact_tree_commit_preserves_actual_source_binding(current_tree):
    from ai_sdlc.core.pr_review_decision import pr_review_source_digest

    root, run, _ = current_tree
    context = _begin(root)
    before = pr_review_source_digest(root, run, context.sources)
    # 这里只验证源码摘要不因合法的同树提交漂移，不冒充已通过独立审查。
    _git(root, "commit", "-m", "test exact tree identity")
    assert _git(root, "rev-parse", "HEAD") != run.head_commit
    assert _git(root, "rev-parse", "HEAD^{tree}") == run.staged_tree_oid
    assert pr_review_source_digest(root, run, context.sources) == before
    (root / "README.md").write_text("modified behavior\n", encoding="utf-8")
    assert pr_review_source_digest(root, run, context.sources) != before


@pytest.mark.parametrize(
    "status,expected", [("PASS", "stop"), ("UNKNOWN", "repair"), ("FAIL", "repair")]
)
def test_actual_obligations_override_perfect_forecast(current_tree, status, expected):
    import time

    from ai_sdlc.core.loop_decision_models import B1Assessment
    from ai_sdlc.core.loop_decision_service import build_b1_review_data
    from ai_sdlc.core.loop_stage_decision_service import StageReviewSnapshot
    from ai_sdlc.core.pr_review_decision import pr_review_source_digest
    from ai_sdlc.core.review_kernel import ReviewInput

    root, run, _ = current_tree
    _scored(root)
    verified = verify_pr_review_command(
        root, cwd=".", argv=(sys.executable, "-c", "print('verified')")
    )
    assert verified.status == "ready"
    context = _apply(root, {"operation": "seal-for-review", "request_id": "seal"})
    path = ".ai-sdlc/reviews/pr/quantified-pr/decision-context.json"
    review = ReviewInput(
        loop_id=run.loop_id,
        loop_type="local-pr-review",
        round_number=1,
        input_digest="a" * 64,
        artifact_paths=["README.md", path],
        expert_roles=["cross-stage-delivery"],
        expert_reasons={"cross-stage-delivery": "跨阶段目标和当前树核对"},
    )
    manifest = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in review.artifact_paths
    }
    snapshot = StageReviewSnapshot(
        review,
        context,
        manifest,
        pr_review_source_digest(root, run, context.sources),
        time.time_ns() // 1_000_000,
        path,
    )
    assessment = B1Assessment.model_validate(
        {
            "input_digest": review.input_digest,
            "context_digest": context.context_digest,
            "selected_route_id": "current-staged-tree",
            "results": [
                {
                    "id": "o0",
                    "status": status,
                    "evidence_refs": ["proof"] if status != "UNKNOWN" else [],
                    "reason": "当前真实工件核对",
                }
            ],
            "evidence": [
                {
                    "id": "proof",
                    "path": "README.md",
                    "sha256": manifest["README.md"],
                    "locator": "authorization",
                    "claim": "实际义务检查",
                }
            ],
            "repair_readiness": {
                "authorization": "PASS",
                "facts": "PASS",
                "verification": "PASS",
                "evidence_refs": ["proof"],
                "reason": "现有修复范围",
            },
        }
    )
    actual = build_b1_review_data(
        snapshot, {"cross-stage-delivery": assessment}, has_actionable_findings=False
    )
    assert actual.decision.action == expected
    assert actual.evaluation.h == (0 if status == "PASS" else 1)
    assert actual.evaluation.q == (100 if status == "PASS" else 0)
    assert actual.selected_route_id == "current-staged-tree"
    assert context.selection.scores["current-staged-tree"].s_low == 100
