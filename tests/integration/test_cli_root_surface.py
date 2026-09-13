"""Normal-user root CLI surface excludes retired parallel authorities."""

from __future__ import annotations

from typer.main import get_command
from typer.testing import CliRunner

from ai_sdlc.cli.main import app

runner = CliRunner()

_VISIBLE = {
    "init",
    "adopt",
    "doctor",
    "status",
    "recover",
    "run",
    "adapter",
    "workitem",
    "verify",
    "loop",
    "pr-review",
    "self-update",
}
_RETAINED_HIDDEN = {
    "index",
    "scan",
    "refresh",
    "handoff",
}
_RETIRED = {
    "agentops",
    "enterprise",
    "gate",
    "rules",
    "studio",
    "stage",
    "program",
    "host-runtime",
    "telemetry",
    "provenance",
    "trace",
}


def test_root_command_metadata_exposes_only_normal_user_surface() -> None:
    root_command = get_command(app)

    assert all(root_command.commands[name].hidden is False for name in _VISIBLE)
    assert all(root_command.commands[name].hidden is True for name in _RETAINED_HIDDEN)
    assert _RETIRED.isdisjoint(root_command.commands)


def test_retired_parallel_authority_is_not_callable() -> None:
    result = runner.invoke(app, ["rules", "--help"])

    assert result.exit_code == 2, result.output
    assert "No such command" in result.output
