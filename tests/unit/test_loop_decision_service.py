"""B1 的持久身份、只读准备和条件写入边界。"""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_sdlc.core.implementation_loop import start_implementation_loop
from ai_sdlc.core.implementation_models import (
    ImplementationInput,
    ImplementationStartOptions,
)
from ai_sdlc.core.implementation_store import implementation_input_digest
from ai_sdlc.core.loop_decision_models import DecisionPrepareInput
from ai_sdlc.core.loop_decision_service import (
    prepare_implementation_decision,
    validate_implementation_context,
)
from ai_sdlc.core.loop_models import LoopRun
from tests.integration.test_quantified_implementation import _ready_project, _request


@pytest.fixture
def decision_project(tmp_path):
    root = _ready_project(tmp_path)
    result = start_implementation_loop(
        ImplementationStartOptions(
            root=root,
            work_item="specs/demo-implementation-loop",
            loop_id="impl-unit",
            decision_mode="adaptive-quantified",
        )
    )
    assert result.status == "ready", result
    request_file = _request(root, root / "candidate.json")
    request = DecisionPrepareInput.model_validate_json(request_file.read_bytes())
    return root, request


def test_service_freezes_context_and_revalidates_selection(decision_project):
    root, request = decision_project
    before = prepare_implementation_decision(root, "impl-unit", request)
    assert before.status == "preview"
    path = root / ".ai-sdlc/loops/implementation/impl-unit/decision-context.json"
    assert not path.exists()
    result = prepare_implementation_decision(
        root, "impl-unit", request, dry_run=False, expected_digest=before.prepare_digest
    )
    assert result.status == "prepared"
    assert result.context.selection.selected_id == "A"
    existing_bytes = path.read_bytes()
    assert (
        prepare_implementation_decision(
            root,
            "impl-unit",
            request,
            dry_run=False,
            expected_digest=before.prepare_digest,
        ).status
        == "existing"
    )
    assert path.read_bytes() == existing_bytes


