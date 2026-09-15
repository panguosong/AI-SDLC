"""多阶段真实CLI协议；合成独立判断只证明接线，不证明模型商业质量。"""

import hashlib
import json

import pytest

from ai_sdlc.cli.loop_stage_cmd import resolve_stage_decision_host
from ai_sdlc.core.loop_simulation_context import SimulationPrepareRequest
from ai_sdlc.core.loop_simulation_models import STAGE_PROFILES
from ai_sdlc.core.loop_stage_decision_service import prepare_stage_simulation_decision
from tests.integration.test_quantified_implementation import (
    _cli,
    _complete_task,
    _payload,
    _ready_project,
)
from tests.unit.test_loop_simulation import assessment_data, candidate_data
from tests.unit.test_loop_simulation_models import contract_data

CAPABILITY = "stage-simulation-v1"
LOOP = "stage-pipeline"
WORK_ITEM = "specs/demo-implementation-loop"


def stage_start(root, stage):
    _ready_project(root)
    if stage == "requirement":
        args = [
            "start",
            "--idea",
            "拒绝越权请求并保留实际审查",
            "--acceptance",
            "拒绝越权请求",
            "--work-item-id",
            "stage-requirement",
        ]
    elif stage == "design-contract":
        args = ["check", "--wi", WORK_ITEM]
    else:
        args = ["start", "--wi", WORK_ITEM]
    return _payload(
        _cli(
            root,
            "loop",
            stage,
            *args,
            "--loop-id",
            LOOP,
            "--decision-mode",
            "adaptive-quantified",
            "--decision-capability",
            CAPABILITY,
            "--json",
        )
    )


def stage_apply(root, stage, data):
    request = SimulationPrepareRequest.model_validate(data)
    kwargs = {"host_resolver": lambda: resolve_stage_decision_host(root, stage, LOOP)}
    preview = prepare_stage_simulation_decision(root, stage, LOOP, request, **kwargs)
    return prepare_stage_simulation_decision(
        root,
        stage,
        LOOP,
        request,
        dry_run=False,
        expected_digest=preview.prepare_digest,
        **kwargs,
    ).context


def stage_selected(root, stage, *, start=True, repair_fact=False):
    if start:
        stage_start(root, stage)
    source = root / WORK_ITEM / "spec.md"
    fact = root / "repair-fact.md"
    if repair_fact:
        fact.write_text("R1 前的实际材料", encoding="utf-8")
    contracts = [
        {
            **contract_data(),
            "capability": CAPABILITY,
            "loop_type": stage,
            "profile_id": profile,
        }
        for profile in STAGE_PROFILES[stage]
    ]
    context = stage_apply(
        root,
        stage,
        {
            "operation": "begin",
            "request_id": "begin",
            "contracts": contracts,
            "sources": [
                {
                    "id": "spec",
                    "path": source.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "locator": "all",
                    "claim": "冻结目标与约束",
                },
                *(
                    [
                        {
                            "id": "repair-fact",
                            "path": fact.name,
                            "sha256": hashlib.sha256(fact.read_bytes()).hexdigest(),
                            "locator": "all",
                            "claim": "待核对的原始材料",
                        }
                    ]
                    if repair_fact
                    else []
                ),
            ],
        },
    )
    candidates = [
        candidate_data(context.plan, "baseline"),
        candidate_data(context.plan, "better"),
    ]
    frozen = stage_apply(
        root,
        stage,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze",
            "candidates": candidates,
        },
    )
    selected = stage_apply(
        root,
        stage,
        {
            "operation": "record-comparison",
            "request_id": "judge",
            "judgement": {
                "judge_input_digest": frozen.pending_batch.judge_input_digest,
                "assessments": [
                    assessment_data("baseline", 2, 2),
                    assessment_data("better", 3, 3),
                ],
            },
        },
    )
    assert selected.initial_selection_id == "better"
    if stage == "implementation":
        _complete_task(root, loop_id=LOOP)
    return selected


def actual_result_args(root, stage, reviewed, *, status="PASS"):
    destination = root / ".ai-sdlc/state/stage-test-experts"
    destination.mkdir(parents=True, exist_ok=True)
    paths = []
    source = next(path for path in reviewed["artifact_paths"] if path.endswith(".md"))
    for index, role in enumerate(reviewed["expert_roles"]):
        data = {
            "execution": {
                "status": "completed",
                "roles": [role],
                "role_reasons": {role: reviewed["expert_reasons"][role]},
                "findings": [],
            },
            "assessment": {
                "input_digest": reviewed["input_digest"],
                "context_digest": reviewed["context_digest"],
                "selected_route_id": reviewed["selected_route_id"],
                "results": [
                    {
                        "id": "o0",
                        "status": status,
                        "evidence_refs": [] if status == "UNKNOWN" else ["actual"],
                        "reason": "核对实际阶段材料",
                    }
                ],
                "evidence": [
                    {
                        "id": "actual",
                        "path": source,
                        "sha256": reviewed["evidence_manifest"][source],
                        "locator": "all",
                        "claim": "当前阶段实际工件",
                    }
                ],
                "repair_readiness": {
                    "authorization": "PASS",
                    "facts": "PASS",
                    "verification": "PASS",
                    "evidence_refs": ["actual"],
                    "reason": "原范围内必要修复",
                },
            },
        }
        path = destination / f"{stage}-{index}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        paths += ["--result", str(path)]
    return paths


