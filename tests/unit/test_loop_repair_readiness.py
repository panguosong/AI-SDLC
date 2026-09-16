"""准备度补录只解锁原 R2；真实文件与原生评分保持绑定。"""

import hashlib
import importlib
import json
from dataclasses import replace

import pytest

from ai_sdlc.core.loop_decision_service import build_b1_review_data
from ai_sdlc.core.loop_review_models import LoopReviewOutcome
from ai_sdlc.core.loop_review_service import (
    RecordLoopReviewOptions,
    prepare_loop_review,
    record_loop_review,
    validate_prepared_outcome_for_close,
)
from ai_sdlc.core.loop_stage_decision_service import read_stage_simulation_context
from tests.unit.test_loop_stage_decision_service import (
    assessments,
    host,
    sealed,
    snapshot,
)
from tests.unit.test_loop_stage_decision_service import stage_project as stage_project


def api():
    name = "ai_sdlc.core.loop_repair_readiness"
    assert importlib.util.find_spec(name) is not None, "缺少保留 R1 的准备度补录入口"
    return importlib.import_module(name)


@pytest.fixture
def blocked(stage_project):
    root = stage_project
    current = host(root, "requirement")
    (current.loop_dir / "requirement-intake.json").write_text(
        json.dumps(
            {
                "decision_mode": "adaptive-quantified",
                "decision_capability": "stage-simulation-v1",
            }
        )
    )
    current, context = sealed(root, current)
    snap = snapshot(root, current, context)
    rows = assessments(snap, "FAIL")
    row = rows["evidence-review"]
    rows["evidence-review"] = row.model_copy(
        update={
            "repair_readiness": row.repair_readiness.model_copy(
                update={"authorization": "UNKNOWN"}
            )
        }
    )
    data = build_b1_review_data(snap, rows, has_actionable_findings=False)
    outcome = LoopReviewOutcome(
        loop_id=current.loop_id,
        loop_type="requirement",
        round_number=1,
        input_digest=snap.review_input.input_digest,
        status="completed",
        expert_roles=snap.review_input.expert_roles,
        recorded_at="2026-09-16T00:00:00Z",
        simulation=data,
        infra_retry_count=0,
    )
    assert outcome.simulation.decision.reason == "repair-unavailable"
    first = current.loop_dir / "review-outcome-round-1.json"
    first.write_text(outcome.model_dump_json(), encoding="utf-8")
    evidence = root / ".ai-sdlc/state/readiness-evidence.md"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        "既有宿主授权；仅补充遗漏边界；同一义务及剩余 R2 验收。", encoding="utf-8"
    )

    def resolve(number):
        read_stage_simulation_context(
            root, current, purpose="review", round_number=number
        )
        return snapshot(root, current, context, round_number=number)

    kwargs = dict(
        loop_dir=current.loop_dir,
        input_resolver=lambda n: resolve(n).review_input,
        b1_snapshot_resolver=resolve,
    )
    return root, current, context, outcome, evidence, kwargs


def prepared(blocked):
    root, current, _, _, evidence, kwargs = blocked
    return api().prepare_repair_readiness(
        root,
        loop_type="requirement",
        loop_id=current.loop_id,
        evidence_paths=(evidence,),
        **kwargs,
    )


def result_paths(blocked, proposal, *, readiness="PASS", roles=None):
    root = blocked[0]
    paths = []
    for index, role in enumerate(roles or proposal.expert_roles):
        sources = [
            dict(
                id=f"proof-{i}",
                path=path,
                sha256=digest,
                locator="all",
                claim="修复依据",
            )
            for i, (path, digest) in enumerate(proposal.evidence_manifest.items())
        ]
        path = root / f".ai-sdlc/state/readiness-result-{index}.json"
        path.write_text(
            json.dumps(
                dict(
                    role=role,
                    prepare_digest=proposal.prepare_digest,
                    repair_readiness=dict(
                        authorization=readiness,
                        facts="PASS",
                        verification="PASS",
                        evidence_refs=[s["id"] for s in sources],
                        reason="已有宿主授权和可执行验收",
                    ),
                    evidence=sources,
                )
            ),
            encoding="utf-8",
        )
        paths.append(path)
    return tuple(paths)


def record(blocked, proposal, paths=None):
    root, current, _, _, evidence, kwargs = blocked
    return api().record_repair_readiness(
        RecordLoopReviewOptions(
            root,
            "requirement",
            current.loop_id,
            proposal.prepare_digest,
            paths if paths is not None else result_paths(blocked, proposal),
        ),
        evidence_paths=(evidence,),
        **kwargs,
    )


