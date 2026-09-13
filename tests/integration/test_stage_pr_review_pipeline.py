"""源码 CLI 的当前树交付链；专家输入是协议夹具，不是真实模型质量证明。"""

from __future__ import annotations

import hashlib
import json
import sys

import pytest

from ai_sdlc.core.loop_simulation_models import StageScoreContract
from tests.integration.test_quantified_implementation import _cli, _payload
from tests.unit.test_loop_simulation import assessment_data, candidate_data
from tests.unit.test_loop_simulation_models import contract_data
from tests.unit.test_pr_review_service import _git, _init_repo

REVIEW_ID = "stage-pr-native"
LOOP_ID = f"loop-{REVIEW_ID}"


def _prepare(root, payload):
    path = (
        root / ".ai-sdlc/reviews/pr" / REVIEW_ID / f"host-{payload['request_id']}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    args = [
        "pr-review",
        "decision-prepare",
        "--review-id",
        REVIEW_ID,
        "--input",
        str(path),
        "--json",
    ]
    preview = _payload(_cli(root, *args, "--dry-run"))
    return _payload(_cli(root, *args, "--expect-digest", preview["prepare_digest"]))


def _guidance(root, command, reason=""):
    status = _payload(
        _cli(root, "loop", "status", "--type", "local-pr-review", "--json")
    )
    assert command in status["next_guidance"]["command"], status
    assert reason in status["next_guidance"]["reason"], status
    assert "implementation" not in status["next_guidance"]["command"]


def _ready(root, *, check_guidance=False):
    _init_repo(root)
    (root / "README.md").write_text(
        "# Current tree\nTenant access is rejected outside its boundary.\n",
        encoding="utf-8",
    )
    _git(root, "add", "README.md")
    started = _payload(
        _cli(
            root,
            "pr-review",
            "start",
            "--provider",
            "mock-reviewer",
            "--review-id",
            REVIEW_ID,
            "--decision-mode",
            "adaptive-quantified",
            "--decision-capability",
            "stage-simulation-v1",
            "--json",
        )
    )
    assert started["decision_capability"] == "stage-simulation-v1"
    if check_guidance:
        _guidance(root, "pr-review decision-prepare --schema", "Host agent")
    data = contract_data()
    data.update(
        capability="stage-simulation-v1",
        loop_type="local-pr-review",
        profile_id="delivery-readiness-v1",
    )
    data["time_plan"].update(
        scope="local-pr-review-close",
        work_breakdown=["当前树风险分析", "实际验证", "独立评审与原Close"],
    )
    contract = StageScoreContract.model_validate(data)
    _prepare(
        root,
        {
            "operation": "begin",
            "request_id": "begin",
            "contracts": [data],
            "sources": [
                {
                    "id": "spec",
                    "path": "README.md",
                    "sha256": hashlib.sha256(
                        (root / "README.md").read_bytes()
                    ).hexdigest(),
                    "locator": "tenant access",
                    "claim": "当前访问义务",
                }
            ],
        },
    )
    if check_guidance:
        _guidance(root, "pr-review decision-prepare", "freeze-comparison")
    frozen = _prepare(
        root,
        {
            "operation": "freeze-comparison",
            "request_id": "freeze",
            "candidates": [
                candidate_data(contract, "current-staged-tree", seconds=None)
            ],
        },
    )
    assert frozen["judge_input"]["source_manifest"]["README.md"]
    if check_guidance:
        _guidance(root, "pr-review decision-prepare", "record-comparison")
    _prepare(
        root,
        {
            "operation": "record-comparison",
            "request_id": "record",
            "judgement": {
                "judge_input_digest": frozen["judge_input"]["judge_input_digest"],
                "assessments": [assessment_data("current-staged-tree", 4, 4)],
            },
        },
    )
    if check_guidance:
        _guidance(root, "pr-review verify", "exact current staged tree")
    verified = _payload(
        _cli(
            root,
            "pr-review",
            "verify",
            "--cwd",
            ".",
            "--json",
            "--",
            sys.executable,
            "-c",
            "print('current tree verified')",
        )
    )
    assert verified["status"] == "ready"
    if check_guidance:
        _guidance(root, "pr-review decision-prepare", "seal-for-review")
    _prepare(root, {"operation": "seal-for-review", "request_id": "seal"})
    if check_guidance:
        _guidance(root, "loop review", "")
    return _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            "local-pr-review",
            "--loop-id",
            LOOP_ID,
            "--json",
        )
    )


