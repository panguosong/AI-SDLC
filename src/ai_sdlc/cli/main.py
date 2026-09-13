"""AI-SDLC CLI entry point."""

import sys

import typer
from rich.console import Console

from ai_sdlc import __version__
from ai_sdlc.cli.adapter_cmd import adapter_app
from ai_sdlc.cli.adopt_cmd import adopt_command
from ai_sdlc.cli.cli_hooks import run_ide_adapter_if_initialized
from ai_sdlc.cli.commands import (
    index_command,
    init_command,
    recover_command,
    refresh_command,
    scan_command,
    status_command,
)
from ai_sdlc.cli.doctor_cmd import doctor_command
from ai_sdlc.cli.handoff_cmd import handoff_app
from ai_sdlc.cli.loop_cmd import loop_app
from ai_sdlc.cli.pr_review_cmd import pr_review_app
from ai_sdlc.cli.run_cmd import run_command
from ai_sdlc.cli.self_update_cmd import (
    consume_update_replay_bypass,
    maybe_render_update_notice,
    self_update_app,
)
from ai_sdlc.cli.verify_cmd import verify_app
from ai_sdlc.cli.workitem_cmd import _WORKITEM_ADAPTER_HOOK_META_KEY, workitem_app

app = typer.Typer(
    name="ai-sdlc",
    help="AI-native SDLC automation framework.",
    no_args_is_help=True,
)

_hook_console = Console()
_READ_ONLY_SUBCOMMANDS = (
    "adapter",
    "init",
    "doctor",
    "handoff",
    "run",
    "status",
    "scan",
    "verify",
    "loop",
    "pr-review",
    "self-update",
)
_UPDATE_NOTICE_BYPASS_SUBCOMMANDS = ("self-update",)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def _global_before_command(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the installed AI-SDLC version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """First non-init command in an initialized project applies IDE adapter."""
    _ = version
    if ctx.invoked_subcommand is None:
        return
    replay_bypass = consume_update_replay_bypass()
    informational_invocation = any(
        option in sys.argv
        for option in (
            "--help",
            "-h",
            "--install-completion",
            "--show-completion",
        )
    )
    if informational_invocation:
        return
    if (
        not replay_bypass
        and ctx.invoked_subcommand not in _UPDATE_NOTICE_BYPASS_SUBCOMMANDS
    ):
        maybe_render_update_notice(machine_output="--json" in sys.argv)
    if ctx.invoked_subcommand == "workitem":
        # 子应用按参数校验与 clean-tree preflight 边界管理 adapter 副作用。
        ctx.meta[_WORKITEM_ADAPTER_HOOK_META_KEY] = run_ide_adapter_if_initialized
        return
    # Read-only and analysis surfaces must not trigger adapter writes.
    if ctx.invoked_subcommand in _READ_ONLY_SUBCOMMANDS:
        return
    run_ide_adapter_if_initialized(console=_hook_console)


app.command(name="init")(init_command)
app.command(name="adopt")(adopt_command)
app.command(name="doctor")(doctor_command)
app.command(name="status")(status_command)
app.command(name="recover")(recover_command)
app.command(name="index", hidden=True)(index_command)
app.command(name="scan", hidden=True)(scan_command)
app.command(name="refresh", hidden=True)(refresh_command)
app.command(name="run")(run_command)
app.add_typer(adapter_app, name="adapter")
app.add_typer(handoff_app, name="handoff", hidden=True)
app.add_typer(workitem_app, name="workitem")
app.add_typer(verify_app, name="verify")
app.add_typer(loop_app, name="loop")
app.add_typer(pr_review_app, name="pr-review")
app.add_typer(self_update_app, name="self-update")

if __name__ == "__main__":
    app()
