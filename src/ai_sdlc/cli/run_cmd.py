"""Read-only ``ai-sdlc run`` routing for the five delivery Loops."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console

from ai_sdlc.cli.loop_cmd import get_review_aware_loop_status
from ai_sdlc.core.loop_router import (
    LoopRouteResult,
    LoopRouteStatus,
    route_five_loops,
)
from ai_sdlc.rules import (
    NormalPathRuleContext,
    RuleContextError,
    RulesLoader,
    supported_decision_capabilities,
)
from ai_sdlc.utils.helpers import find_project_root

console = Console()


def run_command(
    mode: str = typer.Option("auto", help="Legacy mode; only auto is supported."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Read the same five-Loop route without executing legacy stages.",
    ),
    acknowledge_execute_batch: bool = typer.Option(
        False,
        "--acknowledge-execute-batch",
        help="Legacy option retained only to report its Loop migration.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Legacy acknowledgement confirmation; does not enable execution.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Emit the five-Loop route and bounded applicable rules as JSON.",
    ),
) -> None:
    """Show Result, Next and Blockers from the current five-Loop truth."""

    del dry_run, yes
    root = find_project_root()
    if root is None:
        result = LoopRouteResult(
            status=LoopRouteStatus.BLOCKED,
            result="Project is not initialized.",
            next_action="Run ai-sdlc init .",
            blockers=[".ai-sdlc is missing."],
        )
        _render(result, as_json=as_json)
        raise typer.Exit(code=1)

    migration_blocker = _legacy_option_blocker(
        mode=mode,
        acknowledge_execute_batch=acknowledge_execute_batch,
    )
    if migration_blocker:
        result = LoopRouteResult(
            status=LoopRouteStatus.NEEDS_USER,
            result="Legacy pipeline execution is retired from ai-sdlc run.",
            next_action="Continue through the current explicit Loop command.",
            blockers=[migration_blocker],
        )
        _render(result, as_json=as_json)
        raise typer.Exit(code=1)

    result = route_five_loops(root, status_loader=_review_aware_status)
    _render(result, as_json=as_json)
    if result.status == LoopRouteStatus.BLOCKED:
        raise typer.Exit(code=1)


def _review_aware_status(root: Path, loop_type: str):
    return get_review_aware_loop_status(root, loop_type)


def _legacy_option_blocker(*, mode: str, acknowledge_execute_batch: bool) -> str:
    normalized_mode = mode.strip().lower()
    if acknowledge_execute_batch:
        return (
            "--acknowledge-execute-batch belonged to the retired seven-stage runner; "
            "record progress in the current Implementation Loop instead."
        )
    if normalized_mode != "auto":
        return (
            f"--mode {mode} belonged to the retired seven-stage runner; "
            "five-Loop routing is read-only and does not prompt between stages."
        )
    return ""


def _render(result: LoopRouteResult, *, as_json: bool = False) -> None:
    rule_context, rule_error = _normal_path_rules(result)
    if as_json:
        payload = result.model_dump(mode="json")
        payload["supported_decision_capabilities"] = supported_decision_capabilities()
        payload["applicable_rules"] = [
            {
                "name": excerpt.name,
                "title": excerpt.title,
                "content": excerpt.content,
            }
            for excerpt in rule_context.excerpts
        ]
        payload["rule_context_error"] = rule_error
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return

    console.print(f"[bold]当前结果 / Result:[/bold] {result.result}")
    if result.current_loop is not None:
        console.print(
            "[bold]Loop:[/bold] "
            f"{result.current_loop.loop_type} / {result.current_loop.loop_id} "
            f"({result.current_loop.status})"
        )
    console.print(f"[bold]下一步 / Next:[/bold] {result.next_action or 'None'}")
    console.print("[bold]阻断项 / Blockers:[/bold]")
    if result.blockers:
        for blocker in result.blockers:
            console.print(f"- {blocker}", markup=False)
    else:
        console.print("- None")
    if rule_context.excerpts:
        console.print("[bold]适用规则 / Applicable Rules:[/bold]")
        for excerpt in rule_context.excerpts:
            console.print(f"- {excerpt.name}: {excerpt.title}", markup=False)
            console.print(excerpt.content, markup=False)
    elif rule_error:
        console.print("[bold]适用规则 / Applicable Rules:[/bold] unavailable")
        console.print(f"- {rule_error}", markup=False)


def _normal_path_rules(
    result: LoopRouteResult,
) -> tuple[NormalPathRuleContext, str | None]:
    current = result.current_loop
    if current is None:
        return NormalPathRuleContext(), None
    try:
        return (
            RulesLoader().get_normal_path_context(
                str(current.loop_type),
                loop_status=str(current.status),
            ),
            None,
        )
    except (FileNotFoundError, OSError, RuleContextError) as exc:
        return NormalPathRuleContext(), f"Built-in rule context is invalid: {exc}"


__all__ = ["run_command"]
