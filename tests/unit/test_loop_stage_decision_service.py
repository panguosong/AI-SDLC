"""共用阶段服务绑定真实文件，不以模拟高分或过期提案取得修改权。"""

import hashlib
import subprocess
from dataclasses import replace

import pytest

from ai_sdlc.core.loop_decision_models import B1Assessment
from ai_sdlc.core.loop_decision_service import (
    build_b1_review_data,
    validate_b1_review_data,
)
from ai_sdlc.core.loop_models import LoopRun
from ai_sdlc.core.loop_simulation_context import SimulationPrepareRequest
from ai_sdlc.core.loop_simulation_models import STAGE_PROFILES
from ai_sdlc.core.loop_stage_decision_service import (
    StageDecisionHost,
    StageReviewSnapshot,
    prepare_stage_simulation_decision,
    read_stage_simulation_context,
    stage_material_digest,
)
from ai_sdlc.core.review_kernel import ReviewInput
from tests.unit.test_loop_simulation import assessment_data, candidate_data
from tests.unit.test_loop_simulation_models import contract_data


@pytest.fixture
def stage_project(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    (root / "source.md").write_text("冻结目标和原始来源", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "source.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    monkeypatch.setattr(
        "ai_sdlc.core.loop_stage_decision_service.time.time_ns", lambda: 100_000_000_000
    )
    return root


def host(root, stage="implementation"):
    loop_dir = root / ".ai-sdlc/loops" / stage / "stage-test"
    loop_dir.mkdir(parents=True)
    artifact = loop_dir / "actual.md"
    artifact.write_text("初始实际工件", encoding="utf-8")
    (loop_dir / "loop-run.json").write_text(
        LoopRun(
            loop_id="stage-test",
            loop_type=stage,
            decision_mode="adaptive-quantified",
            decision_capability="stage-simulation-v1",
            input_digest="sha256:" + "a" * 64,
        ).model_dump_json(),
        encoding="utf-8",
    )
    return StageDecisionHost(
        stage,
        "stage-test",
        "sha256:" + "a" * 64,
        loop_dir,
        (artifact,),
        (artifact,),
        True,
        False,
        False,
    )


def begin(root, current):
    contracts = [
        {
            **contract_data(),
            "capability": "stage-simulation-v1",
            "loop_type": current.stage_kind,
            "profile_id": profile,
        }
        for profile in STAGE_PROFILES[current.stage_kind]
    ]
    return {
        "operation": "begin",
        "request_id": "begin",
        "contracts": contracts,
        "sources": [
            {
                "id": "spec",
                "path": "source.md",
                "sha256": hashlib.sha256((root / "source.md").read_bytes()).hexdigest(),
                "locator": "all",
                "claim": "冻结目标",
            }
        ],
    }


def prepare(root, current, payload, **kwargs):
    request = SimulationPrepareRequest.model_validate(payload)
    preview = prepare_stage_simulation_decision(
        root,
        current.stage_kind,
        current.loop_id,
        request,
        host_resolver=lambda: current,
    )
    if kwargs.pop("preview", False):
        return preview
    return prepare_stage_simulation_decision(
        root,
        current.stage_kind,
        current.loop_id,
        request,
        host_resolver=lambda: current,
        dry_run=False,
        expected_digest=preview.prepare_digest,
        **kwargs,
    )


def select(root, current):
    context = prepare(root, current, begin(root, current)).context
    candidate = candidate_data(context.plan)
    frozen = prepare(
        root,
        current,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze",
            "candidates": [candidate],
        },
    ).context
    return prepare(
        root,
        current,
        {
            "operation": "record-comparison",
            "request_id": "record",
            "judgement": {
                "judge_input_digest": frozen.pending_batch.judge_input_digest,
                "assessments": [assessment_data(lower=2, upper=2)],
            },
        },
    ).context


def improvement(root, current, context):
    contract = next(
        (c for c in context.contracts if c.profile_id == "code-result-v1"), context.plan
    )
    incumbent = candidate_data(contract, "keep")
    incumbent["cost_decision_point"] = "before-improvement"
    return {
        "operation": "begin-improvement",
        "request_id": "improvement",
        "improvement": {
            "incumbent": incumbent,
            "baseline_digest": stage_material_digest(root, current),
            "criterion_ids": ["coverage"],
            "hypothesis": "用明确失败分支提高覆盖",
            "future_cost_estimate": incumbent["future_cost_estimate"],
        },
    }


def sealed(root, current, *, improve=False):
    context = select(root, current)
    current = replace(
        current, initial_ready=False, actual_ready=True, execution_started=True
    )
    current.actual_paths[0].write_text("真实执行后的当前证据", encoding="utf-8")
    if improve:
        context = prepare(root, current, improvement(root, current, context)).context
        incumbent = context.improvement.incumbent.model_dump(mode="json")
        challenger = candidate_data(context.current_contract, "better")
        challenger["cost_decision_point"] = "before-improvement"
        context = prepare(
            root,
            current,
            {
                "operation": "freeze-comparison",
                "request_id": "freeze-improve",
                "candidates": [incumbent, challenger],
            },
        ).context
        context = prepare(
            root,
            current,
            {
                "operation": "record-comparison",
                "request_id": "record-improve",
                "judgement": {
                    "judge_input_digest": context.pending_batch.judge_input_digest,
                    "assessments": [
                        assessment_data("keep", 2, 2),
                        assessment_data("better", 4, 4),
                    ],
                },
            },
        ).context
    context = prepare(
        root, current, {"operation": "seal-for-review", "request_id": "seal"}
    ).context
    return current, context


def snapshot(root, current, context, *, round_number=1, observed_at_ms=100_000):
    context_path = current.loop_dir / "decision-context.json"
    paths = (*current.actual_paths, context_path, root / "source.md")
    manifest = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }
    review = ReviewInput(
        loop_type=current.stage_kind,
        loop_id=current.loop_id,
        round_number=round_number,
        input_digest=hashlib.sha256(str(manifest).encode()).hexdigest(),
        artifact_paths=list(manifest),
        expert_roles=["evidence-review"],
        expert_reasons={"evidence-review": "实际义务"},
    )
    return StageReviewSnapshot(
        review, context, manifest, stage_material_digest(root, current), observed_at_ms
    )


