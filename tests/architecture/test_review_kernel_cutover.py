"""Repository-wide cutover contract for the minimal review product."""

from __future__ import annotations

import ast
from pathlib import Path

from typer.testing import CliRunner

from ai_sdlc.cli.main import app

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "ai_sdlc"
_LEGACY_ROOT_MODULES = {
    "design_close_artifact_verification.py",
    "design_close_authority_store.py",
    "design_close_enforce_authority.py",
    "design_close_enforce_evidence.py",
    "design_close_shadow_authority.py",
    "design_scope_authority_transition.py",
    "lean_code_reviewer_authority.py",
    "scope_authority_store.py",
}
_CLOSE_MODULES = (
    "requirement_loop.py",
    "design_contract_loop.py",
    "implementation_loop.py",
    "frontend_evidence_loop.py",
    "pr_review_service.py",
)
_SLIMMING_NON_AUTHORITY_FUNCTIONS = {
    "core/implementation_loop.py": (
        "_close_blockers",
        "close_implementation_loop",
        "_close_implementation_loop_locked",
    ),
    "core/pr_review_service.py": (
        "close_pr_review",
        "commit_pr_review",
    ),
    "cli/pr_review_cmd.py": (
        "pr_review_close",
        "pr_review_commit",
    ),
}


def _retained_python_paths() -> list[Path]:
    paths: list[Path] = []
    for path in _SRC.rglob("*.py"):
        relative = path.relative_to(_SRC)
        if relative.parts[:2] == ("core", "stage_review"):
            continue
        if relative.parts[:1] == ("core",) and (
            relative.name.startswith("lean_code")
            or relative.name in _LEGACY_ROOT_MODULES
        ):
            continue
        paths.append(path)
    return paths


def test_retained_runtime_has_no_legacy_review_imports_or_close_authority() -> None:
    violations: list[str] = []
    for path in _retained_python_paths():
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            if any(
                module.startswith("ai_sdlc.core.stage_review")
                or module.startswith("ai_sdlc.core.lean_code")
                for module in modules
            ):
                violations.append(str(path.relative_to(_ROOT)))
    assert violations == []

    for name in _CLOSE_MODULES:
        text = (_SRC / "core" / name).read_text(encoding="utf-8")
        assert "execute_stage_close" not in text


def test_retired_public_commands_and_workflows_are_absent() -> None:
    runner = CliRunner()
    root_help = runner.invoke(app, ["--help"])
    verify_help = runner.invoke(app, ["verify", "--help"])
    implementation_help = runner.invoke(app, ["loop", "implementation", "--help"])
    pr_review_help = runner.invoke(app, ["pr-review", "--help"])

    assert root_help.exit_code == 0
    assert verify_help.exit_code == 0
    assert implementation_help.exit_code == 0
    assert pr_review_help.exit_code == 0
    combined = "\n".join(
        (
            root_help.output,
            verify_help.output,
            implementation_help.output,
            pr_review_help.output,
        )
    )
    for retired in (
        "activation",
        "stage-certificate",
        "lean-check",
        "lean-verify",
        "lean-regression",
        "lean-no-go",
        "attest",
    ):
        assert retired not in combined

    for workflow in (
        "activation-evidence.yml",
        "ci-certificate.yml",
        "reviewer-isolation.yml",
    ):
        assert not (_ROOT / ".github" / "workflows" / workflow).exists()


def test_constraint_gate_does_not_turn_comment_deletion_into_authority() -> None:
    constraints = (_SRC / "core" / "verify_constraints.py").read_text(encoding="utf-8")

    assert "collect_comment_deletion_blockers" not in constraints


def test_run_routes_only_to_five_loop_truth() -> None:
    run_command = (_SRC / "cli" / "run_cmd.py").read_text(encoding="utf-8")
    tree = ast.parse(run_command)
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert "SDLCRunner" not in run_command
    assert not any(
        module.startswith(
            (
                "ai_sdlc.core.runner",
                "ai_sdlc.core.executor",
                "ai_sdlc.core.agentops_bridge",
                "ai_sdlc.telemetry",
            )
        )
        for module in imported
    )
    assert "route_five_loops" in run_command


def test_review_values_cannot_become_a_persisted_authority() -> None:
    kernel = (_SRC / "core" / "review_kernel.py").read_text(encoding="utf-8")
    tree = ast.parse(kernel)
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        token in module
        for module in imported
        for token in (
            "store",
            "pointer",
            "workflow",
            "subprocess",
            "urllib",
            "requests",
        )
    )

    field_names = {
        target.id
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        for statement in node.body
        if isinstance(statement, ast.AnnAssign)
        for target in (statement.target,)
        if isinstance(target, ast.Name)
    }
    assert field_names.isdisjoint(
        {"verdict", "passed", "closed", "certificate", "session", "quorum", "score"}
    )


def test_slimming_never_participates_in_status_close_or_commit_decisions() -> None:
    assert (
        "slimming"
        not in (_SRC / "core" / "loop_status.py").read_text(encoding="utf-8").lower()
    )

    for module_name, function_names in _SLIMMING_NON_AUTHORITY_FUNCTIONS.items():
        tree = ast.parse((_SRC / module_name).read_text(encoding="utf-8"))
        functions = {
            node.name: ast.unparse(node).lower()
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for function_name in function_names:
            assert function_name in functions
            assert "slimming" not in functions[function_name]


def test_generic_normal_path_guidance_has_no_fixed_product_stack_or_self_release() -> (
    None
):
    paths = (
        _ROOT / "AGENTS.md",
        _SRC / "adapters" / "codex" / "AI-SDLC.md",
        _SRC / "adapters" / "claude_code" / "AI-SDLC.md",
        _SRC / "adapters" / "cursor" / "rules" / "ai-sdlc.md",
        _SRC / "adapters" / "vscode" / "AI-SDLC.md",
        _SRC / "rules" / "pipeline.md",
    )
    forbidden = (
        "public-primevue",
        "enterprise-vue2",
        "primevue + @primeuix/themes",
        "stage show <阶段名>",
        "竞赛",
        "比赛",
    )

    for path in paths:
        text = path.read_text(encoding="utf-8")
        if path == _ROOT / "AGENTS.md":
            text = text.partition("## Local Repository PR Protocol")[0]
        lowered = text.lower()
        assert all(token not in lowered for token in forbidden), path
        assert "Applicable Rules" in text, path
