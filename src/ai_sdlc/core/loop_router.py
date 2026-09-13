"""Read-only routing across the five delivery Loops."""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ai_sdlc.core.loop_models import LoopStatus, LoopType
from ai_sdlc.core.loop_status import (
    LoopStatusCommandStatus,
    LoopStatusResult,
    LoopSummary,
    get_loop_status,
)
from ai_sdlc.rules import supported_decision_capabilities

_LOOP_ORDER: tuple[str, ...] = (
    LoopType.REQUIREMENT.value,
    LoopType.DESIGN_CONTRACT.value,
    LoopType.IMPLEMENTATION.value,
    LoopType.FRONTEND_EVIDENCE.value,
    LoopType.LOCAL_PR_REVIEW.value,
)
_PREDECESSOR_FIELDS: dict[str, tuple[str, str, str]] = {
    LoopType.DESIGN_CONTRACT.value: (
        LoopType.REQUIREMENT.value,
        "design-contract-input.json",
        "requirement_loop_id",
    ),
    LoopType.IMPLEMENTATION.value: (
        LoopType.DESIGN_CONTRACT.value,
        "implementation-input.json",
        "design_contract_loop_id",
    ),
    LoopType.FRONTEND_EVIDENCE.value: (
        LoopType.IMPLEMENTATION.value,
        "frontend-evidence-input.json",
        "implementation_loop_id",
    ),
}
_START_COMMANDS: dict[str, str] = {
    LoopType.REQUIREMENT.value: ('ai-sdlc loop requirement start --idea "<需求描述>"'),
    LoopType.DESIGN_CONTRACT.value: (
        "ai-sdlc loop design-contract check --wi specs/<work-item>"
    ),
    LoopType.IMPLEMENTATION.value: (
        "ai-sdlc loop implementation start --wi specs/<work-item>"
    ),
    LoopType.FRONTEND_EVIDENCE.value: (
        "ai-sdlc loop frontend-evidence start --wi specs/<work-item>"
    ),
    LoopType.LOCAL_PR_REVIEW.value: "ai-sdlc pr-review start",
}


def _new_start_command(loop_type: str) -> str:
    command = _START_COMMANDS[loop_type]
    capabilities = supported_decision_capabilities().get(loop_type, [])
    if capabilities:
        command += (
            " --decision-mode adaptive-quantified --decision-capability "
            + capabilities[-1]
        )
    return command


StatusLoader = Callable[[Path, str], LoopStatusResult]


class LoopRouteStatus(StrEnum):
    """Outcome of resolving the current delivery route."""

    ROUTED = "routed"
    NEEDS_USER = "needs_user"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class LoopRouteItem(BaseModel):
    """Minimal current Loop truth surfaced by ``ai-sdlc run``."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    loop_type: LoopType
    loop_id: str
    status: LoopStatus
    next_action: str = ""


class LoopRouteResult(BaseModel):
    """Read-only Result/Next/Blockers view for the five-Loop route."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: LoopRouteStatus
    result: str
    current_loop: LoopRouteItem | None = None
    next_action: str = ""
    blockers: list[str] = Field(default_factory=list)
    observed_loops: list[LoopRouteItem] = Field(default_factory=list)


def route_five_loops(
    root: Path,
    *,
    status_loader: StatusLoader = get_loop_status,
) -> LoopRouteResult:
    """Resolve one current delivery route without writing project state."""

    resolved_root = root.resolve()
    statuses: dict[str, LoopStatusResult] = {}
    items: list[LoopRouteItem] = []
    summaries: dict[str, LoopSummary] = {}
    for loop_type in _LOOP_ORDER:
        try:
            status_result = status_loader(resolved_root, loop_type)
        except (OSError, ValueError) as exc:
            return _blocked(f"Unable to read current {loop_type} Loop: {exc}", items)
        statuses[loop_type] = status_result
        if status_result.status == LoopStatusCommandStatus.BLOCKED:
            return _blocked(
                status_result.blocker
                or f"Current {loop_type} Loop artifacts are invalid.",
                items,
                next_action=(
                    status_result.next_action
                    if status_result.blocker == "implementation-continuation-retired"
                    else ""
                ),
            )
        if status_result.status == LoopStatusCommandStatus.NO_CURRENT:
            continue
        if status_result.current_loop is None:
            return _blocked(
                f"Current {loop_type} status omitted its Loop identity.",
                items,
            )
        summary = status_result.current_loop
        summaries[loop_type] = summary
        items.append(_route_item(summary))

    if not items:
        return LoopRouteResult(
            status=LoopRouteStatus.NEEDS_USER,
            result="No delivery Loop has been started.",
            next_action=_new_start_command(LoopType.REQUIREMENT.value),
        )

    chain_problems = _chain_problems(resolved_root, items)
    active = [item for item in items if item.status != LoopStatus.CLOSED]
    if len(active) > 1:
        detail = (
            "; ".join(chain_problems)
            if chain_problems
            else ("more than one current Loop is active")
        )
        return LoopRouteResult(
            status=LoopRouteStatus.NEEDS_USER,
            result="Current delivery route is ambiguous.",
            next_action="Inspect each current Loop and explicitly choose one to continue.",
            blockers=["Cannot prove one active predecessor chain: " + detail + "."],
            observed_loops=items,
        )
    if chain_problems:
        return LoopRouteResult(
            status=LoopRouteStatus.NEEDS_USER,
            result="Current delivery route is inconsistent.",
            next_action="Repair or explicitly select the current Loop pointers.",
            blockers=[
                "Cannot prove current predecessor chain: " + "; ".join(chain_problems)
            ],
            observed_loops=items,
        )

    if active:
        current = active[0]
        current_index = _LOOP_ORDER.index(str(current.loop_type))
        later = [
            item
            for item in items
            if _LOOP_ORDER.index(str(item.loop_type)) > current_index
        ]
        if later:
            return LoopRouteResult(
                status=LoopRouteStatus.NEEDS_USER,
                result="A later Loop is already recorded after an active predecessor.",
                next_action="Inspect the current Loop pointers and select the valid chain.",
                blockers=[
                    f"{current.loop_type} {current.loop_id} is still active while "
                    f"{later[0].loop_type} {later[0].loop_id} is also current."
                ],
                observed_loops=items,
            )
        source_status = statuses[str(current.loop_type)]
        blockers = [source_status.blocker] if source_status.blocker else []
        if not blockers and current.status in {
            LoopStatus.BLOCKED,
            LoopStatus.NEEDS_FIX,
            LoopStatus.NEEDS_USER,
        }:
            blockers.append(f"Current Loop status is {current.status}.")
        return LoopRouteResult(
            status=LoopRouteStatus.ROUTED,
            result=(
                f"Current delivery Loop: {current.loop_type} "
                f"{current.loop_id} ({current.status})."
            ),
            current_loop=current,
            next_action=current.next_action or source_status.next_action,
            blockers=blockers,
            observed_loops=items,
        )

    if len(items) == len(_LOOP_ORDER):
        return LoopRouteResult(
            status=LoopRouteStatus.COMPLETED,
            result="All five delivery Loops are closed.",
            observed_loops=items,
        )

    next_type = _next_missing_loop(items, summaries)
    return LoopRouteResult(
        status=LoopRouteStatus.NEEDS_USER,
        result="The current closed Loop chain is ready for its next Loop.",
        next_action=_new_start_command(next_type),
        observed_loops=items,
    )