def assessments(snap, status="PASS"):
    path = snap.review_input.artifact_paths[0]
    return {
        "evidence-review": B1Assessment.model_validate(
            {
                "input_digest": snap.review_input.input_digest,
                "context_digest": snap.context.context_digest,
                "selected_route_id": snap.context.initial_selection_id,
                "results": [
                    {
                        "id": "o0",
                        "status": status,
                        "evidence_refs": ["proof"],
                        "reason": "核对真实工件",
                    }
                ],
                "evidence": [
                    {
                        "id": "proof",
                        "path": path,
                        "sha256": snap.manifest[path],
                        "locator": "all",
                        "claim": "当前工件原文",
                    }
                ],
                "repair_readiness": {
                    "authorization": "PASS",
                    "facts": "PASS",
                    "verification": "PASS",
                    "evidence_refs": ["proof"],
                    "reason": "原授权内必要修复",
                },
            }
        )
    }


@pytest.mark.parametrize("stage", tuple(STAGE_PROFILES.keys())[:-1])
def test_all_stage_context_changes_do_not_manufacture_source_drift(
    stage_project, stage
):
    root = stage_project
    current = host(root, stage)
    before = stage_material_digest(root, current)
    context = select(root, current)
    assert context.initial_selection_id == "A"
    assert stage_material_digest(root, current) == before
    current, context = sealed_new_snapshot(root, current, context)
    data = build_b1_review_data(
        snapshot(root, current, context),
        assessments(snapshot(root, current, context)),
        has_actionable_findings=False,
    )
    assert data.evaluation.h == 0 and data.decision.action == "stop"


def sealed_new_snapshot(root, current, context):
    current = replace(
        current, initial_ready=False, actual_ready=True, execution_started=True
    )
    current.actual_paths[0].write_text("真实成果变化", encoding="utf-8")
    return current, prepare(
        root, current, {"operation": "seal-for-review", "request_id": "seal"}
    ).context


@pytest.mark.parametrize(
    "damage", ["not-ready", "review-started", "missing-artifact", "baseline-drift"]
)
def test_improvement_requires_real_current_material_and_pre_r1(stage_project, damage):
    root = stage_project
    current = host(root)
    context = select(root, current)
    current = replace(
        current, initial_ready=False, actual_ready=True, execution_started=True
    )
    payload = improvement(root, current, context)
    if damage == "not-ready":
        current = replace(current, actual_ready=False)
    elif damage == "review-started":
        current = replace(current, review_started=True)
    elif damage == "missing-artifact":
        current.actual_paths[0].unlink()
    else:
        current.actual_paths[0].write_text("另一成果", encoding="utf-8")
    before = (current.loop_dir / "decision-context.json").read_bytes()
    with pytest.raises((ValueError, OSError)):
        prepare(root, current, payload)
    assert (current.loop_dir / "decision-context.json").read_bytes() == before


@pytest.mark.parametrize(
    "status,action", [("PASS", "improve"), ("FAIL", "repair"), ("UNKNOWN", "repair")]
)
def test_only_actual_r1_pass_can_consume_conditional_improvement(
    stage_project, status, action
):
    root = stage_project
    current, context = sealed(root, host(root), improve=True)
    snap = snapshot(root, current, context)
    data = build_b1_review_data(
        snap, assessments(snap, status), has_actionable_findings=False
    )
    assert data.decision.action == action
    assert validate_b1_review_data(snap, data, has_actionable_findings=False) == data
    findings = build_b1_review_data(
        snap, assessments(snap), has_actionable_findings=True
    )
    assert findings.decision.action == "repair"


def test_expired_conditional_improvement_shrinks_to_stop(stage_project):
    root = stage_project
    current, context = sealed(root, host(root), improve=True)
    snap = snapshot(root, current, context, observed_at_ms=4_000_000)
    data = build_b1_review_data(snap, assessments(snap), has_actionable_findings=False)
    assert data.decision.action == "stop"
    assert data.decision.reason != "requirements-satisfied"


def test_current_digest_drift_blocks_seal_review_record_and_close(stage_project):
    root = stage_project
    current, context = sealed(root, host(root), improve=True)
    old = snapshot(root, current, context)
    data = build_b1_review_data(old, assessments(old), has_actionable_findings=False)
    current.actual_paths[0].write_text("封存后坏文件", encoding="utf-8")
    for purpose in ("review", "close"):
        with pytest.raises(ValueError, match="baseline-drift"):
            read_stage_simulation_context(root, current, purpose=purpose)
    with pytest.raises(ValueError, match="baseline-drift"):
        build_b1_review_data(
            snapshot(root, current, context),
            assessments(snapshot(root, current, context)),
            has_actionable_findings=False,
        )
    with pytest.raises(ValueError, match="source-drift"):
        validate_b1_review_data(
            snapshot(root, current, context), data, has_actionable_findings=False
        )
    current.actual_paths[0].write_text("R2必要修复后工件", encoding="utf-8")
    assert (
        read_stage_simulation_context(root, current, purpose="review", round_number=2)
        == context
    )
    r2 = snapshot(root, current, context, round_number=2)
    result = build_b1_review_data(
        r2, assessments(r2, "FAIL"), has_actionable_findings=False, baseline=data
    )
    assert result.decision.action == "blocked"
    assert result.evaluation.h == 1


def test_prepare_digest_includes_non_source_actual_artifacts(stage_project):
    root = stage_project
    current = host(root, "requirement")
    request = SimulationPrepareRequest.model_validate(begin(root, current))
    preview = prepare_stage_simulation_decision(
        root,
        current.stage_kind,
        current.loop_id,
        request,
        host_resolver=lambda: current,
    )
    current.actual_paths[0].write_text("改过的当前需求工件", encoding="utf-8")
    with pytest.raises(ValueError, match="prepare-digest-mismatch"):
        prepare_stage_simulation_decision(
            root,
            current.stage_kind,
            current.loop_id,
            request,
            host_resolver=lambda: current,
            dry_run=False,
            expected_digest=preview.prepare_digest,
        )
    assert not (current.loop_dir / "decision-context.json").exists()


