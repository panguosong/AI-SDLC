#!/usr/bin/env python3
"""Report retired runtime families reachable from retained product entry modules."""

from __future__ import annotations

import argparse
import ast
import json
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "src" / "ai_sdlc"

RETAINED_ENTRY_MODULES = (
    "ai_sdlc.cli.main",
    "ai_sdlc.cli.commands",
    "ai_sdlc.cli.doctor_cmd",
    "ai_sdlc.cli.handoff_cmd",
    "ai_sdlc.cli.loop_cmd",
    "ai_sdlc.cli.loop_review_cmd",
    "ai_sdlc.cli.pr_review_cmd",
    "ai_sdlc.cli.run_cmd",
    "ai_sdlc.cli.self_update_cmd",
    "ai_sdlc.cli.verify_cmd",
    "ai_sdlc.cli.workitem_cmd",
    "ai_sdlc.core.close_check",
    "ai_sdlc.core.design_contract_loop",
    "ai_sdlc.core.frontend_delivery_service",
    "ai_sdlc.core.frontend_evidence_loop",
    "ai_sdlc.core.handoff",
    "ai_sdlc.core.implementation_loop",
    "ai_sdlc.core.pr_review_service",
    "ai_sdlc.core.requirement_loop",
    "ai_sdlc.core.review_kernel",
    "ai_sdlc.core.slimming_advice",
    "ai_sdlc.core.update_advisor",
    "ai_sdlc.core.verify_constraints",
    "packaging_backend",
    "packaging.offline.verify_offline_bundle",
    "scripts.validate_public_release_identity",
)

RETIRED_FAMILIES = {
    "program": (
        "ai_sdlc.cli.program_cmd",
        "ai_sdlc.core.program_service",
        "ai_sdlc.models.program",
    ),
    "telemetry": ("ai_sdlc.telemetry", "ai_sdlc.cli.telemetry_cmd"),
    "provenance": (
        "ai_sdlc.cli.provenance_cmd",
        "ai_sdlc.cli.trace_cmd",
        "ai_sdlc.core.provenance_gate",
    ),
    "agentops": (
        "ai_sdlc.cli.agentops_cmd",
        "ai_sdlc.cli.enterprise_cmd",
        "ai_sdlc.core.agentops_bridge",
    ),
    "studio": ("ai_sdlc.studios",),
    "host-runtime": (
        "ai_sdlc.cli.host_runtime_cmd",
        "ai_sdlc.core.host_runtime_manager",
        "ai_sdlc.models.host_runtime_plan",
    ),
    "seven-stage": (
        "ai_sdlc.cli.stage_cmd",
        "ai_sdlc.core.dispatcher",
        "ai_sdlc.core.executor",
        "ai_sdlc.core.runner",
        "ai_sdlc.backends",
        "ai_sdlc.parallel",
    ),
    "legacy-release-gate": ("ai_sdlc.core.release_gate",),
    "historical-frontend": (
        "ai_sdlc.core.frontend_cross_provider_consistency",
        "ai_sdlc.core.frontend_page_ui_schema",
        "ai_sdlc.core.frontend_provider_expansion",
        "ai_sdlc.core.frontend_provider_runtime_adapter",
        "ai_sdlc.core.frontend_quality_platform",
        "ai_sdlc.core.frontend_theme_token_governance",
        "ai_sdlc.generators.frontend_cross_provider_consistency_artifacts",
        "ai_sdlc.generators.frontend_generation_constraint_artifacts",
        "ai_sdlc.generators.frontend_page_ui_schema_artifacts",
        "ai_sdlc.generators.frontend_provider_expansion_artifacts",
        "ai_sdlc.generators.frontend_provider_profile_artifacts",
        "ai_sdlc.generators.frontend_provider_runtime_adapter_artifacts",
        "ai_sdlc.generators.frontend_quality_platform_artifacts",
        "ai_sdlc.generators.frontend_theme_token_governance_artifacts",
        "ai_sdlc.generators.frontend_ui_kernel_artifacts",
        "ai_sdlc.models.frontend_cross_provider_consistency",
        "ai_sdlc.models.frontend_generation_constraints",
        "ai_sdlc.models.frontend_page_ui_schema",
        "ai_sdlc.models.frontend_provider_expansion",
        "ai_sdlc.models.frontend_provider_profile",
        "ai_sdlc.models.frontend_provider_runtime_adapter",
        "ai_sdlc.models.frontend_quality_platform",
        "ai_sdlc.models.frontend_theme_token_governance",
        "ai_sdlc.models.frontend_ui_kernel",
    ),
}


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(("ai_sdlc", *parts))


