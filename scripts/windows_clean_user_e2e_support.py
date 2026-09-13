"""Windows 普通用户 E2E 的用户项目夹具。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

DEFAULT_SOLUTION_TOKENS = (
    '"status": "dry_run"',
    '"recommended_option_source": "existing-project-facts"',
    '"recommended_frontend_stack": "vue3"',
    '"recommended_provider_id": "public-primevue"',
    '"recommended_style_pack_id": "modern-saas"',
    '"option_id": "custom"',
    '"custom_choice_supported": true',
)
CUSTOM_SOLUTION_TOKENS = (
    '"requested_frontend_stack": "vue3"',
    '"requested_provider_id": "public-primevue"',
    '"requested_style_pack_id": "data-console"',
    '"effective_frontend_stack": "vue3"',
    '"effective_provider_id": "public-primevue"',
    '"effective_style_pack_id": "data-console"',
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _business_hashes(project_root: Path, paths: list[Path]) -> dict[str, str]:
    return {str(path.relative_to(project_root)): _file_sha256(path) for path in paths}


def _initialize_existing_repo(project_root: Path) -> None:
    commands = (
        ("init",),
        ("config", "user.email", "windows-e2e@example.com"),
        ("config", "user.name", "Windows E2E"),
        ("config", "core.autocrlf", "false"),
        ("add", "--all"),
        ("commit", "-m", "existing project baseline"),
        ("switch", "-c", "feature/001-customer-approval-dashboard-docs"),
    )
    for command in commands:
        _run_git(project_root, *command)


def _commit_current_state(project_root: Path, message: str) -> None:
    _run_git(project_root, "add", "--all")
    _run_git(project_root, "commit", "-m", message)


def _run_git(project_root: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"Git existing-project setup failed: {args}")


def _write_existing_project(project_root: Path) -> list[Path]:
    business_files = {
        "package.json": (
            '{\n  "name": "existing-customer-portal",\n'
            '  "private": true,\n  "scripts": {"build": "vite build"},\n'
            '  "dependencies": {"vue": "^3.5.0", "primevue": "^4.0.0"}\n}\n'
        ),
        "README.md": "# Existing Customer Portal\n\nProduction project fixture.\n",
        "TODO.md": "- [ ] Add the customer approval dashboard\n",
        "src/main.ts": "console.log('existing customer portal');\n",
    }
    paths: list[Path] = []
    for relative_path, content in business_files.items():
        path = project_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        paths.append(path)
    return paths


def _write_hashes(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        json.dumps(values, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_refined_frontend_requirement(project_root: Path) -> Path:
    path = project_root / "requirements" / "customer-approval-dashboard.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_requirement_text(), encoding="utf-8")
    return path


def _write_summary(evidence_root: Path) -> None:
    summary = {
        "result": "passed",
        "install_source": os.environ.get("AI_SDLC_E2E_INSTALL_SOURCE", "remote-main"),
        "source_revision": os.environ.get("AI_SDLC_E2E_SOURCE_REVISION", ""),
        "terminal_backend": "ConPTY",
        "init_command": "ai-sdlc init .",
        "selected_agent_target": "codex",
        "selected_shell": "powershell",
        "requirement_flow": "public-loop-and-workitem-cli",
        "project_fact_frontend_stack": "vue3",
        "project_fact_provider": "public-primevue",
        "project_fact_style_pack": "modern-saas",
        "custom_style_pack": "data-console",
        "managed_delivery_apply_executed": False,
        "business_files_unchanged": True,
    }
    (evidence_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _requirement_text() -> str:
    return """# Customer Approval Dashboard Requirement

## Goal

Add a responsive enterprise approval dashboard to the existing portal.

## Scope

- Dashboard summary cards, searchable approval table, detail drawer and approval form.
- Desktop and mobile layouts, Chinese and English copy, light theme only.
- Frontend delivery only; existing backend APIs remain unchanged.

## Acceptance Criteria

- Users can filter, inspect and approve or reject a pending request.
- Loading, empty, validation, permission and network-error states are visible.
- Keyboard navigation and browser E2E coverage are required.
"""
