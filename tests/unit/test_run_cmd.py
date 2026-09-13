"""Unit tests for the minimal read-only run surface."""

from __future__ import annotations

from ai_sdlc.cli import run_cmd
from ai_sdlc.core.loop_models import LoopStatus, LoopType
from ai_sdlc.core.loop_router import (
    LoopRouteItem,
    LoopRouteResult,
    LoopRouteStatus,
)


def test_auto_mode_has_no_legacy_blocker() -> None:
    assert (
        run_cmd._legacy_option_blocker(
            mode="auto",
            acknowledge_execute_batch=False,
        )
        == ""
    )


def test_confirm_mode_and_batch_ack_are_migration_blockers() -> None:
    confirm = run_cmd._legacy_option_blocker(
        mode="confirm",
        acknowledge_execute_batch=False,
    )
    acknowledge = run_cmd._legacy_option_blocker(
        mode="auto",
        acknowledge_execute_batch=True,
    )

    assert "retired seven-stage runner" in confirm
    assert "read-only" in confirm
    assert "retired seven-stage runner" in acknowledge
    assert "Implementation Loop" in acknowledge


def test_render_shows_only_result_next_and_blockers() -> None:
    result = LoopRouteResult(
        status=LoopRouteStatus.ROUTED,
        result="Current delivery Loop.",
        current_loop=LoopRouteItem(
            loop_type=LoopType.REQUIREMENT,
            loop_id="req-1",
            status=LoopStatus.NEEDS_REVIEW,
        ),
        next_action="Run bounded experts.",
        blockers=["review-result-missing"],
    )

    with run_cmd.console.capture() as capture:
        run_cmd._render(result)

    output = capture.get()
    assert "Result: Current delivery Loop." in output
    assert "Next: Run bounded experts." in output
    assert "Blockers:" in output
    assert "review-result-missing" in output
    assert "AgentOps" not in output
    assert "checkpoint" not in output