def _expert_results(root, reviewed, status):
    paths = []
    for index, role in enumerate(reviewed["expert_roles"]):
        snapshot = _payload(
            _cli(
                root,
                "loop",
                "review",
                "--type",
                "local-pr-review",
                "--loop-id",
                LOOP_ID,
                "--expect-digest",
                reviewed["input_digest"],
                "--read-path",
                "README.md",
                "--json",
            )
        )["review_snapshot"]
        assert snapshot["sha256"] == reviewed["evidence_manifest"]["README.md"]
        payload = {
            "execution": {
                "status": "completed",
                "roles": [role],
                "role_reasons": {role: reviewed["expert_reasons"][role]},
                "findings": [],
            },
            "assessment": {
                "input_digest": reviewed["input_digest"],
                "context_digest": reviewed["context_digest"],
                "selected_route_id": "current-staged-tree",
                "results": [
                    {
                        "id": "o0",
                        "status": status,
                        "evidence_refs": ["actual"] if status != "UNKNOWN" else [],
                        "reason": "协议夹具的当前实际义务评价",
                    }
                ],
                "evidence": [
                    {
                        "id": "actual",
                        "path": "README.md",
                        "sha256": snapshot["sha256"],
                        "locator": "tenant access",
                        "claim": "同次快照中的当前交付义务",
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
        path = root / ".ai-sdlc/reviews/pr" / REVIEW_ID / f"host-expert-{index}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths.append(str(path))
    return paths


def _record_actual(root, reviewed, status):
    args = [
        "loop",
        "review-record",
        "--type",
        "local-pr-review",
        "--loop-id",
        LOOP_ID,
        "--expect-digest",
        reviewed["input_digest"],
        "--json",
    ]
    for path in _expert_results(root, reviewed, status):
        args.extend(["--result", path])
    return _payload(_cli(root, *args))


@pytest.mark.parametrize("status", ["PASS", "UNKNOWN", "FAIL"])
def test_native_pr_stage_review_exact_commit_and_close(tmp_path, status):
    root = tmp_path.resolve()
    reviewed = _ready(root, check_guidance=status == "PASS")
    directory = root / ".ai-sdlc/reviews/pr" / REVIEW_ID
    sealed = (directory / "decision-context.json").read_bytes()
    original_head = _git(root, "rev-parse", "HEAD")
    assert reviewed["decision_capability"] == "stage-simulation-v1"
    assert reviewed["selected_route_id"] == "current-staged-tree"
    recorded = _record_actual(root, reviewed, status)
    actual = json.loads((directory / "review-outcome-round-1.json").read_bytes())[
        "simulation"
    ]
    assert actual["evaluation"]["h"] == (0 if status == "PASS" else 1)
    assert actual["evaluation"]["q"] == ("100" if status == "PASS" else "0")
    close = [
        "pr-review",
        "close",
        "--review-id",
        REVIEW_ID,
        "--loop-id",
        LOOP_ID,
        "--expect-review-digest",
        reviewed["input_digest"],
        "--json",
    ]
    if status == "PASS":
        _guidance(root, "pr-review commit", "Actual independent review passed")
    committed = _cli(
        root,
        "pr-review",
        "commit",
        "--message",
        "exact current tree delivery",
        "--json",
    )
    if status != "PASS":
        assert recorded["status"] == "needs_fix"
        assert committed.returncode != 0
        assert json.loads(committed.stdout)["status"] == "blocked"
        rejected_close = _cli(root, *close)
        assert rejected_close.returncode != 0
        assert json.loads(rejected_close.stdout)["status"] == "blocked"
        assert _git(root, "rev-parse", "HEAD") == original_head
        assert not (directory / "final-report.md").exists()
    else:
        assert recorded["status"] == "passed"
        committed_data = _payload(committed)
        assert committed_data["tree_oid"] == _git(root, "rev-parse", "HEAD^{tree}")
        _guidance(root, "pr-review close", "exact reviewed tree is committed")
        closed = _payload(_cli(root, *close))
        assert closed["status"] == "closed"
        assert closed["verdict"] == "fully_clean"
        assert _payload(_cli(root, *close))["status"] == "closed"
        final_status = _payload(
            _cli(root, "loop", "status", "--type", "local-pr-review", "--json")
        )
        assert final_status["current_loop"]["status"] == "closed"
        assert final_status["next_guidance"]["safety"] == "no_action"
    assert (directory / "decision-context.json").read_bytes() == sealed
    assert not (directory / "review-outcome-round-3.json").exists()
    assert not (root / ".ai-sdlc/loops/local-pr-review").exists()


@pytest.mark.parametrize("second_status", ["PASS", "FAIL"])
def test_native_pr_necessary_repair_keeps_context_and_two_round_limit(
    tmp_path, second_status
):
    root = tmp_path.resolve()
    first = _ready(root)
    directory = root / ".ai-sdlc/reviews/pr" / REVIEW_ID
    context = (directory / "decision-context.json").read_bytes()
    assert _record_actual(root, first, "UNKNOWN")["status"] == "needs_fix"
    first_original = (directory / "review-outcome-round-1.json").read_bytes()
    original_run = json.loads((directory / "review-run.json").read_bytes())
    (root / "README.md").write_text(
        "# Repaired boundary\nExplicit tenant rejection and error response.\n",
        encoding="utf-8",
    )
    _git(root, "add", "README.md")
    assert _payload(_cli(root, "pr-review", "rerun", "--json"))["status"] == "started"
    assert (
        _payload(
            _cli(
                root,
                "pr-review",
                "verify",
                "--json",
                "--",
                sys.executable,
                "-c",
                "print('repair verified')",
            )
        )["status"]
        == "ready"
    )
    second = _payload(
        _cli(
            root,
            "loop",
            "review",
            "--type",
            "local-pr-review",
            "--loop-id",
            LOOP_ID,
            "--json",
        )
    )
    assert second["round_number"] == 2
    assert second["input_digest"] != first["input_digest"]
    recorded = _record_actual(root, second, second_status)
    new_run = json.loads((directory / "review-run.json").read_bytes())
    assert (
        new_run["decision_staged_tree_oid"] == original_run["decision_staged_tree_oid"]
    )
    assert new_run["staged_tree_oid"] != original_run["staged_tree_oid"]
    assert new_run["decision_started_at_ms"] == original_run["decision_started_at_ms"]
    assert new_run["decision_started_at_ms"] == json.loads(context)["started_at_ms"]
    commit = _cli(root, "pr-review", "commit", "--message", "reviewed repair", "--json")
    close = _cli(
        root,
        "pr-review",
        "close",
        "--review-id",
        REVIEW_ID,
        "--loop-id",
        LOOP_ID,
        "--expect-review-digest",
        second["input_digest"],
        "--json",
    )
    if second_status == "PASS":
        assert recorded["status"] == "passed"
        assert _payload(commit)["status"] == "ready"
        assert _payload(close)["status"] == "closed"
    else:
        assert recorded["status"] in {"blocked", "needs_user"}
        assert commit.returncode != 0
        assert close.returncode != 0
        assert json.loads(commit.stdout)["status"] == "blocked"
        assert json.loads(close.stdout)["status"] == "blocked"
    assert (directory / "decision-context.json").read_bytes() == context
    assert (directory / "review-outcome-round-1.json").read_bytes() == first_original
    assert not (directory / "review-outcome-round-3.json").exists()
