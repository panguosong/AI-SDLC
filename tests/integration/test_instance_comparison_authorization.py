"""第三批真实 CLI 接线；合成判断不表示业务专家已经评审通过。"""

import hashlib
import json

from ai_sdlc.core.loop_simulation_context import SimulationContext
from tests.integration.test_quantified_implementation import _cli, _payload
from tests.integration.test_stage_quantified_pipeline import (
    CAPABILITY,
    LOOP,
    WORK_ITEM,
    stage_apply,
    stage_start,
)
from tests.unit.test_instance_comparison_authorization import (
    authorization_request,
    third_candidates,
)
from tests.unit.test_loop_simulation import assessment_data, candidate_data
from tests.unit.test_loop_simulation_models import contract_data
from tests.unit.test_quantified_input_correction import (
    time_revision_candidates,
    time_revision_request,
)


def _materialize(root, source, text):
    path = root / source["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    source["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()


def _two_native_rejections(root):
    stage_start(root, "implementation")
    source = root / WORK_ITEM / "spec.md"
    context = stage_apply(
        root,
        "implementation",
        dict(
            operation="begin",
            request_id="begin",
            contracts=[
                {
                    **contract_data(),
                    "capability": CAPABILITY,
                    "loop_type": "implementation",
                    "profile_id": profile,
                }
                for profile in ("implementation-plan-v1", "code-result-v1")
            ],
            sources=[
                dict(
                    id="spec",
                    path=source.relative_to(root).as_posix(),
                    sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                    locator="all",
                    claim="原始范围及质量合同",
                )
            ],
        ),
    )
    frozen = stage_apply(
        root,
        "implementation",
        dict(
            operation="freeze-comparison",
            request_id="freeze-first",
            candidates=[
                candidate_data(context.plan, name, seconds=(3600, 7200))
                for name in ("A", "B")
            ],
        ),
    )
    original = stage_apply(
        root,
        "implementation",
        dict(
            operation="record-comparison",
            request_id="judge-first",
            judgement=dict(
                judge_input_digest=frozen.pending_batch.judge_input_digest,
                assessments=[assessment_data(name) for name in ("A", "B")],
            ),
        ),
    )
    assert original.initial_selection_id is None
    request = time_revision_request(original)
    _materialize(
        root, request["sources"][0], "已完成的第一批准备回执，完整剩余工作需继续判断。"
    )
    revised = stage_apply(root, "implementation", request)
    frozen = stage_apply(
        root,
        "implementation",
        dict(
            operation="freeze-comparison",
            request_id="freeze-second",
            candidates=time_revision_candidates(original, revised),
        ),
    )
    context = stage_apply(
        root,
        "implementation",
        dict(
            operation="record-comparison",
            request_id="judge-second",
            judgement=dict(
                judge_input_digest=frozen.pending_batch.judge_input_digest,
                assessments=[
                    {
                        **assessment_data(name),
                        "cost_check": "incomplete",
                        "cost_reason": "第二批未包含完整后续验证成本",
                    }
                    for name in ("A", "B")
                ],
            ),
        ),
    )
    assert context.initial_selection_id is None
    return context


def _cli_prepare(root, request, *, apply=True):
    path = (
        root / ".ai-sdlc/state/instance-comparison" / (request["request_id"] + ".json")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(request), encoding="utf-8")
    args = (
        "loop",
        "decision-prepare",
        "--type",
        "implementation",
        "--loop-id",
        LOOP,
        "--input",
        str(path),
        "--json",
    )
    result = _cli(root, *args, "--dry-run")
    if not apply:
        return result
    preview = _payload(result)
    return _payload(_cli(root, *args, "--expect-digest", preview["prepare_digest"]))


def test_native_third_batch_authorization_reaches_original_task_without_new_loop(
    initialized_project_dir,
):
    root = initialized_project_dir
    old = _two_native_rejections(root)
    directory = root / ".ai-sdlc/loops/implementation" / LOOP
    run_before = (directory / "loop-run.json").read_bytes()
    old_context = (directory / "decision-context.json").read_bytes()
    request = authorization_request(old)
    for source in request["sources"]:
        _materialize(
            root, source, source["claim"] + "；只用于本合成测试，保留原失败与成本。"
        )
    unauthorized = _cli_prepare(
        root,
        {k: v for k, v in request.items() if k != "comparison_authorization"},
        apply=False,
    )
    assert unauthorized.returncode == 1
    assert (directory / "decision-context.json").read_bytes() == old_context
    authorized = _cli_prepare(root, request)
    context = SimulationContext.model_validate(authorized["context"])
    assert context.pending_batch.number == 3
    assert context.comparisons == old.comparisons
    assert context.started_at_ms == old.started_at_ms
    assert (directory / "loop-run.json").read_bytes() == run_before
    assert not list(directory.glob("review-outcome-round-*.json"))
    frozen = _cli_prepare(
        root,
        dict(
            operation="freeze-comparison",
            request_id="freeze-third",
            candidates=third_candidates(context),
        ),
    )
    judge_input = frozen["judge_input"]
    assert judge_input["judge_input_digest"] not in {
        b.judge_input_digest for b in old.comparisons
    }
    selected = _cli_prepare(
        root,
        dict(
            operation="record-comparison",
            request_id="judge-third",
            judgement=dict(
                judge_input_digest=judge_input["judge_input_digest"],
                assessments=[assessment_data(name) for name in ("A", "B")],
            ),
        ),
    )
    assert selected["context"]["initial_selection_id"] == "A"
    assert len(selected["context"]["comparisons"]) == 3
    fourth = _cli_prepare(root, {**request, "request_id": "fourth"}, apply=False)
    assert fourth.returncode == 1
    assert "unavailable" in fourth.stdout
    recorded = _payload(
        _cli(
            root,
            "loop",
            "implementation",
            "record",
            "--loop-id",
            LOOP,
            "--task-id",
            "T11",
            "--status",
            "in_progress",
            "--json",
        )
    )
    assert recorded["status"] != "blocked"
    assert not list(directory.glob("review-outcome-round-*.json"))
    # 已绑定的新授权原件删除后，后续原生消费必须拒绝，不借 selected 绕过来源真实性。
    (root / request["sources"][0]["path"]).unlink()
    blocked = _cli(
        root,
        "loop",
        "implementation",
        "record",
        "--loop-id",
        LOOP,
        "--task-id",
        "T11",
        "--status",
        "in_progress",
        "--json",
    )
    assert blocked.returncode == 1
    assert json.loads(blocked.stdout)["status"] == "blocked"