def review(blocked):
    root, current, _, _, _, kwargs = blocked
    return prepare_loop_review(
        root, loop_type="requirement", loop_id=current.loop_id, **kwargs
    )


def test_repair_readiness_continues_only_original_second_round(blocked):
    root, current, context, outcome, _, kwargs = blocked
    original = (current.loop_dir / "review-outcome-round-1.json").read_bytes()
    context_bytes = (current.loop_dir / "decision-context.json").read_bytes()
    run_bytes = (current.loop_dir / "loop-run.json").read_bytes()
    assert review(blocked).reason == "repair-unavailable"
    assert "review-repair-prepare" in review(blocked).next_action
    proposal = prepared(blocked)
    record(blocked, proposal)
    assert review(blocked).status == "needs_fix"
    from ai_sdlc.core.loop_stage_input import _require_stage_revision

    _require_stage_revision(root, "requirement", current.loop_dir, context)
    with pytest.raises(ValueError):
        validate_prepared_outcome_for_close(
            review(blocked), expected_digest=outcome.input_digest
        )
    current.actual_paths[0].write_text("补齐原需求的失败边界", encoding="utf-8")
    (root / "source.md").write_text("原目标及边界澄清", encoding="utf-8")
    next_review = review(blocked)
    assert next_review.status == "review_missing"
    assert next_review.review_input.round_number == 2
    assert next_review.baseline_outcome == outcome
    snap = kwargs["b1_snapshot_resolver"](2)
    paths = []
    for index, (role, assessment) in enumerate(assessments(snap).items()):
        path = root / f".ai-sdlc/state/quality-r2-{index}.json"
        path.write_text(
            json.dumps(
                dict(
                    execution=dict(
                        status="completed",
                        roles=[role],
                        role_reasons={role: snap.review_input.expert_reasons[role]},
                        findings=[],
                    ),
                    assessment=assessment.model_dump(mode="json"),
                )
            ),
            encoding="utf-8",
        )
        paths.append(path)
    result = record_loop_review(
        RecordLoopReviewOptions(
            root,
            "requirement",
            current.loop_id,
            snap.review_input.input_digest,
            tuple(paths),
        ),
        **kwargs,
    )
    assert result.status == "passed"
    assert review(blocked).review_input.round_number == 2
    assert (current.loop_dir / "review-outcome-round-1.json").read_bytes() == original
    assert (current.loop_dir / "decision-context.json").read_bytes() == context_bytes
    assert (current.loop_dir / "loop-run.json").read_bytes() == run_bytes
    with pytest.raises(ValueError):
        record(blocked, proposal)


@pytest.mark.parametrize("value", ["UNKNOWN", "FAIL"])
def test_unknown_or_failed_readiness_never_unlocks(blocked, value):
    proposal = prepared(blocked)
    with pytest.raises(ValueError, match="repair-readiness-not-ready"):
        record(blocked, proposal, result_paths(blocked, proposal, readiness=value))
    assert review(blocked).reason == "repair-unavailable"
    assert not (blocked[1].loop_dir / "repair-readiness-supplement.json").exists()


def test_role_mismatch_and_duplicate_cannot_replace_original(blocked):
    proposal = prepared(blocked)
    with pytest.raises(ValueError, match="repair-readiness-role-mismatch"):
        record(
            blocked, proposal, result_paths(blocked, proposal, roles=["other-expert"])
        )
    paths = result_paths(blocked, proposal)
    with pytest.raises(ValueError, match="repair-readiness-role-mismatch"):
        record(blocked, proposal, paths + paths)
    record(blocked, proposal)
    saved = (blocked[1].loop_dir / "repair-readiness-supplement.json").read_bytes()
    with pytest.raises(ValueError, match="repair-readiness-already-recorded"):
        record(blocked, proposal)
    assert (
        blocked[1].loop_dir / "repair-readiness-supplement.json"
    ).read_bytes() == saved


@pytest.mark.parametrize("target", ["r1", "context", "evidence"])
def test_tampering_after_supplement_fails_closed(blocked, target):
    proposal = prepared(blocked)
    record(blocked, proposal)
    paths = {
        "r1": blocked[1].loop_dir / "review-outcome-round-1.json",
        "context": blocked[1].loop_dir / "decision-context.json",
        "evidence": blocked[4],
    }
    paths[target].write_bytes(paths[target].read_bytes() + b" ")
    with pytest.raises(ValueError):
        review(blocked)


