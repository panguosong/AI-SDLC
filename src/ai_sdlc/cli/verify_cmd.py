"""Verify subcommands for read-only governance checks."""

from __future__ import annotations

import json

import typer
from rich.console import Console

from ai_sdlc.core.verify_constraints import (
    ConstraintProfile,
    build_constraint_report,
)
from ai_sdlc.utils.helpers import find_project_root

verify_app = typer.Typer(
    help=(
        "Read-only verification. Complements `ai-sdlc doctor` (environment/PATH); "
        "this command checks governance files, checkpoint vs specs tree, and "
        "repo-local framework backlog structure when present."
    ),
)
console = Console()


@verify_app.command(
    "constraints",
    help=(
        "Read-only: required governance files and checkpoint/specs consistency; "
        "when tasks.md exists under feature.spec_dir, task-level acceptance must "
        "match gate decompose (SC-014). Self-development checks are enabled only "
        "with --profile self-development. Does not write project state. "
        "Exit 0 if no BLOCKERs, else 1."
    ),
)
def verify_constraints(
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Machine-readable report on stdout.",
    ),
    profile: ConstraintProfile = typer.Option(
        ConstraintProfile.PROJECT,
        "--profile",
        help="Verification scope: project or self-development.",
        case_sensitive=False,
    ),
) -> None:
    """Validate the constitution, checkpoint spec_dir, and task acceptance criteria."""
    root = find_project_root()
    if root is None:
        msg = "Not inside an AI-SDLC project (.ai-sdlc/ not found)."
        if as_json:
            typer.echo(
                json.dumps(
                    {"ok": False, "error": msg, "blockers": [], "root": None}, indent=2
                )
            )
        else:
            console.print(f"[red]{msg}[/red]")
        raise typer.Exit(code=1)

    report = build_constraint_report(root, profile=profile)
    effective_blockers = list(report.blockers)
    advisories: list[str] = []

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "ok": len(effective_blockers) == 0,
                    "profile": report.profile.value,
                    "blockers": effective_blockers,
                    "advisories": advisories,
                    "root": str(root),
                    "verification_gate": {
                        "name": report.gate_name,
                        "source_name": report.source_name,
                        "profile": report.profile.value,
                        "sources": [report.source_name],
                        "check_objects": list(report.check_objects),
                        "coverage_gaps": list(report.coverage_gaps),
                    },
                    "decision": "block" if effective_blockers else "allow",
                },
                indent=2,
            )
        )
    else:
        if effective_blockers:
            console.print("[bold red]Constraint violations[/bold red]")
            for b in _string_list(effective_blockers):
                console.print(f"  {b}")
        else:
            console.print("[green]verify constraints: no BLOCKERs.[/green]")
            for advisory in _string_list(advisories):
                console.print(f"[yellow]{advisory}[/yellow]")
    raise typer.Exit(code=1 if effective_blockers else 0)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    items: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in items:
            items.append(text)
    return items
