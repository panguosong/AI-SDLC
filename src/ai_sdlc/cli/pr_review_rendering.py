"""Local PR Review CLI 的稳定人类与 JSON 输出。"""

from __future__ import annotations

import json

import typer
from rich.console import Console

_CONSOLE = Console(soft_wrap=True)


def emit_pr_review_result(
    payload: dict[str, object],
    *,
    json_output: bool,
) -> None:
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    _CONSOLE.print(f"Result: {payload.get('status', '')}")
    if payload.get("blocker"):
        _CONSOLE.print(f"Blocker: {payload['blocker']}")
    _CONSOLE.print(f"Next: {payload.get('next_action') or '-'}")
    _emit_fields(payload)


def _emit_fields(payload: dict[str, object]) -> None:
    for key in (
        "source_adapter",
        "source_access_status",
        "provider_id",
        "model_selector",
        "resolved_model",
        "code_egress",
    ):
        if key in payload:
            _CONSOLE.print(f"{key}: {payload.get(key)}")
    source = payload.get("diff_source")
    if isinstance(source, dict) and source.get("source_kind"):
        _CONSOLE.print(f"diff_source: {source.get('source_kind')}")
    for key, label in (
        ("review_pack_path", "review_pack"),
        ("source_resolution_path", "source_resolution"),
        ("findings_path", "findings"),
    ):
        if payload.get(key):
            _CONSOLE.print(f"{label}: {payload[key]}")


__all__ = ["emit_pr_review_result"]
