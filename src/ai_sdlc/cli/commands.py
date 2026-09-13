"""Simple CLI commands — init, status, recover, index, scan, refresh."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ai_sdlc.branch.git_client import GitClient, GitError
from ai_sdlc.cli.beginner_guidance import render_init_complete_guidance
from ai_sdlc.context.state import (
    CheckpointLoadError,
    ResumePackError,
    load_checkpoint,
    load_execution_plan,
    load_latest_summary,
    load_resume_pack,
    load_runtime_state,
    load_working_set,
    save_checkpoint,
)
from ai_sdlc.core.config import load_project_config, load_project_state
from ai_sdlc.core.frontend_contract_observation_provider import (
    load_frontend_contract_observation_artifact,
)
from ai_sdlc.core.frontend_contract_observation_runtime_policy import (
    classify_frontend_contract_observation_source,
)
from ai_sdlc.core.handoff import check_handoff
from ai_sdlc.core.p1_artifacts import (
    load_execution_path,
    load_latest_reviewer_decision,
    load_parallel_coordination_artifact,
    load_resume_point,
)
from ai_sdlc.core.reconcile import (
    ReconcileHint,
    detect_reconcile_hint,
    reconcile_checkpoint,
)
from ai_sdlc.gates.governance_guard import load_governance_state
from ai_sdlc.generators.index_gen import (
    generate_all_extended_indexes,
    generate_index,
    save_index,
)
from ai_sdlc.integrations.agent_target import (
    agent_target_label,
    interactive_select_agent_target,
    interactive_select_preferred_shell,
    is_interactive_terminal,
    preferred_shell_label,
    recommended_shell_for_platform,
)
from ai_sdlc.integrations.ide_adapter import (
    IDEKind,
    build_adapter_governance_surface,
    detect_ide,
    ensure_ide_adaptation,
)
from ai_sdlc.knowledge.engine import apply_refresh, compute_refresh_level, load_baseline
from ai_sdlc.models.project import PreferredShell, ProjectStatus
from ai_sdlc.models.state import Checkpoint, CompletedStage
from ai_sdlc.routers.bootstrap import (
    EXISTING_INITIALIZED,
    EXISTING_UNINITIALIZED,
    detect_project_state,
    init_project,
)
from ai_sdlc.routers.existing_project_init import run_full_scan
from ai_sdlc.scanners.frontend_contract_scanner import (
    write_frontend_contract_scanner_artifact,
)
from ai_sdlc.utils.helpers import AI_SDLC_DIR, find_project_root, now_iso

console = Console()


def _dedupe_status_text_items(values: object) -> list[str]:
    deduped: list[str] = []
    for value in values or []:
        normalized = str(value).strip()
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return deduped


class InitSafeRehearsalError(RuntimeError):
    """Raised when init's automatic dry-run crashes instead of reaching gates."""


def _run_init_safe_rehearsal(root: Path) -> tuple[bool, list[str], bool]:
    """Run the current project and five-Loop entry checks without writes."""
    constitution = root / AI_SDLC_DIR / "memory" / "constitution.md"
    if not constitution.is_file():
        return False, [f"missing required file: {constitution.relative_to(root)}"], False
    return True, [], True


def _run_init_safe_rehearsal_or_exit(root: Path) -> tuple[bool, list[str], bool]:
    try:
        return _run_init_safe_rehearsal(root)
    except InitSafeRehearsalError as exc:
        console.print(
            Panel(
                "[bold]当前结果 / Result[/bold]\n"
                "  初始化未完成：自动安全预演运行失败。\n"
                "  Initialization is not complete: the automatic safe rehearsal failed.\n\n"
                "[bold]下一步 / Next[/bold]\n"
                "  [cyan]ai-sdlc doctor[/cyan]\n"
                "  先检查本机运行环境；修复错误后重新执行 init。\n"
                "  Check the local runtime first; rerun init after fixing the error.\n\n"
                "[bold]错误 / Error[/bold]\n"
                f"  {exc}",
                title="ai-sdlc init",
                border_style="red",
            )
        )
        raise typer.Exit(code=1) from exc