def actual_record(root, stage, reviewed, *, status="PASS", timeout=30):
    paths = actual_result_args(root, stage, reviewed, status=status)
    return _payload(
        _cli(
            root,
            "loop",
            "review-record",
            "--type",
            stage,
            "--loop-id",
            LOOP,
            "--expect-digest",
            reviewed["input_digest"],
            *paths,
            "--json",
            timeout=timeout,
        )
    )


@pytest.mark.parametrize("stage", ["requirement", "design-contract", "implementation"])
@pytest.mark.parametrize("status", ["PASS", "UNKNOWN"])
def test_stage_forecast_and_actual_review_feed_original_close(
    initialized_project_dir, stage, status
):
    root = initialized_project_dir
    selected = stage_selected(root, stage)
    assert len(selected.comparisons) == 1
    unsealed = _cli(
        root, "loop", "review", "--type", stage, "--loop-id", LOOP, "--json"
    )
    assert unsealed.returncode == 1 and "sealed" in unsealed.stdout
    stage_apply(root, stage, {"operation": "seal-for-review", "request_id": "seal"})
    reviewed = _payload(
        _cli(root, "loop", "review", "--type", stage, "--loop-id", LOOP, "--json")
    )
    assert reviewed["decision_capability"] == CAPABILITY
    assert reviewed["decision_context"]["contracts"][0]["loop_type"] == stage
    assert reviewed["snapshot_read_command"][4] == stage
    recorded = actual_record(root, stage, reviewed, status=status)
    directory = root / ".ai-sdlc/loops" / stage / LOOP
    saved = json.loads((directory / "review-outcome-round-1.json").read_bytes())
    assert saved["simulation"]["evaluation"]["h"] == (0 if status == "PASS" else 1)
    assert "b1" not in saved
    close = _cli(
        root,
        "loop",
        stage,
        "freeze" if stage == "requirement" else "close",
        "--loop-id",
        LOOP,
        "--expect-review-digest",
        reviewed["input_digest"],
        "--yes",
        "--json",
    )
    if status == "PASS":
        assert recorded["status"] == "passed"
        assert close.returncode == 0, close.stdout + close.stderr
        state = _payload(_cli(root, "loop", stage, "status", "--json"))
        assert state["current_loop"]["status"] == "closed"
    else:
        assert recorded["status"] == "needs_fix"
        assert close.returncode == 1
        assert not (directory / f"{stage}-close.json").exists()


def test_new_schema_keeps_d1_operations_separate(initialized_project_dir):
    root = initialized_project_dir
    for capability in (CAPABILITY, "implementation-simulation-v1"):
        schema = _payload(
            _cli(
                root,
                "loop",
                "decision-prepare",
                "--schema",
                "--capability",
                capability,
                "--json",
            )
        )
        assert ("begin-improvement" in schema["properties"]["operation"]["enum"]) == (
            capability == CAPABILITY
        )
    assert len(schema["x-guidance"]["stage_profiles"]["profiles"]) == 6


@pytest.mark.parametrize("stage", ["requirement", "design-contract"])
def test_stage_necessary_repair_preserves_contract_and_stops_at_r2(
    initialized_project_dir, monkeypatch, stage
):
    root = initialized_project_dir
    stage_selected(root, stage, repair_fact=True)
    stage_apply(root, stage, {"operation": "seal-for-review", "request_id": "seal"})
    reviewed = _payload(
        _cli(root, "loop", "review", "--type", stage, "--loop-id", LOOP, "--json")
    )
    assert (
        actual_record(root, stage, reviewed, status="UNKNOWN")["status"] == "needs_fix"
    )
    directory = root / ".ai-sdlc/loops" / stage / LOOP
    original = {
        name: (directory / name).read_bytes()
        for name in ("decision-context.json", "review-outcome-round-1.json")
    }
    (root / "repair-fact.md").write_text(
        "R1 后原范围内修正的实际材料", encoding="utf-8"
    )
    if stage == "requirement":
        args = [
            "start",
            "--idea",
            "拒绝越权请求并保留实际审查",
            "--work-item-id",
            "stage-requirement",
            "--acceptance",
            "拒绝越权请求并给出明确错误，附实际材料",
        ]
    else:
        plan = root / WORK_ITEM / "plan.md"
        plan.write_text(
            plan.read_text() + "\n原范围内补充失败处理的实际说明。\n", encoding="utf-8"
        )
        args = ["check", "--wi", WORK_ITEM]
    result = _payload(
        _cli(
            root,
            "loop",
            stage,
            *args,
            "--loop-id",
            LOOP,
            "--decision-mode",
            "adaptive-quantified",
            "--decision-capability",
            CAPABILITY,
            "--json",
        )
    )
    assert result["status"] == "ready"
    _q004_assert_guidance(root, stage, result, monkeypatch, expected="loop review")
    for name, content in original.items():
        assert (directory / name).read_bytes() == content
    second = _payload(
        _cli(root, "loop", "review", "--type", stage, "--loop-id", LOOP, "--json")
    )
    assert second["round_number"] == 2
    assert actual_record(root, stage, second)["status"] == "passed"
    closed = _cli(
        root,
        "loop",
        stage,
        "freeze" if stage == "requirement" else "close",
        "--loop-id",
        LOOP,
        "--expect-review-digest",
        second["input_digest"],
        "--yes",
        "--json",
    )
    assert closed.returncode == 0, closed.stdout + closed.stderr
    (root / "after-closed-r2.py").write_text("VALUE = 2\n", encoding="utf-8")
    consumed = _payload(
        _cli(root, "loop", "review", "--type", stage, "--loop-id", LOOP, "--json")
    )
    assert consumed["round_number"] == 2
    assert consumed["review_status"] == "passed"
    assert (directory / "review-outcome-round-1.json").read_bytes() == original[
        "review-outcome-round-1.json"
    ]
    forbidden = _cli(
        root,
        "loop",
        stage,
        *args,
        "--loop-id",
        LOOP,
        "--decision-mode",
        "adaptive-quantified",
        "--decision-capability",
        CAPABILITY,
        "--json",
    )
    assert (
        forbidden.returncode == 1 or json.loads(forbidden.stdout).get("frozen") is True
    )
    assert not (directory / "review-outcome-round-3.json").exists()


