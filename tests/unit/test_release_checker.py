from __future__ import annotations

import json
import subprocess
from pathlib import Path

from helix_mcp_knowledge.config import UpdateSettings
from helix_mcp_knowledge.release_checker import JOB_ID, ReleaseUpdateChecker
from helix_mcp_knowledge.storage.automation import AutomationStore


class Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class ReleaseRunner:
    def __init__(self, *, version: str = "1.3.0", fail: bool = False) -> None:
        self.version = version
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        rendered = [str(item) for item in command]
        self.calls.append(rendered)
        if self.fail:
            return subprocess.CompletedProcess(
                rendered,
                1,
                stdout="",
                stderr="authentication required",
            )
        return subprocess.CompletedProcess(
            rendered,
            0,
            stdout=json.dumps(
                {
                    "tagName": f"v{self.version}",
                    "isDraft": False,
                    "isPrerelease": False,
                    "publishedAt": "2026-09-05T10:00:00Z",
                    "url": f"https://github.test/releases/v{self.version}",
                }
            ),
            stderr="",
        )


def _checker(app, tmp_path: Path, runner: ReleaseRunner, clock: Clock) -> ReleaseUpdateChecker:
    gh = tmp_path / "gh"
    gh.write_text("command", encoding="utf-8")
    return ReleaseUpdateChecker(
        store=AutomationStore(app.database),
        settings=UpdateSettings(
            enabled=True,
            interval_hours=24,
            retry_minutes=15,
            repository="example/private",
            gh_command=str(gh),
            timeout_seconds=30,
        ),
        current_version="1.2.0",
        runner=runner,
        clock=clock,
        owner_id="checker-one",
    )


def test_release_checker_persists_available_release_and_respects_interval(
    app, tmp_path: Path
) -> None:
    runner = ReleaseRunner(version="1.3.0")
    clock = Clock(1_800_000_000.0)
    checker = _checker(app, tmp_path, runner, clock)

    first = checker.check()
    second = checker.check()

    assert first.status == "available"
    assert first.current_version == "1.2.0"
    assert first.latest_version == "1.3.0"
    assert first.update_available is True
    assert first.release_url == "https://github.test/releases/v1.3.0"
    assert second == first
    assert len(runner.calls) == 1
    assert AutomationStore(app.database).state(JOB_ID)["status"] == "available"


def test_release_checker_force_refreshes_a_cached_current_release(app, tmp_path: Path) -> None:
    runner = ReleaseRunner(version="1.2.0")
    clock = Clock(1_800_000_000.0)
    checker = _checker(app, tmp_path, runner, clock)

    first = checker.check()
    clock.value += 60
    second = checker.check(force=True)

    assert first.status == "current"
    assert first.update_available is False
    assert second.checked_at != first.checked_at
    assert len(runner.calls) == 2


def test_release_checker_retains_last_known_release_after_controlled_error(
    app, tmp_path: Path
) -> None:
    clock = Clock(1_800_000_000.0)
    initial_runner = ReleaseRunner(version="1.3.0")
    checker = _checker(app, tmp_path, initial_runner, clock)
    assert checker.check().status == "available"

    failing_runner = ReleaseRunner(fail=True)
    checker.runner = failing_runner
    clock.value += 60
    result = checker.check(force=True)

    assert result.status == "error"
    assert result.latest_version == "1.3.0"
    assert result.update_available is True
    assert result.error is not None and "authentication required" in result.error
    assert result.next_check_at is not None


def test_disabled_release_checker_never_calls_external_command(app, tmp_path: Path) -> None:
    runner = ReleaseRunner()
    checker = ReleaseUpdateChecker(
        store=AutomationStore(app.database),
        settings=UpdateSettings(enabled=False),
        current_version="1.2.0",
        runner=runner,
    )

    result = checker.check(force=True)

    assert result.status == "disabled"
    assert result.update_available is None
    assert runner.calls == []


def test_short_lived_server_stops_before_launching_background_check(app, tmp_path: Path) -> None:
    runner = ReleaseRunner()
    clock = Clock(1_800_000_000.0)
    checker = _checker(app, tmp_path, runner, clock)

    checker.start()
    checker.stop(timeout=1.0)

    assert runner.calls == []
    assert checker.status().status == "unknown"