def _ensure_init_checkpoint_completed(root: Path, *, init_gate_passed: bool) -> None:
    """Align init's project-state success with the stage checkpoint ledger."""
    cp = load_checkpoint(root)
    if cp is None:
        return

    changed = False
    if init_gate_passed and not any(
        stage.stage == "init" for stage in cp.completed_stages
    ):
        cp.completed_stages.append(
            CompletedStage(stage="init", completed_at=now_iso())
        )
        changed = True
    if not init_gate_passed:
        original_count = len(cp.completed_stages)
        cp.completed_stages = [
            stage for stage in cp.completed_stages if stage.stage != "init"
        ]
        changed = len(cp.completed_stages) != original_count
    if changed:
        save_checkpoint(root, cp)


def _is_interactive_terminal() -> bool:
    return is_interactive_terminal()


def _surface_work_item_id(cp: Checkpoint | None) -> str | None:
    if cp is None:
        return None
    if cp.linked_wi_id:
        return cp.linked_wi_id
    if cp.feature and cp.feature.id:
        return cp.feature.id
    return None


def _live_current_branch(root: Path, checkpoint: Checkpoint | None) -> str:
    try:
        return GitClient(root).current_branch().strip()
    except GitError:
        if checkpoint is not None and checkpoint.feature is not None:
            return str(checkpoint.feature.current_branch or "").strip()
        return ""


def _print_reconcile_guidance(
    hint: ReconcileHint,
    *,
    current_command: str,
    blocking: bool,
) -> None:
    status_word = "已暂停" if blocking else "检测到"
    console.print(
        Panel(
            (f"{status_word}已有产物与 checkpoint 可能不一致。\n{hint.reason}"),
            title=f"{current_command} 状态诊断",
            border_style="yellow",
        )
    )

    table = _property_table("Existing Artifact Probe")
    table.add_row("Artifact Layout", hint.layout)
    table.add_row(
        "Detected Files", ", ".join(_dedupe_status_text_items(hint.detected_files))
    )
    table.add_row("Checkpoint Stage", hint.checkpoint_stage)
    table.add_row("Checkpoint Feature", hint.checkpoint_feature_id)
    table.add_row("Suggested Stage", hint.current_stage)
    table.add_row("Suggested Spec Dir", hint.spec_dir)
    table.add_row("Suggested Feature ID", hint.feature_id)
    console.print(table)
    console.print("[bold]下一步你可以：[/bold]")
    console.print("  1. [cyan]ai-sdlc recover --reconcile[/cyan] 进行状态对齐")
    console.print("  2. [cyan]ai-sdlc status[/cyan] 查看当前 checkpoint")
    console.print("  3. [cyan]ai-sdlc run --dry-run[/cyan] 在对齐后预演流水线")


def _latest_summary_preview(summary: str) -> str:
    for line in summary.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        return candidate
    return "present"


def _print_resume_pack_event(message: str) -> None:
    console.print(f"[yellow]{message}[/yellow]")


def _add_optional_row(table: Table, title: str, value: object) -> bool:
    text = str(value).strip()
    if not text:
        return False
    table.add_row(title, text)
    return True


def _property_table(title: str) -> Table:
    table = Table(title=title)
    table.add_column("Property", style="cyan")
    table.add_column("Value")
    return table


def _add_state_detail_rows(
    table: Table,
    *,
    title: str,
    state: object,
    detail_title: str,
    detail: object,
) -> None:
    table.add_row(title, str(state))
    table.add_row(detail_title, str(detail))


def _add_governance_rows(table: Table, governance: Any) -> None:
    table.add_row("Governance Frozen", "yes" if governance.frozen else "no")
    if governance.frozen_at:
        table.add_row("Governance Frozen At", governance.frozen_at)


def _add_branch_context_rows(
    table: Table,
    *,
    current_branch: str | None,
    docs_baseline_ref: str | None,
    docs_baseline_at: str | None,
) -> None:
    if current_branch:
        table.add_row("Current Branch", current_branch)
    if docs_baseline_ref:
        table.add_row("Docs Baseline", docs_baseline_ref)
    if docs_baseline_at:
        table.add_row("Docs Baseline At", docs_baseline_at)