@pytest.mark.parametrize("downstream_changes", [False, True])
def test_normal_run_preserves_quantified_predecessor_chain(
    initialized_project_dir, downstream_changes
):
    from tests.integration.test_quantified_implementation import _git
    from tests.unit.test_implementation_loop import _write_ready_work_item

    root = initialized_project_dir
    _write_ready_work_item(root)
    source = root / "src/ai_sdlc/core/implementation_loop.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Stage Integration")
    _git(root, "config", "user.email", "stage@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "original goal")
    for stage in ("requirement", "design-contract", "implementation"):
        if stage == "requirement":
            args = [
                "start",
                "--idea",
                "系统必须记录实现任务证据",
                "--acceptance",
                "完成任务后可关闭",
                "--design-scope-family",
                "implementation",
                "--work-item-id",
                "demo-implementation-loop",
            ]
        elif stage == "design-contract":
            args = ["check", "--wi", WORK_ITEM, "--requirement-loop-id", LOOP]
        else:
            args = ["start", "--wi", WORK_ITEM, "--design-contract-loop-id", LOOP]
        _payload(
            _cli(
                root,
                "loop",
                stage,
                *args,
                "--loop-id",
                LOOP,
                "--decision-mode",
                "adaptive-quantified",
                "--decision-capability",
                CAPABILITY,
                "--json",
            )
        )
        routed = _payload(_cli(root, "run", "--json"))
        assert routed["current_loop"]["loop_type"] == stage, routed
        assert "begin" in routed["next_action"], routed
        stage_selected(root, stage, start=False)
        routed = _payload(_cli(root, "run", "--json"))
        assert "seal-for-review" in routed["next_action"], routed
        stage_apply(root, stage, {"operation": "seal-for-review", "request_id": "seal"})
        reviewed = _payload(
            _cli(root, "loop", "review", "--type", stage, "--loop-id", LOOP, "--json")
        )
        if stage != "requirement":
            assert any(
                path.endswith("decision-context.json")
                for path in reviewed["upstream_context_paths"]
            )
            assert any(
                "review-outcome-round-1.json" in path
                for path in reviewed["upstream_context_paths"]
            )
        assert actual_record(root, stage, reviewed)["status"] == "passed"
        _payload(
            _cli(
                root,
                "loop",
                stage,
                "freeze" if stage == "requirement" else "close",
                "--loop-id",
                LOOP,
                "--expect-review-digest",
                reviewed["input_digest"],
                "--yes",
                "--json",
            )
        )
        status = _payload(_cli(root, "loop", stage, "status", "--json"))
        assert status["current_loop"]["status"] == "closed"
        if downstream_changes and stage == "requirement":
            plan = root / WORK_ITEM / "plan.md"
            plan.write_text(
                plan.read_text(encoding="utf-8") + "\n后续设计补充实现顺序。\n",
                encoding="utf-8",
            )
        elif downstream_changes and stage == "design-contract":
            source.write_text("VALUE = 2\n", encoding="utf-8")


@pytest.mark.parametrize("stage", ["requirement", "design-contract"])
@pytest.mark.parametrize("missing", ["digest", "validator"])
def test_stage_core_close_requires_actual_review(
    initialized_project_dir, stage, missing
):
    from ai_sdlc.cli.loop_review_cmd import validate_review_input_for_close
    from ai_sdlc.core.design_contract_loop import (
        DesignContractCloseOptions,
        close_design_contract_loop,
    )
    from ai_sdlc.core.requirement_loop import (
        RequirementFreezeOptions,
        freeze_requirement_loop,
    )

    root = initialized_project_dir
    stage_start(root, stage)
    directory = root / ".ai-sdlc/loops" / stage / LOOP
    before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    option_type, operation = (
        (RequirementFreezeOptions, freeze_requirement_loop)
        if stage == "requirement"
        else (DesignContractCloseOptions, close_design_contract_loop)
    )
    result = operation(
        option_type(
            root=root,
            loop_id=LOOP,
            yes=True,
            expected_review_digest="" if missing == "digest" else "a" * 64,
        ),
        review_input_validator=(
            validate_review_input_for_close if missing == "digest" else None
        ),
    )
    assert result.status == "blocked"
    assert "quantified-stage-close-review-required" in result.blocker
    assert {
        p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()
    } == before


@pytest.fixture
def sealed_implementation_snapshot(initialized_project_dir):
    root = initialized_project_dir
    selected = stage_selected(root, "implementation")
    sealed = stage_apply(
        root,
        "implementation",
        {"operation": "seal-for-review", "request_id": "seal"},
    )
    return root, selected, sealed


@pytest.mark.parametrize("stage", ["requirement", "design-contract", "implementation"])
def test_stage_snapshot_preserves_fresh_hosts_and_complete_material(
    initialized_project_dir, monkeypatch, stage
):
    import ai_sdlc.cli.loop_review_cmd as review_command
    import ai_sdlc.cli.loop_stage_cmd as stage_command
    import ai_sdlc.core.implementation_loop as implementation

    root = initialized_project_dir
    stage_selected(root, stage)
    context = stage_apply(
        root, stage, {"operation": "seal-for-review", "request_id": "seal"}
    )
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    resolve_host = stage_command.resolve_stage_decision_host
    build_report = implementation._build_report
    hosts, reports = [], []

    def fresh_host(*args, **kwargs):
        result = resolve_host(*args, **kwargs)
        hosts.append(result)
        return result

    def current_report(*args, **kwargs):
        result = build_report(*args, **kwargs)
        reports.append(result)
        return result

    monkeypatch.setattr(stage_command, "resolve_stage_decision_host", fresh_host)
    monkeypatch.setattr(implementation, "_build_report", current_report)
    expected = review_command.resolve_review_input(
        root, loop_type=stage, loop_id=LOOP, review_round_number=1
    )
    assert len(hosts) == 1
    if stage == "implementation":
        assert len(reports) == 1
    hosts.clear()
    reports.clear()
    snapshot = stage_command.stage_review_snapshot(root, stage, LOOP, 1)
    # 性能约束不以删掉尾部重读换取；两个宿主均来自实际报告与原件。
    assert len(hosts) == 2 and hosts[0] is not hosts[1]
    assert hosts[0] == hosts[1]
    if stage == "implementation":
        assert len(reports) == 2
        assert all(report.status == "needs_review" for report in reports)
    assert snapshot.review_input == expected
    assert snapshot.context == context
    material = {
        *snapshot.review_input.artifact_paths,
        *snapshot.review_input.upstream_context_paths,
    }
    assert set(snapshot.manifest) == material
    assert {source.path for source in context.sources} <= material
    assert snapshot.context_path in material
    assert snapshot.manifest == {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in material
    }
    assert before == {
        path: path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("damage", ["task-state", "source"])
def test_stage_snapshot_rejects_drift_after_material_capture(
    sealed_implementation_snapshot, monkeypatch, damage
):
    import ai_sdlc.cli.loop_review_cmd as review_command
    import ai_sdlc.cli.loop_stage_cmd as stage_command

    root, _, _ = sealed_implementation_snapshot
    build_input = review_command.build_review_input
    resolve_host = stage_command.resolve_stage_decision_host
    hosts = []

    def fresh_host(*args, **kwargs):
        host = resolve_host(*args, **kwargs)
        hosts.append(host)
        return host

    def capture_then_change(*args, **kwargs):
        reviewed = build_input(*args, **kwargs)
        if damage == "task-state":
            path = (
                root
                / ".ai-sdlc/loops/implementation"
                / LOOP
                / "implementation-progress.json"
            )
            progress = json.loads(path.read_bytes())
            next(task for task in progress["tasks"] if task["task_id"] == "T11")[
                "status"
            ] = "blocked"
            path.write_text(json.dumps(progress), encoding="utf-8")
        else:
            (root / "snapshot-extra.py").write_text("VALUE = 2\n", encoding="utf-8")
        return reviewed

    monkeypatch.setattr(stage_command, "resolve_stage_decision_host", fresh_host)
    monkeypatch.setattr(review_command, "build_review_input", capture_then_change)
    with pytest.raises(ValueError, match="review-input-drift"):
        stage_command.stage_review_snapshot(root, "implementation", LOOP, 1)
    assert len(hosts) == 2
    if damage == "task-state":
        assert hosts[0].actual_ready is True and hosts[1].actual_ready is False


def test_stage_snapshot_rejects_changed_captured_context(
    sealed_implementation_snapshot, monkeypatch
):
    import ai_sdlc.cli.loop_review_cmd as review_command
    import ai_sdlc.cli.loop_stage_cmd as stage_command

    root, selected, _ = sealed_implementation_snapshot
    build_input = review_command.build_review_input
    context_path = (
        root / ".ai-sdlc/loops/implementation" / LOOP / "decision-context.json"
    )

    def change_then_capture(*args, **kwargs):
        # 两份都是原生生成的有效合同；不能仅凭模型解析成功接受混合快照。
        context_path.write_text(selected.model_dump_json(), encoding="utf-8")
        return build_input(*args, **kwargs)

    monkeypatch.setattr(review_command, "build_review_input", change_then_capture)
    with pytest.raises(ValueError, match="decision-context-drift"):
        stage_command.stage_review_snapshot(root, "implementation", LOOP, 1)


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("mode", "decision-identity-mismatch"),
        ("input", "decision-identity-mismatch"),
        ("start", "simulation-start-marker-mismatch"),
        ("source", "decision-source-digest-mismatch"),
        ("phase", "simulation-review-not-sealed"),
    ],
)
def test_stage_snapshot_rechecks_context_admission_after_first_host(
    sealed_implementation_snapshot, monkeypatch, damage, reason
):
    import ai_sdlc.cli.loop_review_cmd as review_command
    import ai_sdlc.cli.loop_stage_cmd as stage_command

    root, selected, _ = sealed_implementation_snapshot
    directory = root / ".ai-sdlc/loops/implementation" / LOOP
    source_material = review_command._stage_source_material

    def read_material_then_change(*args, **kwargs):
        material = source_material(*args, **kwargs)
        if damage == "phase":
            (directory / "decision-context.json").write_text(
                selected.model_dump_json(), encoding="utf-8"
            )
        elif damage == "source":
            (root / WORK_ITEM / "spec.md").write_text(
                "原始来源在首读之后改变", encoding="utf-8"
            )
        else:
            path = directory / (
                "implementation-input.json" if damage == "input" else "loop-run.json"
            )
            payload = json.loads(path.read_bytes())
            if damage == "mode":
                payload["decision_mode"] = "legacy"
                for name in ("decision_capability", "decision_started_at_ms"):
                    payload.pop(name, None)
            elif damage == "input":
                payload["declared_scope"].append("src/changed.py")
            else:
                payload["decision_started_at_ms"] += 1
            path.write_text(json.dumps(payload), encoding="utf-8")
        return material

    monkeypatch.setattr(
        review_command, "_stage_source_material", read_material_then_change
    )
    with pytest.raises(ValueError, match=reason):
        stage_command.stage_review_snapshot(root, "implementation", LOOP, 1)


