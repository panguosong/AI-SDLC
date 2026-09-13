"""Repo-level regression tests for the public release tree."""

from __future__ import annotations

from pathlib import Path


def test_public_release_excludes_internal_program_history() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    assert not (repo_root / "program-manifest.yaml").exists()
    assert not any(path.is_file() for path in (repo_root / "specs").rglob("*"))
    assert not any(
        path.is_file() for path in (repo_root / ".ai-sdlc" / "work-items").rglob("*")
    )


def test_public_release_contains_no_release_history_documents() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    release_dir = repo_root / "docs" / "releases"

    assert not any(path.is_file() for path in release_dir.rglob("*"))