def _route_item(summary: LoopSummary) -> LoopRouteItem:
    return LoopRouteItem(
        loop_type=summary.loop_type,
        loop_id=summary.loop_id,
        status=summary.status,
        next_action=summary.next_action or summary.next_guidance.command,
    )


def _blocked(
    message: str, items: list[LoopRouteItem], *, next_action: str = ""
) -> LoopRouteResult:
    return LoopRouteResult(
        status=LoopRouteStatus.BLOCKED,
        result="The five-Loop route could not be read safely.",
        next_action=next_action
        or "Repair the reported current Loop artifact before continuing.",
        blockers=[message],
        observed_loops=items,
    )


def _chain_problems(root: Path, items: list[LoopRouteItem]) -> list[str]:
    by_type = {str(item.loop_type): item for item in items}
    problems: list[str] = []
    for loop_type, (predecessor_type, filename, field) in _PREDECESSOR_FIELDS.items():
        item = by_type.get(loop_type)
        if item is None:
            continue
        predecessor_id, error = _read_predecessor_id(
            root,
            item,
            filename=filename,
            field=field,
        )
        if error:
            problems.append(error)
            continue
        predecessor = by_type.get(predecessor_type)
        if predecessor is None:
            problems.append(
                f"{loop_type} {item.loop_id} references absent "
                f"{predecessor_type} {predecessor_id}"
            )
            continue
        if predecessor.loop_id != predecessor_id:
            problems.append(
                f"{loop_type} {item.loop_id} references {predecessor_id}, not "
                f"current {predecessor_type} {predecessor.loop_id}"
            )
            continue
        if predecessor.status != LoopStatus.CLOSED:
            problems.append(
                f"predecessor {predecessor_type} {predecessor.loop_id} is "
                f"{predecessor.status}, not closed"
            )

    local_review = by_type.get(LoopType.LOCAL_PR_REVIEW.value)
    if local_review is not None:
        predecessor = by_type.get(LoopType.FRONTEND_EVIDENCE.value) or by_type.get(
            LoopType.IMPLEMENTATION.value
        )
        if predecessor is None:
            problems.append(
                f"local-pr-review {local_review.loop_id} has no current delivery predecessor"
            )
        elif predecessor.status != LoopStatus.CLOSED:
            problems.append(
                f"predecessor {predecessor.loop_type} {predecessor.loop_id} is "
                f"{predecessor.status}, not closed"
            )
    return problems


def _read_predecessor_id(
    root: Path,
    item: LoopRouteItem,
    *,
    filename: str,
    field: str,
) -> tuple[str, str]:
    path = root / ".ai-sdlc" / "loops" / str(item.loop_type) / item.loop_id / filename
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "", f"cannot read {item.loop_type} predecessor: {exc}"
    if not isinstance(payload, dict):
        return "", f"{filename} root is not an object"
    value = payload.get(field, "")
    if not isinstance(value, str) or not value.strip():
        return "", f"{filename} does not identify {field}"
    return value.strip(), ""


def _next_missing_loop(
    items: list[LoopRouteItem],
    summaries: dict[str, LoopSummary],
) -> str:
    present = {str(item.loop_type) for item in items}
    if (
        LoopType.IMPLEMENTATION.value in present
        and not {
            LoopType.FRONTEND_EVIDENCE.value,
            LoopType.LOCAL_PR_REVIEW.value,
        }
        & present
    ):
        command = summaries[LoopType.IMPLEMENTATION.value].next_guidance.command.lower()
        return (
            LoopType.FRONTEND_EVIDENCE.value
            if "frontend-evidence" in command
            else LoopType.LOCAL_PR_REVIEW.value
        )
    for loop_type in _LOOP_ORDER:
        if loop_type not in present:
            return loop_type
    return LoopType.LOCAL_PR_REVIEW.value


__all__ = [
    "LoopRouteItem",
    "LoopRouteResult",
    "LoopRouteStatus",
    "route_five_loops",
]