@pytest.mark.parametrize("target", ["artifact", "evidence"])
def test_drift_between_prepare_and_record_rejects(blocked, target):
    proposal = prepared(blocked)
    paths = result_paths(blocked, proposal)
    path = blocked[1].actual_paths[0] if target == "artifact" else blocked[4]
    path.write_text("准备后材料发生变化", encoding="utf-8")
    with pytest.raises(ValueError):
        record(blocked, proposal, paths)
    assert not (blocked[1].loop_dir / "repair-readiness-supplement.json").exists()


@pytest.mark.parametrize(
    "reason", ["explicit-fail", "r2", "closed", "no-new-evidence", "unsupported"]
)
def test_ineligible_readiness_cannot_reopen_history(blocked, reason):
    root, current, _, outcome, evidence, kwargs = blocked
    loop_type = "requirement"
    if reason == "explicit-fail":
        payload = outcome.model_dump(mode="json")
        payload["simulation"]["assessments"]["evidence-review"]["repair_readiness"][
            "authorization"
        ] = "FAIL"
        (current.loop_dir / "review-outcome-round-1.json").write_text(
            json.dumps(payload)
        )
    elif reason == "r2":
        (current.loop_dir / "review-outcome-round-2.json").write_text("{}")
    elif reason == "closed":
        (current.loop_dir / "requirement-freeze.json").write_text("{}")
    elif reason == "no-new-evidence":
        evidence = current.actual_paths[0]
    else:
        loop_type = "implementation"
    with pytest.raises(ValueError):
        api().prepare_repair_readiness(
            root,
            loop_type=loop_type,
            loop_id=current.loop_id,
            evidence_paths=(evidence,),
            **kwargs,
        )


@pytest.mark.parametrize(
    "kind",
    ["external", "symlink", "parent-symlink", "derived", "original", "duplicate"],
)
def test_evidence_path_boundaries(blocked, kind, tmp_path):
    root, current, _, _, evidence, kwargs = blocked
    paths = (evidence,)
    if kind == "external":
        evidence = root.parent / "external-readiness.md"
        evidence.write_text("外部输入")
        paths = (evidence,)
    elif kind == "symlink":
        alias = evidence.parent / "alias.md"
        alias.symlink_to(evidence)
        paths = (alias,)
    elif kind == "parent-symlink":
        alias = root / "state-alias"
        alias.symlink_to(evidence.parent, target_is_directory=True)
        paths = (alias / evidence.name,)
    elif kind == "derived":
        paths = (current.loop_dir / "decision-context.json",)
    elif kind == "original":
        paths = (current.actual_paths[0],)
    else:
        paths = (evidence, evidence)
    with pytest.raises(ValueError):
        api().prepare_repair_readiness(
            root,
            loop_type="requirement",
            loop_id=current.loop_id,
            evidence_paths=paths,
            **kwargs,
        )


def test_supplement_cannot_self_prove_business_quality(blocked):
    from ai_sdlc.core.loop_stage_decision_service import validate_stage_source_boundary

    proposal = prepared(blocked)
    record(blocked, proposal)
    with pytest.raises(ValueError, match="decision-source-derived-state-forbidden"):
        validate_stage_source_boundary(
            blocked[0], [blocked[1].loop_dir / "repair-readiness-supplement.json"]
        )


@pytest.mark.parametrize(
    "name",
    [
        "source-resolution.json",
        "model-resolution.json",
        "redaction-report.json",
        "future-provider-metadata.json",
        "findings.json",
    ],
)
def test_framework_pr_artifacts_cannot_supply_new_readiness(blocked, name):
    root, current, _, _, _, kwargs = blocked
    derived = root / ".ai-sdlc/reviews/pr/previous-review" / name
    derived.parent.mkdir(parents=True)
    derived.write_text('{"access_status":"resolved"}', encoding="utf-8")
    original = (current.loop_dir / "review-outcome-round-1.json").read_bytes()
    with pytest.raises(ValueError, match="derived.*forbidden"):
        api().prepare_repair_readiness(
            root,
            loop_type="requirement",
            loop_id=current.loop_id,
            evidence_paths=(derived,),
            **kwargs,
        )
    assert (current.loop_dir / "review-outcome-round-1.json").read_bytes() == original
    assert not (current.loop_dir / "repair-readiness-supplement.json").exists()
    assert not (current.loop_dir / "review-outcome-round-2.json").exists()