def _add_reconcile_rows(table: Table, hint: ReconcileHint) -> None:
    table.add_row("Reconciled Stage", hint.current_stage)
    table.add_row("Reconciled Spec Dir", hint.spec_dir)
    table.add_row(
        "Detected Files", ", ".join(_dedupe_status_text_items(hint.detected_files))
    )


def _add_working_set_snapshot_rows(table: Table, snapshot: Any) -> None:
    if snapshot.prd_path:
        table.add_row("PRD", snapshot.prd_path)
    if snapshot.spec_path:
        table.add_row("Spec", snapshot.spec_path)
    if snapshot.plan_path:
        table.add_row("Plan", snapshot.plan_path)
    if snapshot.context_summary:
        table.add_row("Context Summary", snapshot.context_summary)


def _add_handoff_status_rows(table: Table, root: Path) -> None:
    handoff = check_handoff(root)
    table.add_row("Continuity Handoff", handoff.state)
    table.add_row("Continuity Handoff Path", str(handoff.path))
    if handoff.summary:
        table.add_row("Continuity Handoff Summary", handoff.summary)
    if handoff.next_steps:
        table.add_row("Continuity Handoff Next", handoff.next_steps[0])


def _add_adapter_governance_rows(
    table: Table, adapter_governance: dict[str, Any]
) -> None:
    table.add_row("Agent Target", str(adapter_governance["agent_target"] or "-"))
    table.add_row(
        "Preferred Shell",
        str(adapter_governance.get("preferred_shell") or "-"),
    )
    if adapter_governance.get("preferred_shell_migration_hint"):
        table.add_row(
            "Shell Migration",
            str(adapter_governance["preferred_shell_migration_hint"]),
        )
    table.add_row(
        "Ingress State",
        str(adapter_governance["adapter_ingress_state"] or "-"),
    )
    table.add_row(
        "Verification Result",
        str(adapter_governance["adapter_verification_result"] or "-"),
    )
    table.add_row(
        "Canonical Path",
        str(adapter_governance["adapter_canonical_path"] or "-"),
    )
    table.add_row(
        "Activation State",
        str(adapter_governance["adapter_activation_state"] or "-"),
    )
    table.add_row(
        "Governance Activation",
        str(adapter_governance["governance_activation_mode"]).replace("_", " "),
    )
    table.add_row(
        "Governance Detail",
        str(adapter_governance["governance_activation_detail"]),
    )


def _load_resume_pack_or_exit(root: Path, *, refreshed_notice: str) -> Any:
    try:
        resume_events: list[str] = []
        pack = load_resume_pack(
            root,
            observer=_print_resume_pack_event,
            event_log=resume_events,
        )
    except ResumePackError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    except CheckpointLoadError as exc:
        console.print(f"[red]Invalid checkpoint: {exc}[/red]")
        raise typer.Exit(code=1) from None
    if resume_events:
        console.print(f"[yellow]{refreshed_notice}[/yellow]")
    return pack


