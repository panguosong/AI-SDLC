"""Environment diagnostics for PATH / venv issues."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ai_sdlc.core.frontend_evidence_loop import doctor_frontend_evidence_provider
from ai_sdlc.core.frontend_evidence_models import FrontendEvidenceDoctorOptions
from ai_sdlc.utils.helpers import find_project_root

console = Console()


def doctor_command() -> None:
    """Print interpreter path, whether `ai-sdlc` is on PATH, and typical shim locations."""
    console.print(f"[bold]Python executable[/bold]: {sys.executable}")
    console.print(f"[bold]sys.prefix[/bold]: {sys.prefix}")

    which = shutil.which("ai-sdlc")
    if which:
        console.print(f"[green]ai-sdlc on PATH[/green]: {which}")
    else:
        console.print(
            "[yellow]ai-sdlc not found on PATH[/yellow] "
            "(activate the venv or use the full path to the Scripts/bin shim)."
        )

    if sys.platform == "win32":
        shim = Path(sys.prefix) / "Scripts" / "ai-sdlc.exe"
        console.print(f"[dim]Typical Windows shim for this interpreter:[/dim] {shim}")
    else:
        shim = Path(sys.prefix) / "bin" / "ai-sdlc"
        console.print(f"[dim]Typical Unix shim for this interpreter:[/dim] {shim}")

    console.print(
        "\n[bold]Fallback[/bold]: run subcommands via "
        "[cyan]python -m ai_sdlc[/cyan] (same as [cyan]ai-sdlc[/cyan])."
    )
    console.print(
        "[bold]Scope[/bold]: doctor checks the installed command, project adapter, "
        "and browser evidence capability. It does not install dependencies or mutate the project."
    )

    table = Table(title="Environment Diagnostics")
    table.add_column("Check", style="cyan")
    table.add_column("State")
    table.add_column("Detail")
    table.add_row(
        "python",
        "[green]ok[/green]" if Path(sys.executable).is_file() else "[red]error[/red]",
        sys.executable,
    )
    table.add_row(
        "ai-sdlc",
        "[green]ok[/green]" if which else "[yellow]warn[/yellow]",
        which or "use python -m ai_sdlc",
    )
    root = find_project_root()
    browser_detail = "not inside an initialized project"
    browser_state = "unavailable"
    if root is not None:
        browser = doctor_frontend_evidence_provider(
            FrontendEvidenceDoctorOptions(root=root)
        )
        browser_state = str(browser.status)
        browser_detail = browser.result
    table.add_row(
        "browser evidence",
        "[green]ok[/green]" if browser_state == "ready" else "[yellow]warn[/yellow]",
        browser_detail,
    )
    console.print()
    console.print(table)
    console.print()
    console.print("[bold]Plain Diagnostics[/bold]")
    typer.echo(f"python: {'ok' if Path(sys.executable).is_file() else 'error'} | {sys.executable}")
    typer.echo(f"ai-sdlc: {'ok' if which else 'warn'} | {which or 'python -m ai_sdlc'}")
    typer.echo(f"browser evidence: {browser_state} | {browser_detail}")
