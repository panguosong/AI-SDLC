"""Enterprise project stacks share one bounded, read-only normal user path."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ai_sdlc.cli.main import app
from ai_sdlc.core.loop_models import LoopStatus, LoopType
from ai_sdlc.core.loop_router import LoopRouteItem, LoopRouteResult, LoopRouteStatus
from ai_sdlc.routers.bootstrap import init_project
from tests.integration.test_quantified_implementation import (
    _expert_envelopes,
    _request,
    _snapshot,
)
from tests.unit.test_implementation_loop import _write_ready_work_item

runner = CliRunner()


def _normal_cli(*args: str, exit_code: int = 0) -> dict:
    command = list(args)
    command.insert(command.index("--") if "--" in command else len(command), "--json")
    with patch("ai_sdlc.cli.main.maybe_render_update_notice"):
        result = runner.invoke(app, command)
    assert result.exit_code == exit_code, result.output
    return json.loads(result.stdout)


def _normal_review(root: Path, loop_type: str, loop_id: str) -> dict:
    reviewed = _normal_cli("loop", "review", "--type", loop_type, "--loop-id", loop_id)
    result_dir = root / ".ai-sdlc/state/normal-path-review" / loop_id
    result_dir.mkdir(parents=True, exist_ok=True)
    args = [
        "loop",
        "review-record",
        "--type",
        loop_type,
        "--loop-id",
        loop_id,
        "--expect-digest",
        reviewed["input_digest"],
    ]
    for index, role in enumerate(reviewed["expert_roles"]):
        path = result_dir / f"expert-{index}.json"
        path.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "roles": [role],
                    "role_reasons": {role: reviewed["expert_reasons"][role]},
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        args.extend(("--result", str(path)))
    _normal_cli(*args)
    return reviewed


@pytest.fixture
def normal_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Path:
    root = tmp_path / "normal-project"
    root.mkdir()
    init_project(root)
    _write_ready_work_item(
        root,
        frontend=getattr(request, "param", False),
        extra_spec="Security permission boundary is required.",
    )
    source = root / "src/ai_sdlc/core/implementation_loop.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.email", "normal-path@example.invalid")
    _git(root, "config", "user.name", "Normal Path Fixture")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "normal path baseline")
    monkeypatch.chdir(root)
    _normal_cli(
        "loop",
        "requirement",
        "start",
        "--idea",
        "Implementation evidence tracking",
        "--acceptance",
        "Implementation evidence can be closed.",
        "--design-scope-family",
        "implementation",
        "--work-item-id",
        "demo-implementation-loop",
        "--loop-id",
        "req-normal",
    )
    reviewed = _normal_review(root, "requirement", "req-normal")
    _normal_cli(
        "loop",
        "requirement",
        "freeze",
        "--loop-id",
        "req-normal",
        "--yes",
        "--expect-review-digest",
        reviewed["input_digest"],
    )
    _normal_cli(
        "loop",
        "design-contract",
        "check",
        "--wi",
        "specs/demo-implementation-loop",
        "--requirement-loop-id",
        "req-normal",
        "--loop-id",
        "dc-normal",
    )
    reviewed = _normal_review(root, "design-contract", "dc-normal")
    _normal_cli(
        "loop",
        "design-contract",
        "close",
        "--loop-id",
        "dc-normal",
        "--yes",
        "--expect-review-digest",
        reviewed["input_digest"],
    )
    return root


def _normal_start() -> dict:
    return _normal_cli(
        "loop",
        "implementation",
        "start",
        "--wi",
        "specs/demo-implementation-loop",
        "--loop-id",
        "impl-normal",
        "--decision-mode",
        "adaptive-quantified",
    )


def _normal_prepare(root: Path) -> dict:
    request = _request(
        root, root / ".ai-sdlc/state/normal-prepare.json", route_id="selected-direct"
    )
    args = [
        "loop",
        "decision-prepare",
        "--type",
        "implementation",
        "--loop-id",
        "impl-normal",
        "--input",
        str(request),
    ]
    preview = _normal_cli(*args, "--dry-run")
    assert "--expect-digest" in preview["next_action"]
    assert "selected-direct" not in preview["next_action"]
    return _normal_cli(*args, "--expect-digest", preview["prepare_digest"])


def _normal_observe(expected_status: str, next_text: str) -> dict:
    routed = _normal_cli("run")
    assert routed["status"] == "routed", routed
    assert routed["current_loop"]["loop_id"] == "impl-normal"
    assert routed["current_loop"]["status"] == expected_status
    assert next_text in routed["next_action"]
    assert [item["status"] for item in routed["observed_loops"][:2]] == [
        "closed",
        "closed",
    ]
    for args in (
        ("loop", "implementation", "status"),
        ("loop", "status", "--type", "implementation"),
    ):
        status = _normal_cli(*args)
        assert status["current_loop"]["status"] == expected_status
        assert next_text in status["next_action"]
    with patch("ai_sdlc.cli.main.maybe_render_update_notice"):
        status = runner.invoke(app, ["status"])
    assert status.exit_code == 0, status.output
    assert next_text in status.output
    return routed


def test_normal_real_closed_chain_keeps_valid_reviewed_predecessors_closed(
    normal_chain: Path,
) -> None:
    before = _repository_state(normal_chain)
    for loop_type in ("requirement", "design-contract"):
        status = _normal_cli("loop", "status", "--type", loop_type)
        assert status["current_loop"]["status"] == "closed"
    routed = _normal_cli("run")
    assert routed["status"] == "needs_user"
    assert "implementation start" in routed["next_action"]
    assert _repository_state(normal_chain) == before


@pytest.mark.parametrize("after_pass", ["close", "drift"])
def test_b1_normal_real_chain_prepares_executes_verifies_reviews_and_closes(
    normal_chain: Path,
    after_pass: str,
) -> None:
    started = _normal_start()
    assert "decision-prepare" in started["next_action"]
    assert "--status done" not in started["next_action"]
    _normal_observe("running", "decision-prepare")
    prepared = _normal_prepare(normal_chain)
    assert "selected-direct" in prepared["next_action"]
    routed = _normal_observe("running", "selected-direct")
    assert "implementation verify" in routed["next_action"]
    assert "round 1" not in routed["next_action"]
    assert "only" in routed["next_action"].lower()
    source = normal_chain / "src/ai_sdlc/core/implementation_loop.py"
    source.write_text("VALUE = 2\n", encoding="utf-8")
    _normal_cli(
        "loop",
        "implementation",
        "record",
        "--loop-id",
        "impl-normal",
        "--task-id",
        "T11",
        "--status",
        "done",
        "--verification",
        "python actual source check",
    )
    _normal_observe("running", "implementation verify")
    _normal_cli(
        "loop",
        "implementation",
        "verify",
        "--loop-id",
        "impl-normal",
        "--task-id",
        "T11",
        "--",
        sys.executable,
        "-c",
        "from pathlib import Path; assert Path('src/ai_sdlc/core/implementation_loop.py').read_text() == 'VALUE = 2\\n'",
    )
    _normal_observe("needs_review", "loop review --type implementation")
    reviewed = _normal_cli(
        "loop", "review", "--type", "implementation", "--loop-id", "impl-normal"
    )
    paths = _expert_envelopes(normal_chain, Path("normal-pass"), reviewed)
    args = [
        "loop",
        "review-record",
        "--type",
        "implementation",
        "--loop-id",
        "impl-normal",
        "--expect-digest",
        reviewed["input_digest"],
    ]
    for path in paths:
        args.extend(("--result", str(path)))
    _normal_cli(*args)
    _normal_observe("passed", "implementation close")
    if after_pass == "drift":
        source.write_text("VALUE = 3\n", encoding="utf-8")
        routed = _normal_observe("needs_user", "outside the allowed repair transition")
        assert routed["blockers"] == ["review-input-drift"]
        reviewed = _normal_cli(
            "loop", "review", "--type", "implementation", "--loop-id", "impl-normal"
        )
        assert reviewed["round_number"] == 1
        assert reviewed["review_reason"] == "review-input-drift"
        _normal_cli(
            "loop",
            "implementation",
            "close",
            "--loop-id",
            "impl-normal",
            "--yes",
            "--expect-review-digest",
            reviewed["input_digest"],
            exit_code=1,
        )
        return
    _normal_cli(
        "loop",
        "implementation",
        "close",
        "--loop-id",
        "impl-normal",
        "--yes",
        "--expect-review-digest",
        reviewed["input_digest"],
    )
    status = _normal_cli("loop", "implementation", "status")
    assert status["current_loop"]["status"] == "closed"
    routed = _normal_cli("run")
    assert routed["status"] == "needs_user"
    assert routed["next_action"] == (
        "ai-sdlc pr-review start --decision-mode adaptive-quantified "
        "--decision-capability stage-simulation-v1"
    )


@pytest.mark.parametrize(
    "corruption", ["malformed-context", "identity", "missing-after-progress"]
)
def test_b1_normal_path_never_treats_corruption_as_unprepared(
    normal_chain: Path, corruption: str
) -> None:
    _normal_start()
    loop_dir = normal_chain / ".ai-sdlc/loops/implementation/impl-normal"
    if corruption == "malformed-context":
        (loop_dir / "decision-context.json").write_text("{broken", encoding="utf-8")
    elif corruption == "identity":
        path = loop_dir / "implementation-input.json"
        payload = json.loads(path.read_text("utf-8"))
        payload.pop("decision_mode")
        payload.pop("decision_capability")
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        _normal_prepare(normal_chain)
        _normal_cli(
            "loop",
            "implementation",
            "record",
            "--task-id",
            "T11",
            "--status",
            "in_progress",
        )
        (loop_dir / "decision-context.json").unlink()
    before = _repository_state(normal_chain)
    status = _normal_cli("loop", "implementation", "status", exit_code=1)
    assert status["status"] == "blocked"
    assert "--schema" not in status["next_action"]
    routed = _normal_cli("run", exit_code=1)
    assert routed["status"] == "blocked"
    assert _repository_state(normal_chain) == before


@pytest.mark.parametrize(
    "corruption", ["missing-review", "malformed-review", "input-drift"]
)
def test_closed_normal_predecessor_still_checks_current_review(
    normal_chain: Path, corruption: str
) -> None:
    loop_dir = normal_chain / ".ai-sdlc/loops/design-contract/dc-normal"
    outcome = loop_dir / "review-outcome-round-1.json"
    if corruption == "missing-review":
        outcome.unlink()
    elif corruption == "malformed-review":
        outcome.write_text("{broken", encoding="utf-8")
    else:
        path = normal_chain / "specs/demo-implementation-loop/plan.md"
        path.write_text(
            path.read_text("utf-8") + "\nChanged after close.\n", encoding="utf-8"
        )
    with patch("ai_sdlc.cli.main.maybe_render_update_notice"):
        result = runner.invoke(
            app, ["loop", "status", "--type", "design-contract", "--json"]
        )
    payload = json.loads(result.stdout)
    assert (
        payload["status"] == "blocked" or payload["current_loop"]["status"] != "closed"
    )
    assert payload["blocker"]


def test_decision_prepare_schema_is_available_without_project_or_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    before = _snapshot(tmp_path)
    schema = _normal_cli("loop", "decision-prepare", "--schema")
    assert set(schema["required"]) == {"route_contract", "candidates", "sources"}
    assert schema["additionalProperties"] is False
    assert "selection" not in schema["properties"]
    assert _snapshot(tmp_path) == before


def test_legacy_running_status_keeps_existing_missing_review_material_fallback(
    normal_chain: Path,
) -> None:
    started = _normal_cli(
        "loop",
        "implementation",
        "start",
        "--wi",
        "specs/demo-implementation-loop",
        "--loop-id",
        "impl-legacy-normal",
    )
    assert "--status done" in started["next_action"]
    loop_dir = normal_chain / ".ai-sdlc/loops/implementation/impl-legacy-normal"
    assert "decision_mode" not in json.loads(
        (loop_dir / "implementation-input.json").read_text("utf-8")
    )
    (loop_dir / "implementation-input.json").unlink()
    status = _normal_cli("loop", "implementation", "status")
    assert status["current_loop"]["status"] == "running"
    assert status["next_action"] == started["next_action"]


def _close_normal_delivery(root: Path) -> None:
    _normal_cli(
        "loop",
        "implementation",
        "start",
        "--wi",
        "specs/demo-implementation-loop",
        "--loop-id",
        "impl-receipts",
    )
    _normal_cli(
        "loop",
        "implementation",
        "record",
        "--task-id",
        "T11",
        "--status",
        "done",
        "--verification",
        "python actual source check",
    )
    _normal_cli(
        "loop",
        "implementation",
        "verify",
        "--task-id",
        "T11",
        "--",
        sys.executable,
        "-c",
        "from pathlib import Path; assert Path('src/ai_sdlc/core/implementation_loop.py').read_text() == 'VALUE = 1\\n'",
    )
    reviewed = _normal_review(root, "implementation", "impl-receipts")
    _normal_cli(
        "loop",
        "implementation",
        "close",
        "--loop-id",
        "impl-receipts",
        "--yes",
        "--expect-review-digest",
        reviewed["input_digest"],
    )
    _normal_cli(
        "loop",
        "frontend-evidence",
        "skip",
        "--wi",
        "specs/demo-implementation-loop",
        "--loop-id",
        "fe-receipts",
        "--reason",
        "Receipt validation fixture explicitly accepts unavailable browser evidence.",
        "--yes",
    )
    reviewed = _normal_review(root, "frontend-evidence", "fe-receipts")
    _normal_cli(
        "loop",
        "frontend-evidence",
        "close",
        "--loop-id",
        "fe-receipts",
        "--yes",
        "--allow-warnings",
        "--expect-review-digest",
        reviewed["input_digest"],
    )


@pytest.mark.parametrize("normal_chain", [True], indirect=True)
@pytest.mark.parametrize(
    "loop_type",
    ["requirement", "design-contract", "implementation", "frontend-evidence"],
)
@pytest.mark.parametrize(
    "corruption", ["missing", "malformed", "identity", "path", "contract"]
)
def test_closed_normal_path_requires_valid_native_receipt(
    normal_chain: Path, loop_type: str, corruption: str
) -> None:
    _close_normal_delivery(normal_chain)
    routed = _normal_cli("run")
    assert [item["status"] for item in routed["observed_loops"]] == ["closed"] * 4
    assert routed["next_action"] == (
        "ai-sdlc pr-review start --decision-mode adaptive-quantified "
        "--decision-capability stage-simulation-v1"
    )
    loop_id = {
        "requirement": "req-normal",
        "design-contract": "dc-normal",
        "implementation": "impl-receipts",
        "frontend-evidence": "fe-receipts",
    }[loop_type]
    filename = (
        "requirement-freeze.json"
        if loop_type == "requirement"
        else f"{loop_type}-close.json"
    )
    receipt = normal_chain / ".ai-sdlc/loops" / loop_type / loop_id / filename
    payload = json.loads(receipt.read_text("utf-8"))
    if corruption == "missing":
        receipt.unlink()
    elif corruption == "malformed":
        receipt.write_text("{broken", encoding="utf-8")
    else:
        if corruption == "identity":
            payload["loop_id"] = "another-loop"
        elif corruption == "path":
            payload["intake_path" if loop_type == "requirement" else "report_path"] = (
                "specs/another-report.json"
            )
        else:
            field, value = {
                "requirement": ("intake_digest", "sha256:" + "0" * 64),
                "design-contract": ("blocker_count", 1),
                "implementation": ("required_task_count", 999),
                "frontend-evidence": ("warning_count", 999),
            }[loop_type]
            payload[field] = value
        receipt.write_text(json.dumps(payload), encoding="utf-8")
    before = _snapshot(
        normal_chain / ".ai-sdlc", normal_chain / "specs", normal_chain / "src"
    )
    before_git = _repository_state(normal_chain)
    status = _normal_cli("loop", "status", "--type", loop_type, exit_code=1)
    assert status["status"] == "blocked"
    assert status["blocker"]
    routed = _normal_cli("run", exit_code=1)
    assert routed["status"] == "blocked"
    assert (
        _snapshot(
            normal_chain / ".ai-sdlc", normal_chain / "specs", normal_chain / "src"
        )
        == before
    )
    assert _repository_state(normal_chain) == before_git


@pytest.mark.parametrize("normal_chain", [True], indirect=True)
@pytest.mark.parametrize(
    "loop_type,loop_id",
    [("implementation", "impl-receipts"), ("frontend-evidence", "fe-receipts")],
)
def test_closed_normal_receipt_cannot_change_successor(
    normal_chain: Path, loop_type: str, loop_id: str
) -> None:
    _close_normal_delivery(normal_chain)
    receipt = (
        normal_chain
        / ".ai-sdlc/loops"
        / loop_type
        / loop_id
        / f"{loop_type}-close.json"
    )
    payload = json.loads(receipt.read_text("utf-8"))
    payload["next_loop_type"] = "requirement"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    status = _normal_cli("loop", "status", "--type", loop_type, exit_code=1)
    assert status["status"] == "blocked"


def test_schema_host_digest_example_executes_and_prepares(normal_chain: Path) -> None:
    _normal_start()
    schema = _normal_cli("loop", "decision-prepare", "--schema")
    request = _request(
        normal_chain, normal_chain / ".ai-sdlc/state/schema-host-draft.json"
    )
    payload = json.loads(request.read_text("utf-8"))
    expected = payload["candidates"][0].pop("contract_digest")
    request.write_text(json.dumps(payload), encoding="utf-8")
    argv = schema["x-guidance"]["contract_digest_command"]
    assert argv[-1] == "<host-generated-input>"
    result = subprocess.run(
        [*argv[:-1], str(request)],
        cwd=normal_chain,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    digest = result.stdout.strip()
    assert digest == expected
    assert (
        digest
        != hashlib.sha256(
            json.dumps(
                payload["route_contract"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    payload["candidates"][0]["contract_digest"] = digest
    request.write_text(json.dumps(payload), encoding="utf-8")
    preview = _normal_cli(
        "loop",
        "decision-prepare",
        "--type",
        "implementation",
        "--loop-id",
        "impl-normal",
        "--input",
        str(request),
        "--dry-run",
    )
    assert preview["status"] == "preview"
    assert preview["selection"]["selected_id"] == "A"


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("package.json", '{"name":"enterprise-node","private":true}\n'),
        (
            "pom.xml",
            "<project><modelVersion>4.0.0</modelVersion>"
            "<groupId>example</groupId><artifactId>enterprise-java</artifactId>"
            "</project>\n",
        ),
        (
            "pyproject.toml",
            '[project]\nname = "enterprise-python"\nversion = "1.0.0"\n',
        ),
    ],
)
def test_enterprise_stack_gets_only_current_loop_rules_without_project_writes(
    tmp_path: Path,
    relative_path: str,
    content: str,
) -> None:
    project = tmp_path / relative_path.split(".", maxsplit=1)[0]
    project.mkdir()
    init_project(project)
    (project / relative_path).write_text(content, encoding="utf-8")
    _git(project, "init", "--initial-branch=main")
    _git(project, "config", "user.email", "enterprise@example.com")
    _git(project, "config", "user.name", "Enterprise Fixture")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "enterprise baseline")
    before = _repository_state(project)
    routed = LoopRouteResult(
        status=LoopRouteStatus.ROUTED,
        result="Current delivery Loop: implementation impl-enterprise (needs_review).",
        current_loop=LoopRouteItem(
            loop_type=LoopType.IMPLEMENTATION,
            loop_id="impl-enterprise",
            status=LoopStatus.NEEDS_REVIEW,
            next_action="Run the selected implementation experts.",
        ),
        next_action="Run the selected implementation experts.",
    )

    with (
        patch("ai_sdlc.cli.main.maybe_render_update_notice"),
        patch("ai_sdlc.cli.run_cmd.find_project_root", return_value=project),
        patch("ai_sdlc.cli.commands.find_project_root", return_value=project),
        patch("ai_sdlc.cli.run_cmd.route_five_loops", return_value=routed),
        patch("ai_sdlc.core.loop_router.route_five_loops", return_value=routed),
    ):
        human = runner.invoke(app, ["run"])
        machine = runner.invoke(app, ["run", "--json"])
        status = runner.invoke(app, ["status"])

    assert human.exit_code == 0, human.output
    assert "Applicable Rules" in human.output
    assert "tdd" in human.output
    assert "verification" in human.output
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.stdout)
    assert [item["name"] for item in payload["applicable_rules"]] == [
        "tdd",
        "verification",
    ]
    assert len(payload["applicable_rules"]) <= 2
    assert status.exit_code == 0, status.output
    assert "Result:" in status.output
    assert "Next:" in status.output
    assert "Blockers:" in status.output
    assert "AI-SDLC Status" not in status.output

    generic_output = "\n".join((human.output, machine.stdout, status.output)).lower()
    for unrelated in (
        "public-primevue",
        "enterprise-vue2",
        "ai-sdlc release",
        "competition",
        "workitem 010",
        "telemetry",
        "certificate",
        "proof ledger",
    ):
        assert unrelated not in generic_output
    assert _repository_state(project) == before


def _repository_state(root: Path) -> tuple[str, str, str]:
    return (
        _git(root, "rev-parse", "HEAD"),
        _git(root, "write-tree"),
        _git(root, "status", "--porcelain=v1", "--untracked-files=all"),
    )


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.strip())
    return result.stdout.strip()