@pytest.mark.parametrize(
    "name",
    [
        "source-resolution.json",
        "model-resolution.json",
        "redaction-report.json",
        "future-provider-metadata.json",
    ],
)
def test_framework_pr_metadata_cannot_self_prove_actual_assessment(blocked, name):
    from ai_sdlc.core.loop_decision_models import B1Assessment

    root, _, _, _, _, kwargs = blocked
    derived = root / ".ai-sdlc/reviews/pr/previous-review" / name
    derived.parent.mkdir(parents=True)
    derived.write_text('{"access_status":"resolved"}', encoding="utf-8")
    relative = derived.relative_to(root).as_posix()
    digest = hashlib.sha256(derived.read_bytes()).hexdigest()
    original_snapshot = kwargs["b1_snapshot_resolver"](1)
    bound = replace(
        original_snapshot,
        manifest={**original_snapshot.manifest, relative: digest},
        review_input=original_snapshot.review_input.model_copy(
            update={"upstream_context_paths": [relative]}
        ),
    )
    rows = assessments(bound)
    bad = rows["evidence-review"].model_dump()
    bad["evidence"][0].update(path=relative, sha256=digest)
    rows["evidence-review"] = B1Assessment.model_validate(bad)
    with pytest.raises(ValueError, match="derived.*forbidden"):
        build_b1_review_data(bound, rows, has_actionable_findings=False)


@pytest.mark.parametrize(
    "relative",
    [
        ".ai-sdlc/reviews/repair-basis.md",
        ".ai-sdlc/reviews/requirement-readiness/author-authorization.md",
    ],
)
def test_original_user_readiness_documents_remain_usable(blocked, relative):
    root, current, context, outcome, _, kwargs = blocked
    evidence = root / relative
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("用户原始授权、具体修法和原剩余 R2 验法。", encoding="utf-8")
    with_original_basis = (root, current, context, outcome, evidence, kwargs)
    proposal = prepared(with_original_basis)
    record(with_original_basis, proposal)
    assert review(with_original_basis).status == "needs_fix"
    assert proposal.evidence_manifest.keys() == {relative}
    assert not (current.loop_dir / "review-outcome-round-2.json").exists()


def test_business_metadata_basename_remains_valid_readiness_evidence(tmp_path):
    evidence = tmp_path / "business/source-resolution.json"
    evidence.parent.mkdir()
    content = '{"user_authorization":"repair the original documented scope"}'
    evidence.write_text(content, encoding="utf-8")
    assert api()._evidence_bytes(tmp_path, evidence) == content.encode()


def test_framework_pr_namespace_cannot_use_case_alias_for_evidence(tmp_path):
    from ai_sdlc.core.loop_stage_decision_service import validate_stage_source_boundary

    derived = tmp_path / ".AI-SDLC/ReViEwS/Pr/previous-review/source-resolution.json"
    derived.parent.mkdir(parents=True)
    derived.write_text('{"access_status":"resolved"}', encoding="utf-8")
    with pytest.raises(ValueError, match="derived.*forbidden"):
        api()._evidence_bytes(tmp_path, derived)
    with pytest.raises(ValueError, match="derived.*forbidden"):
        validate_stage_source_boundary(tmp_path, [derived])


