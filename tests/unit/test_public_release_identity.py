from pathlib import Path

from scripts.validate_public_release_identity import (
    CURRENT_REPOSITORY_URL,
    REQUIRED_SURFACES,
    STABLE_SOURCE_CLONE,
    scan_paths,
    validate_required_surfaces,
)


def test_scan_rejects_non_public_state_and_pre_release_identity(
    tmp_path: Path,
) -> None:
    candidate_version = f"{0}.{8}.{0}"
    files = {
        f"docs/releases/v{candidate_version}.md": "candidate release",
        ".ai-sdlc/work-items/001-demo/handoff.md": "runtime state",
        ".ai-sdlc/state/checkpoint.yml": "current_stage: init",
        "internal-notes.md": "private material",
        "README.md": f"AI-SDLC v{candidate_version}",
    }

    findings = scan_paths(tmp_path, files)

    assert {finding.marker for finding in findings} == {
        "non-public-root-doc",
        "non-public-work-state",
        "pre-1.0-product-version",
        "runtime-state",
    }


def test_scan_rejects_repository_mismatch_and_local_path_disclosure(
    tmp_path: Path,
) -> None:
    local_path = "/" + "Users" + "/demo/project/sample"
    files = {
        "README.md": "https://github.com/example/sample\n" + local_path,
    }

    findings = scan_paths(tmp_path, files)

    assert {finding.marker for finding in findings} == {
        "local-path-disclosure",
        "repository-identity-mismatch",
    }


def test_required_surfaces_enforce_current_release_identity() -> None:
    files = {
        "README.md": (
            f"{CURRENT_REPOSITORY_URL}\nAI-SDLC 3.1.0\n{STABLE_SOURCE_CLONE}"
        ),
    }

    findings = validate_required_surfaces(files)

    assert any(
        finding.marker == "required-public-surface-missing" for finding in findings
    )
    assert not any(finding.path == "README.md" for finding in findings)


def test_scan_allows_current_release_and_dependency_versions(tmp_path: Path) -> None:
    files = {
        "README.md": f"{CURRENT_REPOSITORY_URL}\nAI-SDLC 3.1.0",
        "uv.lock": 'name = "example"\nversion = "3.4.2"',
        "managed/frontend/package-lock.json": '{"version":"3.3.0"}',
        "src/provider.py": 'release_ref = "refs/tags/rust-v0.138.0"',
        "tests/test_attestation.py": (
            'media_type = "application/vnd.dev.sigstore.bundle.v0.3+json"'
        ),
    }

    assert scan_paths(tmp_path, files) == []


def test_public_identity_does_not_require_release_history_documents() -> None:
    public_paths = set(REQUIRED_SURFACES)

    assert not any(path.startswith("docs/releases/") for path in public_paths)
    assert not any("prd" in path.casefold() for path in public_paths)
    assert "docs/v3-migration.zh-CN.md" in public_paths
    assert "docs/v2-migration.zh-CN.md" not in public_paths
    assert "docs/user-guide-release-standard.zh-CN.md" in public_paths


def test_user_guide_identity_requires_new_user_release_paths() -> None:
    markers = REQUIRED_SURFACES["USER_GUIDE.zh-CN.md"]

    assert "## 第一章：全新用户 + 全新空项目" in markers
    assert "## 第二章：全新用户 + 已有项目" in markers
    assert "ai-sdlc init ." in markers
    assert "ai-sdlc adopt ." in markers
    assert STABLE_SOURCE_CLONE not in markers