@pytest.mark.parametrize("mode", ["legacy", "adaptive-quantified"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_lifecycle_rejects_renamed_start_without_mutating_state(
    decision_project, mode, dry_run
):
    from tests.integration.test_quantified_implementation import _snapshot

    root, _ = decision_project
    state = root / ".ai-sdlc"
    before = _snapshot(state)
    result = start_implementation_loop(
        ImplementationStartOptions(
            root=root,
            work_item="specs/demo-implementation-loop",
            loop_id="renamed",
            decision_mode=mode,
            dry_run=dry_run,
        )
    )
    assert result.status == "blocked"
    assert "decision-lifecycle-conflict" in result.blocker
    assert _snapshot(state) == before


@pytest.mark.parametrize(
    "damage", ["missing-input", "missing-run", "bad-input", "bad-run", "digest"]
)
def test_lifecycle_does_not_ignore_incomplete_or_invalid_previous_b1(
    decision_project, damage
):
    root, _ = decision_project
    loop = root / ".ai-sdlc/loops/implementation/impl-unit"
    if damage.startswith("missing"):
        (
            loop
            / (
                "implementation-input.json"
                if damage == "missing-input"
                else "loop-run.json"
            )
        ).unlink()
    elif damage.startswith("bad"):
        (
            loop
            / (
                "implementation-input.json"
                if damage == "bad-input"
                else "loop-run.json"
            )
        ).write_text("{}")
    else:
        path = loop / "loop-run.json"
        payload = json.loads(path.read_bytes())
        payload["input_digest"] = "changed"
        path.write_text(json.dumps(payload))
    result = start_implementation_loop(
        ImplementationStartOptions(
            root=root,
            work_item="specs/demo-implementation-loop",
            loop_id="renamed",
            decision_mode="adaptive-quantified",
        )
    )
    assert result.status == "blocked"
    assert "decision-lifecycle-unavailable" in result.blocker
    assert not (loop.parent / "renamed").exists()


@pytest.mark.parametrize("same_contract", [True, False])
def test_lifecycle_compares_frozen_design_content_not_its_id(
    decision_project, same_contract
):
    from ai_sdlc.core.design_contract_models import DesignContractInput
    from ai_sdlc.core.design_contract_store import design_contract_input_digest
    from ai_sdlc.core.implementation_store import validate_implementation_lifecycle

    root, _ = decision_project
    original = ImplementationInput.model_validate_json(
        (
            root / ".ai-sdlc/loops/implementation/impl-unit/implementation-input.json"
        ).read_bytes()
    )
    old = root / ".ai-sdlc/loops/design-contract" / original.design_contract_loop_id
    contract = DesignContractInput.model_validate_json(
        (old / "design-contract-input.json").read_bytes()
    )
    run = LoopRun.model_validate_json((old / "loop-run.json").read_bytes())
    # 合成新 Design 持久身份，测试其冻结内容等同性；不冒充真实专家批准。
    new = old.parent / "renamed-design"
    new.mkdir()
    contract = contract.model_copy(update={"loop_id": new.name})
    if not same_contract:
        spec = root / original.spec_path
        spec.write_text(spec.read_text() + "\n新的授权范围\n")
        contract = contract.model_copy(
            update={
                "spec_digest": "sha256:" + hashlib.sha256(spec.read_bytes()).hexdigest()
            }
        )
    run = run.model_copy(
        update={
            "loop_id": new.name,
            "input_digest": design_contract_input_digest(contract),
        }
    )
    (new / "design-contract-input.json").write_text(contract.model_dump_json())
    (new / "loop-run.json").write_text(run.model_dump_json())
    current = original.model_copy(
        update={"loop_id": "new-implementation", "design_contract_loop_id": new.name}
    )
    if same_contract:
        with pytest.raises(ValueError, match="decision-lifecycle-conflict"):
            validate_implementation_lifecycle(root, current)
    else:
        # 当前文档已更新，旧 Design 保留原摘要；不能据此拒绝合法新合同。
        validate_implementation_lifecycle(root, current)


def test_lifecycle_ignores_identifiable_other_work_item_partial_state(decision_project):
    from ai_sdlc.core.implementation_store import validate_implementation_lifecycle

    root, _ = decision_project
    loop = root / ".ai-sdlc/loops/implementation/impl-unit"
    current = ImplementationInput.model_validate_json(
        (loop / "implementation-input.json").read_bytes()
    )
    peer = loop.parent / "other-work-item"
    peer.mkdir()
    other = current.model_copy(
        update={
            "loop_id": peer.name,
            "work_item_id": "other",
            "work_item_path": "specs/other",
        }
    )
    (peer / "implementation-input.json").write_text(other.model_dump_json())
    # 另一个任务尚未完成 start 写入时，不要求其 run 或旧 Design 已经存在。
    validate_implementation_lifecycle(root, current)


@pytest.mark.parametrize("field", ["selection", "evaluation", "authorized", "budget"])
def test_prepare_rejects_author_supplied_authority(decision_project, field):
    _, request = decision_project
    with pytest.raises(ValidationError):
        DecisionPrepareInput.model_validate({**request.model_dump(), field: True})


@pytest.mark.parametrize("change", ["source", "progress", "outcome", "input"])
def test_apply_rejects_any_changed_frozen_boundary(decision_project, change):
    root, request = decision_project
    before = prepare_implementation_decision(root, "impl-unit", request)
    loop = root / ".ai-sdlc/loops/implementation/impl-unit"
    path = {
        "source": root / "src/ai_sdlc/core/implementation_loop.py",
        "progress": loop / "implementation-progress.json",
        "outcome": loop / "review-outcome-round-1.json",
        "input": loop / "implementation-input.json",
    }[change]
    path.write_bytes((path.read_bytes() if path.exists() else b"{}") + b"\n")
    with pytest.raises(ValueError):
        prepare_implementation_decision(
            root,
            "impl-unit",
            request,
            dry_run=False,
            expected_digest=before.prepare_digest,
        )
    assert not (loop / "decision-context.json").exists()


def test_source_hash_mismatch_is_not_a_valid_reference(decision_project):
    root, request = decision_project
    payload = request.model_dump()
    payload["sources"][0]["sha256"] = "f" * 64
    changed = DecisionPrepareInput.model_validate(payload)
    with pytest.raises(ValueError, match="source"):
        prepare_implementation_decision(root, "impl-unit", changed)


@pytest.mark.parametrize("dry_run", [False, True])
def test_legacy_first_cannot_start_b1_for_the_same_contract(tmp_path, dry_run):
    from tests.integration.test_quantified_implementation import _snapshot

    root = _ready_project(tmp_path)
    options = ImplementationStartOptions(
        root=root, work_item="specs/demo-implementation-loop", loop_id="legacy-first"
    )
    assert start_implementation_loop(options).status == "ready"
    before = _snapshot(root / ".ai-sdlc")
    result = start_implementation_loop(
        ImplementationStartOptions(
            root=root,
            work_item=options.work_item,
            loop_id="b1-second",
            decision_mode="adaptive-quantified",
            dry_run=dry_run,
        )
    )
    assert result.status == "blocked"
    assert "decision-lifecycle-conflict" in result.blocker
    assert _snapshot(root / ".ai-sdlc") == before


@pytest.mark.parametrize("document", ["spec.md", "plan.md", "tasks.md"])
@pytest.mark.parametrize("entry", ["existing", "context"])
def test_prepared_context_rejects_frozen_design_drift(
    decision_project, document, entry
):
    root, request = decision_project
    preview = prepare_implementation_decision(root, "impl-unit", request)
    prepare_implementation_decision(
        root,
        "impl-unit",
        request,
        dry_run=False,
        expected_digest=preview.prepare_digest,
    )
    loop = root / ".ai-sdlc/loops/implementation/impl-unit"
    original = (loop / "decision-context.json").read_bytes()
    path = root / "specs/demo-implementation-loop" / document
    path.write_bytes(path.read_bytes() + b"\nFrozen document drift.\n")
    with pytest.raises(ValueError, match="upstream-changed"):
        if entry == "existing":
            prepare_implementation_decision(root, "impl-unit", request)
        else:
            validate_implementation_context(
                root,
                LoopRun.model_validate_json((loop / "loop-run.json").read_bytes()),
                ImplementationInput.model_validate_json(
                    (loop / "implementation-input.json").read_bytes()
                ),
            )
    assert (loop / "decision-context.json").read_bytes() == original


def test_b1_verification_rechecks_design_before_persisting(decision_project):
    import sys

    from ai_sdlc.core.implementation_loop import verify_implementation_task
    from ai_sdlc.core.implementation_models import ImplementationVerifyOptions
    from tests.integration.test_quantified_implementation import _snapshot

    root, request = decision_project
    preview = prepare_implementation_decision(root, "impl-unit", request)
    prepare_implementation_decision(
        root,
        "impl-unit",
        request,
        dry_run=False,
        expected_digest=preview.prepare_digest,
    )
    loop = root / ".ai-sdlc/loops/implementation/impl-unit"
    before = _snapshot(loop)
    result = verify_implementation_task(
        ImplementationVerifyOptions(
            root=root,
            loop_id="impl-unit",
            task_id="T11",
            cwd=".",
            argv=(
                sys.executable,
                "-c",
                "from pathlib import Path; "
                "p = Path('specs/demo-implementation-loop/plan.md'); "
                "p.write_bytes(p.read_bytes() + b'\\nDrift during verification.\\n')",
            ),
        )
    )
    assert result.status == "blocked"
    assert "upstream-changed" in result.blocker
    assert _snapshot(loop) == before


def test_same_contract_allows_multiple_pure_legacy_instances(tmp_path):
    root = _ready_project(tmp_path)
    for loop_id in ("legacy-one", "legacy-two"):
        result = start_implementation_loop(
            ImplementationStartOptions(
                root=root,
                work_item="specs/demo-implementation-loop",
                loop_id=loop_id,
            )
        )
        assert result.status == "ready"


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    "name",
    [
        "design-contract-close.json",
        "review-outcome-round-1.json",
        "review-outcome-round-3.json",
        "review-outcome-round-99.json",
        "review-continuation.json",
    ],
)
def test_prepare_rejects_other_loop_derived_sources_before_freezing(
    decision_project, dry_run, name
):
    from tests.integration.test_quantified_implementation import _snapshot

    root, request = decision_project
    path = root / ".ai-sdlc/loops/implementation/other" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    payload = request.model_dump()
    payload["sources"][0].update(
        path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    before = _snapshot(root / ".ai-sdlc")
    with pytest.raises(ValueError, match="decision-source-derived-state-forbidden"):
        prepare_implementation_decision(
            root,
            "impl-unit",
            DecisionPrepareInput.model_validate(payload),
            dry_run=dry_run,
            expected_digest="0" * 64,
        )
    assert _snapshot(root / ".ai-sdlc") == before


@pytest.mark.parametrize(
    "path",
    [
        "../secret",
        "/tmp/secret",
        "x/../secret",
        ".git/config",
        "C:/secret",
        "x\\secret",
    ],
)
def test_source_paths_are_lexically_safe(decision_project, path):
    _, request = decision_project
    payload = request.model_dump()
    payload["sources"][0]["path"] = path
    with pytest.raises(ValidationError):
        DecisionPrepareInput.model_validate(payload)


@pytest.mark.parametrize("failure", ["mkstemp", "replace"])
def test_atomic_write_failure_never_falls_back_to_direct_target(
    decision_project, monkeypatch, failure
):
    import ai_sdlc.core.loop_decision_service as service

    root, request = decision_project
    before = prepare_implementation_decision(root, "impl-unit", request)

    def denied(*args, **kwargs):
        raise PermissionError("injected write refusal")

    target = service.tempfile if failure == "mkstemp" else service.os
    monkeypatch.setattr(target, failure, denied)
    with pytest.raises(ValueError):
        prepare_implementation_decision(
            root,
            "impl-unit",
            request,
            dry_run=False,
            expected_digest=before.prepare_digest,
        )
    loop = root / ".ai-sdlc/loops/implementation/impl-unit"
    assert not (loop / "decision-context.json").exists()
    assert not list(loop.glob(".decision-context-*.tmp"))


@pytest.mark.parametrize(
    "tamper", ["selection", "loop_id", "context_digest", "schema", "duplicate_source"]
)
def test_corrupt_context_cannot_be_used_or_replaced(decision_project, tamper):
    root, request = decision_project
    before = prepare_implementation_decision(root, "impl-unit", request)
    prepare_implementation_decision(
        root, "impl-unit", request, dry_run=False, expected_digest=before.prepare_digest
    )
    path = root / ".ai-sdlc/loops/implementation/impl-unit/decision-context.json"
    payload = json.loads(path.read_bytes())
    if tamper == "selection":
        payload["selection"]["selected_id"] = "forged"
        payload["context_digest"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in payload.items()
                    if key != "context_digest"
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    elif tamper == "schema":
        payload["schema_version"] = "implementation-b2"
    elif tamper == "duplicate_source":
        payload["sources"].append(payload["sources"][0])
    else:
        payload[tamper] = "f" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    corrupted = path.read_bytes()
    with pytest.raises(ValueError):
        prepare_implementation_decision(
            root,
            "impl-unit",
            request,
            dry_run=False,
            expected_digest=before.prepare_digest,
        )
    assert path.read_bytes() == corrupted


def test_runtime_source_is_bound_even_when_excluded_from_source_manifest(
    decision_project,
):
    root, request = decision_project
    path = root / ".ai-sdlc/state/source.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("授权的原始来源", encoding="utf-8")
    payload = request.model_dump()
    payload["sources"][0].update(
        path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    request = DecisionPrepareInput.model_validate(payload)
    before = prepare_implementation_decision(root, "impl-unit", request)
    path.write_text("已变化", encoding="utf-8")
    with pytest.raises(ValueError):
        prepare_implementation_decision(
            root,
            "impl-unit",
            request,
            dry_run=False,
            expected_digest=before.prepare_digest,
        )


def test_legacy_identity_cannot_ignore_a_retained_b1_context(decision_project):
    root, request = decision_project
    before = prepare_implementation_decision(root, "impl-unit", request)
    prepare_implementation_decision(
        root, "impl-unit", request, dry_run=False, expected_digest=before.prepare_digest
    )
    loop = root / ".ai-sdlc/loops/implementation/impl-unit"
    input_payload = json.loads((loop / "implementation-input.json").read_bytes())
    run_payload = json.loads((loop / "loop-run.json").read_bytes())
    for payload in (input_payload, run_payload):
        payload.pop("decision_mode")
        payload.pop("decision_capability")
    impl_input = ImplementationInput.model_validate(input_payload)
    run_payload["input_digest"] = implementation_input_digest(impl_input)
    with pytest.raises(ValueError, match="conflicts-with-legacy"):
        validate_implementation_context(
            root, LoopRun.model_validate(run_payload), impl_input
        )


def test_constructed_invalid_identity_cannot_bypass_validation():
    value = ImplementationInput.model_validate(legacy_input())
    run = LoopRun(loop_id=value.loop_id, loop_type="implementation")
    with pytest.raises(ValueError):
        validate_implementation_context(
            Path.cwd(),
            run.model_copy(update={"decision_capability": "implementation-b1"}),
            value.model_copy(update={"decision_capability": "implementation-b1"}),
        )


def test_prepare_cannot_accept_new_goal_text_after_design_close(decision_project):
    root, request = decision_project
    path = root / request.sources[0].path
    path.write_bytes(path.read_bytes() + b"\nChanged frozen requirement.\n")
    payload = request.model_dump()
    payload["sources"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="upstream-changed"):
        prepare_implementation_decision(
            root, "impl-unit", DecisionPrepareInput.model_validate(payload)
        )


def test_maximum_contract_can_prepare_persist_and_reload(decision_project):
    import time

    from ai_sdlc.core.loop_decision import route_contract_digest
    from ai_sdlc.core.loop_decision_models import RouteSelectionContract

    root, original = decision_project
    payload = original.model_dump(mode="json")
    goal = payload["route_contract"]["goal_contract"]["goals"][0]
    obligation = payload["route_contract"]["goal_contract"]["obligations"][0]
    payload["route_contract"]["goal_contract"] = {
        "goals": [{**goal, "id": f"g{index}"} for index in range(128)],
        "obligations": [
            {
                **obligation,
                "id": f"o{index}",
                "goal_id": f"g{index // 16}",
                "weight_share": "0.0625",
            }
            for index in range(2048)
        ],
    }
    conditions = {
        "unit": "case",
        "population": "frozen",
        "procedure": "fixed-oracle",
        "environment": "fixture",
    }
    payload["route_contract"]["metrics"] = [
        {
            "id": f"m{index}",
            "goal_id": "g0",
            "conditions": conditions,
            "direction": "maximize",
            "source_refs": ["frozen-spec"],
        }
        for index in range(16)
    ]
    payload["route_contract"]["ordered_metric_ids"] = [
        f"m{index}" for index in range(16)
    ]
    candidate = payload["candidates"][0]
    candidate["contract_digest"] = route_contract_digest(
        RouteSelectionContract.model_validate(payload["route_contract"])
    )
    candidate["expected_obligation_changes"] = [
        {"id": f"o{index}", "expectation": "在冻结范围内实现此义务"}
        for index in range(2048)
    ]
    candidate["metric_estimates"] = [
        {
            "id": f"m{index}",
            "kind": "unknown",
            "conditions": conditions,
            "reason": "结构规模测试没有业务观测",
        }
        for index in range(16)
    ]
    payload["candidates"] = [
        {**candidate, "id": route, "mechanism": mechanism}
        for route, mechanism in (
            ("A", "使用直接条件逐项判定"),
            ("B", "使用数据表匹配规则"),
            ("C", "将规则编译为确定性状态机"),
        )
    ]
    request = DecisionPrepareInput.model_validate(payload)
    started = time.monotonic()
    preview = prepare_implementation_decision(root, "impl-unit", request)
    prepared = prepare_implementation_decision(
        root,
        "impl-unit",
        request,
        dry_run=False,
        expected_digest=preview.prepare_digest,
    )
    reloaded = prepare_implementation_decision(root, "impl-unit", request)
    assert prepared.context == reloaded.context
    assert len(reloaded.context.route_contract.goal_contract.obligations) == 2048
    assert len(reloaded.context.candidates) == 3
    assert reloaded.selection.selected_id == "A"
    print(
        json.dumps(
            {
                "goals": 128,
                "obligations": 2048,
                "metrics": 16,
                "candidates": 3,
                "request_bytes": len(request.model_dump_json().encode()),
                "context_bytes": len(reloaded.context.model_dump_json().encode()),
                "elapsed_seconds": time.monotonic() - started,
            },
            sort_keys=True,
        )
    )


def legacy_input():
    return {
        "schema_version": "1",
        "artifact_kind": "implementation-input",
        "created_by": "ai-sdlc",
        "created_at": "2026-09-06T00:00:00Z",
        "ai_sdlc_version": "3.0.1",
        "loop_id": "impl-001",
        "work_item_id": "demo",
        "work_item_path": "specs/demo",
        "spec_path": "specs/demo/spec.md",
        "plan_path": "specs/demo/plan.md",
        "tasks_path": "specs/demo/tasks.md",
        "design_contract_loop_id": "design-001",
        "design_contract_report_path": "",
        "work_type": "uncertain",
        "quality_profiles": [],
        "declared_scope": [],
        "tasks_digest": "",
        "acceptance_digest": "",
    }


def test_legacy_input_roundtrip_has_no_new_serialized_fields():
    payload = legacy_input()
    value = ImplementationInput.model_validate_json(json.dumps(payload))
    assert value.decision_mode == "legacy"
    assert value.decision_capability is None
    assert value.model_dump(mode="json") == payload
    explicit = ImplementationInput.model_validate(
        {**payload, "decision_mode": "legacy", "decision_capability": None}
    )
    assert explicit.model_dump(mode="json") == payload
    assert implementation_input_digest(explicit) == implementation_input_digest(value)


def test_legacy_loop_run_keeps_original_json():
    old = LoopRun(loop_id="impl-001", loop_type="implementation").model_dump()
    restored = LoopRun.model_validate(old)
    assert restored.decision_mode == "legacy"
    assert "decision_mode" not in restored.model_dump()
    assert "decision_capability" not in restored.model_dump()
    assert restored.model_dump() == old


def test_b1_identity_is_explicit_and_changes_input_digest():
    legacy = ImplementationInput.model_validate(legacy_input())
    quantified = ImplementationInput.model_validate(
        {
            **legacy_input(),
            "decision_mode": "adaptive-quantified",
            "decision_capability": "implementation-b1",
        }
    )
    assert quantified.model_dump()["decision_capability"] == "implementation-b1"
    assert implementation_input_digest(quantified) != implementation_input_digest(
        legacy
    )


@pytest.mark.parametrize(
    ("mode", "capability"),
    [
        ("legacy", "implementation-b1"),
        ("adaptive-quantified", None),
        ("adaptive-quantified", "implementation-b2"),
        ("unknown", None),
        (True, None),
        (None, None),
    ],
)
def test_invalid_identity_pairs_are_rejected(mode, capability):
    for model, payload in (
        (ImplementationInput, legacy_input()),
        (LoopRun, {"loop_id": "impl-001", "loop_type": "implementation"}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(
                {**payload, "decision_mode": mode, "decision_capability": capability}
            )


@pytest.mark.parametrize(
    "loop_type",
    ["requirement", "design-contract", "frontend-evidence", "local-pr-review"],
)
def test_other_four_loops_reject_b1(loop_type):
    with pytest.raises(ValidationError):
        LoopRun(
            loop_id="not-implementation",
            loop_type=loop_type,
            decision_mode="adaptive-quantified",
            decision_capability="implementation-b1",
        )


def _assessment_snapshot(decision_project, *, round_number=1, digest="a" * 64):
    from ai_sdlc.core.loop_decision_service import B1ReviewSnapshot
    from ai_sdlc.core.review_kernel import ReviewInput

    root, request = decision_project
    context = prepare_implementation_decision(root, "impl-unit", request).context
    actual = "src/ai_sdlc/core/implementation_loop.py"
    context_path = ".ai-sdlc/loops/implementation/impl-unit/decision-context.json"
    upstream = request.sources[0].path
    manifest = {
        actual: hashlib.sha256((root / actual).read_bytes()).hexdigest(),
        context_path: hashlib.sha256(context.model_dump_json().encode()).hexdigest(),
        upstream: request.sources[0].sha256,
    }
    review_input = ReviewInput(
        loop_id="impl-unit",
        loop_type="implementation",
        round_number=round_number,
        input_digest=digest,
        artifact_paths=[actual, context_path],
        upstream_context_paths=[upstream]
        if upstream not in [actual, context_path]
        else [],
        expert_roles=["business-review", "evidence-review"],
        expert_reasons={"business-review": "业务判据", "evidence-review": "证据检查"},
    )
    return B1ReviewSnapshot(review_input, context, manifest)


def _assessment(snapshot, *, status="PASS", readiness="PASS", prefix="proof"):
    from ai_sdlc.core.loop_decision_models import B1Assessment

    path = snapshot.review_input.artifact_paths[0]
    return B1Assessment.model_validate(
        {
            "input_digest": snapshot.review_input.input_digest,
            "context_digest": snapshot.context.context_digest,
            "selected_route_id": snapshot.context.selection.selected_id,
            "results": [
                {
                    "id": item.id,
                    "status": status,
                    "evidence_refs": [prefix] if status != "UNKNOWN" else [],
                    "reason": "依据当前实际文件逐项核对",
                }
                for item in snapshot.context.route_contract.goal_contract.obligations
            ],
            "evidence": [
                {
                    "id": prefix,
                    "path": path,
                    "sha256": snapshot.manifest[path],
                    "locator": "VALUE",
                    "claim": "当前实际输出符合判据",
                }
            ],
            "repair_readiness": {
                "authorization": readiness,
                "facts": readiness,
                "verification": readiness,
                "evidence_refs": [prefix],
                "reason": "冻结权限和事实足以进行唯一必要修复",
            },
        }
    )


def test_b1_assessments_are_conservatively_merged_and_recomputed(decision_project):
    from ai_sdlc.core.loop_decision_service import (
        build_b1_review_data,
        validate_b1_review_data,
    )

    snapshot = _assessment_snapshot(decision_project)
    assessments = {
        "business-review": _assessment(snapshot),
        "evidence-review": _assessment(snapshot, status="UNKNOWN"),
    }
    saved = build_b1_review_data(snapshot, assessments, has_actionable_findings=False)
    assert saved.evaluation.h == 1
    assert saved.evaluation.q == 0
    assert saved.evaluation.u == 100
    assert saved.decision.action == "repair"
    assert saved.evaluation.results[0].status == "UNKNOWN"
    assert len(saved.evaluation.results[0].evidence_refs) == 2
    assert saved.assessments == assessments
    assert (
        validate_b1_review_data(snapshot, saved, has_actionable_findings=False) == saved
    )


@pytest.mark.parametrize("field", ["authorization", "facts", "verification"])
@pytest.mark.parametrize("status", ["FAIL", "UNKNOWN"])
def test_b1_one_expert_unsafe_readiness_blocks_repair(decision_project, field, status):
    from ai_sdlc.core.loop_decision_models import B1Assessment
    from ai_sdlc.core.loop_decision_service import build_b1_review_data

    snapshot = _assessment_snapshot(decision_project)
    payload = _assessment(snapshot, status="FAIL").model_dump()
    payload["repair_readiness"][field] = status
    saved = build_b1_review_data(
        snapshot,
        {
            "business-review": _assessment(snapshot),
            "evidence-review": B1Assessment.model_validate(payload),
        },
        has_actionable_findings=False,
    )
    assert saved.evaluation.h == 1
    assert saved.evaluation.f == 100
    assert saved.decision.action == "blocked"
    assert saved.decision.reason == "repair-unavailable"


@pytest.mark.parametrize(
    "tamper", ["input", "context", "route", "path", "sha", "coverage", "reference"]
)
def test_b1_rejects_unbound_or_incomplete_assessments(decision_project, tamper):
    from ai_sdlc.core.loop_decision_models import B1Assessment
    from ai_sdlc.core.loop_decision_service import build_b1_review_data

    snapshot = _assessment_snapshot(decision_project)
    payload = _assessment(snapshot).model_dump()
    if tamper in {"input", "context", "route"}:
        payload[
            {
                "input": "input_digest",
                "context": "context_digest",
                "route": "selected_route_id",
            }[tamper]
        ] = "f" * 64
    elif tamper == "path":
        payload["evidence"][0]["path"] = "unreviewed.txt"
    elif tamper == "sha":
        payload["evidence"][0]["sha256"] = "f" * 64
    elif tamper == "coverage":
        payload["results"][0]["id"] = "invented"
    else:
        payload["results"][0]["evidence_refs"] = ["invented"]
    with pytest.raises(ValueError):
        build_b1_review_data(
            snapshot,
            {
                "business-review": _assessment(snapshot),
                "evidence-review": B1Assessment.model_validate(payload),
            },
            has_actionable_findings=False,
        )


@pytest.mark.parametrize("tamper", ["h", "decision", "manifest", "aggregate_ref"])
def test_b1_close_recomputes_instead_of_trusting_saved_authority(
    decision_project, tamper
):
    from ai_sdlc.core.loop_decision_models import B1ReviewData
    from ai_sdlc.core.loop_decision_service import (
        build_b1_review_data,
        validate_b1_review_data,
    )

    snapshot = _assessment_snapshot(decision_project)
    saved = build_b1_review_data(
        snapshot,
        {
            role: _assessment(snapshot, status="UNKNOWN")
            for role in snapshot.review_input.expert_roles
        },
        has_actionable_findings=False,
    )
    payload = saved.model_dump(mode="json")
    if tamper == "h":
        payload["evaluation"]["h"] = 0
    elif tamper == "decision":
        payload["decision"] = {"action": "stop", "reason": "requirements-satisfied"}
    elif tamper == "manifest":
        payload["manifest"][snapshot.review_input.artifact_paths[0]] = "f" * 64
    else:
        payload["evaluation"]["results"][0]["evidence_refs"] = [
            "expert:invented:obligation:required"
        ]
    with pytest.raises(ValueError):
        validate_b1_review_data(
            snapshot,
            B1ReviewData.model_validate(payload),
            has_actionable_findings=False,
        )


def test_b1_r2_rebuilds_historical_baseline_without_current_file_hashes(
    decision_project,
):
    from dataclasses import replace

    from ai_sdlc.core.loop_decision_service import (
        build_b1_review_data,
        validate_b1_review_data,
    )

    first = _assessment_snapshot(decision_project)
    baseline = build_b1_review_data(
        first,
        {
            role: _assessment(first, status="FAIL")
            for role in first.review_input.expert_roles
        },
        has_actionable_findings=False,
    )
    current = replace(
        first,
        review_input=first.review_input.model_copy(
            update={"round_number": 2, "input_digest": "b" * 64}
        ),
        manifest={**first.manifest, first.review_input.artifact_paths[0]: "c" * 64},
    )
    result = build_b1_review_data(
        current,
        {role: _assessment(current) for role in current.review_input.expert_roles},
        has_actionable_findings=False,
        baseline=baseline,
    )
    assert result.evaluation.delta_q == 100
    assert result.evaluation.baseline_artifact_digest == first.review_input.input_digest
    assert result.decision.action == "stop"
    assert (
        validate_b1_review_data(
            current, result, has_actionable_findings=False, baseline=baseline
        )
        == result
    )


@pytest.mark.parametrize("tamper", ["missing", "extra", "unsafe", "round"])
def test_b1_snapshot_requires_exact_material_and_frozen_sources(
    decision_project, tamper
):
    from dataclasses import replace

    from ai_sdlc.core.loop_decision_service import build_b1_review_data

    snapshot = _assessment_snapshot(decision_project)
    manifest = dict(snapshot.manifest)
    if tamper == "missing":
        manifest.pop(snapshot.review_input.upstream_context_paths[0])
    elif tamper == "extra":
        manifest["unreviewed.txt"] = "f" * 64
    elif tamper == "unsafe":
        manifest["../escape"] = "f" * 64
    else:
        snapshot = replace(
            snapshot,
            review_input=snapshot.review_input.model_copy(
                update={"round_number": True}
            ),
        )
    with pytest.raises(ValueError):
        build_b1_review_data(
            replace(snapshot, manifest=manifest),
            {
                role: _assessment(snapshot)
                for role in snapshot.review_input.expert_roles
            },
            has_actionable_findings=False,
        )


@pytest.mark.parametrize(
    "kind", ["self-proof", "constructed", "missing-role", "author-score"]
)
def test_b1_cannot_smuggle_authority_through_assessment(decision_project, kind):
    from ai_sdlc.core.loop_decision_models import B1Assessment
    from ai_sdlc.core.loop_decision_service import build_b1_review_data

    snapshot = _assessment_snapshot(decision_project)
    assessment = _assessment(snapshot)
    values = {role: assessment for role in snapshot.review_input.expert_roles}
    if kind == "self-proof":
        payload = assessment.model_dump()
        path = snapshot.review_input.artifact_paths[1]
        payload["evidence"][0].update(path=path, sha256=snapshot.manifest[path])
        values["business-review"] = B1Assessment.model_validate(payload)
    elif kind == "constructed":
        values["business-review"] = assessment.model_copy(update={"results": ()})
    elif kind == "missing-role":
        values.pop("business-review")
    else:
        with pytest.raises(ValueError):
            B1Assessment.model_validate({**assessment.model_dump(), "q": 100})
        return
    with pytest.raises(ValueError):
        build_b1_review_data(snapshot, values, has_actionable_findings=False)


def test_b1_unknown_without_observation_is_not_a_fabricated_pass(decision_project):
    from ai_sdlc.core.loop_decision_models import B1Assessment
    from ai_sdlc.core.loop_decision_service import build_b1_review_data

    snapshot = _assessment_snapshot(decision_project)
    payload = _assessment(snapshot, status="UNKNOWN", readiness="UNKNOWN").model_dump()
    payload["evidence"] = []
    payload["repair_readiness"]["evidence_refs"] = []
    value = B1Assessment.model_validate(payload)
    saved = build_b1_review_data(
        snapshot,
        {role: value for role in snapshot.review_input.expert_roles},
        has_actionable_findings=False,
    )
    assert saved.evaluation.h == 1 and saved.evaluation.q == 0
    assert saved.decision.action == "blocked"


def test_b1_planning_source_can_change_during_the_selected_implementation(
    decision_project,
):
    from dataclasses import replace

    from ai_sdlc.core.loop_decision_service import build_b1_review_data

    root, request = decision_project
    path = "src/ai_sdlc/core/implementation_loop.py"
    payload = request.model_dump()
    payload["sources"][0].update(
        path=path, sha256=hashlib.sha256((root / path).read_bytes()).hexdigest()
    )
    snapshot = _assessment_snapshot(
        (root, DecisionPrepareInput.model_validate(payload))
    )
    prior_digest = snapshot.context.sources[0].sha256
    # 准备时引用现有代码不禁止实施；新证据必须绑定改动后的实际字节。
    snapshot = replace(snapshot, manifest={**snapshot.manifest, path: "c" * 64})
    saved = build_b1_review_data(
        snapshot,
        {role: _assessment(snapshot) for role in snapshot.review_input.expert_roles},
        has_actionable_findings=False,
    )
    assert saved.decision.action == "stop"
    assert snapshot.context.sources[0].sha256 == prior_digest
    assert saved.manifest[path] != prior_digest


def test_b1_business_file_named_decision_context_is_not_framework_self_proof(
    decision_project,
):
    from dataclasses import replace

    from ai_sdlc.core.loop_decision_models import B1Assessment
    from ai_sdlc.core.loop_decision_service import build_b1_review_data

    snapshot = _assessment_snapshot(decision_project)
    path = "src/business/decision-context.json"
    snapshot = replace(
        snapshot,
        review_input=snapshot.review_input.model_copy(
            update={"artifact_paths": [*snapshot.review_input.artifact_paths, path]}
        ),
        manifest={**snapshot.manifest, path: "c" * 64},
    )
    payload = _assessment(snapshot).model_dump()
    payload["evidence"][0].update(path=path, sha256="c" * 64)
    saved = build_b1_review_data(
        snapshot,
        {
            role: B1Assessment.model_validate(payload)
            for role in snapshot.review_input.expert_roles
        },
        has_actionable_findings=False,
    )
    assert saved.decision.action == "stop"


def test_b1_r2_requires_original_recomputed_baseline(decision_project):
    from dataclasses import replace

    from ai_sdlc.core.loop_decision_service import build_b1_review_data

    first = _assessment_snapshot(decision_project)
    assessments = {
        role: _assessment(first, status="FAIL")
        for role in first.review_input.expert_roles
    }
    baseline = build_b1_review_data(first, assessments, has_actionable_findings=False)
    current = replace(
        first,
        review_input=first.review_input.model_copy(
            update={"round_number": 2, "input_digest": "b" * 64}
        ),
    )
    current_assessments = {
        role: _assessment(current) for role in current.review_input.expert_roles
    }
    for invalid in (
        None,
        baseline.model_copy(
            update={"evaluation": baseline.evaluation.model_copy(update={"q": 100})}
        ),
    ):
        with pytest.raises(ValueError):
            build_b1_review_data(
                current,
                current_assessments,
                has_actionable_findings=False,
                baseline=invalid,
            )
    still_failed = build_b1_review_data(
        current,
        {
            role: _assessment(current, status="FAIL")
            for role in current.review_input.expert_roles
        },
        has_actionable_findings=False,
        baseline=baseline,
    )
    assert still_failed.decision.action == "blocked"
    assert still_failed.decision.reason == "review-round-limit"


def test_b1_maximum_obligation_assessments_keep_both_full_reference_sets(
    decision_project,
):
    import time
    from dataclasses import replace

    from ai_sdlc.core.loop_decision import route_contract_digest, select_routes
    from ai_sdlc.core.loop_decision_models import (
        B1Assessment,
        B1ReviewData,
        DecisionContext,
        RouteCandidate,
        RouteSelectionContract,
    )
    from ai_sdlc.core.loop_decision_service import (
        build_b1_review_data,
        validate_b1_review_data,
    )

    snapshot = _assessment_snapshot(decision_project)
    payload = snapshot.context.model_dump(mode="json")
    contract = payload["route_contract"]["goal_contract"]
    goal, obligation = contract["goals"][0], contract["obligations"][0]
    contract["goals"] = [{**goal, "id": f"g{index}"} for index in range(128)]
    contract["obligations"] = [
        {
            **obligation,
            "id": f"o:{index}",
            "goal_id": f"g{index // 16}",
            "weight_share": "0.0625",
        }
        for index in range(2048)
    ]
    conditions = {
        "unit": "case",
        "population": "frozen",
        "procedure": "fixed-oracle",
        "environment": "fixture",
    }
    payload["route_contract"]["metrics"] = [
        {
            "id": f"m{index}",
            "goal_id": "g0",
            "conditions": conditions,
            "direction": "maximize",
            "source_refs": ["frozen-spec"],
        }
        for index in range(16)
    ]
    payload["route_contract"]["ordered_metric_ids"] = [
        f"m{index}" for index in range(16)
    ]
    route_contract = RouteSelectionContract.model_validate(payload["route_contract"])
    candidate = payload["candidates"][0]
    candidate["contract_digest"] = route_contract_digest(route_contract)
    candidate["metric_estimates"] = [
        {
            "id": f"m{index}",
            "kind": "unknown",
            "conditions": conditions,
            "reason": "规模验收不伪造实际业务观测",
        }
        for index in range(16)
    ]
    payload["candidates"] = [
        {**candidate, "id": route, "mechanism": mechanism}
        for route, mechanism in (("A", "直接条件"), ("B", "数据表"), ("C", "状态机"))
    ]
    candidates = tuple(
        RouteCandidate.model_validate(row) for row in payload["candidates"]
    )
    payload["selection"] = select_routes(route_contract, candidates).model_dump(
        mode="json"
    )
    payload = DecisionContext.model_validate(payload).model_dump(mode="json")
    payload.pop("context_digest")
    payload["context_digest"] = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    context = DecisionContext.model_validate(payload)
    manifest = dict(snapshot.manifest)
    manifest[snapshot.review_input.artifact_paths[1]] = hashlib.sha256(
        context.model_dump_json().encode()
    ).hexdigest()
    snapshot = replace(snapshot, context=context, manifest=manifest)
    assessments = {}
    for role in snapshot.review_input.expert_roles:
        payload = _assessment(snapshot).model_dump()
        original = payload["evidence"][0]
        payload["evidence"] = [
            {**original, "id": f"{role}-{index}"} for index in range(64)
        ]
        refs = tuple(item["id"] for item in payload["evidence"])
        payload["results"] = [
            {**row, "evidence_refs": refs, "reason": "R" * 4096}
            for row in payload["results"]
        ]
        payload["repair_readiness"]["evidence_refs"] = refs
        assessments[role] = B1Assessment.model_validate(payload)
    started = time.monotonic()
    saved = build_b1_review_data(snapshot, assessments, has_actionable_findings=False)
    encoded = saved.model_dump_json()
    restored = B1ReviewData.model_validate_json(encoded)
    assert (
        validate_b1_review_data(snapshot, restored, has_actionable_findings=False)
        == saved
    )

    assert saved.evaluation.h == 0 and saved.evaluation.q == 100
    assert saved.decision.action == "stop"
    for row in saved.evaluation.results:
        assert len(row.evidence_refs) == 2
        for role in snapshot.review_input.expert_roles:
            assert f"expert:{role}:obligation:{row.id}" in row.evidence_refs
            original = next(
                item for item in saved.assessments[role].results if item.id == row.id
            )
            assert len(original.evidence_refs) == 64 and original.reason == "R" * 4096
    assert set(
        saved.assessments["business-review"].results[0].evidence_refs
    ).isdisjoint(saved.assessments["evidence-review"].results[0].evidence_refs)
    print(
        json.dumps(
            {
                "experts": 2,
                "goals": 128,
                "candidates": 3,
                "metrics": 16,
                "obligations_per_expert": 2048,
                "refs_per_obligation_per_expert": 64,
                "assessments_bytes": sum(
                    len(item.model_dump_json().encode())
                    for item in assessments.values()
                ),
                "outcome_data_bytes": len(encoded.encode()),
                "elapsed_seconds": time.monotonic() - started,
                "exit_code": 0,
            },
            sort_keys=True,
        )
    )


@pytest.mark.parametrize("left", ["PASS", "UNKNOWN", "FAIL"])
@pytest.mark.parametrize("right", ["PASS", "UNKNOWN", "FAIL"])
def test_b1_merge_uses_fail_then_unknown_then_pass(decision_project, left, right):
    from ai_sdlc.core.loop_decision_service import build_b1_review_data

    snapshot = _assessment_snapshot(decision_project)
    result = build_b1_review_data(
        snapshot,
        {
            "business-review": _assessment(snapshot, status=left),
            "evidence-review": _assessment(snapshot, status=right),
        },
        has_actionable_findings=False,
    )
    expected = (
        "FAIL"
        if "FAIL" in (left, right)
        else "UNKNOWN"
        if "UNKNOWN" in (left, right)
        else "PASS"
    )
    assert result.evaluation.results[0].status == expected
    assert (result.evaluation.h == 0) == (left == right == "PASS")
