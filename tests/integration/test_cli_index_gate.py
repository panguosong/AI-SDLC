"""Integration test for the retained hidden project index command."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_sdlc.cli.main import app

runner = CliRunner()


def test_index_rebuilds_project_fact_indexes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "print('hello')\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )

    assert runner.invoke(app, ["init", "."]).exit_code == 0
    (tmp_path / ".ai-sdlc" / "project" / "generated" / "key-files.json").unlink(
        missing_ok=True
    )
    (tmp_path / ".ai-sdlc" / "state" / "repo-facts.json").unlink(
        missing_ok=True
    )

    result = runner.invoke(app, ["index"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".ai-sdlc" / "state" / "repo-facts.json").is_file()
    assert (
        tmp_path / ".ai-sdlc" / "project" / "generated" / "key-files.json"
    ).is_file()