def _add_checkpoint_progress_rows(
    table: Table,
    *,
    checkpoint: Checkpoint,
    resume_pack: Any,
    show_active_binding: bool = True,
    current_branch_override: str | None = None,
) -> None:
    table.add_row("Pipeline Stage", checkpoint.current_stage)
    table.add_row("Execution Mode", checkpoint.execution_mode)
    table.add_row("AI Decisions", str(checkpoint.ai_decisions_count))
    completed = (
        ", ".join(
            _dedupe_status_text_items(s.stage for s in checkpoint.completed_stages)
        )
        or "none"
    )
    table.add_row("Completed Stages", completed)
    display_current_branch = current_branch_override
    if not display_current_branch and checkpoint.feature is not None:
        display_current_branch = checkpoint.feature.current_branch
    if checkpoint.feature and show_active_binding:
        table.add_row("Feature ID", checkpoint.feature.id)
        _add_branch_context_rows(
            table,
            current_branch=display_current_branch,
            docs_baseline_ref=checkpoint.feature.docs_baseline_ref,
            docs_baseline_at=checkpoint.feature.docs_baseline_at,
        )
    elif display_current_branch:
        _add_branch_context_rows(
            table,
            current_branch=display_current_branch,
            docs_baseline_ref=None,
            docs_baseline_at=None,
        )
    if checkpoint.execute_progress:
        progress = checkpoint.execute_progress
        table.add_row(
            "Execute Progress",
            f"Batch {progress.current_batch}/{progress.total_batches}",
        )
    if resume_pack is not None and resume_pack.current_batch:
        table.add_row("Resume Batch", str(resume_pack.current_batch))
    if resume_pack is not None and resume_pack.last_committed_task:
        table.add_row("Resume Last Task", resume_pack.last_committed_task)
    if show_active_binding and checkpoint.linked_wi_id:
        table.add_row("Linked WI ID", checkpoint.linked_wi_id)
    if checkpoint.linked_plan_uri:
        table.add_row("Linked plan URI", checkpoint.linked_plan_uri)
    if checkpoint.last_synced_at:
        table.add_row("Last synced (plan)", checkpoint.last_synced_at)