def test_completed_r1_permits_material_reload_for_r2_not_new_simulation(stage_project):
    from ai_sdlc.core.loop_review_models import LoopReviewOutcome

    root = stage_project
    current, context = sealed(root, host(root), improve=True)
    snap = snapshot(root, current, context)
    data = build_b1_review_data(snap, assessments(snap), has_actionable_findings=False)
    outcome = LoopReviewOutcome(
        loop_id=current.loop_id,
        loop_type=current.stage_kind,
        round_number=1,
        input_digest=snap.review_input.input_digest,
        status="completed",
        expert_roles=snap.review_input.expert_roles,
        recorded_at="2026-09-07T00:00:00Z",
        simulation=data,
        infra_retry_count=0,
    )
    (current.loop_dir / "review-outcome-round-1.json").write_text(
        outcome.model_dump_json(), encoding="utf-8"
    )
    current.actual_paths[0].write_text("当前R2实际成果", encoding="utf-8")
    (root / "source.md").write_text("R1允许修复后的真实来源", encoding="utf-8")
    current = replace(current, review_started=True)
    assert read_stage_simulation_context(root, current, purpose="review") == context
    with pytest.raises(ValueError, match="review-already-started"):
        prepare(
            root,
            current,
            {
                "operation": "begin-improvement",
                "request_id": "after-r1",
                "improvement": context.improvement.model_dump(mode="json"),
            },
        )
    assert replace(snap, observed_at_ms=snap.observed_at_ms + 1) == snap


def test_repeated_seal_cannot_hide_baseline_drift(stage_project):
    root = stage_project
    current, _ = sealed(root, host(root), improve=True)
    current.actual_paths[0].write_text("封存请求后的未审修改", encoding="utf-8")
    with pytest.raises(ValueError, match="baseline-drift"):
        prepare(root, current, {"operation": "seal-for-review", "request_id": "seal"})


def test_real_implementation_old_entrypoints_dispatch_stage_capability(tmp_path, monkeypatch):
    from ai_sdlc.core.implementation_loop import start_implementation_loop
    from ai_sdlc.core.implementation_models import ImplementationStartOptions
    from ai_sdlc.core.implementation_store import read_input, read_loop_run
    from ai_sdlc.core.loop_decision_service import (
        implementation_stage_host,
        parse_implementation_context,
        prepare_simulation_decision,
        validate_implementation_context,
    )
    from tests.integration.test_quantified_implementation import _ready_project
    from tests.integration.test_simulation_quantified_loop import begin_request

    root = _ready_project(tmp_path)
    result = start_implementation_loop(
        ImplementationStartOptions(
            root=root,
            work_item="specs/demo-implementation-loop",
            loop_id="stage-real",
            decision_mode="adaptive-quantified",
            decision_capability="stage-simulation-v1",
        )
    )
    assert result.status == "ready", result
    current = implementation_stage_host(root, loop_id="stage-real")
    payload = begin_request(root)
    for profile, contract in zip(
        STAGE_PROFILES["implementation"], payload["contracts"], strict=True
    ):
        contract["capability"] = "stage-simulation-v1"
        contract["loop_type"] = "implementation"
        contract["profile_id"] = profile
    request = SimulationPrepareRequest.model_validate(payload)
    preview = prepare_simulation_decision(root, "stage-real", request)
    saved = prepare_simulation_decision(
        root,
        "stage-real",
        request,
        dry_run=False,
        expected_digest=preview.prepare_digest,
    )
    content = (current.loop_dir / "decision-context.json").read_bytes()
    assert parse_implementation_context(content) == saved.context
    from ai_sdlc.core import implementation_loop

    original_report = implementation_loop._build_report
    report_calls = []

    def counted_report(*args, **kwargs):
        report_calls.append(True)
        return original_report(*args, **kwargs)

    monkeypatch.setattr(implementation_loop, "_build_report", counted_report)
    assert (
        validate_implementation_context(
            root,
            read_loop_run(current.loop_dir / "loop-run.json"),
            read_input(current.loop_dir / "implementation-input.json"),
        )
        == saved.context
    )
    for purpose, reason in (
        ("execute", "simulation-time-estimate-unavailable"),
        ("verification", "simulation-initial-selection-missing"),
    ):
        with pytest.raises(ValueError, match=reason):
            validate_implementation_context(
                root,
                read_loop_run(current.loop_dir / "loop-run.json"),
                read_input(current.loop_dir / "implementation-input.json"),
                purpose=purpose,
            )
    from ai_sdlc.cli.loop_stage_cmd import read_stage_decision_context

    assert read_stage_decision_context(root, "implementation", "stage-real") == saved.context
    # 两个决定读取入口均不消费验收就绪值；真实阶段准备仍计算完整报告。
    assert report_calls == []
    assert implementation_stage_host(root, loop_id="stage-real") == current
    assert len(report_calls) == 1


@pytest.mark.parametrize(
    "name",
    [
        "decision-context.json",
        "requirement-freeze.json",
        "design-contract-close.json",
        "review-outcome-round-1.json",
        "review-continuation.json",
        "loop-run.json",
    ],
)
def test_upstream_framework_lineage_is_bound_but_cannot_self_prove_actual_pass(
    stage_project, name
):
    root = stage_project
    current, context = sealed(root, host(root))
    before = snapshot(root, current, context)
    path = f".ai-sdlc/loops/requirement/upstream/{name}"
    manifest = {**before.manifest, path: "c" * 64}
    snap = replace(
        before,
        manifest=manifest,
        review_input=before.review_input.model_copy(
            update={"upstream_context_paths": [path]}
        ),
    )
    # 保留上游原件在manifest，不因其是框架元数据而删除血缘绑定。
    data = build_b1_review_data(snap, assessments(snap), has_actionable_findings=False)
    assert data.evaluation.h == 0
    payload = assessments(snap)["evidence-review"].model_dump()
    payload["evidence"][0].update(path=path, sha256="c" * 64)
    with pytest.raises(ValueError, match="derived-state-forbidden"):
        build_b1_review_data(
            snap,
            {"evidence-review": B1Assessment.model_validate(payload)},
            has_actionable_findings=False,
        )


