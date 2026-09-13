"""Tests for the read-only five-Loop delivery router."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_sdlc.core.loop_models import LoopStatus, LoopType
from ai_sdlc.core.loop_router import LoopRouteStatus, route_five_loops
from ai_sdlc.core.loop_status import (
    LoopStatusCommandStatus,
    LoopStatusResult,
    LoopSummary,
)

_ORDER = (
    "requirement",
    "design-contract",
    "implementation",
    "frontend-evidence",
    "local-pr-review",
)


def test_route_without_any_loop_requests_requirement_start(tmp_path: Path) -> None:
    result = route_five_loops(tmp_path, status_loader=_loader({}))

    assert result.status == LoopRouteStatus.NEEDS_USER
    assert result.current_loop is None
    assert "requirement start" in result.next_action


@pytest.mark.parametrize("retired", [False, True])
def test_blocked_retirement_preserves_terminal_next_without_changing_other_blockers(
    tmp_path, retired
):
    terminal_next = (
        "Preserve all historical artifacts unchanged. No further review is permitted."
    )
    blocker = (
        "implementation-continuation-retired" if retired else "review-input-invalid"
    )
    statuses = {
        "implementation": LoopStatusResult(
            status=LoopStatusCommandStatus.BLOCKED,
            blocker=blocker,
            next_action=terminal_next if retired else "Inspect the malformed input.",
        )
    }

    result = route_five_loops(tmp_path, status_loader=_loader(statuses))

    assert result.status == LoopRouteStatus.BLOCKED
    assert result.current_loop is None
    assert result.blockers == [blocker]
    if retired:
        assert result.next_action == terminal_next
        assert "repair" not in result.next_action.lower()
    else:
        assert result.next_action == (
            "Repair the reported current Loop artifact before continuing."
        )


def test_new_implementation_next_selects_supported_simulation_without_migrating_old(
    tmp_path,
):
    statuses = {
        "requirement": _ready("requirement", "req-1", LoopStatus.CLOSED),
        "design-contract": _ready("design-contract", "design-1", LoopStatus.CLOSED),
    }
    _write_predecessor(
        tmp_path,
        loop_type="design-contract",
        loop_id="design-1",
        field="requirement_loop_id",
        predecessor_id="req-1",
    )
    result = route_five_loops(tmp_path, status_loader=_loader(statuses))
    assert "--decision-capability stage-simulation-v1" in result.next_action
    assert "--decision-mode adaptive-quantified" in result.next_action


def test_route_one_active_loop(tmp_path: Path) -> None:
    statuses = {
        "requirement": _ready("requirement", "req-1", LoopStatus.NEEDS_REVIEW),
    }

    result = route_five_loops(tmp_path, status_loader=_loader(statuses))

    assert result.status == LoopRouteStatus.ROUTED
    assert result.current_loop is not None
    assert result.current_loop.loop_type == "requirement"
    assert result.current_loop.loop_id == "req-1"


def test_route_deepest_active_loop_on_closed_predecessor_chain(
    tmp_path: Path,
) -> None:
    statuses = {
        "requirement": _ready("requirement", "req-1", LoopStatus.CLOSED),
        "design-contract": _ready(
            "design-contract",
            "design-1",
            LoopStatus.NEEDS_REVIEW,
        ),
    }
    _write_predecessor(
        tmp_path,
        loop_type="design-contract",
        loop_id="design-1",
        field="requirement_loop_id",
        predecessor_id="req-1",
    )

    result = route_five_loops(tmp_path, status_loader=_loader(statuses))

    assert result.status == LoopRouteStatus.ROUTED
    assert result.current_loop is not None
    assert result.current_loop.loop_type == "design-contract"
    assert result.current_loop.loop_id == "design-1"


def test_route_refuses_unrelated_active_loops(tmp_path: Path) -> None:
    statuses = {
        "requirement": _ready("requirement", "req-current", LoopStatus.RUNNING),
        "design-contract": _ready(
            "design-contract",
            "design-other",
            LoopStatus.NEEDS_REVIEW,
        ),
    }
    _write_predecessor(
        tmp_path,
        loop_type="design-contract",
        loop_id="design-other",
        field="requirement_loop_id",
        predecessor_id="req-other",
    )

    result = route_five_loops(tmp_path, status_loader=_loader(statuses))

    assert result.status == LoopRouteStatus.NEEDS_USER
    assert result.current_loop is None
    assert "predecessor chain" in result.blockers[0]
    assert "req-current" in result.blockers[0]
    assert "req-other" in result.blockers[0]


def test_route_all_closed_reports_delivery_complete(tmp_path: Path) -> None:
    statuses = {
        loop_type: _ready(loop_type, f"loop-{index}", LoopStatus.CLOSED)
        for index, loop_type in enumerate(_ORDER, start=1)
    }
    _write_predecessor(
        tmp_path,
        loop_type="design-contract",
        loop_id="loop-2",
        field="requirement_loop_id",
        predecessor_id="loop-1",
    )
    _write_predecessor(
        tmp_path,
        loop_type="implementation",
        loop_id="loop-3",
        field="design_contract_loop_id",
        predecessor_id="loop-2",
    )
    _write_predecessor(
        tmp_path,
        loop_type="frontend-evidence",
        loop_id="loop-4",
        field="implementation_loop_id",
        predecessor_id="loop-3",
    )

    result = route_five_loops(tmp_path, status_loader=_loader(statuses))

    assert result.status == LoopRouteStatus.COMPLETED
    assert result.current_loop is None
    assert result.blockers == []
    assert result.next_action == ""


def test_route_preserves_head_index_worktree_and_checkpoint_bytes(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "--initial-branch=main")
    _git(tmp_path, "config", "user.email", "router@example.com")
    _git(tmp_path, "config", "user.name", "Router Test")
    (tmp_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "initial")
    (tmp_path / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    checkpoint = tmp_path / ".ai-sdlc" / "state" / "checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b'{"stage":"legacy"}\n')
    before = _repository_state(tmp_path, checkpoint)

    route_five_loops(tmp_path, status_loader=_loader({}))

    assert _repository_state(tmp_path, checkpoint) == before


def _ready(
    loop_type: str,
    loop_id: str,
    status: LoopStatus,
) -> LoopStatusResult:
    return LoopStatusResult(
        status=LoopStatusCommandStatus.READY,
        result="Current loop found.",
        current_loop=LoopSummary(
            loop_id=loop_id,
            loop_type=LoopType(loop_type),
            status=status,
            is_current=True,
            next_action=f"Continue {loop_type}.",
        ),
        next_action=f"Continue {loop_type}.",
    )


def _loader(statuses: dict[str, LoopStatusResult]):
    def load(_root: Path, loop_type: str) -> LoopStatusResult:
        return statuses.get(
            loop_type,
            LoopStatusResult(
                status=LoopStatusCommandStatus.NO_CURRENT,
                result=f"No current {loop_type} loop.",
            ),
        )

    return load


def _write_predecessor(
    root: Path,
    *,
    loop_type: str,
    loop_id: str,
    field: str,
    predecessor_id: str,
) -> None:
    filenames = {
        "design-contract": "design-contract-input.json",
        "implementation": "implementation-input.json",
        "frontend-evidence": "frontend-evidence-input.json",
    }
    path = root / ".ai-sdlc" / "loops" / loop_type / loop_id / filenames[loop_type]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({field: predecessor_id}), encoding="utf-8")


def _repository_state(root: Path, checkpoint: Path) -> tuple[str, str, str, bytes]:
    return (
        _git(root, "rev-parse", "HEAD"),
        _git(root, "write-tree"),
        _git(root, "status", "--porcelain=v1", "--untracked-files=all"),
        checkpoint.read_bytes(),
    )


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.strip())
    return result.stdout.strip()