def _add_active_work_item_status_rows(
    table: Table,
    *,
    root: Path,
    active_work_item: str,
) -> None:
    execution_plan = load_execution_plan(root, active_work_item)
    runtime = load_runtime_state(root, active_work_item)
    working_set = load_working_set(root, active_work_item)
    latest_summary = load_latest_summary(root, active_work_item)
    if execution_plan is not None:
        table.add_row(
            "Execution Plan",
            f"{execution_plan.total_tasks} tasks / {execution_plan.total_batches} batches",
        )
    if runtime is not None:
        if runtime.current_task:
            table.add_row("Runtime Task", runtime.current_task)
        if runtime.last_updated:
            table.add_row("Runtime Updated", runtime.last_updated)
    if working_set is not None and working_set.active_files:
        table.add_row(
            "Active Files",
            ", ".join(_dedupe_status_text_items(working_set.active_files)),
        )
    if latest_summary:
        table.add_row("Latest Summary", _latest_summary_preview(latest_summary))
    reviewer_decision = load_latest_reviewer_decision(root, active_work_item)
    if reviewer_decision is not None:
        status_view = reviewer_decision.to_status_view()
        table.add_row(
            "Latest Reviewer Decision",
            f"{status_view['summary']} | next: {status_view['next_action']}",
        )

    resume_point = load_resume_point(root, active_work_item)
    if resume_point is not None:
        table.add_row(
            "Resume Point",
            f"{resume_point.stage} / batch {resume_point.batch}",
        )
    execution_path = load_execution_path(root, active_work_item)
    if execution_path is not None and execution_path.ordered_task_ids:
        table.add_row(
            "Execution Path",
            ", ".join(execution_path.ordered_task_ids[:3]),
        )
    coordination = load_parallel_coordination_artifact(root, active_work_item)
    if coordination is not None:
        table.add_row(
            "Parallel Coordination",
            f"{coordination.worker_count} workers",
        )
        if coordination.merge_order:
            table.add_row(
                "Parallel Merge Order",
                ", ".join(coordination.merge_order),
            )


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def init_command(
    path: str = typer.Argument(".", help="Project directory to initialize."),
    agent_target: IDEKind | None = typer.Option(
        None,
        "--agent-target",
        help="Explicit IDE/agent target to install instead of auto-detection.",
    ),
    shell: PreferredShell | None = typer.Option(
        None,
        "--shell",
        help="Explicit project shell to persist instead of prompting.",
    ),
) -> None:
    """Initialize AI-SDLC in a project directory.

    For existing projects (with source code but no .ai-sdlc/), this also
    runs a deep project scan and generates the engineering knowledge baseline.
    """
    root = Path(path).resolve()
    if not root.is_dir():
        console.print(f"[red]Error: {root} is not a directory[/red]")
        raise typer.Exit(code=2)

    project_type = detect_project_state(root)
    if project_type == EXISTING_INITIALIZED:
        ensure_ide_adaptation(root, agent_target=agent_target)
        adapter_payload = build_adapter_governance_surface(
            root,
            detected_ide=detect_ide(root),
        )
        (
            dry_run_passed,
            open_reasons,
            init_gate_passed,
        ) = _run_init_safe_rehearsal_or_exit(root)
        _ensure_init_checkpoint_completed(root, init_gate_passed=init_gate_passed)
        console.print(
            Panel(
                f"Project already initialized at [bold]{root}[/bold]"
                "\n\n"
                + render_init_complete_guidance(
                    adapter_payload=adapter_payload,
                    dry_run_passed=dry_run_passed,
                    open_reasons=open_reasons,
                ),
                title="ai-sdlc init",
                border_style="yellow",
            )
        )
        raise typer.Exit(code=0)

    is_existing = project_type == EXISTING_UNINITIALIZED
    if is_existing:
        console.print("[bold]Detected existing project — running deep scan...[/bold]")

    target_note = ""
    selected_target = agent_target
    if selected_target is None:
        detected_target = detect_ide(root)
        if _is_interactive_terminal():
            selected_target = interactive_select_agent_target(detected_target)
            target_note = (
                f"[dim]AI 代理入口: {agent_target_label(selected_target)} "
                f"(detected default: {agent_target_label(detected_target)})[/dim]"
            )
        else:
            selected_target = detected_target
            target_note = (
                f"[dim]AI 代理入口: {agent_target_label(selected_target)} "
                "(non-interactive fallback)[/dim]"
            )
    else:
        target_note = (
            f"[dim]AI 代理入口: {agent_target_label(selected_target)} "
            "(explicit override)[/dim]"
        )

    default_shell = recommended_shell_for_platform()
    if shell is not None:
        selected_shell = shell
        shell_note = (
            f"[dim]Project shell: {preferred_shell_label(selected_shell)} "
            "(explicit override)[/dim]"
        )
    elif _is_interactive_terminal():
        selected_shell = interactive_select_preferred_shell(default_shell)
        shell_note = (
            f"[dim]Project shell: {preferred_shell_label(selected_shell)} "
            f"(recommended default: {preferred_shell_label(default_shell)})[/dim]"
        )
    else:
        selected_shell = default_shell
        shell_note = (
            f"[dim]Project shell: {preferred_shell_label(selected_shell)} "
            "(non-interactive default)[/dim]"
        )

    state = init_project(
        root,
        agent_target=selected_target.value if selected_target else None,
        preferred_shell=selected_shell.value,
    )
    cfg = load_project_config(root)
    if target_note:
        console.print(target_note)
    console.print(shell_note)

    info = (
        f"[green]Initialized AI-SDLC project[/green]\n"
        f"  Name: [bold]{state.project_name}[/bold]\n"
        f"  Type: {project_type}\n"
        f"  Path: {root / '.ai-sdlc'}"
    )
    if cfg.agent_target:
        info += f"\n  Agent Target: {cfg.agent_target}"
    if cfg.detected_ide and cfg.detected_ide != cfg.agent_target:
        info += f"\n  Detected Host: {cfg.detected_ide}"
    if is_existing:
        info += "\n  [dim]Knowledge baseline generated (corpus + indexes)[/dim]"
    adapter_payload = build_adapter_governance_surface(
        root,
        detected_ide=detect_ide(root),
    )
    (
        dry_run_passed,
        open_reasons,
        init_gate_passed,
    ) = _run_init_safe_rehearsal_or_exit(root)
    _ensure_init_checkpoint_completed(root, init_gate_passed=init_gate_passed)
    info += "\n\n" + render_init_complete_guidance(
        adapter_payload=adapter_payload,
        dry_run_passed=dry_run_passed,
        open_reasons=open_reasons,
    )

    console.print(Panel(info, title="ai-sdlc init", border_style="green"))
    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def status_command(
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Machine-readable detailed project status.",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Show the legacy detailed diagnostic status surface.",
    ),
) -> None:
    """Show the current delivery Result, Next action and Blockers."""
    root = find_project_root()
    if root is None:
        console.print(
            "[red]Not inside an AI-SDLC project. Run 'ai-sdlc init' first.[/red]"
        )
        raise typer.Exit(code=1)

    if as_json and details:
        console.print("[red]--details cannot be combined with --json.[/red]")
        raise typer.Exit(code=2)

    state = load_project_state(root)
    if state.status == ProjectStatus.UNINITIALIZED:
        console.print("[yellow]Project found but not initialized.[/yellow]")
        raise typer.Exit(code=1)

    from ai_sdlc.cli.loop_cmd import get_review_aware_loop_status
    from ai_sdlc.core.loop_router import LoopRouteStatus, route_five_loops

    route = route_five_loops(root, status_loader=get_review_aware_loop_status)
    adapter_surface = build_adapter_governance_surface(root)
    handoff = check_handoff(root)
    frontend_solution = (
        root
        / AI_SDLC_DIR
        / "memory"
        / "frontend-delivery"
        / "solution"
        / "latest.yaml"
    )
    frontend_apply = frontend_solution.parent.parent / "apply" / "latest.yaml"
    frontend_browser = frontend_solution.parent.parent / "browser" / "latest.yaml"
    status_payload = {
        "schema_version": "project-status/v2",
        "project": {
            "name": state.project_name,
            "status": state.status.value,
            "version": state.version,
            "next_work_item_seq": state.next_work_item_seq,
        },
        "adapter": adapter_surface,
        "five_loops": {
            "status": str(getattr(route.status, "value", route.status)),
            "result": route.result,
            "next_action": route.next_action,
            "blockers": list(route.blockers),
        },
        "frontend_delivery": {
            "solution_confirmed": frontend_solution.is_file(),
            "apply_available": frontend_apply.is_file(),
            "browser_evidence_available": frontend_browser.is_file(),
        },
        "handoff": {
            "state": handoff.state,
            "path": handoff.path.as_posix(),
            "next_steps": list(handoff.next_steps),
        },
    }
    if as_json:
        typer.echo(json.dumps(status_payload, indent=2, ensure_ascii=False))
        raise typer.Exit(code=0)
    if details:
        table = _property_table("AI-SDLC Status")
        table.add_row("Project", state.project_name)
        table.add_row("Status", state.status.value)
        table.add_row("Version", state.version)
        table.add_row("Adapter", str(adapter_surface))
        table.add_row("Five Loops", str(status_payload["five_loops"]["status"]))
        table.add_row("Result", route.result)
        table.add_row("Next", route.next_action or "None")
        table.add_row("Frontend Solution", "yes" if frontend_solution.is_file() else "no")
        table.add_row("Frontend Apply", "yes" if frontend_apply.is_file() else "no")
        table.add_row("Browser Evidence", "yes" if frontend_browser.is_file() else "no")
        table.add_row("Continuity Handoff", handoff.state)
        console.print(table)
        raise typer.Exit(code=0)

    console.print(f"[bold]当前结果 / Result:[/bold] {route.result}")
    console.print(f"[bold]下一步 / Next:[/bold] {route.next_action or 'None'}")
    console.print("[bold]阻断项 / Blockers:[/bold]")
    if route.blockers:
        for blocker in route.blockers:
            console.print(f"- {blocker}", markup=False)
    else:
        console.print("- None")
    raise typer.Exit(code=1 if route.status == LoopRouteStatus.BLOCKED else 0)


