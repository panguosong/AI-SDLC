"""Project-local identity for the visual baseline used by frontend evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path

FRONTEND_VISUAL_BASELINES = Path(".ai-sdlc/memory/frontend-delivery/visual-baselines")


def compute_frontend_visual_baseline_identity(
    root: Path,
    baseline_root_ref: str,
) -> dict[str, str] | None:
    """Return a stable content identity, or ``None`` when no baseline exists."""

    project_root = root.resolve()
    relative_root = Path(baseline_root_ref)
    if relative_root.is_absolute():
        raise ValueError("Frontend visual baseline root must be project-relative.")
    baseline_root = (project_root / relative_root).resolve()
    allowed_root = (project_root / FRONTEND_VISUAL_BASELINES).resolve()
    try:
        baseline_root.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError(
            "Frontend visual baseline root is outside the managed baseline directory."
        ) from exc

    image = baseline_root / "baseline.png"
    metadata = baseline_root / "baseline.yaml"
    existing = (image.exists(), metadata.exists())
    if not any(existing):
        return None
    if not all(existing):
        raise ValueError("Frontend visual baseline is incomplete.")
    if any(path.is_symlink() or not path.is_file() for path in (image, metadata)):
        raise ValueError("Frontend visual baseline files must be ordinary files.")

    digest = hashlib.sha256()
    for path in (image, metadata):
        resolved = path.resolve()
        try:
            resolved.relative_to(baseline_root)
        except ValueError as exc:
            raise ValueError("Frontend visual baseline file escapes its root.") from exc
        relative = resolved.relative_to(project_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(resolved.read_bytes())
        digest.update(b"\0")

    return {
        "root": relative_root.as_posix(),
        "image_path": image.relative_to(project_root).as_posix(),
        "metadata_path": metadata.relative_to(project_root).as_posix(),
        "digest": digest.hexdigest(),
    }


def validate_frontend_visual_baseline_identity(
    root: Path,
    identity: object,
    *,
    expected_root: str = "",
) -> dict[str, str]:
    """Validate a captured baseline identity against the current local files."""

    if not isinstance(identity, dict):
        raise ValueError("Frontend browser capture baseline identity is missing.")
    required = ("root", "image_path", "metadata_path", "digest")
    captured: dict[str, str] = {}
    for field_name in required:
        value = identity.get(field_name, "")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"Frontend browser capture baseline {field_name} is missing."
            )
        captured[field_name] = value.strip()
    if expected_root and captured["root"] != Path(expected_root).as_posix():
        raise ValueError(
            "Frontend browser capture baseline root does not match its context."
        )
    current = compute_frontend_visual_baseline_identity(root, captured["root"])
    if current is None:
        raise ValueError("Frontend browser capture baseline is no longer available.")
    if current != captured:
        raise ValueError(
            "Frontend browser capture is stale for the current visual baseline."
        )
    return captured


__all__ = [
    "FRONTEND_VISUAL_BASELINES",
    "compute_frontend_visual_baseline_identity",
    "validate_frontend_visual_baseline_identity",
]
