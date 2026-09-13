"""Tests for Loop Engine artifact persistence helpers."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import yaml

import ai_sdlc.core.loop_artifacts as loop_artifacts_module
from ai_sdlc.core.loop_artifacts import LoopArtifactStore
from ai_sdlc.core.loop_models import LoopRun, LoopType
from ai_sdlc.core.pr_review_models import ReviewPack


def test_artifact_store_creates_required_loop_and_review_directories(tmp_path) -> None:
    store = LoopArtifactStore(tmp_path)

    loop_dir = store.create_loop_run_dir("loop-001")
    review_dir = store.create_review_run_dir("review-001")

    assert loop_dir == tmp_path / ".ai-sdlc" / "loops" / "local-pr-review" / "loop-001"
    assert review_dir == tmp_path / ".ai-sdlc" / "reviews" / "pr" / "review-001"
    assert loop_dir.is_dir()
    assert review_dir.is_dir()


def test_artifact_store_rejects_unsafe_identifiers(tmp_path) -> None:
    store = LoopArtifactStore(tmp_path)

    for value in ("../escape", "/tmp/review", r"C:\tmp\review", "", "."):
        with pytest.raises(ValueError, match="Unsafe artifact identifier"):
            store.create_review_run_dir(value)
        with pytest.raises(ValueError, match="Unsafe artifact identifier"):
            store.create_loop_run_dir(value)


def test_write_json_artifact_serializes_pydantic_model(tmp_path) -> None:
    store = LoopArtifactStore(tmp_path)
    loop_run = LoopRun(loop_id="loop-001", loop_type=LoopType.LOCAL_PR_REVIEW)
    path = store.loop_run_dir("loop-001") / "loop-run.json"

    written = store.write_json_artifact(path, loop_run)

    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["artifact_kind"] == "loop-run"
    assert payload["loop_id"] == "loop-001"
    assert payload["loop_type"] == "local-pr-review"
    assert written.read_bytes().endswith(b"}\n")
    assert b"\r\n" not in written.read_bytes()
    assert not list(written.parent.glob("*.tmp"))


def test_write_yaml_artifact_serializes_mapping(tmp_path) -> None:
    store = LoopArtifactStore(tmp_path)
    path = store.review_run_dir("review-001") / "resolution.yaml"

    written = store.write_yaml_artifact(
        path,
        {"schema_version": "1", "artifact_kind": "resolution", "items": []},
    )

    payload = yaml.safe_load(written.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": "1",
        "artifact_kind": "resolution",
        "items": [],
    }


def test_write_markdown_artifact_normalizes_trailing_newline(tmp_path) -> None:
    store = LoopArtifactStore(tmp_path)
    path = store.review_run_dir("review-001") / "final-report.md"

    written = store.write_markdown_artifact(path, "# Report")

    assert written.read_text(encoding="utf-8") == "# Report\n"


def test_write_markdown_artifact_rewrites_legacy_crlf_bytes(tmp_path) -> None:
    store = LoopArtifactStore(tmp_path)
    path = store.create_review_run_dir("review-001") / "final-report.md"
    path.write_bytes(b"# Report\r\n")

    written = store.write_markdown_artifact(path, "# Report")

    assert written.read_bytes() == b"# Report\n"


def test_concurrent_writers_do_not_share_a_coarse_clock_temporary(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LoopArtifactStore(tmp_path)
    path = store.loop_run_dir("loop-001") / "status.md"
    replace_barrier = Barrier(2)
    original_replace = loop_artifacts_module._replace_with_retry

    monkeypatch.setattr(loop_artifacts_module.time, "monotonic_ns", lambda: 1)

    def synchronized_replace(source, destination) -> None:
        replace_barrier.wait(timeout=5)
        original_replace(source, destination)

    monkeypatch.setattr(
        loop_artifacts_module,
        "_replace_with_retry",
        synchronized_replace,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        written = tuple(
            executor.map(
                lambda content: store.write_markdown_artifact(path, content),
                ("first", "second"),
            )
        )

    assert written == (path, path)
    assert path.read_text(encoding="utf-8") in {"first\n", "second\n"}
    assert not list(path.parent.glob(".*.tmp"))


def test_read_json_artifact_returns_mapping(tmp_path) -> None:
    store = LoopArtifactStore(tmp_path)
    review_pack = ReviewPack(
        review_id="review-001",
        loop_id="loop-001",
        repo_root="/repo",
        base_ref="main",
        head_ref="HEAD",
        base_commit="a" * 40,
        head_commit="b" * 40,
    )
    path = store.review_run_dir("review-001") / "review-pack.json"
    store.write_json_artifact(path, review_pack)

    payload = store.read_json_artifact(path)

    assert payload["artifact_kind"] == "review-pack"
    assert payload["review_id"] == "review-001"