@pytest.mark.parametrize(
    "name",
    [
        "review-outcome-round-1.json",
        "review-outcome-round-2.json",
        "review-run.json",
        "decision-context.json",
        "current-review.json",
        "review-continuation.json",
        "final-report.md",
        "reviewer-invocation.json",
        "schema-validation.json",
        "resolution-history.yaml",
        "verdict.json",
        "status.json",
    ],
)
def test_prior_pr_derived_verdict_cannot_be_forecast_source_or_actual_pass(
    stage_project, name
):
    root = stage_project
    current = host(root)
    path = root / ".ai-sdlc/reviews/pr/previous-review" / name
    path.parent.mkdir(parents=True)
    path.write_text('{"verdict":"clean","status":"completed"}', encoding="utf-8")
    reference = {
        "id": "old-pr-verdict",
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "locator": "verdict",
        "claim": "旧 PR clean 被误用为当前业务义务通过",
    }
    payload = begin(root, current)
    payload["sources"].append(reference)
    with pytest.raises(ValueError, match="derived-state-forbidden"):
        prepare(root, current, payload)
    assert not (current.loop_dir / "decision-context.json").exists()

    current, context = sealed(root, current)
    before = snapshot(root, current, context)
    relative = reference["path"]
    snap = replace(
        before,
        manifest={**before.manifest, relative: reference["sha256"]},
        review_input=before.review_input.model_copy(
            update={"upstream_context_paths": [relative]}
        ),
    )
    # 裁决仍保留在血缘快照；只有把它冒充当前实际证明时才拒绝。
    actual = build_b1_review_data(
        snap, assessments(snap), has_actionable_findings=False
    )
    assert actual.evaluation.h == 0
    bad = assessments(snap)["evidence-review"].model_dump()
    bad["evidence"][0].update(path=relative, sha256=reference["sha256"])
    with pytest.raises(ValueError, match="derived-state-forbidden"):
        build_b1_review_data(
            snap,
            {"evidence-review": B1Assessment.model_validate(bad)},
            has_actionable_findings=False,
        )


@pytest.mark.parametrize(
    "path",
    [
        ".ai-sdlc/reviews/pr/current-review/review-pack.json",
        ".ai-sdlc/reviews/pr/current-review/current.diff",
        ".ai-sdlc/reviews/pr/current-review/verification-evidence.json",
        ".ai-sdlc/reviews/pr/current-review/findings.json",
        ".ai-sdlc/reviews/pr/current-review/resolution.yaml",
        "business/review-outcome-round-1.json",
        "business/final-report.md",
    ],
)
def test_pr_original_content_and_business_names_remain_usable(stage_project, path):
    from ai_sdlc.core.loop_stage_decision_service import validate_stage_source_boundary

    root = stage_project
    current = host(root)
    source_path = root / path
    source_path.parent.mkdir(parents=True)
    source_path.write_text("当前实际差异或逐项验证的原始内容", encoding="utf-8")
    validate_stage_source_boundary(root, [source_path])
    payload = begin(root, current)
    payload["sources"].append(
        {
            "id": "current-content",
            "path": path,
            "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "locator": "all",
            "claim": "当前原始内容",
        }
    )
    context = prepare(root, current, payload, preview=True).context
    assert path in {source.path for source in context.sources}
    current, context = sealed(root, current)
    before = snapshot(root, current, context)
    digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    snap = replace(
        before,
        manifest={**before.manifest, path: digest},
        review_input=before.review_input.model_copy(
            update={"upstream_context_paths": [path]}
        ),
    )
    evidence = assessments(snap)["evidence-review"].model_dump()
    evidence["evidence"][0].update(path=path, sha256=digest)
    data = build_b1_review_data(
        snap,
        {"evidence-review": B1Assessment.model_validate(evidence)},
        has_actionable_findings=False,
    )
    assert data.evaluation.h == 0


def test_initial_extra_runtime_fact_is_bound_without_fake_source_drift(stage_project):
    root = stage_project
    current = host(root)
    context = prepare(root, current, begin(root, current)).context
    path = root / ".ai-sdlc/state/existing-fact.md"
    path.parent.mkdir(parents=True)
    path.write_text("独立原始项目事实", encoding="utf-8")
    source = {
        "id": "extra",
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "locator": "all",
        "claim": "原始项目事实",
    }
    frozen = prepare(
        root,
        current,
        {
            "operation": "freeze-comparison",
            "request_id": "with-extra-source",
            "candidates": [candidate_data(context.plan)],
            "sources": [source],
        },
    ).context
    assert source["path"] in frozen.pending_batch.source_manifest
    path.write_text("评分后变化的事实", encoding="utf-8")
    with pytest.raises(ValueError, match="source.*(mismatch|drift)"):
        prepare(
            root,
            current,
            {
                "operation": "record-comparison",
                "request_id": "reject-fact-drift",
                "judgement": {
                    "judge_input_digest": frozen.pending_batch.judge_input_digest,
                    "assessments": [assessment_data()],
                },
            },
        )


def test_deleted_context_cannot_restart_original_window(stage_project):
    root = stage_project
    current = host(root)
    payload = begin(root, current)
    context = prepare(root, current, payload).context
    run_path = current.loop_dir / "loop-run.json"
    run = LoopRun.model_validate_json(run_path.read_bytes())
    assert run.decision_started_at_ms == context.started_at_ms
    before = run_path.read_bytes()
    (current.loop_dir / "decision-context.json").unlink()
    with pytest.raises(ValueError, match="context-lost-after-begin"):
        prepare(root, current, payload)
    assert run_path.read_bytes() == before
    assert not (current.loop_dir / "decision-context.json").exists()


def test_failed_initial_context_write_keeps_started_marker(stage_project, monkeypatch):
    import ai_sdlc.core.loop_stage_decision_service as service

    root = stage_project
    current = host(root)
    payload = begin(root, current)

    def fail_write(*args):
        raise OSError("simulated-context-write-failure")

    original_write = service._write_context
    monkeypatch.setattr(service, "_write_context", fail_write)
    with pytest.raises(OSError, match="context-write-failure"):
        prepare(root, current, payload)
    assert (
        LoopRun.model_validate_json(
            (current.loop_dir / "loop-run.json").read_bytes()
        ).decision_started_at_ms
        == 100_000
    )
    monkeypatch.setattr(service, "_write_context", original_write)
    monkeypatch.setattr(service.time, "time_ns", lambda: 120_000_000_000)
    before = (current.loop_dir / "loop-run.json").read_bytes()
    preview = prepare(root, current, payload, preview=True)
    assert preview.context.started_at_ms == 100_000
    assert (current.loop_dir / "loop-run.json").read_bytes() == before
    recovered = prepare(root, current, payload).context
    assert recovered == preview.context
    assert recovered.pending_batch.number == 1
    assert recovered.comparisons == ()
    assert len(recovered.receipts) == 1
    assert (
        "decision_begin_pending_digest"
        not in LoopRun.model_validate_json(
            (current.loop_dir / "loop-run.json").read_bytes()
        ).model_dump()
    )
    assert prepare(root, current, payload).status == "existing"


