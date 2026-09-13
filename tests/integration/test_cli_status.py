"""Status exposes current project, five-Loop, frontend, adapter and handoff facts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_sdlc.cli.main import app

runner = CliRunner()


def _initialize(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(root)
    result = runner.invoke(app, ["init", "."])
    assert result.exit_code == 0, result.output


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_status_default_is_compact_five_loop_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0, result.output
    assert "Result" in result.output
    assert "Next" in result.output
    assert "Blockers" in result.output
    assert "No delivery Loop has been started" in result.output
    for retired in ("Program Truth", "Telemetry", "Provenance", "AgentOps"):
        assert retired not in result.output


def test_status_json_exposes_only_retained_status_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize(tmp_path, monkeypatch)

    result = runner.invoke(app, ["status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "project-status/v2"
    assert set(payload) == {
        "schema_version",
        "project",
        "adapter",
        "five_loops",
        "frontend_delivery",
        "handoff",
    }
    assert payload["project"]["status"] == "initialized"
    assert payload["five_loops"]["status"] == "needs_user"
    assert payload["frontend_delivery"] == {
        "solution_confirmed": False,
        "apply_available": False,
        "browser_evidence_available": False,
    }
    serialized = json.dumps(payload)
    for retired in ("program_truth", "telemetry", "provenance", "agentops"):
        assert retired not in serialized.lower()


def test_status_details_and_json_are_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize(tmp_path, monkeypatch)
    before = _tree(tmp_path)

    details = runner.invoke(app, ["status", "--details"])
    machine = runner.invoke(app, ["status", "--json"])

    assert details.exit_code == 0, details.output
    assert machine.exit_code == 0, machine.output
    assert "Five Loops" in details.output
    assert _tree(tmp_path) == before


def test_status_surfaces_continuity_handoff_without_program_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize(tmp_path, monkeypatch)
    handoff = tmp_path / ".ai-sdlc" / "state" / "codex-handoff.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(
        "# Handoff\n\n## Exact Next Steps\n\n- Continue the retained Loop.\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["status", "--json"])

    payload = json.loads(result.output)
    assert payload["handoff"]["state"] == "ready"
    assert payload["handoff"]["next_steps"] == ["Continue the retained Loop."]