@pytest.mark.parametrize("change", ["none", "source", "task", "evidence"])
def test_quantified_close_rechecks_after_in_memory_close_preparation(
    sealed_implementation_snapshot, monkeypatch, change
):
    from typer.testing import CliRunner

    import ai_sdlc.core.implementation_loop as implementation
    from ai_sdlc.cli.main import app

    root, _, _ = sealed_implementation_snapshot
    reviewed = _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            "implementation",
            "--loop-id",
            LOOP,
            "--json",
        )
    )
    assert actual_record(root, "implementation", reviewed)["status"] == "passed"
    directory = root / ".ai-sdlc/loops/implementation" / LOOP
    protected = {
        path: path.read_bytes()
        for path in directory.iterdir()
        if path.name
        in {
            "loop-run.json",
            "decision-context.json",
            "review-outcome-round-1.json",
            "implementation-report.json",
            "implementation-report.md",
        }
    }
    close_path = directory / "implementation-close.json"
    assert not close_path.exists()
    record_outputs = implementation._record_close_outputs
    observed = []

    def change_after_preparation(current_root, execution_round, artifacts):
        record_outputs(current_root, execution_round, artifacts)
        # 已构造关闭模型及内存 round，末次真实守卫仍须拒绝后发生的字节变化。
        observed.append(change)
        if change == "source":
            (root / "src/ai_sdlc/core/implementation_loop.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
        elif change == "task":
            path = directory / "implementation-progress.json"
            progress = json.loads(path.read_bytes())
            next(item for item in progress["tasks"] if item["task_id"] == "T11")[
                "status"
            ] = "blocked"
            path.write_text(json.dumps(progress), encoding="utf-8")
        elif change == "evidence":
            path = directory / "verification-evidence.json"
            path.write_bytes(path.read_bytes() + b"\n")

    # Close 在本进程调用真实 CLI，确保注入发生于本次写入路径而非子进程之外。
    monkeypatch.chdir(root)
    for module in ("loop_cmd", "loop_review_cmd"):
        monkeypatch.setattr(f"ai_sdlc.cli.{module}.find_project_root", lambda: root)
    monkeypatch.setattr(
        implementation, "_record_close_outputs", change_after_preparation
    )
    result = CliRunner().invoke(
        app,
        [
            "loop",
            "implementation",
            "close",
            "--loop-id",
            LOOP,
            "--expect-review-digest",
            reviewed["input_digest"],
            "--yes",
            "--json",
        ],
    )
    assert observed == [change], result.output
    payload = json.loads(result.output)
    if change == "none":
        assert result.exit_code == 0, result.output
        assert payload["closed"] is True and payload["loop_status"] == "closed"
        assert close_path.exists()
        assert (
            json.loads((directory / "loop-run.json").read_bytes())["status"] == "closed"
        )
        protected.pop(directory / "loop-run.json")
    else:
        assert result.exit_code == 1, result.output
        assert payload["reason"] == "review-input-drift", result.output
        assert not close_path.exists()
    assert {path: path.read_bytes() for path in protected} == protected


def test_quantified_close_retains_incomplete_task_report(
    sealed_implementation_snapshot,
):
    from ai_sdlc.cli.loop_review_cmd import (
        resolve_review_input,
        validate_review_input_for_close,
    )
    from ai_sdlc.core.implementation_loop import close_implementation_loop
    from ai_sdlc.core.implementation_models import ImplementationCloseOptions

    root, _, _ = sealed_implementation_snapshot
    reviewed = resolve_review_input(
        root, loop_type="implementation", loop_id=LOOP, review_round_number=1
    )
    directory = root / ".ai-sdlc/loops/implementation" / LOOP
    context_before = (directory / "decision-context.json").read_bytes()
    path = directory / "implementation-progress.json"
    progress = json.loads(path.read_bytes())
    next(item for item in progress["tasks"] if item["task_id"] == "T11")["status"] = (
        "blocked"
    )
    path.write_text(json.dumps(progress), encoding="utf-8")

    result = close_implementation_loop(
        ImplementationCloseOptions(
            root=root,
            loop_id=LOOP,
            yes=True,
            expected_review_digest=reviewed.input_digest,
        ),
        review_input_validator=validate_review_input_for_close,
    )
    assert result.status == "needs_fix" and result.closed is False
    assert result.blocker == "T11 is not done."
    assert not (directory / "implementation-close.json").exists()
    assert (directory / "decision-context.json").read_bytes() == context_before
    report = json.loads((directory / "implementation-report.json").read_bytes())
    assert report["status"] == "needs_fix" and report["blocker_count"] > 0
    assert "T11 is not done." in report["blockers"]
    assert (
        json.loads((directory / "loop-run.json").read_bytes())["status"] == "needs_fix"
    )


@pytest.mark.parametrize("change", ["none", "source", "task", "evidence", "r1"])
def test_quantified_record_rechecks_after_temporary_result_fsync(
    sealed_implementation_snapshot, monkeypatch, change
):
    from typer.testing import CliRunner

    import ai_sdlc.core.loop_review_service as service
    from ai_sdlc.cli.main import app

    root, _, _ = sealed_implementation_snapshot
    folder = root / ".ai-sdlc/loops/implementation" / LOOP
    ids = ["--type", "implementation", "--loop-id", LOOP]
    first = _payload(_cli(root, "loop", "review", *ids, "--json"))
    assert (
        actual_record(root, "implementation", first, status="UNKNOWN")["status"]
        == "needs_fix"
    )
    r1 = folder / "review-outcome-round-1.json"
    source = root / "src/ai_sdlc/core/implementation_loop.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    _complete_task(root, loop_id=LOOP)
    reviewed = _payload(_cli(root, "loop", "review", *ids, "--json"))
    assert (
        reviewed["round_number"] == 2 and reviewed["review_status"] == "review_missing"
    )
    results = actual_result_args(root, "implementation", reviewed)
    outcome = folder / "review-outcome-round-2.json"
    protected = {
        folder / name: (folder / name).read_bytes()
        for name in {
            "loop-run.json",
            "decision-context.json",
            "review-outcome-round-1.json",
            "implementation-report.json",
            "implementation-report.md",
        }
    }
    fsync, observed = service.os.fsync, []

    def after_fsync(fd):
        fsync(fd)
        path = next(folder.glob(".review-outcome-round-2.json.*.tmp"), None)
        if observed or path is None:
            return
        if not service.os.path.samestat(service.os.fstat(fd), path.stat()):
            return
        assert json.loads(path.read_bytes())["round_number"] == 2
        assert not outcome.exists()
        observed.append(change)
        if change == "source":
            source.write_text("VALUE = 3\n", encoding="utf-8")
        elif change == "task":
            path = folder / "implementation-progress.json"
            progress = json.loads(path.read_bytes())
            next(row for row in progress["tasks"] if row["task_id"] == "T11")[
                "status"
            ] = "blocked"
            path.write_text(json.dumps(progress), encoding="utf-8")
        elif change == "evidence":
            path = folder / "verification-evidence.json"
            path.write_bytes(path.read_bytes() + b"\n")
        elif change == "r1":
            # 改合法元数据，排除坏 JSON 假拒绝；原 baseline 模型相等检查必须识别变化。
            baseline = json.loads(r1.read_bytes())
            baseline["recorded_at"] = "2000-01-01T00:00:00Z"
            r1.write_text(json.dumps(baseline), encoding="utf-8")

    monkeypatch.chdir(root)
    monkeypatch.setattr("ai_sdlc.cli.loop_review_cmd.find_project_root", lambda: root)
    monkeypatch.setattr(service.os, "fsync", after_fsync)
    result = CliRunner().invoke(
        app,
        [
            "loop",
            "review-record",
            *ids,
            "--expect-digest",
            f" {reviewed['input_digest'].upper()} ",
            *results,
            "--json",
        ],
    )
    assert observed == [change], result.output
    assert not list(folder.glob(".review-outcome-round-2.json.*.tmp"))
    payload = json.loads(result.output)
    if change == "none":
        assert result.exit_code == 0 and payload["status"] == "passed", result.output
        saved = json.loads(outcome.read_bytes())
        assert saved["round_number"] == payload["round_number"] == 2
        assert (
            saved["input_digest"] == payload["input_digest"] == reviewed["input_digest"]
        )
        assert root / payload["outcome_path"] == outcome
        assert saved["simulation"]["decision"]["action"] == "stop"
    else:
        assert result.exit_code == 1, result.output
        assert payload["reason"] == "review-input-drift", result.output
        assert not outcome.exists()
    if change == "r1":
        # 排除显式注入，其他原件仍逐字节保护。
        protected.pop(r1)
    assert {path: path.read_bytes() for path in protected} == protected
    assert not (folder / "review-outcome-round-3.json").exists()


@pytest.mark.parametrize(
    "round_number,change",
    [(1, "none"), (1, "add-r2"), (2, "none"), (2, "r1"), (2, "r2"), (2, "remove-r2")],
)
def test_close_capture_preserves_current_review_history(
    sealed_implementation_snapshot, monkeypatch, round_number, change
):
    import ai_sdlc.cli.loop_review_cmd as command

    root, _, _ = sealed_implementation_snapshot
    folder = root / ".ai-sdlc/loops/implementation" / LOOP
    args = ["loop", "review", "--type", "implementation", "--loop-id", LOOP, "--json"]
    reviewed = _payload(_cli(root, *args))
    if round_number == 2:
        assert (
            actual_record(root, "implementation", reviewed, status="UNKNOWN")["status"]
            == "needs_fix"
        )
        (root / "src/ai_sdlc/core/implementation_loop.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        _complete_task(root, loop_id=LOOP)
        reviewed = _payload(_cli(root, *args))
    assert actual_record(root, "implementation", reviewed)["status"] == "passed"
    assert reviewed["round_number"] == round_number
    before = {p: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    captured, observed = {}, []
    resolve = command.resolve_review_input
    r1, r2 = (folder / f"review-outcome-round-{n}.json" for n in (1, 2))

    def capture_then_change(*args, **kwargs):
        result = resolve(*args, **kwargs)
        if kwargs.get("captured_artifacts") is not captured:
            return result
        assert not observed and captured
        observed.append(change)
        if change == "remove-r2":
            r2.unlink()
        elif change != "none":
            path = r1 if change == "r1" else r2
            data = json.loads((r1 if change == "add-r2" else path).read_bytes())
            # 合法元数据变化仍应被识别；不能只依靠坏 JSON 被拒绝。
            data["recorded_at"] = "2000-01-01T00:00:00Z"
            if change == "add-r2":
                data["round_number"] = 2
            path.write_text(json.dumps(data), encoding="utf-8")
        return result

    monkeypatch.setattr(command, "resolve_review_input", capture_then_change)
    options = dict(
        loop_type="implementation",
        loop_id=LOOP,
        expected_digest=reviewed["input_digest"],
        captured_artifacts=captured,
    )
    if change == "none":
        result = command.validate_review_input_for_close(root, **options)
        assert (
            result.input_digest == reviewed["input_digest"]
            and result.round_number == round_number
        )
    else:
        with pytest.raises(command.ReviewInputGuardError) as error:
            command.validate_review_input_for_close(root, **options)
        assert error.value.reason == {
            "remove-r2": "review-result-missing",
            "add-r2": "review-outcome-sequence-invalid",
        }.get(change, "review-input-drift")
    assert observed == [change]
    changed = r1 if change == "r1" else r2 if change != "none" else None
    assert {p: p.read_bytes() for p in before if p != changed} == {
        p: raw for p, raw in before.items() if p != changed
    }
    assert not (folder / "implementation-close.json").exists()



def _q004_refresh_args(stage, *, loop_id=LOOP):
    if stage == "requirement":
        args = ["start", "--idea", "拒绝越权请求并保留实际审查",
                "--acceptance", "拒绝越权请求", "--work-item-id", "stage-requirement"]
    else:
        args = ["check" if stage == "design-contract" else "start", "--wi", WORK_ITEM]
    return ["loop", stage, *args, "--loop-id", loop_id,
            "--decision-mode", "adaptive-quantified", "--decision-capability", CAPABILITY]


def _q004_loop_bytes(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def _q004_assert_guidance(root, stage, payload, monkeypatch, *, expected):
    from io import StringIO

    from rich.console import Console

    from ai_sdlc.cli import loop_cmd as command
    from ai_sdlc.core.design_contract_models import DesignContractCommandResult
    from ai_sdlc.core.frontend_evidence_models import FrontendEvidenceCommandResult
    from ai_sdlc.core.requirement_loop import RequirementLoopCommandResult

    models = {"requirement": RequirementLoopCommandResult,
              "design-contract": DesignContractCommandResult,
              "frontend-evidence": FrontendEvidenceCommandResult}
    current = _payload(_cli(root, "loop", stage, "status", "--json"))
    assert payload["loop_id"] == current["current_loop"]["loop_id"]
    assert payload["next_action"] == current["next_action"]
    assert expected in payload["next_action"] and "operation=begin" not in payload["next_action"]
    if stage != "requirement":
        assert payload["next_guidance"] == current["next_guidance"]
    output = StringIO()
    with monkeypatch.context() as patch:
        patch.setattr(command, "console", Console(file=output, width=20000, color_system=None))
        emit = getattr(command, "_emit_" + stage.replace("-", "_") + "_result")
        emit(models[stage].model_validate(payload), json_output=False)
    assert "Next: " + payload["next_action"] in output.getvalue()


def _q004_reject_corrupt_guidance_after_saved_result(root, stage, payload):
    from ai_sdlc.cli.loop_cmd import _stage_start_guidance
    from ai_sdlc.core.design_contract_models import DesignContractCommandResult
    from ai_sdlc.core.frontend_evidence_models import FrontendEvidenceCommandResult
    from ai_sdlc.core.requirement_loop import RequirementLoopCommandResult

    models = {"requirement": RequirementLoopCommandResult,
              "design-contract": DesignContractCommandResult,
              "frontend-evidence": FrontendEvidenceCommandResult}
    directory = root / ".ai-sdlc/loops" / stage / payload["loop_id"]
    path = directory / "decision-context.json"
    original = path.read_bytes()
    # 真实写入回执已产生；只在指引消费前破坏合同，证明错误不冒充回滚或首次准备。
    path.write_bytes(b"{")
    before = _q004_loop_bytes(directory)
    result = models[stage].model_validate(payload)
    _stage_start_guidance(root, stage, result, CAPABILITY)
    assert result.status == "blocked" and result.blocker
    assert "begin" not in result.next_action and result.artifacts
    if hasattr(result, "next_guidance"):
        assert result.next_guidance.safety == "blocked"
        assert not result.next_guidance.requires_model
    assert _q004_loop_bytes(directory) == before
    current = _cli(root, "loop", stage, "status", "--json")
    assert current.returncode == 1
    assert json.loads(current.stdout)["status"] == "blocked"
    assert _q004_loop_bytes(directory) == before
    path.write_bytes(original)


@pytest.mark.parametrize("stage", ["requirement", "design-contract"])
def test_q004_stage_refresh_preserves_selected_guidance(
    initialized_project_dir, monkeypatch, stage
):
    from ai_sdlc.cli import loop_cmd as command
    from ai_sdlc.core.design_contract_models import DesignContractCommandResult
    from ai_sdlc.core.requirement_loop import RequirementLoopCommandResult

    root = _ready_project(initialized_project_dir)
    args = _q004_refresh_args(stage)
    directory = root / ".ai-sdlc/loops" / stage / LOOP
    preview = _payload(_cli(root, *args, "--dry-run", "--json"))
    assert preview["dry_run"] and "operation=begin" in preview["next_action"]
    assert not directory.exists()
    first = _payload(_cli(root, *args, "--json"))
    assert "operation=begin" in first["next_action"]
    stage_selected(root, stage, start=False)
    context = (directory / "decision-context.json").read_bytes()
    old_run = json.loads((directory / "loop-run.json").read_bytes())
    refreshed = _payload(_cli(root, *args, "--json"))
    _q004_assert_guidance(root, stage, refreshed, monkeypatch, expected="seal-for-review")
    before = _q004_loop_bytes(directory)
    preview = _payload(_cli(root, *args, "--dry-run", "--json"))
    assert preview["dry_run"]
    _q004_assert_guidance(root, stage, preview, monkeypatch, expected="seal-for-review")
    assert _q004_loop_bytes(directory) == before
    run = json.loads((directory / "loop-run.json").read_bytes())
    assert (directory / "decision-context.json").read_bytes() == context
    for key in ("loop_id", "current_round", "created_at", "decision_started_at_ms"):
        assert run[key] == old_run[key]
    assert not (directory / "review-outcome-round-1.json").exists()
    # 写入回执显式绑定返回的 Loop，不另取 current 状态替代该实例。
    model = RequirementLoopCommandResult if stage == "requirement" else DesignContractCommandResult
    seen = []
    original_status = command._stage_decision_status
    def explicit_status(root, kind, loop_id):
        seen.append((kind, loop_id))
        return original_status(root, kind, loop_id)
    with monkeypatch.context() as patch:
        patch.setattr(command, "_stage_decision_status", explicit_status)
        patch.setattr(command, "get_loop_status", lambda *a, **k: pytest.fail("must use returned Loop"))
        result = model.model_validate(refreshed)
        command._stage_start_guidance(root, stage, result, CAPABILITY)
    assert seen == [(stage, refreshed["loop_id"])]
    assert result.next_action == refreshed["next_action"]
    _q004_reject_corrupt_guidance_after_saved_result(root, stage, refreshed)
