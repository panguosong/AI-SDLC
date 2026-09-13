"""Tests for Typer-derived CLI command paths."""

from __future__ import annotations

from ai_sdlc.cli.command_names import collect_flat_command_strings


def test_collect_flat_command_strings_includes_visible_nested_subcommands() -> None:
    cmds = collect_flat_command_strings()
    assert "ai-sdlc adopt" in cmds
    assert "ai-sdlc adapter activate" in cmds
    assert "ai-sdlc adapter select" in cmds
    assert "ai-sdlc adapter status" in cmds
    assert "ai-sdlc verify constraints" in cmds
    assert "ai-sdlc workitem close-check" in cmds
    assert "ai-sdlc workitem init" in cmds
    assert "ai-sdlc workitem plan-check" in cmds
    assert "ai-sdlc workitem truth-check" in cmds
    assert "ai-sdlc loop status" in cmds
    assert "ai-sdlc loop list" in cmds
    assert "ai-sdlc pr-review doctor" in cmds
    assert "ai-sdlc pr-review start" in cmds
    assert "ai-sdlc pr-review status" in cmds
    assert "ai-sdlc pr-review fix" in cmds
    assert "ai-sdlc pr-review rerun" in cmds
    assert "ai-sdlc pr-review close" in cmds
    assert "ai-sdlc agentops doctor" not in cmds
    assert "ai-sdlc gate check" not in cmds
    assert "ai-sdlc provenance summary" not in cmds
    assert len(cmds) > 5
