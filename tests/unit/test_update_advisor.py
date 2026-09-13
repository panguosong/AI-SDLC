"""Unit tests for installed runtime update advisor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_sdlc.core.update_advisor import (
    AUTO_NOTICE_REPEAT_INTERVAL,
    NOTICE_ACTIONABLE,
    NOTICE_LIGHT,
    _cache_path,
    ack_notice,
    detect_runtime_identity,
    evaluate_update_advisor,
    fetch_latest_github_release,
    notice_already_acknowledged,
    notice_recently_rendered,
    record_notice_rendered,
)


def test_latest_release_redirect_returns_existing_fetch_contract(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return "https://github.com/SinclairPan/Ai_AutoSDLC/releases/tag/v2.0.0"

        def read(self) -> bytes:
            raise AssertionError("release HTML body must not be read")

    def fake_urlopen(request, *, timeout: float):
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(
        "ai_sdlc.core.update_advisor.urllib.request.urlopen",
        fake_urlopen,
    )

    result = fetch_latest_github_release(1.5)

    assert seen == {
        "url": "https://github.com/SinclairPan/Ai_AutoSDLC/releases/latest",
        "method": "HEAD",
        "timeout": 1.5,
    }
    assert result == {
        "tag_name": "v2.0.0",
        "html_url": "https://github.com/SinclairPan/Ai_AutoSDLC/releases/tag/v2.0.0",
        "draft": False,
        "prerelease": False,
    }


@pytest.mark.parametrize(
    "final_url",
    [
        "http://github.com/SinclairPan/Ai_AutoSDLC/releases/tag/v2.0.0",
        "https://example.com/SinclairPan/Ai_AutoSDLC/releases/tag/v2.0.0",
        "https://user@github.com/SinclairPan/Ai_AutoSDLC/releases/tag/v2.0.0",
        "https://github.com:443/SinclairPan/Ai_AutoSDLC/releases/tag/v2.0.0",
        "https://github.com/Other/Ai_AutoSDLC/releases/tag/v2.0.0",
        "https://github.com/SinclairPan/Ai_AutoSDLC/releases/latest",
        "https://github.com/SinclairPan/Ai_AutoSDLC/releases/tag/v2.0",
        "https://github.com/SinclairPan/Ai_AutoSDLC/releases/tag/v2.0.0?x=1",
        "https://github.com/SinclairPan/Ai_AutoSDLC/releases/tag/v2.0.0#x",
    ],
)
def test_latest_release_redirect_rejects_noncanonical_final_url(
    monkeypatch,
    final_url: str,
) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return final_url

        def read(self) -> bytes:
            raise AssertionError("release HTML body must not be read")

    monkeypatch.setattr(
        "ai_sdlc.core.update_advisor.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )

    with pytest.raises(ValueError, match="canonical GitHub release tag URL"):
        fetch_latest_github_release(1.5)


def _force_installed(monkeypatch, tmp_path, *, channel: str = "github-archive") -> None:
    monkeypatch.setenv("AI_SDLC_UPDATE_ADVISOR_TEST_INSTALLED", "1")
    monkeypatch.setenv("AI_SDLC_UPDATE_ADVISOR_TEST_VERSION", "1.0.0")
    monkeypatch.setenv("AI_SDLC_UPDATE_ADVISOR_TEST_CHANNEL", channel)
    monkeypatch.setenv("AI_SDLC_UPDATE_ADVISOR_CACHE_DIR", str(tmp_path))


def test_source_runtime_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AI_SDLC_SOURCE_RUNTIME", "1")
    monkeypatch.setenv("AI_SDLC_UPDATE_ADVISOR_CACHE_DIR", str(tmp_path))

    identity = detect_runtime_identity()
    evaluation = evaluate_update_advisor()

    assert identity.installed_runtime is False
    assert evaluation.refresh_attempted is False
    assert evaluation.refresh_result == "disabled"
    assert evaluation.eligible_notice_classes == ()


def test_installed_module_invocation_is_installed_runtime(monkeypatch, tmp_path) -> None:
    site_packages = tmp_path / "site-packages"
    executable = site_packages / "ai_sdlc" / "__main__.py"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")

    class FakeDistribution:
        version = "1.1.0"

        def read_text(self, name: str) -> str | None:
            return None

        def locate_file(self, path: str) -> Path:
            return site_packages / path

    monkeypatch.setattr("sys.argv", [str(executable), "self-update", "check"])
    monkeypatch.setattr(
        "ai_sdlc.core.update_advisor.metadata.distribution",
        lambda name: FakeDistribution(),
    )
    monkeypatch.setenv("AI_SDLC_UPDATE_ADVISOR_CACHE_DIR", str(tmp_path / "cache"))

    identity = detect_runtime_identity()

    assert identity.installed_runtime is True
    assert identity.installed_version == "1.1.0"
    assert identity.reason_code == "installed_runtime"


def test_github_archive_installed_runtime_gets_actionable_notice(
    monkeypatch, tmp_path
) -> None:
    _force_installed(monkeypatch, tmp_path, channel="github-archive")
    monkeypatch.setenv("AI_SDLC_UPDATE_ADVISOR_TEST_LATEST_VERSION", "v1.0.1")

    evaluation = evaluate_update_advisor(
        now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    )

    assert evaluation.refresh_attempted is True
    assert evaluation.refresh_result == "success"
    assert evaluation.freshness == "fresh"
    assert evaluation.upstream_latest_version == "1.0.1"
    assert evaluation.channel_latest_version == "1.0.1"
    assert NOTICE_LIGHT in evaluation.eligible_notice_classes
    assert NOTICE_ACTIONABLE in evaluation.eligible_notice_classes
    assert evaluation.upgrade_command == "ai-sdlc self-update check"


def test_cache_path_sanitizes_runtime_identity_for_windows(monkeypatch, tmp_path) -> None:
    _force_installed(monkeypatch, tmp_path, channel="github-archive")

    identity = detect_runtime_identity()

    assert identity.runtime_identity.startswith("sha256:")
    assert ":" not in _cache_path(identity).name


def test_unknown_installed_channel_still_gets_actionable_update(
    monkeypatch, tmp_path
) -> None:
    _force_installed(monkeypatch, tmp_path, channel="unknown")
    monkeypatch.setenv("AI_SDLC_UPDATE_ADVISOR_TEST_LATEST_VERSION", "v1.0.1")

    evaluation = evaluate_update_advisor()

    assert evaluation.upstream_latest_version == "1.0.1"
    assert evaluation.channel_latest_version == "1.0.1"
    assert NOTICE_LIGHT in evaluation.eligible_notice_classes
    assert NOTICE_ACTIONABLE in evaluation.eligible_notice_classes
    assert evaluation.upgrade_command == "ai-sdlc self-update check"


def test_failure_backoff_prevents_repeated_refresh(monkeypatch, tmp_path) -> None:
    _force_installed(monkeypatch, tmp_path)
    calls = 0

    def fail_fetch(timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise OSError("network unavailable")

    first = evaluate_update_advisor(
        now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        fetch_latest=fail_fetch,
    )
    second = evaluate_update_advisor(
        now=datetime(2026, 5, 1, 13, 0, tzinfo=UTC),
        fetch_latest=fail_fetch,
    )

    assert first.refresh_attempted is True
    assert first.refresh_result == "network_error"
    assert second.refresh_attempted is False
    assert second.refresh_result == "backoff"
    assert calls == 1


def test_explicit_check_can_ignore_failure_backoff(monkeypatch, tmp_path) -> None:
    _force_installed(monkeypatch, tmp_path)
    calls = 0

    def fail_fetch(timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise OSError("network unavailable")

    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    first = evaluate_update_advisor(now=now, fetch_latest=fail_fetch)
    second = evaluate_update_advisor(
        now=now + timedelta(hours=1),
        fetch_latest=fail_fetch,
        ignore_failure_backoff=True,
    )

    assert first.refresh_attempted is True
    assert second.refresh_attempted is True
    assert second.refresh_result == "network_error"
    assert calls == 2


def test_stale_cache_still_emits_known_update_notice_without_refresh(
    monkeypatch, tmp_path
) -> None:
    _force_installed(monkeypatch, tmp_path)
    monkeypatch.setenv("AI_SDLC_UPDATE_ADVISOR_TEST_LATEST_VERSION", "v1.0.1")
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    evaluate_update_advisor(now=now)

    stale = evaluate_update_advisor(now=now + timedelta(days=2), allow_refresh=False)

    assert stale.freshness == "stale_but_usable"
    assert stale.refresh_attempted is False
    assert NOTICE_LIGHT in stale.eligible_notice_classes
    assert NOTICE_ACTIONABLE in stale.eligible_notice_classes
    assert stale.upgrade_command == "ai-sdlc self-update check"


def test_future_cache_timestamp_forces_refresh(monkeypatch, tmp_path) -> None:
    _force_installed(monkeypatch, tmp_path)
    calls = 0

    def fetch_latest(timeout: float) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"tag_name": "v1.0.1", "draft": False, "prerelease": False}

    future = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    evaluate_update_advisor(now=future, fetch_latest=fetch_latest)

    after_rollback = evaluate_update_advisor(
        now=future - timedelta(days=1),
        fetch_latest=fetch_latest,
    )

    assert after_rollback.refresh_attempted is True
    assert after_rollback.refresh_result == "success"
    assert calls == 2


def test_future_cache_timestamp_is_expired_without_refresh(
    monkeypatch, tmp_path
) -> None:
    _force_installed(monkeypatch, tmp_path)
    monkeypatch.setenv("AI_SDLC_UPDATE_ADVISOR_TEST_LATEST_VERSION", "v1.0.1")
    future = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    evaluate_update_advisor(now=future)

    after_rollback = evaluate_update_advisor(
        now=future - timedelta(days=1),
        allow_refresh=False,
    )

    assert after_rollback.freshness == "expired"
    assert after_rollback.refresh_attempted is False
    assert after_rollback.eligible_notice_classes == ()


def test_rendered_notice_throttles_without_acknowledging(
    monkeypatch, tmp_path
) -> None:
    _force_installed(monkeypatch, tmp_path)
    monkeypatch.setenv("AI_SDLC_UPDATE_ADVISOR_TEST_LATEST_VERSION", "v1.0.1")
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    evaluation = evaluate_update_advisor(now=now)

    recorded = record_notice_rendered(NOTICE_ACTIONABLE, "1.0.1", now=now)

    assert recorded is True
    assert notice_already_acknowledged(evaluation, NOTICE_ACTIONABLE) is False
    assert notice_recently_rendered(
        evaluation,
        NOTICE_ACTIONABLE,
        now=now + AUTO_NOTICE_REPEAT_INTERVAL - timedelta(seconds=1),
    )
    assert not notice_recently_rendered(
        evaluation,
        NOTICE_ACTIONABLE,
        now=now + AUTO_NOTICE_REPEAT_INTERVAL + timedelta(seconds=1),
    )


def test_ack_notice_records_notice_version(monkeypatch, tmp_path) -> None:
    _force_installed(monkeypatch, tmp_path)
    monkeypatch.setenv("AI_SDLC_UPDATE_ADVISOR_TEST_LATEST_VERSION", "v1.0.1")
    evaluation = evaluate_update_advisor()

    ack = ack_notice(NOTICE_LIGHT, "1.0.1")

    assert ack.ack_recorded is True
    assert notice_already_acknowledged(evaluation, NOTICE_LIGHT) is True