@pytest.mark.parametrize("marker", [None, 99_000])
def test_context_start_requires_the_same_saved_run_marker(stage_project, marker):
    root = stage_project
    current = host(root)
    payload = begin(root, current)
    prepare(root, current, payload)
    path = current.loop_dir / "loop-run.json"
    run = LoopRun.model_validate_json(path.read_bytes()).model_copy(
        update={"decision_started_at_ms": marker}
    )
    path.write_text(run.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="start-marker-mismatch"):
        read_stage_simulation_context(root, current)
    with pytest.raises(ValueError, match="start-marker-mismatch"):
        prepare(root, current, payload)


@pytest.mark.parametrize(
    "capability", [None, "implementation-b1", "implementation-simulation-v1"]
)
def test_old_run_formats_cannot_gain_start_marker(capability):
    payload = {"loop_id": "old", "loop_type": "implementation"}
    if capability is not None:
        payload.update(
            decision_mode="adaptive-quantified", decision_capability=capability
        )
    assert "decision_started_at_ms" not in LoopRun.model_validate(payload).model_dump()
    with pytest.raises(ValueError, match="start-marker-capability-mismatch"):
        LoopRun.model_validate({**payload, "decision_started_at_ms": 100_000})


@pytest.mark.parametrize("execution_started", [False, True])
def test_readonly_time_admission_cannot_return_consumed_stage_time(
    monkeypatch, execution_started
):
    from ai_sdlc.core.loop_decision_service import require_simulation_time_admission
    from tests.unit.test_stage_simulation import complete_improvement, transition

    context = transition(
        complete_improvement(), "seal-for-review", source="4" * 64, now=5000
    )
    monkeypatch.setattr(
        "ai_sdlc.core.loop_decision_service.time.time_ns",
        lambda: (context.last_observed_at_ms - 1) * 1_000_000,
    )
    with pytest.raises(ValueError, match="simulation-clock-moved-backwards"):
        require_simulation_time_admission(context, execution_started=execution_started)


@pytest.mark.parametrize("stage", tuple(STAGE_PROFILES.keys())[:-1])
@pytest.mark.parametrize(
    "purpose", ["execute", "seal", "review", "close", "repeat-seal"]
)
def test_selected_source_drift_is_rejected_without_improvement(
    stage_project, stage, purpose
):
    root = stage_project
    current = host(root, stage)
    context = select(root, current)
    if purpose in {"review", "close", "repeat-seal"}:
        current, context = sealed_new_snapshot(root, current, context)
    elif purpose == "seal":
        current = replace(current, initial_ready=False, actual_ready=True)
    path = current.loop_dir / "decision-context.json"
    before = path.read_bytes()
    (root / "source.md").write_text("选择路线后变化的需求来源", encoding="utf-8")
    with pytest.raises(ValueError, match="source-digest-mismatch"):
        if purpose in {"seal", "repeat-seal"}:
            prepare(
                root, current, {"operation": "seal-for-review", "request_id": "seal"}
            )
        else:
            read_stage_simulation_context(root, current, purpose=purpose)
    assert path.read_bytes() == before


@pytest.mark.parametrize("damage", ["request", "contract", "source", "clock"])
def test_pending_begin_recovery_rejects_changed_inputs(
    stage_project, monkeypatch, damage
):
    import ai_sdlc.core.loop_stage_decision_service as service

    root = stage_project
    current = host(root)
    payload = begin(root, current)
    original_write = service._write_context

    def fail_write(*args):
        raise OSError("interrupted-begin")

    monkeypatch.setattr(service, "_write_context", fail_write)
    with pytest.raises(OSError, match="interrupted-begin"):
        prepare(root, current, payload)
    monkeypatch.setattr(service, "_write_context", original_write)
    if damage == "request":
        payload["request_id"] = "new-window"
    elif damage == "contract":
        payload["contracts"][0]["criteria"][0]["statement"] = "不同的评分标准"
    elif damage == "source":
        (root / "source.md").write_text("改过的开始来源", encoding="utf-8")
        payload = begin(root, current)
    else:
        monkeypatch.setattr(service.time, "time_ns", lambda: 99_000_000_000)
    before = (current.loop_dir / "loop-run.json").read_bytes()
    with pytest.raises(ValueError, match="begin-recovery|clock-moved-backwards"):
        prepare(root, current, payload)
    assert (current.loop_dir / "loop-run.json").read_bytes() == before
    assert not (current.loop_dir / "decision-context.json").exists()


@pytest.mark.parametrize(
    "capability", [None, "implementation-b1", "implementation-simulation-v1"]
)
def test_old_modes_cannot_gain_pending_begin_receipt(capability):
    payload = {"loop_id": "old", "loop_type": "implementation"}
    if capability is not None:
        payload.update(
            decision_mode="adaptive-quantified", decision_capability=capability
        )
    assert (
        "decision_begin_pending_digest"
        not in LoopRun.model_validate(payload).model_dump()
    )
    with pytest.raises(ValueError, match="pending-identity-mismatch"):
        LoopRun.model_validate({**payload, "decision_begin_pending_digest": "c" * 64})