# ---------------------------------------------------------------------------
# recover
# ---------------------------------------------------------------------------


def recover_command(
    reconcile: bool = typer.Option(
        False,
        "--reconcile",
        help="Detect existing artifacts and reconcile stale checkpoint state before recovering.",
    ),
) -> None:
    """Recover pipeline state from last checkpoint."""
    root = find_project_root()
    if root is None:
        console.print("[red]Not inside an AI-SDLC project.[/red]")
        raise typer.Exit(code=1)

    hint = detect_reconcile_hint(root)
    if hint is not None and not reconcile:
        _print_reconcile_guidance(
            hint,
            current_command="ai-sdlc recover",
            blocking=False,
        )
        if _is_interactive_terminal():
            reconcile = typer.confirm(
                "检测到已有产物并怀疑 checkpoint 已过时。是否现在执行 reconcile？",
                default=True,
            )
        if not reconcile:
            console.print(
                "[yellow]已停止当前恢复，建议先执行 `ai-sdlc recover --reconcile`。[/yellow]"
            )
            raise typer.Exit(code=1)

    if reconcile:
        applied = reconcile_checkpoint(root)
        if applied is not None:
            console.print(
                Panel(
                    (
                        "[green]Checkpoint 已根据现有产物完成对齐。[/green]\n"
                        f"下一阶段：{applied.current_stage}"
                    ),
                    title="ai-sdlc recover --reconcile",
                    border_style="green",
                )
            )
            hint = applied

    pack = _load_resume_pack_or_exit(
        root,
        refreshed_notice="recover continuing with refreshed resume-pack",
    )
    cp = load_checkpoint(root)

    table = _property_table("Recovery Info")
    table.add_row("Resume Stage", pack.current_stage)
    table.add_row("Current Batch", str(pack.current_batch))
    table.add_row("Last Task", pack.last_committed_task or "none")
    table.add_row("Timestamp", pack.timestamp)
    _add_branch_context_rows(
        table,
        current_branch=pack.current_branch,
        docs_baseline_ref=pack.docs_baseline_ref,
        docs_baseline_at=pack.docs_baseline_at,
    )
    if hint is not None:
        _add_reconcile_rows(table, hint)

    _add_working_set_snapshot_rows(table, pack.working_set_snapshot)
    _add_handoff_status_rows(table, root)
    work_item_id = _surface_work_item_id(cp)
    if work_item_id:
        governance = load_governance_state(root, work_item_id)
        if governance is not None:
            _add_governance_rows(table, governance)

    console.print(
        Panel(
            "[green]Pipeline state recovered successfully.[/green]",
            title="ai-sdlc recover",
            border_style="green",
        )
    )
    console.print(table)
    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------


