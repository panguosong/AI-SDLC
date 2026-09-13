"""Constraint verification is a read-only project/self-development report."""

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


def test_project_constraints_json_is_direct_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize(tmp_path, monkeypatch)
    before = _tree(tmp_path)

    result = runner.invoke(app, ["verify", "constraints", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["profile"] == "project"
    assert payload["decision"] == "allow"
    assert payload["blockers"] == []
    assert payload["verification_gate"]["source_name"] == "verify constraints"
    assert _tree(tmp_path) == before
    assert not (tmp_path / ".ai-sdlc" / "telemetry").exists()


def test_project_constraints_terminal_reports_direct_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialize(tmp_path, monkeypatch)

    result = runner.invoke(app, ["verify", "constraints"])

    assert result.exit_code == 0, result.output
    assert "verify constraints: no BLOCKERs." in result.output
    for retired in ("Telemetry", "Provenance", "AgentOps", "certificate"):
        assert retired not in result.output


def test_self_development_profile_remains_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(repo_root)

    result = runner.invoke(
        app,
        ["verify", "constraints", "--profile", "self-development", "--json"],
    )

    assert result.exit_code in {0, 1}, result.output
    payload = json.loads(result.output)
    assert payload["profile"] == "self-development"
    assert payload["verification_gate"]["source_name"] == "verify constraints"