@pytest.mark.parametrize(
    "name",
    ["Review-Outcome-Round-1.json", "LOOP-RUN.JSON", "Decision-Context.Json",
     "Repair-Readiness-Supplement.JSON", "STATUS.JSON"],
)
def test_mixed_case_derived_basename_is_never_new_evidence(tmp_path, name):
    derived = tmp_path / name
    derived.write_text('{"derived": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="derived.*forbidden"):
        api()._evidence_bytes(tmp_path, derived)


@pytest.mark.parametrize("target", ["r1", "pr-metadata", "original"])
def test_hardlink_alias_cannot_unlock_repair_readiness(blocked, target):
    root, current, context, outcome, evidence, _ = blocked
    if target == "r1":
        original = current.loop_dir / "review-outcome-round-1.json"
    elif target == "pr-metadata":
        original = root / ".ai-sdlc/reviews/pr/prior/source-resolution.json"
        original.parent.mkdir(parents=True)
        original.write_text('{"access_status":"resolved"}', encoding="utf-8")
    else:
        original = current.actual_paths[0]
    alias = evidence.parent / "new-authority.md"
    alias.hardlink_to(original)
    before = original.read_bytes()
    with pytest.raises(ValueError, match="hardlink|new-evidence-required"):
        prepared((root, current, context, outcome, alias, blocked[-1]))
    assert original.read_bytes() == before
    assert not (current.loop_dir / "repair-readiness-supplement.json").exists()
    assert not (current.loop_dir / "review-outcome-round-2.json").exists()


def test_preparation_rejects_case_alias_of_original_manifest(blocked):
    from pydantic import ValidationError

    proposal = prepared(blocked)
    original_path, digest = next(iter(proposal.original_manifest.items()))
    payload = proposal.model_dump()
    payload["evidence_manifest"] = {original_path.upper(): digest}
    with pytest.raises(ValidationError, match="new-evidence-required"):
        api().RepairReadinessPreparation.model_validate(payload)


def test_new_hardlink_after_prepare_cannot_be_recorded(blocked):
    proposal = prepared(blocked)
    (blocked[4].parent / "authority-alias.md").hardlink_to(blocked[4])
    with pytest.raises(ValueError, match="hardlink"):
        record(blocked, proposal)
    assert not (blocked[1].loop_dir / "repair-readiness-supplement.json").exists()


def test_hardlink_created_during_read_is_rejected(tmp_path, monkeypatch):
    module = api()
    evidence = tmp_path / "authority.md"
    evidence.write_text("原始修复授权", encoding="utf-8")
    original_read = module.read_stable_bytes

    def read_and_link(root, path):
        content = original_read(root, path)
        (root / "alias.md").hardlink_to(path)
        return content

    monkeypatch.setattr(module, "read_stable_bytes", read_and_link)
    with pytest.raises(ValueError, match="hardlink"):
        module._evidence_bytes(tmp_path, evidence)


def test_independent_original_document_with_same_content_remains_usable(tmp_path):
    original = tmp_path / "original.md"
    evidence = tmp_path / "authority.md"
    original.write_text("原始修复授权", encoding="utf-8")
    evidence.write_bytes(original.read_bytes())
    assert not original.samefile(evidence)
    assert api()._evidence_bytes(tmp_path, evidence) == original.read_bytes()


def test_deleted_supplement_does_not_restore_repair_authority(blocked):
    proposal = prepared(blocked)
    record(blocked, proposal)
    (blocked[1].loop_dir / "repair-readiness-supplement.json").unlink()
    assert review(blocked).reason == "repair-unavailable"
    from ai_sdlc.core.loop_stage_input import _require_stage_revision

    with pytest.raises(
        ValueError, match="simulation-stage-r1-does-not-permit-revision"
    ):
        _require_stage_revision(
            blocked[0], "requirement", blocked[1].loop_dir, blocked[2]
        )


@pytest.mark.parametrize(
    "window", ["prepare-evidence", "record-evidence", "record-result"]
)
@pytest.mark.parametrize("target", ["artifact", "source"])
def test_late_read_cannot_publish_against_changed_original(
    blocked, monkeypatch, window, target
):
    module = api()
    root, current, _, _, _, _ = blocked
    proposal = prepared(blocked)
    paths = result_paths(blocked, proposal)
    originals = {
        name: (current.loop_dir / name).read_bytes()
        for name in (
            "review-outcome-round-1.json",
            "decision-context.json",
            "loop-run.json",
        )
    }
    changed = (
        current.actual_paths[0]
        if target == "artifact"
        else root / "unreferenced-code.py"
    )
    calls = 0
    if window == "record-result":
        original_read = module.read_stable_bytes

        def read_result(project, path):
            nonlocal calls
            content = original_read(project, path)
            if path == paths[0]:
                calls += 1
                if calls == 2:
                    changed.write_text("读取完专家结果后原成果变化", encoding="utf-8")
            return content

        monkeypatch.setattr(module, "read_stable_bytes", read_result)
    else:
        original_read = module._evidence_bytes
        trigger = 2 if window == "prepare-evidence" else 4

        def read_evidence(project, path):
            nonlocal calls
            content = original_read(project, path)
            calls += 1
            if calls == trigger:
                changed.write_text("末次补充证据读取后原成果变化", encoding="utf-8")
            return content

        monkeypatch.setattr(module, "_evidence_bytes", read_evidence)
    with pytest.raises(ValueError, match="repair-readiness-original-drift"):
        if window == "prepare-evidence":
            prepared(blocked)
        else:
            record(blocked, proposal, paths)
    assert not (current.loop_dir / "repair-readiness-supplement.json").exists()
    assert not (current.loop_dir / "review-outcome-round-2.json").exists()
    assert not list(current.loop_dir.glob(".readiness-*.tmp"))
    for name, content in originals.items():
        assert (current.loop_dir / name).read_bytes() == content