def _module_paths() -> dict[str, Path]:
    paths = {_module_name(path): path for path in PACKAGE_ROOT.rglob("*.py")}
    paths.update(
        {
            "packaging_backend": ROOT / "packaging_backend.py",
            "packaging.offline.verify_offline_bundle": (
                ROOT / "packaging" / "offline" / "verify_offline_bundle.py"
            ),
            "scripts.validate_public_release_identity": (
                ROOT / "scripts" / "validate_public_release_identity.py"
            ),
        }
    )
    return {module: path for module, path in paths.items() if path.is_file()}


def _parent_packages(module: str, known: set[str]) -> set[str]:
    parents: set[str] = set()
    candidate = module
    while "." in candidate:
        candidate = candidate.rsplit(".", 1)[0]
        if candidate in known:
            parents.add(candidate)
    return parents


def _resolve_imports(module: str, path: Path, known: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    package_parts = module.split(".")[:-1]
    for node in _runtime_import_nodes(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = max(0, len(package_parts) - node.level + 1)
                prefix = package_parts[:keep]
                if node.module:
                    prefix.extend(node.module.split("."))
                base = ".".join(prefix)
            else:
                base = node.module or ""
            if base:
                candidates.append(base)
                candidates.extend(f"{base}.{alias.name}" for alias in node.names)
        for candidate in candidates:
            # Keep retired targets visible even after their source file has been
            # physically deleted. Otherwise the resolver collapses the import to
            # the nearest surviving parent package and the Stage B guard becomes
            # blind to a dangling production dependency.
            if _family_for_module(candidate) is not None:
                imported.add(candidate)
                imported.update(_parent_packages(candidate, known))
                continue
            if candidate in known:
                imported.add(candidate)
                imported.update(_parent_packages(candidate, known))
                continue
            parent = candidate
            while "." in parent:
                parent = parent.rsplit(".", 1)[0]
                if parent in known:
                    imported.add(parent)
                    imported.update(_parent_packages(parent, known))
                    break
    return imported


def _runtime_import_nodes(node: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    imports: list[ast.Import | ast.ImportFrom] = []

    def visit(current: ast.AST) -> None:
        if isinstance(current, (ast.Import, ast.ImportFrom)):
            imports.append(current)
            return
        if (
            isinstance(current, ast.If)
            and isinstance(current.test, ast.Name)
            and current.test.id == "TYPE_CHECKING"
        ):
            return
        for child in ast.iter_child_nodes(current):
            visit(child)

    visit(node)
    return imports


def _build_graph(module_paths: dict[str, Path]) -> dict[str, set[str]]:
    known = set(module_paths)
    graph: dict[str, set[str]] = {}
    for module, path in module_paths.items():
        imported = _resolve_imports(module, path, known)
        imported.update(_parent_packages(module, known))
        imported.discard(module)
        graph[module] = imported
    return graph


def _family_for_module(module: str) -> str | None:
    for family, prefixes in RETIRED_FAMILIES.items():
        if any(
            module == prefix or module.startswith(f"{prefix}.") for prefix in prefixes
        ):
            return family
    return None


def scan_retired_runtime_inbound(
    *, selected_family: str | None = None
) -> list[dict[str, object]]:
    """Return retained-entry import paths that reach a retired family."""

    module_paths = _module_paths()
    graph = _build_graph(module_paths)
    return _scan_graph(
        graph,
        roots=RETAINED_ENTRY_MODULES,
        selected_family=selected_family,
    )


def _scan_graph(
    graph: dict[str, set[str]],
    *,
    roots: tuple[str, ...],
    selected_family: str | None = None,
) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []
    for root in roots:
        if root not in graph:
            continue
        queue: deque[tuple[str, tuple[str, ...]]] = deque([(root, (root,))])
        visited = {root}
        while queue:
            module, chain = queue.popleft()
            for imported in sorted(graph.get(module, ())):
                if imported in visited:
                    continue
                visited.add(imported)
                next_chain = (*chain, imported)
                family = _family_for_module(imported)
                if family is not None and (
                    selected_family is None or selected_family == family
                ):
                    violations.append(
                        {
                            "entry": root,
                            "family": family,
                            "module": imported,
                            "chain": list(next_chain),
                        }
                    )
                    continue
                queue.append((imported, next_chain))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--family", choices=sorted(RETIRED_FAMILIES))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    violations = scan_retired_runtime_inbound(selected_family=args.family)
    if args.json:
        print(json.dumps({"violations": violations}, indent=2, sort_keys=True))
    else:
        for item in violations:
            print(f"{item['entry']} -> {item['family']}: " + " -> ".join(item["chain"]))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
