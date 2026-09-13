"""Work item subcommands, including plan-check and close-check."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ai_sdlc.branch.git_client import GitClient, GitError
from ai_sdlc.context.state import load_checkpoint, save_checkpoint
from ai_sdlc.core.close_check import (
    BranchCheckResult,
    CloseCheckResult,
    format_branch_check_json,
    format_close_check_json,
    run_branch_check,
    run_close_check,
)
from ai_sdlc.core.plan_check import PlanCheckResult, format_json, run_plan_check
from ai_sdlc.core.task_guard import evaluate_task_guard
from ai_sdlc.core.workitem_scaffold import WorkitemScaffolder, WorkitemScaffoldError
from ai_sdlc.core.workitem_truth import (
    WorkitemTruthResult,
    format_truth_check_json,
    run_truth_check,
)
from ai_sdlc.utils.helpers import find_project_root, is_git_repo, now_iso

_WORKITEM_ADAPTER_HOOK_META_KEY = "ai_sdlc.cli.workitem.adapter_hook"

workitem_app = typer.Typer(
    help=(
        "Work item canonical docs, plan reconciliation, branch lifecycle truth, "
        "and checkpoint linkage."
    ),
)
console = Console()


def _run_workitem_adapter(ctx: typer.Context) -> None:
    """调用 root 为当前请求注入的 adapter hook。"""
    ctx.meta[_WORKITEM_ADAPTER_HOOK_META_KEY](console=console)


@workitem_app.callback()
def _workitem_before_command(ctx: typer.Context) -> None:
    """由子应用在参数解析后管理 adapter 调用时机。"""
    if ctx.invoked_subcommand in (None, "init"):
        return
    _run_workitem_adapter(ctx)


def _dedupe_cli_text_items(values: object) -> list[str]:
    deduped: list[str] = []
    for value in values or []:
        normalized = str(value).strip()
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return deduped


def _format_cli_path(path: Path) -> str:
    return path.as_posix()


def _render_workitem_init_git_status(root: Path) -> None:
    """Show branch and dirty-tree context without controlling project Git flow."""
    if not is_git_repo(root):
        return

    git = GitClient(root)
    try:
        current_branch = git.current_branch().strip()
        console.print(f"[dim]Git branch: {current_branch or '(detached)'}[/dim]")
        if git.has_uncommitted_changes():
            console.print(
                "[yellow]Git notice: unrelated changes exist; WorkItem init will "
                "only create its canonical draft files.[/yellow]"
            )
    except GitError as exc:
        console.print(f"[yellow]Git status unavailable: {exc}[/yellow]")


@workitem_app.command(
    "init",
    help=(
        "Initialize one project WorkItem draft under specs/<WI>/ "
        "(spec.md + plan.md + tasks.md + task-execution-log.md)."
    ),
)
def workitem_init(
    ctx: typer.Context,
    title: str = typer.Option(
        ...,
        "--title",
        help="Human-readable work item title used in generated formal docs.",
    ),
    wi_id: str | None = typer.Option(
        None,
        "--wi-id",
        help="Optional explicit work item id in `NNN-short-name` form.",
    ),
    input_text: str | None = typer.Option(
        None,
        "--input",
        help="Optional input/brief shown in the generated spec.md.",
    ),
    related_plan: str | None = typer.Option(
        None,
        "--related-plan",
        help="Optional external design/plan reference stored as reference-only metadata.",
    ),
    related_doc: list[str] | None = typer.Option(
        None,
        "--related-doc",
        help="Optional external design/doc reference. Can be provided multiple times.",
    ),
) -> None:
    """Initialize one project WorkItem draft."""
    root = find_project_root()
    if root is None:
        console.print("[red]Not inside an AI-SDLC project.[/red]")
        raise typer.Exit(code=1)

    scaffolder = WorkitemScaffolder()
    try:
        scaffolder.preview_work_item_id(
            root=root,
            title=title,
            wi_id=wi_id,
        )
        _render_workitem_init_git_status(root)
        _run_workitem_adapter(ctx)
        result = scaffolder.scaffold(
            root=root,
            title=title,
            wi_id=wi_id,
            input_text=input_text,
            related_plan=related_plan,
            related_docs=tuple(related_doc or ()),
        )
    except WorkitemScaffoldError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        "[green]Created canonical formal docs under "
        f"{_format_cli_path(result.spec_dir.relative_to(root))}[/green]"
    )
    for path_label in _dedupe_cli_text_items(
        _format_cli_path(path.relative_to(root)) for path in result.created_paths
    ):
        console.print(f"  - {path_label}")
    if related_plan or related_doc:
        console.print(
            "[dim]External design inputs were recorded as references only; "
            "no second canonical doc set was created.[/dim]"
        )


@workitem_app.command(
    "plan-check",
    help="Read-only: compare pending plan todos to Git changes; does not write checkpoint.",
)
def workitem_plan_check(
    wi: Path | None = typer.Option(
        None,
        "--wi",
        help="Path to specs/<WI>/ (reads related_plan from tasks.md or plan.md).",
    ),
    plan: Path | None = typer.Option(
        None,
        "--plan",
        help="Explicit plan Markdown file (YAML frontmatter with todos).",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Machine-readable report (stdout). Does not write checkpoint.",
    ),
) -> None:
    """Check pending plan todos against Git working tree changes.

    Exit 0: no drift detected. Exit 1: drift or internal error. Exit 2: user error.
    """
    if wi is not None and plan is not None:
        console.print("[red]Use either --wi or --plan, not both.[/red]")
        raise typer.Exit(code=2)

    if wi is None and plan is None:
        console.print("[red]Specify --wi <specs/WI-dir> or --plan <file>.[/red]")
        raise typer.Exit(code=2)

    result = run_plan_check(cwd=Path.cwd(), wi=wi, plan=plan)

    if result.error:
        if as_json:
            console.print(format_json(result), soft_wrap=True)
        else:
            console.print(f"[red]{result.error}[/red]")
        raise typer.Exit(code=1)

    if as_json:
        console.print(format_json(result), soft_wrap=True)
    else:
        _print_table(result)

    raise typer.Exit(code=1 if result.drift else 0)


def _print_table(result: PlanCheckResult) -> None:
    table = Table(title="workitem plan-check (read-only)")
    table.add_column("Field", style="cyan")
    table.add_column("Value")

    table.add_row("Plan file", str(result.plan_file) if result.plan_file else "—")
    table.add_row("Pending todos", str(result.pending_todos))
    table.add_row("Changed paths (git)", str(len(result.changed_paths)))
    table.add_row("Drift", "[red]YES[/red]" if result.drift else "[green]NO[/green]")

    console.print(table)
    if result.changed_paths:
        console.print("[dim]Changed:[/dim]")
        changed_paths = _dedupe_cli_text_items(result.changed_paths)
        for p in changed_paths[:50]:
            console.print(f"  - {p}")
        if len(changed_paths) > 50:
            console.print(f"  … ({len(changed_paths) - 50} more)")


@workitem_app.command(
    "guard",
    help="Read-only: show the current next executable task guard state.",
)
def workitem_guard(
    wi: Path | None = typer.Option(
        None,
        "--wi",
        help="Optional specs/<WI>/ directory. Defaults to the active checkpoint.",
    ),
    request_text: str = typer.Option(
        "处理当前用户请求",
        "--request",
        help="Optional user request text used only for a minimal task candidate.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Machine-readable guard report.",
    ),
) -> None:
    """Read-only next executable task guard surface."""
    root = find_project_root()
    if root is None:
        console.print("[red]Not inside an AI-SDLC project.[/red]")
        raise typer.Exit(code=1)

    result = evaluate_task_guard(root=root, wi=wi, request_text=request_text)
    if as_json:
        console.print(
            json.dumps(result.to_json_dict(), ensure_ascii=False, indent=2),
            soft_wrap=True,
        )
    else:
        _print_guard_table(result.to_json_dict())
    raise typer.Exit(code=0 if result.allowed else 1)


def _print_guard_table(payload: dict[str, object]) -> None:
    table = Table(title="workitem guard (read-only)")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row(
        "Status",
        "可以修改代码" if payload.get("allowed") else "请先准备当前任务",
    )
    if payload.get("active_work_item"):
        table.add_row("Work item", str(payload.get("active_work_item")))
    if payload.get("task_id"):
        table.add_row("Next task", str(payload.get("task_id")))
    if payload.get("task_goal"):
        table.add_row("Goal", str(payload.get("task_goal")))
    table.add_row("Detail", str(payload.get("detail", "")))
    console.print(table)
    for action in _dedupe_cli_text_items(payload.get("next_actions", [])):
        console.print(f"  - {action}")


@workitem_app.command(
    "close-check",
    help=(
        "Read-only close-stage checks for a work item: tasks completion, related_plan "
        "drift, and execution-log required fields. Does not write checkpoint."
    ),
)
def workitem_close_check(
    wi: Path = typer.Option(
        ...,
        "--wi",
        help="Path to specs/<WI>/ directory to validate before close/merge.",
    ),
    all_docs: bool = typer.Option(
        False,
        "--all-docs",
        help=(
            "Scan all docs/**/*.md for CLI wording drift (compatibility full scan). "
            "Default: only specs/<WI>/*.md plus docs/pull-request-checklist.zh.md "
            "and USER_GUIDE.zh-CN.md."
        ),
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Machine-readable report (ok/blockers/checks). Read-only.",
    ),
) -> None:
    """Run read-only close checks and exit 0 when no BLOCKERs remain."""
    result = run_close_check(cwd=Path.cwd(), wi=wi, all_docs=all_docs)

    if as_json:
        console.print(format_close_check_json(result), soft_wrap=True)
    else:
        _print_close_table(result)

    if result.error:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0 if result.ok else 1)


def _print_close_table(result: CloseCheckResult) -> None:
    """Render close-check output in a compact table."""
    table = Table(title="workitem close-check (read-only)")
    table.add_column("Check", style="cyan")
    table.add_column("Result")
    table.add_column("Detail")

    for item in result.checks:
        ok = bool(item.get("ok"))
        table.add_row(
            str(item.get("name", "unknown")),
            "[green]PASS[/green]" if ok else "[red]BLOCKER[/red]",
            _format_close_check_detail(item),
        )
    console.print(table)

    branch_lifecycle_next_actions = _extract_branch_lifecycle_next_actions(result.checks)
    if branch_lifecycle_next_actions:
        console.print("[bold yellow]Branch Lifecycle Next Actions[/bold yellow]")
        for action in branch_lifecycle_next_actions:
            console.print(f"  - {action}")

    if result.blockers:
        console.print("[bold red]BLOCKERs[/bold red]")
        for b in _dedupe_cli_text_items(result.blockers):
            console.print(f"  {b}")


def _format_close_check_detail(item: dict[str, object]) -> str:
    return str(item.get("detail", "")).strip()


def _extract_branch_lifecycle_next_actions(checks: list[dict[str, object]]) -> list[str]:
    actions: list[str] = []
    for check in checks:
        if str(check.get("name", "")).strip() != "branch_lifecycle":
            continue
        for action in check.get("next_required_actions", []) or []:
            normalized = str(action).strip()
            if normalized and normalized not in actions:
                actions.append(normalized)
    return actions


@workitem_app.command(
    "branch-check",
    help=(
        "Read-only: report branch/worktree lifecycle drift associated with one work item. "
        "Does not write checkpoint or perform Git cleanup."
    ),
)
def workitem_branch_check(
    wi: Path = typer.Option(
        ...,
        "--wi",
        help="Path to specs/<WI>/ directory to inspect for branch/worktree lifecycle drift.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Machine-readable branch lifecycle report. Read-only.",
    ),
) -> None:
    """Read-only branch lifecycle truth for one work item."""
    result = run_branch_check(cwd=Path.cwd(), wi=wi)

    if as_json:
        console.print(format_branch_check_json(result), soft_wrap=True)
    else:
        _print_branch_table(result)

    if result.error:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0 if result.ok else 1)


def _print_branch_table(result: BranchCheckResult) -> None:
    table = Table(title="workitem branch-check (read-only)")
    table.add_column("Branch", style="cyan")
    table.add_column("Kind")
    table.add_column("Ahead")
    table.add_column("Disposition")
    table.add_column("Status")
    table.add_column("Detail")

    for item in result.entries:
        table.add_row(
            str(item.get("name", "unknown")),
            str(item.get("kind", "")),
            str(item.get("ahead_of_main", "")),
            str(item.get("branch_disposition") or "unknown"),
            str(item.get("status", "")),
            str(item.get("detail", "")),
        )
    console.print(table)

    if result.next_required_actions:
        console.print("[bold yellow]Next Actions[/bold yellow]")
        for action in _dedupe_cli_text_items(result.next_required_actions):
            console.print(f"  - {action}")

    if result.blockers:
        console.print("[bold red]BLOCKERs[/bold red]")
        for blocker in _dedupe_cli_text_items(result.blockers):
            console.print(f"  {blocker}")
    elif result.warnings:
        console.print("[bold yellow]Warnings[/bold yellow]")
        for warning in _dedupe_cli_text_items(result.warnings):
            console.print(f"  {warning}")


@workitem_app.command(
    "truth-check",
    help=(
        "Read-only: classify one work item's execution truth for the current checkout "
        "or a specific git revision."
    ),
)
def workitem_truth_check(
    wi: Path = typer.Option(
        ...,
        "--wi",
        help="Path to specs/<WI>/ directory to inspect.",
    ),
    rev: str | None = typer.Option(
        None,
        "--rev",
        help="Optional git branch / commit / tag to inspect instead of the current checkout.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Machine-readable truth report. Read-only.",
    ),
) -> None:
    """Read-only work item truth classification anchored to one revision."""
    result = run_truth_check(cwd=Path.cwd(), wi=wi, rev=rev)

    if as_json:
        console.print(format_truth_check_json(result), soft_wrap=True)
    else:
        _print_truth_table(result)

    if result.error:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


def _print_truth_table(result: WorkitemTruthResult) -> None:
    table = Table(title="workitem truth-check (read-only)")
    table.add_column("Field", style="cyan")
    table.add_column("Value")

    table.add_row("Work item", str(result.wi_path or "unknown"))
    table.add_row("Requested rev", str(result.requested_revision or "HEAD"))
    table.add_row("Resolved rev", str(result.resolved_revision or "unknown"))
    table.add_row("Current branch", str(result.current_branch or "unknown"))
    table.add_row("Current HEAD", str(result.head_revision or "unknown"))
    table.add_row("HEAD matches rev", "yes" if result.head_matches_revision else "no")
    table.add_row("Classification", str(result.classification or "unknown"))
    table.add_row("Execute started", "yes" if result.execution_started else "no")
    table.add_row("Contained in main", "yes" if result.contained_in_main else "no")
    table.add_row(
        "Ahead / behind main",
        f"{result.ahead_of_main or 0} / {result.behind_of_main or 0}",
    )
    table.add_row(
        "Formal docs",
        ", ".join(
            f"{name}={'yes' if present else 'no'}"
            for name, present in result.formal_docs.items()
        ),
    )
    table.add_row("Detail", str(result.detail or result.error or ""))
    console.print(table)

    if result.code_paths:
        console.print("[dim]Code paths:[/dim]")
        for path in _dedupe_cli_text_items(result.code_paths):
            console.print(f"  - {path}")
    if result.next_required_actions:
        console.print("[dim]Next actions:[/dim]")
        for action in _dedupe_cli_text_items(result.next_required_actions):
            console.print(f"  - {action}")
    if result.test_paths:
        console.print("[dim]Test paths:[/dim]")
        for path in _dedupe_cli_text_items(result.test_paths):
            console.print(f"  - {path}")
    if result.error:
        console.print(f"[bold red]ERROR[/bold red] {result.error}")


@workitem_app.command(
    "link",
    help="Write optional checkpoint linkage fields using an atomic YAML save.",
)
def workitem_link(
    wi_id: str | None = typer.Option(
        None,
        "--wi-id",
        help="Work item id to record (e.g. 001-sample-work-item).",
    ),
    plan_uri: str | None = typer.Option(
        None,
        "--plan-uri",
        help="URI or repo-relative path to an external plan file.",
    ),
    synced_at: str | None = typer.Option(
        None,
        "--synced-at",
        help="ISO-8601 timestamp; defaults to now when setting wi-id or plan-uri.",
    ),
) -> None:
    """Persist linked_wi_id / linked_plan_uri / last_synced_at on checkpoint.yml."""
    if wi_id is None and plan_uri is None and synced_at is None:
        console.print(
            "[red]Provide at least one of --wi-id, --plan-uri, --synced-at.[/red]"
        )
        raise typer.Exit(code=2)

    root = find_project_root()
    if root is None:
        console.print("[red]Not inside an AI-SDLC project.[/red]")
        raise typer.Exit(code=1)

    cp = load_checkpoint(root)
    if cp is None:
        console.print("[red]No checkpoint found. Run the pipeline or create state first.[/red]")
        raise typer.Exit(code=1)

    if wi_id is not None:
        cp.linked_wi_id = wi_id
    if plan_uri is not None:
        cp.linked_plan_uri = plan_uri
    if synced_at is not None:
        cp.last_synced_at = synced_at
    elif wi_id is not None or plan_uri is not None:
        cp.last_synced_at = now_iso()

    save_checkpoint(root, cp)
    console.print("[green]Checkpoint linkage updated.[/green]")
    if cp.linked_wi_id:
        console.print(f"  linked_wi_id: {cp.linked_wi_id}")
    if cp.linked_plan_uri:
        console.print(f"  linked_plan_uri: {cp.linked_plan_uri}")
    if cp.last_synced_at:
        console.print(f"  last_synced_at: {cp.last_synced_at}")
    raise typer.Exit(code=0)