def index_command() -> None:
    """Rebuild project index files."""
    root = find_project_root()
    if root is None:
        console.print("[red]Not inside an AI-SDLC project.[/red]")
        raise typer.Exit(code=1)

    index = generate_index(root)
    if "error" in index:
        console.print(f"[red]{index['error']}[/red]")
        raise typer.Exit(code=1)

    save_index(root, index)
    scan = run_full_scan(root)
    extended = generate_all_extended_indexes(root, scan)
    file_count = index.get("file_count", 0)
    console.print(
        f"[green]Index rebuilt: {file_count} files indexed, "
        f"{len(extended)} extended indexes refreshed.[/green]"
    )
    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def scan_command(
    path: str = typer.Argument(".", help="Project directory to scan."),
    frontend_contract_spec_dir: str | None = typer.Option(
        None,
        "--frontend-contract-spec-dir",
        help=(
            "Write canonical frontend contract observations into the given spec "
            "directory using the frontend contract scanner candidate."
        ),
    ),
    frontend_contract_generated_at: str | None = typer.Option(
        None,
        "--frontend-contract-generated-at",
        help="Override generated_at for frontend contract export mode.",
    ),
) -> None:
    """Run a deep project scan and display results."""
    root = Path(path).resolve()
    if not root.is_dir():
        console.print(f"[red]Error: {root} is not a directory[/red]")
        raise typer.Exit(code=2)

    if frontend_contract_spec_dir is not None:
        spec_dir = Path(frontend_contract_spec_dir).expanduser().resolve()
        generated_at = frontend_contract_generated_at or now_iso()
        console.print(
            f"[bold]Scanning frontend contract observations at {root}...[/bold]"
        )
        try:
            artifact_path = write_frontend_contract_scanner_artifact(
                root,
                spec_dir,
                generated_at=generated_at,
            )
            artifact = load_frontend_contract_observation_artifact(artifact_path)
        except Exception as exc:
            console.print(f"[red]Frontend contract scan failed: {exc}[/red]")
            raise typer.Exit(code=1) from None

        console.print(
            "[green]Frontend contract observations exported:[/green] "
            f"{len(artifact.observations)} observations -> {artifact_path}"
        )
        console.print(
            "[dim]source profile: "
            f"{classify_frontend_contract_observation_source(artifact)}[/dim]"
        )
        raise typer.Exit(code=0)

    console.print(f"[bold]Scanning project at {root}...[/bold]")
    try:
        scan = run_full_scan(root)
    except Exception as exc:
        console.print(f"[red]Scan failed: {exc}[/red]")
        raise typer.Exit(code=1) from None

    table = Table(title="Scan Results")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    table.add_row("Total Files", str(scan.total_files))
    table.add_row("Total Lines", str(scan.total_lines))
    table.add_row("Languages", str(len(scan.languages)))
    table.add_row("Dependencies", str(len(scan.dependencies)))
    table.add_row("API Endpoints", str(len(scan.api_endpoints)))
    table.add_row("Test Files", str(len(scan.tests)))
    table.add_row("Symbols", str(len(scan.symbols)))
    table.add_row("Risks", str(len(scan.risks)))

    console.print(table)

    if scan.languages:
        lang_table = Table(title="Languages")
        lang_table.add_column("Language")
        lang_table.add_column("Files", justify="right")
        for lang, count in sorted(scan.languages.items(), key=lambda x: -x[1]):
            lang_table.add_row(lang, str(count))
        console.print(lang_table)

    if scan.risks:
        console.print(f"\n[yellow]Risks detected: {len(scan.risks)}[/yellow]")
        risk_lines: list[str] = []
        for risk in scan.risks:
            rendered = (
                f"[{risk.severity}] {risk.category}: {risk.path} — {risk.description}"
            )
            if rendered in risk_lines:
                continue
            risk_lines.append(rendered)
            if len(risk_lines) >= 10:
                break
        for risk_line in risk_lines:
            console.print(f"  {risk_line}")


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


