"""Deletion and anti-revival contract for retired review authority systems."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_REMOVED_PATHS = _ROOT / "tests" / "architecture" / "review_kernel_removed_paths.txt"
_KERNEL = _ROOT / "src" / "ai_sdlc" / "core" / "review_kernel.py"
_SLIMMING = _ROOT / "src" / "ai_sdlc" / "core" / "slimming_advice.py"
_RETIRED_RELEASE_AUTHORITY_PATHS = (
    "scripts/release_truth.py",
    "src/ai_sdlc/core/github_attestation_verifier.py",
    "src/ai_sdlc/core/release_truth.py",
    "src/ai_sdlc/core/release_truth_models.py",
)


def _inventory() -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in _REMOVED_PATHS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _imports(tree: ast.AST) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_frozen_legacy_review_paths_are_absent() -> None:
    inventory = _inventory()
    assert len(inventory) == len(set(inventory))
    assert list(inventory) == sorted(inventory)

    survivors = [path for path in inventory if (_ROOT / path).exists()]
    assert survivors == []


def test_review_kernel_cannot_become_runtime_or_persisted_authority() -> None:
    source = _KERNEL.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_imports = {
        "stage_review",
        "lean_code",
        "artifact_store",
        "subprocess",
        "socket",
        "urllib",
        "requests",
        "httpx",
        "provider",
        "workflow",
    }
    assert not {
        dependency
        for dependency in _imports(tree)
        if any(fragment in dependency for fragment in forbidden_imports)
    }

    forbidden_calls = {
        "write_text",
        "write_bytes",
        "mkdir",
        "touch",
        "unlink",
        "replace",
        "rename",
    }
    assert not {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden_calls
    }

    model_fields = {
        node.target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert model_fields.isdisjoint(
        {
            "authorization",
            "certificate",
            "close",
            "closed",
            "history",
            "panel",
            "passed",
            "quorum",
            "score",
            "session",
            "verdict",
        }
    )


def test_slimming_advice_cannot_block_or_close_a_loop() -> None:
    source = _SLIMMING.read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert not any(
        fragment in dependency
        for dependency in _imports(tree)
        for fragment in (
            "close_check",
            "loop_status",
            "loop_store",
            "loop_writer",
            "pr_review_service",
            "runner",
        )
    )
    public_names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        and not node.name.startswith("_")
    }
    assert public_names == {"SlimmingAdvice", "collect_slimming_advice"}


def test_retired_release_authority_is_physically_absent() -> None:
    assert [path for path in _RETIRED_RELEASE_AUTHORITY_PATHS if (_ROOT / path).exists()] == []

    production_paths = (
        _ROOT / ".github" / "workflows" / "release-build.yml",
        _ROOT / ".github" / "workflows" / "release-artifact-smoke.yml",
        _ROOT / "src" / "ai_sdlc" / "core" / "update_advisor.py",
        _ROOT / "scripts" / "loop_e2e_release_gate.py",
    )
    retired_markers = (
        "release-satisfaction-proof",
        "release-certificate",
        "terminal-generation-burn",
        "pr-review\", \"attest",
        "latest-attestation.json",
    )
    for path in production_paths:
        source = path.read_text(encoding="utf-8").lower()
        assert not {marker for marker in retired_markers if marker in source}, path
