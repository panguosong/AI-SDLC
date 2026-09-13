"""Doctor reports retained local facts without retired runtime side effects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_sdlc.cli.main import app

runner = CliRunner()


def _init_project(root: Path) -> None:
    (root / ".ai-sdlc" / "project" / "config").mkdir(parents=True)
    (root / ".ai-sdlc" / "project" / "config" / "project-config.yaml").write_text(
        "project:\n  name: doctor-fixture\n",
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"vue": "^3.5.0"}}),
        encoding="utf-8",
    )


def test_doctor_reports_environment_and_browser_capability() -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Python executable" in result.output
    assert "sys.prefix" in result.output
    assert "python -m ai_sdlc" in result.output
    assert "Environment Diagnostics" in result.output
    assert "browser evidence" in result.output
    assert "does not install dependencies or mutate the project" in result.output
    for retired in ("telemetry", "provenance", "program manifest", "AgentOps"):
        assert retired not in result.output


def test_doctor_is_read_only_in_initialized_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_project(tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["doctor"])

    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert result.exit_code == 0, result.output
    assert "browser evidence" in result.output
    assert after == before
    assert not (tmp_path / ".ai-sdlc" / "telemetry").exists()
    assert not (tmp_path / "program-manifest.yaml").exists()