def refresh_command(
    path: str = typer.Argument(".", help="Project directory."),
    work_item_id: str = typer.Option("WI-MANUAL", help="Work item ID for the refresh."),
    files: list[str] = typer.Option(
        [], "--file", "-f", help="Changed files to consider."
    ),
    spec_changed: bool = typer.Option(False, help="Whether spec files changed."),
    force_level: int = typer.Option(
        -1, help="Force a specific refresh level (0-3). -1 = auto."
    ),
) -> None:
    """Compute and apply knowledge refresh."""
    root = Path(path).resolve()

    if (root / AI_SDLC_DIR).is_dir():
        ensure_ide_adaptation(root)

    baseline = load_baseline(root)
    if not baseline.initialized:
        console.print(
            "[red]Knowledge baseline not initialized. Run 'ai-sdlc init' first.[/red]"
        )
        raise typer.Exit(code=1)

    if force_level >= 0:
        from ai_sdlc.models.scanner import RefreshLevel

        try:
            level = RefreshLevel(force_level)
        except ValueError:
            console.print(
                f"[red]Invalid refresh level: {force_level}. Must be 0-3.[/red]"
            )
            raise typer.Exit(code=2) from None
    else:
        level = compute_refresh_level(files, spec_changed=spec_changed)

    console.print(f"[bold]Refresh level: L{level.value}[/bold]")

    if level.value == 0:
        console.print("[green]No refresh needed.[/green]")
        raise typer.Exit(code=0)

    entry = apply_refresh(root, work_item_id, files, level)
    console.print(f"[green]Refresh completed at {entry.completed_at}[/green]")
    console.print(f"  Updated indexes: {len(entry.updated_indexes)}")
    console.print(f"  Updated docs: {len(entry.updated_docs)}")

    updated_baseline = load_baseline(root)
    console.print(
        f"  Baseline: corpus v{updated_baseline.corpus_version}, "
        f"index v{updated_baseline.index_version}, "
        f"refreshes: {updated_baseline.refresh_count}"
    )
