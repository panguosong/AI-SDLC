"""Fresh enterprise projects do not receive retired runtime artifacts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ai_sdlc.cli.main import app

runner = CliRunner()


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
def test_fresh_init_creates_no_retired_runtime_noise(
    tmp_path: Path,
    relative_path: str,
    content: str,
) -> None:
    project = tmp_path / "enterprise-project"
    project.mkdir()
    (project / relative_path).write_text(content, encoding="utf-8")

    with patch("ai_sdlc.cli.main.maybe_render_update_notice"):
        result = runner.invoke(
            app,
            [
                "init",
                str(project),
                "--agent-target",
                "codex",
                "--shell",
                "powershell",
            ],
        )

    assert result.exit_code == 0, result.output
    forbidden = (
        project / "program-manifest.yaml",
        project / ".ai-sdlc" / "local" / "telemetry",
        project / ".ai-sdlc" / "agentops",
        project / ".ai-sdlc" / "provenance",
        project / ".ai-sdlc" / "proof",
        project / ".ai-sdlc" / "authority",
    )
    assert [
        path.relative_to(project).as_posix() for path in forbidden if path.exists()
    ] == []
