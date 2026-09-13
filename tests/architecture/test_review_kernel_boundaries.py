"""Negative architecture contract for the minimal review kernel."""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_KERNEL = _ROOT / "src" / "ai_sdlc" / "core" / "review_kernel.py"
_OUTCOME_MODELS = _ROOT / "src" / "ai_sdlc" / "core" / "loop_review_models.py"
_OUTCOME_SERVICE = _ROOT / "src" / "ai_sdlc" / "core" / "loop_review_service.py"


def test_review_kernel_has_no_runtime_or_persistence_dependencies() -> None:
    tree = ast.parse(_KERNEL.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden_fragments = {
        "loop_artifacts",
        "requirement_loop",
        "design_contract_loop",
        "implementation_loop",
        "frontend_evidence_loop",
        "pr_review_service",
        "stage_review",
        "lean_code",
        "subprocess",
        "urllib",
        "requests",
        "provider",
        "model",
    }
    assert not {
        dependency
        for dependency in imported
        if any(fragment in dependency for fragment in forbidden_fragments)
    }


def test_review_kernel_exposes_no_close_or_persistence_symbols() -> None:
    tree = ast.parse(_KERNEL.read_text(encoding="utf-8"))
    public_symbols = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        and not node.name.startswith("_")
    }

    forbidden_fragments = {
        "close",
        "persist",
        "store",
        "save",
        "record",
        "authorize",
        "certificate",
        "attest",
        "session",
    }
    assert not {
        symbol
        for symbol in public_symbols
        if any(fragment in symbol.lower() for fragment in forbidden_fragments)
    }


def test_review_outcome_has_no_governance_platform_fields() -> None:
    tree = ast.parse(_OUTCOME_MODELS.read_text(encoding="utf-8"))
    outcome = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LoopReviewOutcome"
    )
    fields = {
        statement.target.id
        for statement in outcome.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
    }

    assert fields.isdisjoint(
        {
            "authority",
            "certificate",
            "session",
            "quorum",
            "score",
            "verdict",
            "passed",
            "closed",
        }
    )


def test_only_normal_two_rounds_remain_and_continuation_is_retired() -> None:
    runtime = _ROOT / "src" / "ai_sdlc"
    occurrences = [
        path.relative_to(_ROOT).as_posix()
        for path in runtime.rglob("*.py")
        if "review-outcome-round-" in path.read_text(encoding="utf-8")
    ]
    assert set(occurrences) == {
        "src/ai_sdlc/core/loop_review_service.py",
        "src/ai_sdlc/core/loop_decision_service.py",
        "src/ai_sdlc/core/loop_stage_decision_service.py",
        "src/ai_sdlc/cli/loop_stage_cmd.py",
    }

    service = _OUTCOME_SERVICE.read_text(encoding="utf-8")
    assert "round_number not in {1, 2}" in service
    assert 'f"review-outcome-round-{round_number}.json"' in service
    assert "reject_retired_implementation_continuation" in service
    assert "load_implementation_review_continuation" not in service
    assert not (runtime / "core/loop_review_continuation.py").exists()
