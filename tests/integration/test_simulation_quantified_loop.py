"""新建 D1 的真实入口；临时项目不改写原开发 Loop。"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from ai_sdlc.core.implementation_loop import start_implementation_loop
from ai_sdlc.core.implementation_models import ImplementationStartOptions
from tests.integration.test_quantified_implementation import _cli, _ready_project

CAPABILITY = "implementation-simulation-v1"
LOOP_ID = "simulation-integration"
WORK_ITEM = "specs/demo-implementation-loop"


def start_simulation(root):
    return start_implementation_loop(
        ImplementationStartOptions(
            root=root,
            work_item=WORK_ITEM,
            loop_id=LOOP_ID,
            decision_mode="adaptive-quantified",
            decision_capability=CAPABILITY,
        )
    )


def test_d1_identity_is_explicit_and_persisted(initialized_project_dir):
    root = _ready_project(initialized_project_dir)
    result = start_simulation(root)
    assert result.status == "ready", result
    loop = root / ".ai-sdlc/loops/implementation" / LOOP_ID
    for name in ("loop-run.json", "implementation-input.json"):
        assert (
            json.loads((loop / name).read_bytes())["decision_capability"] == CAPABILITY
        )
    assert not (loop / "decision-context.json").exists()


def test_cli_announces_supported_d1_without_switching_old_default(
    initialized_project_dir,
):
    root = _ready_project(initialized_project_dir)
    result = _cli(
        root,
        "loop",
        "implementation",
        "start",
        "--wi",
        WORK_ITEM,
        "--loop-id",
        LOOP_ID,
        "--decision-mode",
        "adaptive-quantified",
        "--decision-capability",
        CAPABILITY,
        "--json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert CAPABILITY in payload["next_action"]
    assert "begin" in payload["next_action"]


def test_discovery_exposes_supported_d1_and_bundled_profiles(initialized_project_dir):
    from tests.integration.test_quantified_implementation import _payload

    root = _ready_project(initialized_project_dir)
    schema = _payload(_cli(root, "loop", "decision-prepare", "--schema", "--json"))
    assert CAPABILITY in schema["x-guidance"]["supported_capabilities"]
    d1 = _payload(
        _cli(
            root,
            "loop",
            "decision-prepare",
            "--schema",
            "--capability",
            CAPABILITY,
            "--json",
        )
    )
    assert (
        d1["x-guidance"]["stage_profiles"]["profiles"]["implementation-plan-v1"][
            "availability"
        ]
        == "supported"
    )
    route = _payload(_cli(root, "run", "--json"))
    assert CAPABILITY in route["supported_decision_capabilities"]["implementation"]


@pytest.mark.parametrize("capability", ["stage-simulation-v2", "unknown"])
def test_future_capability_is_rejected_without_start(
    initialized_project_dir, capability
):
    root = _ready_project(initialized_project_dir)
    result = start_implementation_loop(
        ImplementationStartOptions(
            root=root,
            work_item=WORK_ITEM,
            loop_id=LOOP_ID,
            decision_mode="adaptive-quantified",
            decision_capability=capability,
        )
    )
    assert result.status == "blocked"
    assert not (root / ".ai-sdlc/loops/implementation" / LOOP_ID).exists()


def begin_request(root):
    import hashlib

    from tests.unit.test_loop_simulation_models import contract_data

    plan = contract_data()
    code = {**plan, "profile_id": "code-result-v1"}
    source = root / WORK_ITEM / "spec.md"
    return {
        "operation": "begin",
        "request_id": "begin-1",
        "contracts": [plan, code],
        "sources": [
            {
                "id": "spec",
                "path": source.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "locator": "spec",
                "claim": "冻结目标与已有授权边界",
            }
        ],
    }


def prepare_request(root, payload, *, dry_run=False):
    from ai_sdlc.core.loop_decision_service import prepare_simulation_decision
    from ai_sdlc.core.loop_simulation_context import SimulationPrepareRequest

    request = SimulationPrepareRequest.model_validate(payload)
    preview = prepare_simulation_decision(root, LOOP_ID, request)
    if dry_run:
        return preview
    return prepare_simulation_decision(
        root, LOOP_ID, request, dry_run=False, expected_digest=preview.prepare_digest
    )


def test_begin_is_guarded_idempotent_and_reserves_before_generation(
    initialized_project_dir,
):
    root = _ready_project(initialized_project_dir)
    assert start_simulation(root).status == "ready"
    payload = begin_request(root)
    preview = prepare_request(root, payload, dry_run=True)
    path = root / ".ai-sdlc/loops/implementation" / LOOP_ID / "decision-context.json"
    assert preview.status == "preview"
    assert not path.exists()
    saved = prepare_request(root, payload)
    assert saved.context.pending_batch.number == 1
    before = path.read_bytes()
    assert prepare_request(root, payload).status == "existing"
    assert path.read_bytes() == before
    assert saved.context.initial_selection_id is None


def test_drafting_failure_has_a_terminal_path_without_fake_winner(
    initialized_project_dir,
):
    root = _ready_project(initialized_project_dir)
    assert start_simulation(root).status == "ready"
    prepare_request(root, begin_request(root))
    for retry in (True, False):
        result = prepare_request(
            root,
            {
                "operation": "record-comparison",
                "request_id": f"failure-{retry}",
                "failure": {
                    "stage": "drafting",
                    "reason": "模型调用未产出草案",
                    "retry": retry,
                    "prior_call_terminated": True,
                },
            },
        )
    assert result.context.pending_batch is None
    assert result.context.comparisons[0].outcome == "technical_failure"
    assert result.context.initial_selection_id is None
    with pytest.raises(ValueError, match="initial-selection"):
        prepare_request(root, {"operation": "seal-for-review", "request_id": "seal-1"})


def choose_route(
    root,
    *,
    scores=(3, 3),
    seconds=(20, 30),
    continue_search=False,
    continuation_cost=(30, 40),
):
    from tests.unit.test_loop_simulation import assessment_data, candidate_data

    assert start_simulation(root).status == "ready"
    begun = prepare_request(root, begin_request(root))
    frozen = prepare_request(
        root,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze-1",
            "candidates": [candidate_data(begun.context.plan, seconds=seconds)],
        },
    )
    request = {
        "operation": "record-comparison",
        "request_id": "judge-1",
        "judgement": {
            "judge_input_digest": frozen.context.pending_batch.judge_input_digest,
            "assessments": [assessment_data(lower=scores[0], upper=scores[1])],
        },
    }
    if continue_search:
        request.update(
            continue_search=True, reason="仍有原目标的覆盖缺口，可比较第二种结构"
        )
        if continuation_cost is not None:
            from tests.unit.test_loop_simulation_models import continuation_data

            proposal = continuation_data()
            proposal["future_cost_estimate"].update(
                lower_seconds=continuation_cost[0], upper_seconds=continuation_cost[1]
            )
            request["judgement"]["initial_search_continuation"] = proposal
    return prepare_request(root, request)


def cli_prepare(root, payload, *, apply=True):
    from tests.integration.test_quantified_implementation import _payload

    path = root / ".ai-sdlc/state/simulation-inputs" / f"{payload['request_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    args = (
        "loop",
        "decision-prepare",
        "--type",
        "implementation",
        "--loop-id",
        LOOP_ID,
        "--input",
        str(path),
        "--json",
    )
    preview = _payload(_cli(root, *args, "--dry-run"))
    if not apply:
        return args, preview
    return _payload(_cli(root, *args, "--expect-digest", preview["prepare_digest"]))


def test_cli_freezes_separate_judge_input_and_guides_actual_seal(
    initialized_project_dir,
):
    from ai_sdlc.core.loop_simulation_models import StageScoreContract
    from tests.integration.test_quantified_implementation import (
        _payload,
    )
    from tests.unit.test_loop_simulation import assessment_data, candidate_data

    root = _ready_project(initialized_project_dir)
    assert start_simulation(root).status == "ready"
    schema = _payload(
        _cli(
            root,
            "loop",
            "decision-prepare",
            "--schema",
            "--capability",
            CAPABILITY,
            "--json",
        )
    )
    assert CAPABILITY in schema["x-guidance"]["supported_capabilities"]
    begun = cli_prepare(root, begin_request(root))
    contract = StageScoreContract.model_validate(begun["context"]["contracts"][0])
    frozen = cli_prepare(
        root,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze-1",
            "candidates": [candidate_data(contract)],
        },
    )
    judge = frozen["judge_input"]
    assert "assessment" not in judge["candidates"][0]
    assert "independent read-only" in judge["instructions"]
    result = cli_prepare(
        root,
        {
            "operation": "record-comparison",
            "request_id": "judge-1",
            "judgement": {
                "judge_input_digest": judge["judge_input_digest"],
                "assessments": [assessment_data()],
            },
        },
    )
    assert result["context"]["initial_selection_id"] == "A"
    assert "selected route A" in result["next_action"]
    _payload(
        _cli(
            root,
            "loop",
            "implementation",
            "record",
            "--loop-id",
            LOOP_ID,
            "--task-id",
            "T11",
            "--status",
            "done",
            "--verification",
            "读取当前源码",
            "--json",
        )
    )
    verified = _payload(
        _cli(
            root,
            "loop",
            "implementation",
            "verify",
            "--loop-id",
            LOOP_ID,
            "--task-id",
            "T11",
            "--json",
            "--",
            sys.executable,
            "-c",
            "from pathlib import Path; assert Path('src/ai_sdlc/core/implementation_loop.py').read_text().startswith('VALUE = ')",
        )
    )
    assert "seal-for-review" in verified["next_action"]
    status = _payload(_cli(root, "loop", "implementation", "status", "--json"))
    assert "seal-for-review" in status["next_action"]
    sealed = cli_prepare(root, {"operation": "seal-for-review", "request_id": "seal-1"})
    assert sealed["context"]["phase"] == "review_sealed"
    assert "actual" in sealed["next_action"]


def test_seal_cannot_trap_unexecuted_selected_route(initialized_project_dir):
    root = _ready_project(initialized_project_dir)
    choose_route(root)
    with pytest.raises(ValueError, match="simulation-actual-tasks-not-ready"):
        prepare_request(root, {"operation": "seal-for-review", "request_id": "seal-1"})


def test_expired_choice_blocks_new_execution_without_reset(
    initialized_project_dir, monkeypatch
):
    from ai_sdlc.cli.loop_cmd import _simulation_guidance
    from ai_sdlc.core import loop_decision_service as decisions
    from ai_sdlc.core.implementation_loop import (
        record_implementation_progress,
        verify_implementation_task,
    )
    from ai_sdlc.core.implementation_models import (
        ImplementationRecordOptions,
        ImplementationVerifyOptions,
    )

    root = _ready_project(initialized_project_dir)
    context = choose_route(root).context
    path = root / ".ai-sdlc/loops/implementation" / LOOP_ID / "decision-context.json"
    before = path.read_bytes()
    monkeypatch.setattr(
        decisions.time,
        "time_ns",
        lambda: (context.started_at_ms + 3_600_000) * 1_000_000,
    )
    assert "Do not start new implementation" in _simulation_guidance(context).reason
    recorded = record_implementation_progress(
        ImplementationRecordOptions(root, "T11", "in_progress", loop_id=LOOP_ID)
    )
    assert recorded.status == "blocked"
    verified = verify_implementation_task(
        ImplementationVerifyOptions(
            root,
            "T11",
            ".",
            (sys.executable, "-c", "raise AssertionError('must not execute')"),
            loop_id=LOOP_ID,
        )
    )
    assert verified.status == "blocked"
    assert "model-plan-not-feasible" in verified.blocker
    assert path.read_bytes() == before


def test_continuing_started_route_does_not_recharge_full_original_estimate(
    initialized_project_dir, monkeypatch
):
    from ai_sdlc.cli.loop_cmd import get_review_aware_loop_status
    from ai_sdlc.core import loop_decision_service as decisions
    from ai_sdlc.core.implementation_loop import record_implementation_progress
    from ai_sdlc.core.implementation_models import ImplementationRecordOptions

    root = _ready_project(initialized_project_dir)
    context = choose_route(root, seconds=(2000, 3000)).context
    options = ImplementationRecordOptions(root, "T11", "in_progress", loop_id=LOOP_ID)
    assert record_implementation_progress(options).status != "blocked"
    monkeypatch.setattr(
        decisions.time,
        "time_ns",
        lambda: (context.started_at_ms + 1_000_000) * 1_000_000,
    )
    assert record_implementation_progress(options).status != "blocked"
    assert (
        "Do not start new implementation"
        not in get_review_aware_loop_status(root, "implementation").next_action
    )


def test_expired_unstarted_work_cannot_bypass_admission_by_recording_done(
    initialized_project_dir, monkeypatch
):
    from ai_sdlc.core import loop_decision_service as decisions
    from ai_sdlc.core.implementation_loop import record_implementation_progress
    from ai_sdlc.core.implementation_models import ImplementationRecordOptions

    root = _ready_project(initialized_project_dir)
    context = choose_route(root).context
    monkeypatch.setattr(
        decisions.time,
        "time_ns",
        lambda: (context.started_at_ms + 3_600_000) * 1_000_000,
    )
    result = record_implementation_progress(
        ImplementationRecordOptions(
            root,
            "T11",
            "done",
            verification=("未开始，不能以文字跳过成本准入",),
            loop_id=LOOP_ID,
        )
    )
    assert result.status == "blocked"


def test_no_winner_cannot_record_actual_progress(initialized_project_dir):
    from ai_sdlc.core.implementation_loop import record_implementation_progress
    from ai_sdlc.core.implementation_models import ImplementationRecordOptions

    root = _ready_project(initialized_project_dir)
    context = choose_route(root, seconds=(4000, 5000)).context
    assert context.initial_selection_id is None
    result = record_implementation_progress(
        ImplementationRecordOptions(
            root, "T11", "blocked", note="仍无任何可执行路线", loop_id=LOOP_ID
        )
    )
    assert result.status == "blocked"
    assert "initial-selection" in result.blocker


def test_expired_r1_repair_next_does_not_invite_new_execution(
    initialized_project_dir, monkeypatch
):
    from ai_sdlc.cli.loop_cmd import get_review_aware_loop_status
    from ai_sdlc.core import loop_decision_service as decisions
    from tests.integration.test_quantified_implementation import (
        _payload,
        _review_record_args,
    )
    from tests.integration.test_simulation_review import (
        _simulation_envelopes,
        _simulation_ready,
    )

    root = initialized_project_dir
    loop, reviewed = _simulation_ready(root)
    result = _payload(
        _cli(
            root,
            *_review_record_args(
                reviewed, _simulation_envelopes(root, reviewed, status="UNKNOWN")
            ),
        )
    )
    assert result["status"] == "needs_fix"
    context = json.loads((loop / "decision-context.json").read_bytes())
    monkeypatch.setattr(
        decisions.time,
        "time_ns",
        lambda: (context["started_at_ms"] + 3_600_000) * 1_000_000,
    )
    status = get_review_aware_loop_status(root, "implementation")
    assert "Do not start new implementation" in status.next_action
    assert not (loop / "review-outcome-round-2.json").exists()


@pytest.mark.parametrize("exit_code", [0, 1])
def test_inflight_actual_verification_survives_window_end(
    initialized_project_dir, monkeypatch, exit_code
):
    from ai_sdlc.core import implementation_loop as implementation
    from ai_sdlc.core import loop_decision_service as decisions
    from ai_sdlc.core.implementation_models import (
        ImplementationRecordOptions,
        ImplementationVerifyOptions,
    )

    root = _ready_project(initialized_project_dir)
    context = choose_route(root).context
    path = root / ".ai-sdlc/loops/implementation" / LOOP_ID / "decision-context.json"
    before = path.read_bytes()
    assert (
        implementation.record_implementation_progress(
            ImplementationRecordOptions(root, "T11", "in_progress", loop_id=LOOP_ID)
        ).status
        != "blocked"
    )
    original = implementation.run_quality_command

    def crosses_deadline(options):
        result = original(options)
        monkeypatch.setattr(
            decisions.time,
            "time_ns",
            lambda: (context.started_at_ms + 3_600_000) * 1_000_000,
        )
        return result

    monkeypatch.setattr(implementation, "run_quality_command", crosses_deadline)
    verified = implementation.verify_implementation_task(
        ImplementationVerifyOptions(
            root,
            "T11",
            ".",
            (sys.executable, "-c", f"raise SystemExit({exit_code})"),
            loop_id=LOOP_ID,
        )
    )
    assert verified.status != "blocked", verified
    progress = json.loads((path.parent / "implementation-progress.json").read_bytes())
    assert progress["tasks"][0]["quality_results"][-1]["exit_code"] == exit_code
    assert path.read_bytes() == before
    recorded = implementation.record_implementation_progress(
        ImplementationRecordOptions(root, "T11", "done", loop_id=LOOP_ID)
    )
    assert recorded.status != "blocked", recorded
    if exit_code == 0:
        assert (
            prepare_request(
                root, {"operation": "seal-for-review", "request_id": "seal-1"}
            ).context.phase
            == "review_sealed"
        )
    else:
        with pytest.raises(ValueError, match="simulation-actual-tasks-not-ready"):
            prepare_request(
                root, {"operation": "seal-for-review", "request_id": "seal-1"}
            )


def test_source_drift_rejects_frozen_grade_without_mutation(initialized_project_dir):
    from tests.unit.test_loop_simulation import assessment_data, candidate_data

    root = _ready_project(initialized_project_dir)
    assert start_simulation(root).status == "ready"
    begun = prepare_request(root, begin_request(root))
    frozen = prepare_request(
        root,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze-1",
            "candidates": [candidate_data(begun.context.plan)],
        },
    )
    path = root / ".ai-sdlc/loops/implementation" / LOOP_ID / "decision-context.json"
    before = path.read_bytes()
    (root / "src/ai_sdlc/core/implementation_loop.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="simulation-source-drift"):
        prepare_request(
            root,
            {
                "operation": "record-comparison",
                "request_id": "judge-1",
                "judgement": {
                    "judge_input_digest": frozen.context.pending_batch.judge_input_digest,
                    "assessments": [assessment_data()],
                },
            },
        )
    assert path.read_bytes() == before


def test_second_batch_failure_preserves_incumbent_without_new_opportunity(
    initialized_project_dir,
):
    root = _ready_project(initialized_project_dir)
    initial = choose_route(root, scores=(2, 2), continue_search=True).context
    result = prepare_request(
        root,
        {
            "operation": "record-comparison",
            "request_id": "failure-2",
            "failure": {"stage": "drafting", "reason": "无有效模型响应"},
        },
    ).context
    assert result.initial_selection_id == "A"
    assert result.started_at_ms == initial.started_at_ms
    assert result.pending_batch is None
    assert len(result.comparisons) == 2
    with pytest.raises(ValueError, match="no-pending-comparison"):
        prepare_request(
            root,
            {
                "operation": "record-comparison",
                "request_id": "failure-3",
                "failure": {"stage": "drafting", "reason": "不得换ID再试"},
            },
        )


def test_second_successful_judgement_can_revoke_old_winner(initialized_project_dir):
    from tests.unit.test_loop_simulation import assessment_data

    root = _ready_project(initialized_project_dir)
    initial = choose_route(root, scores=(2, 2), continue_search=True).context
    frozen = prepare_request(
        root,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze-2",
            "candidates": [initial.selected_candidate.model_dump(mode="json")],
        },
    )
    assessment = assessment_data(lower=0, upper=0)
    assessment["known_required_conflicts"] = ["o0"]
    context = prepare_request(
        root,
        {
            "operation": "record-comparison",
            "request_id": "judge-2",
            "judgement": {
                "judge_input_digest": frozen.context.pending_batch.judge_input_digest,
                "assessments": [assessment],
            },
        },
    ).context
    assert context.initial_selection_id is None
    assert context.selection.selected_id is None
    assert context.selected_candidate is None
    assert len(context.comparisons) == 2


@pytest.mark.parametrize("cost", [None, (4000, 5000), (1, 2)])
def test_second_batch_requires_independently_supported_complete_cost(
    initialized_project_dir, cost
):
    root = _ready_project(initialized_project_dir)
    context = choose_route(
        root, scores=(2, 2), continue_search=True, continuation_cost=cost
    ).context
    assert context.pending_batch is None
    assert context.initial_selection_id == "A"
    assert context.comparisons[0].continuation_stop_reason


@pytest.mark.parametrize("same_request", [False, True])
def test_concurrent_begin_has_one_identity_and_frozen_start(
    initialized_project_dir, same_request
):
    from tests.integration.test_quantified_implementation import _payload

    root = _ready_project(initialized_project_dir)
    assert start_simulation(root).status == "ready"
    first = begin_request(root)
    second = first if same_request else {**first, "request_id": "begin-conflict"}
    args1, preview1 = cli_prepare(root, first, apply=False)
    args2, preview2 = cli_prepare(root, second, apply=False)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _cli, root, *args, "--expect-digest", preview["prepare_digest"]
            )
            for args, preview in ((args1, preview1), (args2, preview2))
        ]
        results = [future.result() for future in futures]
    if same_request:
        assert sorted(_payload(result)["status"] for result in results) == [
            "existing",
            "prepared",
        ]
    else:
        assert sum(result.returncode == 0 for result in results) == 1
    context = json.loads(
        (
            root / ".ai-sdlc/loops/implementation" / LOOP_ID / "decision-context.json"
        ).read_bytes()
    )
    assert len(context["receipts"]) == 1
    assert context["pending_batch"]["number"] == 1