def test_interrupted_begin_finalization_requires_exact_begin_retry(
    stage_project, monkeypatch
):
    import ai_sdlc.core.loop_stage_decision_service as service

    root = stage_project
    current = host(root)
    payload = begin(root, current)
    original_write = service.LoopArtifactStore.write_json_artifact
    calls = 0

    def fail_finalize(store, path, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted-finalization")
        return original_write(store, path, value)

    monkeypatch.setattr(service.LoopArtifactStore, "write_json_artifact", fail_finalize)
    with pytest.raises(OSError, match="interrupted-finalization"):
        prepare(root, current, payload)
    context_path = current.loop_dir / "decision-context.json"
    before = context_path.read_bytes()
    with pytest.raises(ValueError, match="begin-incomplete"):
        read_stage_simulation_context(root, current, purpose="execute")
    with pytest.raises(ValueError, match="begin-recovery"):
        prepare(root, current, {"operation": "seal-for-review", "request_id": "seal"})
    monkeypatch.setattr(
        service.LoopArtifactStore, "write_json_artifact", original_write
    )
    recovered = prepare(root, current, payload)
    assert recovered.status == "prepared"
    assert context_path.read_bytes() == before
    assert prepare(root, current, payload).status == "existing"


@pytest.mark.parametrize(
    "damage", ["missing", "loop", "context", "route", "stop", "failed"]
)
def test_source_revision_exception_requires_matching_completed_r1(
    stage_project, damage
):
    from ai_sdlc.core.loop_review_models import LoopReviewOutcome

    root = stage_project
    current, context = sealed(root, host(root))
    snap = snapshot(root, current, context)
    data = build_b1_review_data(
        snap, assessments(snap, "FAIL"), has_actionable_findings=True
    )
    if damage == "context":
        data = data.model_copy(update={"context_digest": "b" * 64})
    elif damage == "route":
        data = data.model_copy(update={"selected_route_id": "other"})
    elif damage == "stop":
        data = build_b1_review_data(
            snap, assessments(snap), has_actionable_findings=False
        )
    outcome = LoopReviewOutcome(
        loop_id="other" if damage == "loop" else current.loop_id,
        loop_type=current.stage_kind,
        round_number=1,
        input_digest=snap.review_input.input_digest,
        status="failed" if damage == "failed" else "completed",
        failure_kind="provider-error" if damage == "failed" else "",
        failure_reason="未完成的 R1" if damage == "failed" else "",
        expert_roles=snap.review_input.expert_roles,
        recorded_at="2026-09-07T00:00:00Z",
        simulation=None if damage == "failed" else data,
        infra_retry_count=0,
    )
    if damage != "missing":
        (current.loop_dir / "review-outcome-round-1.json").write_text(
            outcome.model_dump_json(), encoding="utf-8"
        )
    (root / "source.md").write_text("未经有效R1许可的来源变化", encoding="utf-8")
    for round_number in (1, 2):
        with pytest.raises(ValueError, match="source-digest-mismatch"):
            read_stage_simulation_context(
                root, current, purpose="review", round_number=round_number
            )


def test_actual_r1_snapshot_cannot_accept_changed_declared_source(stage_project):
    root = stage_project
    current, context = sealed(root, host(root))
    (root / "source.md").write_text("R1之前不同来源", encoding="utf-8")
    snap = snapshot(root, current, context)
    with pytest.raises(ValueError, match="source-digest-mismatch"):
        build_b1_review_data(snap, assessments(snap), has_actionable_findings=False)


def _cost_rejected_host(root, monkeypatch, current=None):
    """真实文件宿主配合合成候选和判断，不代表业务或模型验收。"""
    current = current or host(root)
    context = prepare(root, current, begin(root, current)).context
    frozen = prepare(
        root,
        current,
        {
            "operation": "freeze-comparison",
            "request_id": "cost-first-freeze",
            "candidates": [
                candidate_data(context.plan, name, seconds=(1800, 3600))
                for name in ("A", "B")
            ],
        },
    ).context
    monkeypatch.setattr(
        "ai_sdlc.core.loop_stage_decision_service.time.time_ns",
        lambda: 101_000_000_000,
    )
    rejected = prepare(
        root,
        current,
        {
            "operation": "record-comparison",
            "request_id": "cost-first-judge",
            "judgement": {
                "judge_input_digest": frozen.pending_batch.judge_input_digest,
                "assessments": [assessment_data(name) for name in ("A", "B")],
            },
        },
    ).context
    assert rejected.initial_selection_id is None
    assert {row.reason for row in rejected.comparisons[0].selection.excluded} == {
        "time_plan_exceeded"
    }
    return current, rejected


def _cost_preparation_fact(root, path="evidence/completed-preparation.json"):
    fact = root / path
    fact.parent.mkdir(parents=True, exist_ok=True)
    fact.write_text('{"synthetic_preparation": "completed"}\n', encoding="utf-8")
    return fact, {
        "id": "completed-preparation",
        "path": path,
        "sha256": hashlib.sha256(fact.read_bytes()).hexdigest(),
        "locator": "synthetic_preparation",
        "claim": "合成准备回执，只供宿主来源绑定测试。",
    }


def _cost_revision_request(context, source):
    candidates = []
    for original in context.comparisons[0].candidates:
        candidate = original.model_dump(mode="json")
        candidate["basis"].append(
            {
                "id": "prepared",
                "kind": "project_fact",
                "source_ref": source["id"],
                "locator": source["locator"],
                "statement": "合成准备事实；剩余成本仍包含判断、实施与验收。",
            }
        )
        candidate["future_cost_estimate"].update(
            lower_seconds=1700,
            upper_seconds=3500,
            basis_refs=[*original.future_cost_estimate.basis_refs, "prepared"],
        )
        candidates.append(candidate)
    return {
        "operation": "freeze-comparison",
        "request_id": "cost-second-freeze",
        "candidates": candidates,
        "sources": [source],
    }


def _cost_host_bytes(current):
    return {
        name: (current.loop_dir / name).read_bytes()
        for name in ("decision-context.json", "loop-run.json")
    }


@pytest.mark.parametrize("late", [False, True])
def test_cost_correction_host_preserves_history_and_records_late_rejection(
    stage_project, monkeypatch, late
):
    root = stage_project
    current, original = _cost_rejected_host(root, monkeypatch)
    _, source = _cost_preparation_fact(root)
    payload = {"operation": "correct-input", "request_id": "cost-correction"}
    before = _cost_host_bytes(current)
    preview = prepare(root, current, payload, preview=True)
    assert preview.status == "preview"
    assert _cost_host_bytes(current) == before
    corrected = prepare(root, current, payload).context
    assert corrected.comparisons == original.comparisons
    assert corrected.started_at_ms == original.started_at_ms
    assert corrected.contracts == original.contracts
    assert (current.loop_dir / "loop-run.json").read_bytes() == before["loop-run.json"]
    frozen = prepare(root, current, _cost_revision_request(original, source)).context
    assert frozen.pending_batch.number == 2
    assert frozen.pending_batch.source_manifest[source["path"]] == source["sha256"]
    assert frozen.sources[-1].sha256 == source["sha256"]
    assert frozen.pending_batch.judge_input_digest != (
        original.comparisons[0].judge_input_digest
    )
    frozen_bytes = _cost_host_bytes(current)
    with pytest.raises(ValueError, match="judgement-input"):
        prepare(
            root,
            current,
            {
                "operation": "record-comparison",
                "request_id": "cost-reject-first-judge-replay",
                "judgement": original.comparisons[0].judgement.model_dump(mode="json"),
            },
        )
    assert _cost_host_bytes(current) == frozen_bytes
    monkeypatch.setattr(
        "ai_sdlc.core.loop_stage_decision_service.time.time_ns",
        lambda: 220_000_000_000 if late else 104_000_000_000,
    )
    completed = prepare(
        root,
        current,
        {
            "operation": "record-comparison",
            "request_id": "cost-second-judge",
            "judgement": {
                "judge_input_digest": frozen.pending_batch.judge_input_digest,
                "assessments": [assessment_data(name) for name in ("A", "B")],
            },
        },
    ).context
    assert len(completed.comparisons) == 2
    assert completed.comparisons[0] == original.comparisons[0]
    assert completed.started_at_ms == original.started_at_ms
    assert completed.contracts == original.contracts
    assert completed.initial_selection_id == (None if late else "A")
    if late:
        assert completed.comparisons[-1].selection.reason == "model_plan_not_feasible"
        assert completed.comparisons[-1].elapsed_seconds == 120
    # 后来的墙钟不应令已保存的选择或否定结果无法历史回读。
    monkeypatch.setattr(
        "ai_sdlc.core.loop_stage_decision_service.time.time_ns",
        lambda: 5_000_000_000_000,
    )
    completed_bytes = _cost_host_bytes(current)
    assert read_stage_simulation_context(root, current) == completed
    assert _cost_host_bytes(current) == completed_bytes


@pytest.mark.parametrize(
    "boundary",
    ["correction-preview", "freeze-preview", "record-new-fact", "record-original"],
)
def test_cost_correction_host_rejects_fact_drift_without_writing_state(
    stage_project, monkeypatch, boundary
):
    root = stage_project
    current, original = _cost_rejected_host(root, monkeypatch)
    fact, source = _cost_preparation_fact(root)
    payload = {"operation": "correct-input", "request_id": "cost-correction"}
    if boundary != "correction-preview":
        prepare(root, current, payload)
        payload = _cost_revision_request(original, source)
    if boundary.startswith("record-"):
        frozen = prepare(root, current, payload).context
        payload = {
            "operation": "record-comparison",
            "request_id": "cost-drift-judge",
            "judgement": {
                "judge_input_digest": frozen.pending_batch.judge_input_digest,
                "assessments": [assessment_data(name) for name in ("A", "B")],
            },
        }
    before = _cost_host_bytes(current)
    preview = prepare(root, current, payload, preview=True)
    assert _cost_host_bytes(current) == before
    changed = root / "source.md" if boundary == "record-original" else fact
    changed.write_text("来源字节已经变化，不能沿用旧摘要。", encoding="utf-8")
    with pytest.raises(ValueError, match="(prepare-digest|source.*(mismatch|drift))"):
        prepare_stage_simulation_decision(
            root,
            current.stage_kind,
            current.loop_id,
            SimulationPrepareRequest.model_validate(payload),
            host_resolver=lambda: current,
            dry_run=False,
            expected_digest=preview.prepare_digest,
        )
    assert _cost_host_bytes(current) == before


@pytest.mark.parametrize("invalid", ["fake-sha", "framework-source"])
def test_cost_correction_host_rejects_false_or_framework_fact_sources(
    stage_project, monkeypatch, invalid
):
    root = stage_project
    current, original = _cost_rejected_host(root, monkeypatch)
    path = (
        ".ai-sdlc/state/completed-preparation.json"
        if invalid == "framework-source"
        else "evidence/completed-preparation.json"
    )
    _, source = _cost_preparation_fact(root, path)
    prepare(
        root,
        current,
        {"operation": "correct-input", "request_id": "cost-correction"},
    )
    if invalid == "fake-sha":
        source["sha256"] = "b" * 64
    before = _cost_host_bytes(current)
    with pytest.raises(ValueError, match="(source-digest-mismatch|correction-cost)"):
        prepare(root, current, _cost_revision_request(original, source))
    assert _cost_host_bytes(current) == before


@pytest.mark.parametrize("late", [False, True])
def test_time_revision_host_preview_apply_freeze_judge_and_execution_read(
    stage_project, monkeypatch, late
):
    from tests.unit.test_quantified_input_correction import (
        time_revision_candidates,
        time_revision_request,
    )

    root = stage_project
    current, original = _cost_rejected_host(root, monkeypatch)
    _, source = _cost_preparation_fact(root)
    payload = time_revision_request(original, [source])
    before = _cost_host_bytes(current)
    preview = prepare(root, current, payload, preview=True)
    assert _cost_host_bytes(current) == before
    revised = prepare(root, current, payload).context
    assert revised.context_digest == preview.context.context_digest
    assert revised.comparisons == original.comparisons
    assert revised.input_correction.old_contracts == original.contracts
    assert revised.sources[-1].sha256 == source["sha256"]
    assert (current.loop_dir / "loop-run.json").read_bytes() == before["loop-run.json"]
    candidates = time_revision_candidates(original, revised)
    for candidate in candidates:
        candidate["basis"][-1]["source_ref"] = source["id"]
    frozen = prepare(
        root,
        current,
        dict(
            operation="freeze-comparison",
            request_id="revised-freeze",
            candidates=candidates,
        ),
    ).context
    assert frozen.pending_batch.source_manifest[source["path"]] == source["sha256"]
    monkeypatch.setattr(
        "ai_sdlc.core.loop_stage_decision_service.time.time_ns",
        lambda: 6_100_000_000_000 if late else 104_000_000_000,
    )
    completed = prepare(
        root,
        current,
        dict(
            operation="record-comparison",
            request_id="revised-judge",
            judgement={
                "judge_input_digest": frozen.pending_batch.judge_input_digest,
                "assessments": [assessment_data(name) for name in ("A", "B")],
            },
        ),
    ).context
    assert completed.comparisons[0] == original.comparisons[0]
    assert completed.initial_selection_id == (None if late else "A")
    if late:
        assert completed.comparisons[-1].elapsed_seconds == 6000
        with pytest.raises(ValueError, match="simulation-time-estimate-unavailable"):
            read_stage_simulation_context(root, current, purpose="execute")
    else:
        assert (
            read_stage_simulation_context(root, current, purpose="execute") == completed
        )
    monkeypatch.setattr(
        "ai_sdlc.core.loop_stage_decision_service.time.time_ns",
        lambda: 20_000_000_000_000,
    )
    assert read_stage_simulation_context(root, current) == completed


@pytest.mark.parametrize("boundary", ["revise-preview", "freeze-preview", "record"])
def test_time_revision_host_fact_drift_never_writes_context_or_run(
    stage_project, monkeypatch, boundary
):
    from tests.unit.test_quantified_input_correction import (
        time_revision_candidates,
        time_revision_request,
    )

    root = stage_project
    current, original = _cost_rejected_host(root, monkeypatch)
    fact, source = _cost_preparation_fact(root)
    payload = time_revision_request(original, [source])
    if boundary != "revise-preview":
        revised = prepare(root, current, payload).context
        candidates = time_revision_candidates(original, revised)
        for candidate in candidates:
            candidate["basis"][-1]["source_ref"] = source["id"]
        payload = dict(
            operation="freeze-comparison",
            request_id="revised-freeze",
            candidates=candidates,
        )
    if boundary == "record":
        frozen = prepare(root, current, payload).context
        payload = dict(
            operation="record-comparison",
            request_id="revised-judge",
            judgement={
                "judge_input_digest": frozen.pending_batch.judge_input_digest,
                "assessments": [assessment_data(name) for name in ("A", "B")],
            },
        )
    before = _cost_host_bytes(current)
    preview = prepare(root, current, payload, preview=True)
    fact.write_text("修订依据已经漂移。", encoding="utf-8")
    with pytest.raises(ValueError, match="(prepare-digest|source.*(mismatch|drift))"):
        prepare_stage_simulation_decision(
            root,
            current.stage_kind,
            current.loop_id,
            SimulationPrepareRequest.model_validate(payload),
            host_resolver=lambda: current,
            dry_run=False,
            expected_digest=preview.prepare_digest,
        )
    assert _cost_host_bytes(current) == before


def test_time_revision_reaches_native_implementation_in_progress(
    initialized_project_dir, monkeypatch
):
    import json

    from typer.testing import CliRunner

    from ai_sdlc.cli.loop_stage_cmd import resolve_stage_decision_host
    from ai_sdlc.cli.main import app
    from ai_sdlc.core.implementation_loop import (
        ImplementationRecordOptions,
        ImplementationStartOptions,
        record_implementation_progress,
        start_implementation_loop,
    )
    from ai_sdlc.core.loop_simulation_context import SimulationContext
    from tests.integration.test_quantified_implementation import _ready_project
    from tests.unit.test_quantified_input_correction import (
        time_revision_candidates,
        time_revision_request,
    )

    root = _ready_project(initialized_project_dir)
    (root / "source.md").write_text("合成冻结目标", encoding="utf-8")
    started = start_implementation_loop(
        ImplementationStartOptions(
            root=root,
            work_item="specs/demo-implementation-loop",
            loop_id="stage-test",
            decision_mode="adaptive-quantified",
            decision_capability="stage-simulation-v1",
        )
    )
    assert started.status == "ready", started
    monkeypatch.setattr(
        "ai_sdlc.core.loop_stage_decision_service.time.time_ns", lambda: 100_000_000_000
    )
    current = resolve_stage_decision_host(root, "implementation", "stage-test")
    current, original = _cost_rejected_host(root, monkeypatch, current)
    blocked = record_implementation_progress(
        ImplementationRecordOptions(
            root=root,
            loop_id="stage-test",
            task_id="T11",
            status="in_progress",
        )
    )
    assert blocked.status == "blocked"
    _, source = _cost_preparation_fact(root)
    monkeypatch.chdir(root)
    runner = CliRunner()

    def cli_prepare(payload):
        path = root.parent / f"{payload['request_id']}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        args = [
            "loop",
            "decision-prepare",
            "--type",
            "implementation",
            "--loop-id",
            "stage-test",
            "--input",
            str(path),
            "--json",
        ]
        before = _cost_host_bytes(current)
        preview = runner.invoke(app, [*args, "--dry-run"])
        assert preview.exit_code == 0, preview.output
        assert _cost_host_bytes(current) == before
        applied = runner.invoke(
            app,
            [*args, "--expect-digest", json.loads(preview.output)["prepare_digest"]],
        )
        assert applied.exit_code == 0, applied.output
        result = json.loads(applied.output)
        if "judge_input" in result:
            assert "time-plan revision" in result["judge_input"]["instructions"]
            assert (
                result["judge_input"]["contract"]["time_plan"]["window_seconds"]
                == "10800"
            )
        return SimulationContext.model_validate(result["context"])

    revised = cli_prepare(time_revision_request(original, [source]))
    candidates = time_revision_candidates(original, revised)
    for candidate in candidates:
        candidate["basis"][-1]["source_ref"] = source["id"]
    frozen = cli_prepare(
        dict(
            operation="freeze-comparison",
            request_id="revised-freeze",
            candidates=candidates,
        ),
    )
    completed = cli_prepare(
        dict(
            operation="record-comparison",
            request_id="revised-judge",
            judgement={
                "judge_input_digest": frozen.pending_batch.judge_input_digest,
                "assessments": [assessment_data(name) for name in ("A", "B")],
            },
        ),
    )
    assert completed.initial_selection_id == "A"
    result = record_implementation_progress(
        ImplementationRecordOptions(
            root=root,
            loop_id="stage-test",
            task_id="T11",
            status="in_progress",
        )
    )
    assert result.status == "ready", result
    from ai_sdlc.core.loop_decision_service import implementation_execution_started

    assert implementation_execution_started(root, "stage-test")
    assert (
        read_stage_simulation_context(
            root, resolve_stage_decision_host(root, "implementation", "stage-test")
        )
        == completed
    )
