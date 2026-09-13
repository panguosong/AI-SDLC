"""Legacy review-authority artifacts are inert project data after cutover."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_sdlc.cli.main import app
from ai_sdlc.routers.bootstrap import init_project

_LEGACY_ARTIFACTS = {
    ".ai-sdlc/sessions/WI-LEGACY/requirement/session-001/session.json": (
        b'{"artifact_kind":"stage-review-session","status":"closed"}\n'
    ),
    ".ai-sdlc/certificates/stage-close.json": (
        b'{"artifact_kind":"stage-close-certificate","authorized":true}\n'
    ),
    ".ai-sdlc/loops/implementation/impl-001/lean/current.json": (
        b'{"artifact_kind":"lean-code-current-pointer","round":99}\n'
    ),
    ".ai-sdlc/activation/decision.json": (
        b'{"artifact_kind":"stage-gate-activation","mode":"enforce"}\n'
    ),
}


def test_status_and_run_ignore_legacy_review_authority_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_project(tmp_path)
    for relative, content in _LEGACY_ARTIFACTS.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    status = runner.invoke(app, ["status"])
    dry_run = runner.invoke(app, ["run", "--dry-run"])

    assert status.exit_code == 0, status.output
    assert dry_run.exit_code == 0, dry_run.output
    for relative, content in _LEGACY_ARTIFACTS.items():
        assert (tmp_path / relative).read_bytes() == content

    combined = f"{status.output}\n{dry_run.output}".lower()
    assert "wi-legacy" not in combined
    assert "impl-001" not in combined
    notices = [
        line
        for line in combined.splitlines()
        if "legacy review artifact" in line or "retired review artifact" in line
    ]
    assert len(notices) <= 1
