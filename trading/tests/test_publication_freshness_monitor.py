"""Tests for the off-box publication staleness monitor.

Entirely offline: the manifest fetch and every GitHub REST call go through
injected fakes, and time is passed in explicitly, so nothing here sleeps,
resolves DNS, or depends on the wall clock.
"""

from __future__ import annotations

import http.client
import importlib.util
import io
import json
import re
import sys
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "publication-freshness.yml"
VERIFY_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "verify.yml"


def _load(name: str):
    """Import one monitor module by path, as the other scripts/ tests here do.

    Registering it in ``sys.modules`` first matters: dataclass processing looks
    the owning module up by name, and a module missing from that table makes
    ``@dataclass`` fail outright on Python 3.11.
    """

    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The monitor's modules import each other by bare name; ``_load`` registers each
# in ``sys.modules`` before the next is executed, so no sys.path mutation is
# needed here (and none is left behind for the rest of the session).
core = _load("publication_freshness_core")
github = _load("publication_freshness_github")
cli = _load("check_publication_freshness")


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
MANIFEST_URL = "https://example.invalid/weather_edge/publication_manifest.json"


def _manifest(published_at: str, **overrides: Any) -> dict[str, Any]:
    """A manifest shaped like the live one, stale artifacts included.

    ``forecast_data.json`` really is two months old on production -- artifacts
    are preserved across publishes -- which is why the monitor must read the
    top-level ``published_at`` and nothing else.
    """

    payload: dict[str, Any] = {
        "artifacts": {
            "cities_data.json": {
                "generated_at": published_at,
                "sha256": "f821e7a67a218101dd07f82cbbdb24c954e53b87b4c52456c1f3394eec281ecb",
                "status": "ready",
            },
            "forecast_data.json": {
                "generated_at": "2026-07-09T21:48:53+00:00",
                "sha256": "b6ee97371a24bf467ad3543a710b588868a65cba6cc80878e7781d31e2ff060d",
                "status": "ready",
            },
        },
        "provenance": {
            "source_dirty": False,
            "source_sha": "2a6432e3bdb29fa1798a4b07e5f5396685b5245b",
            "synced_at_utc": "2026-09-05T04:27:51Z",
        },
        "published_at": published_at,
        "schema_version": 1,
        "snapshot_id": "53481cfb43b904dfcf1e7701",
    }
    payload.update(overrides)
    return payload


def _fetch(
    published_at: str | None,
    *,
    http_date: datetime | None = None,
    error: str | None = None,
    **kwargs: Any,
):
    manifest = None if published_at is None else _manifest(published_at)
    return core.ManifestFetch(
        url=MANIFEST_URL,
        manifest=manifest,
        http_date=http_date,
        error=error,
        **kwargs,
    )


def _fetch_at(age_minutes: float, *, now: datetime = NOW, **kwargs: Any):
    published = now - timedelta(minutes=age_minutes)
    return _fetch(published.isoformat(), **kwargs)


def _page_number(path: str) -> int:
    """The `page=` value of a request path.

    Parsed from the end on purpose: a naive `"page=1" in path` also matches the
    `per_page=100` that sits beside it.
    """

    return int(path.rsplit("page=", 1)[1])


class _FakeResponse:
    """Minimal ``urlopen`` context manager: body plus headers."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = io.BytesIO(body)
        self.headers = dict(headers or {})

    def read(self) -> bytes:
        return self._body.read()

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class FakeGitHub:
    """In-memory GitHub REST double: serves the few endpoints the monitor
    touches and records every call for assertions on method, path and body."""

    def __init__(
        self,
        *,
        deployments: list[dict[str, Any]] | None = None,
        statuses: list[dict[str, Any]] | None = None,
        issues: list[dict[str, Any]] | None = None,
        closed_issues: list[dict[str, Any]] | None = None,
        comments: list[dict[str, Any]] | None = None,
        label_exists: bool = True,
    ) -> None:
        self.deployments = deployments or []
        self.statuses = statuses or []
        self.issues = issues or []
        self.closed_issues = closed_issues or []
        self.comments = comments or []
        self.label_exists = label_exists
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        if method == "GET" and "/deployments?" in path:
            return self.deployments
        if method == "GET" and "/statuses" in path:
            return self.statuses
        if method == "GET" and "/issues?" in path:
            # These fakes hold one short page; a real second page would be empty.
            if _page_number(path) != 1:
                return []
            if "labels=" in path and not self.label_exists:
                return []
            return self.closed_issues if "state=closed" in path else self.issues
        if method == "GET" and "/comments?" in path:
            return self.comments if _page_number(path) == 1 else []
        if method == "GET" and "/labels/" in path:
            if not self.label_exists:
                raise core.GitHubApiError("GET labels -> HTTP 404")
            return {"name": core.ISSUE_LABEL}
        if method == "POST" and path.endswith("/labels"):
            self.label_exists = True
            return {"name": core.ISSUE_LABEL}
        if method == "POST" and path.endswith("/issues"):
            return {"number": 999}
        if method == "POST" and path.endswith("/comments"):
            return {"id": 1}
        if method == "PATCH":
            return {"number": 999, "state": "closed"}
        raise AssertionError(f"unexpected call: {method} {path}")

    def paths(self, method: str) -> list[str]:
        return [path for call_method, path, _ in self.calls if call_method == method]


# --- evaluate() ---


def test_evaluate_uses_published_at_not_artifact_generated_at():
    # published_at is 5 minutes old; an artifact inside is two months old.
    fetch = _fetch_at(5)
    assert fetch.manifest is not None
    assert fetch.manifest["artifacts"]["forecast_data.json"]["generated_at"] == "2026-07-09T21:48:53+00:00"

    verdict = core.evaluate(fetch, NOW)

    assert verdict.state == core.STATE_FRESH
    assert verdict.age_minutes == pytest.approx(5.0)
    assert verdict.snapshot_id == "53481cfb43b904dfcf1e7701"
    assert verdict.source_sha == "2a6432e3bdb29fa1798a4b07e5f5396685b5245b"


def test_evaluate_prefers_http_date_when_runner_clock_skewed_over_300s():
    published = NOW - timedelta(minutes=5)
    # The origin says it is 5 minutes after the publish; the runner clock is two
    # hours ahead. Believing the runner would report a two-hour outage.
    skewed_runner_now = NOW + timedelta(hours=2)

    verdict = core.evaluate(
        _fetch(published.isoformat(), http_date=NOW),
        skewed_runner_now,
    )

    assert verdict.reference_source == "http-date"
    assert verdict.age_minutes == pytest.approx(5.0)
    assert verdict.state == core.STATE_FRESH


def test_evaluate_keeps_runner_clock_when_skew_is_within_tolerance():
    published = NOW - timedelta(minutes=200)
    runner_now = NOW + timedelta(seconds=299)

    verdict = core.evaluate(_fetch(published.isoformat(), http_date=NOW), runner_now)

    assert verdict.reference_source == "runner-clock"
    assert verdict.state == core.STATE_STALE


def test_evaluate_keeps_runner_clock_at_exactly_the_skew_tolerance():
    # The boundary itself, so that loosening `>` to `>=` cannot pass unnoticed.
    published = NOW - timedelta(minutes=200)
    runner_now = NOW + timedelta(seconds=core.CLOCK_SKEW_TOLERANCE_SECONDS)

    verdict = core.evaluate(_fetch(published.isoformat(), http_date=NOW), runner_now)

    assert verdict.reference_source == "runner-clock"

    one_second_further = runner_now + timedelta(seconds=1)
    beyond = core.evaluate(_fetch(published.isoformat(), http_date=NOW), one_second_further)
    assert beyond.reference_source == "http-date"


def test_cached_http_date_does_not_make_a_live_outage_look_fresh():
    # A Fastly HIT answers with the Date the object was CACHED. Believing it
    # turns a three-hour outage into "4 minutes old" -- and then closes the
    # issue that is reporting the outage.
    published = NOW - timedelta(hours=3)
    cached_date = published + timedelta(minutes=4)
    fetch = _fetch(
        published.isoformat(),
        http_date=cached_date,
        http_age_seconds=float(176 * 60),
        cache_hit=True,
    )

    verdict = core.evaluate(fetch, NOW)

    # Age is added back, so the origin clock lands on the real now.
    assert verdict.age_minutes == pytest.approx(180.0, abs=0.5)
    assert verdict.state == core.STATE_STALE
    issue = core.OpenIssue(number=7, body=core.MONITOR_MARKER)
    assert core.plan_action(verdict, issue, 90.0, 20.0).kind != core.ACTION_CLOSE


def test_uncorrectable_cache_hit_is_refused_as_a_clock():
    # A hit with no Age header cannot be corrected, so the runner clock stands.
    published = NOW - timedelta(hours=3)
    fetch = _fetch(published.isoformat(), http_date=published + timedelta(minutes=4), cache_hit=True)

    verdict = core.evaluate(fetch, NOW)

    assert core.origin_clock(fetch) is None
    assert verdict.reference_source == "runner-clock"
    assert verdict.age_minutes == pytest.approx(180.0)


def test_future_published_at_is_never_acted_on():
    published = NOW + timedelta(minutes=30)

    verdict = core.evaluate(_fetch(published.isoformat()), NOW)
    action = core.plan_action(verdict, None, 90.0, 20.0)

    assert verdict.state == core.STATE_FUTURE
    assert action.kind == core.ACTION_NONE
    # And an already-open issue is left exactly as it is, never auto-closed.
    issue = core.OpenIssue(number=7)
    assert core.plan_action(verdict, issue, 90.0, 20.0).kind == core.ACTION_NONE


def test_a_clock_fault_beyond_the_open_threshold_is_reported_not_swallowed():
    # A box whose clock jumps days forward stamps a future published_at and then
    # dies. Refusing to read that as freshness is right; saying nothing is not --
    # the box cannot catch this itself, it validates against the same clock.
    published = NOW + timedelta(days=3)

    verdict = core.evaluate(_fetch(published.isoformat()), NOW)
    action = core.plan_action(verdict, None, 90.0, 20.0)

    assert verdict.state == core.STATE_FUTURE
    assert action.kind == core.ACTION_CREATE
    assert "future" in action.reason
    # Still never closes, and never files a second issue.
    issue = core.OpenIssue(number=7, body=core.MONITOR_MARKER)
    assert core.plan_action(verdict, issue, 90.0, 20.0).kind == core.ACTION_NONE


def test_small_future_skew_inside_tolerance_is_still_fresh():
    published = NOW + timedelta(minutes=2)

    verdict = core.evaluate(_fetch(published.isoformat()), NOW)

    assert verdict.state == core.STATE_FRESH


def test_future_skew_at_exactly_the_tolerance_is_still_fresh():
    # The boundary itself: tightening `<` to `<=` must fail here.
    published = NOW + timedelta(minutes=core.FUTURE_TOLERANCE_MINUTES)

    assert core.evaluate(_fetch(published.isoformat()), NOW).state == core.STATE_FRESH

    one_second_further = published + timedelta(seconds=1)
    assert core.evaluate(_fetch(one_second_further.isoformat()), NOW).state == core.STATE_FUTURE


def test_unreadable_manifest_takes_no_issue_action():
    # Transient failures only: a timeout, a reset, a 5xx. These warn and wait.
    for fetch in (
        _fetch(None, error="URLError: [Errno -2] Name or service not known"),
        _fetch(None, error="HTTP 503 Service Unavailable"),
    ):
        verdict = core.evaluate(fetch, NOW)
        assert verdict.state == core.STATE_UNREADABLE
        assert core.plan_action(verdict, None, 90.0, 20.0).kind == core.ACTION_NONE
        issue = core.OpenIssue(number=7)
        assert core.plan_action(verdict, issue, 90.0, 20.0).kind == core.ACTION_NONE


def test_structurally_missing_manifest_opens_an_issue_instead_of_warning_forever():
    # gh-pages is force-re-rooted roughly every ten days and a failed Pages build
    # serves a 404 page. A site that is GONE is a worse outage than a stale one,
    # and the old behaviour was ::warning:: on a green job, 96 times a day,
    # indefinitely -- the exact "nobody noticed" failure this monitor exists for.
    for fetch in (
        _fetch(None, error="HTTP 404 Not Found", structural=True),
        core.ManifestFetch(url=MANIFEST_URL, manifest={"schema_version": 1}),
        core.ManifestFetch(url=MANIFEST_URL, manifest={"published_at": "not a timestamp"}),
    ):
        verdict = core.evaluate(fetch, NOW)
        assert verdict.state == core.STATE_BROKEN
        assert core.plan_action(verdict, None, 90.0, 20.0).kind == core.ACTION_CREATE
        # One issue, then quiet: there is no age to escalate on, and nothing
        # here can ever count as a recovery.
        issue = core.OpenIssue(number=7, body=core.MONITOR_MARKER)
        assert core.plan_action(verdict, issue, 90.0, 20.0).kind == core.ACTION_NONE


def test_a_404_is_structural_but_a_5xx_or_transport_error_is_not():
    def http_error(code: str | int):
        def opener(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, int(code), "boom", {}, None)

        return opener

    assert core.fetch_manifest(MANIFEST_URL, NOW, opener=http_error(404)).structural is True
    assert core.fetch_manifest(MANIFEST_URL, NOW, opener=http_error(410)).structural is True
    assert core.fetch_manifest(MANIFEST_URL, NOW, opener=http_error(503)).structural is False

    def html_page(request, timeout=None):
        return _FakeResponse(b"<html>404</html>", {})

    assert core.fetch_manifest(MANIFEST_URL, NOW, opener=html_page).structural is True


def test_fetch_manifest_retries_a_transient_failure_without_retrying_a_404():
    slept: list[float] = []
    attempts: list[str] = []

    def flaky(request, timeout=None):
        attempts.append(request.full_url)
        if len(attempts) < 3:
            raise OSError("connection reset by peer")
        return _FakeResponse(json.dumps(_manifest(NOW.isoformat())).encode("utf-8"), {})

    fetch = core.fetch_manifest_with_retries(
        MANIFEST_URL, NOW, opener=flaky, sleeper=slept.append
    )

    assert fetch.error is None
    assert len(attempts) == 3
    assert slept == [core.RETRY_BACKOFF_SECONDS, core.RETRY_BACKOFF_SECONDS]
    # Each retry carries its own probe value, so a retry cannot be answered by
    # whatever answered the attempt that just failed.
    assert len(set(attempts)) == 3

    dead: list[str] = []

    def gone(request, timeout=None):
        dead.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    fetch = core.fetch_manifest_with_retries(MANIFEST_URL, NOW, opener=gone, sleeper=slept.append)

    assert fetch.structural is True
    assert len(dead) == 1, "a 404 will still be a 404 in fifteen seconds"


def test_fetch_manifest_survives_a_raw_http_protocol_error():
    # BadStatusLine and friends subclass neither OSError nor ValueError, so an
    # uncaught one is a red scheduled run with no verdict at all.
    def truncated(request, timeout=None):
        raise http.client.BadStatusLine("garbage")

    fetch = core.fetch_manifest(MANIFEST_URL, NOW, opener=truncated)

    assert fetch.manifest is None
    assert "BadStatusLine" in (fetch.error or "")
    assert core.evaluate(fetch, NOW).state == core.STATE_UNREADABLE


def test_unreadable_manifest_exits_zero_without_touching_github(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "fetch_manifest_with_retries",
        lambda url, now, **kwargs: _fetch(None, error="HTTP 503 Service Unavailable"),
    )
    monkeypatch.setattr(
        cli,
        "make_api",
        lambda *args, **kwargs: pytest.fail("no GitHub call may happen for an unreadable manifest"),
    )
    monkeypatch.setenv("GH_TOKEN", "token")

    exit_code = cli.main(["--manifest-url", MANIFEST_URL])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "UNREADABLE" in out
    assert "::warning::" in out


def test_structurally_broken_manifest_reports_and_files(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "fetch_manifest_with_retries",
        lambda url, now, **kwargs: _fetch(None, error="HTTP 404 Not Found", structural=True),
    )
    api = FakeGitHub()
    monkeypatch.setattr(cli, "make_api", lambda *args, **kwargs: api)
    monkeypatch.setenv("GH_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    exit_code = cli.main(["--manifest-url", MANIFEST_URL])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "::error::published manifest is unusable" in out
    created = [body for method, path, body in api.calls if method == "POST" and path.endswith("/issues")]
    assert len(created) == 1
    assert "404" in created[0]["body"]

    # With no way to file anything, the run must go red rather than pass green:
    # a failed scheduled run is the last remaining notification channel.
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert cli.main(["--manifest-url", MANIFEST_URL]) == 1


def test_clock_fault_is_always_annotated(monkeypatch, capsys):
    ahead = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    monkeypatch.setattr(cli, "fetch_manifest_with_retries", lambda url, now, **kwargs: _fetch(ahead))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    exit_code = cli.main(["--manifest-url", MANIFEST_URL, "--dry-run"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "::warning::publication clock fault" in out
    assert f"PLAN {core.ACTION_NONE}" in out


# --- open_threshold() ---


def test_base_threshold_boundaries_89_90_91_minutes_without_deployment():
    thresholds = core.Thresholds()

    for age, expected in ((89.0, core.ACTION_NONE), (90.0, core.ACTION_CREATE), (91.0, core.ACTION_CREATE)):
        verdict = core.evaluate(_fetch_at(age), NOW)
        assert verdict.state == core.STATE_STALE
        threshold = core.open_threshold(verdict, None, thresholds)
        assert threshold == 90.0
        action = core.plan_action(verdict, None, threshold, thresholds.close_at_or_below_minutes)
        assert action.kind == expected, f"age {age} min"


def test_in_progress_deployment_raises_threshold_to_expected_window_plus_margin():
    verdict = core.evaluate(_fetch_at(200), NOW)
    assert verdict.published_at is not None
    deployment = core.DeploymentRecord(
        identifier=41,
        created_at=verdict.published_at + timedelta(minutes=1),
        expected_dark_minutes=240.0,
        state="in_progress",
    )

    threshold = core.open_threshold(verdict, deployment, core.Thresholds())

    assert threshold == 285.0  # 240 + 45
    assert core.plan_action(verdict, None, threshold, 20.0).kind == core.ACTION_NONE
    # Past the declared window plus margin it opens anyway.
    later = core.evaluate(_fetch_at(290), NOW)
    assert core.plan_action(later, None, threshold, 20.0).kind == core.ACTION_CREATE


def test_deployment_with_no_status_yet_is_treated_as_in_flight():
    verdict = core.evaluate(_fetch_at(200), NOW)
    assert verdict.published_at is not None
    deployment = core.DeploymentRecord(
        identifier=42,
        created_at=verdict.published_at,
        expected_dark_minutes=120.0,
        state=None,
    )

    assert core.open_threshold(verdict, deployment, core.Thresholds()) == 165.0


def test_deployment_without_expected_dark_minutes_gets_a_typical_window_not_the_ceiling():
    # A deploy that dies without ever posting a terminal status is exactly both
    # September outages. Defaulting its unclaimed window to the maximum would
    # regress detection of that case from 90 minutes to 8 h 45 min.
    verdict = core.evaluate(_fetch_at(200), NOW)
    assert verdict.published_at is not None
    deployment = core.DeploymentRecord(
        identifier=43,
        created_at=verdict.published_at,
        expected_dark_minutes=None,
        state="in_progress",
    )

    assert core.open_threshold(verdict, deployment, core.Thresholds()) == 135.0  # 90 + 45


def test_a_deployment_record_can_only_raise_the_threshold_never_lower_it():
    # A routine deploy honestly declaring a five-minute dark window must not
    # turn the false-positive suppressor into a false-positive generator.
    verdict = core.evaluate(_fetch_at(50), NOW)
    assert verdict.published_at is not None
    thresholds = core.Thresholds()

    for claimed in (0.0, 1.0, 5.0, 44.0, 45.0):
        deployment = core.DeploymentRecord(
            identifier=50,
            created_at=verdict.published_at + timedelta(minutes=1),
            expected_dark_minutes=claimed,
            state="in_progress",
        )
        threshold = core.open_threshold(verdict, deployment, thresholds)
        assert threshold >= thresholds.base_open_minutes, claimed
        # ...and therefore no issue at 50 minutes, just as with no record at all.
        assert core.plan_action(verdict, None, threshold, 20.0).kind == core.ACTION_NONE, claimed

    assert core.open_threshold(verdict, None, thresholds) == 90.0


def test_failure_deployment_status_opens_issue_after_20_minutes():
    thresholds = core.Thresholds()
    for state in ("failure", "error"):
        verdict = core.evaluate(_fetch_at(25), NOW)
        assert verdict.published_at is not None
        deployment = core.DeploymentRecord(
            identifier=44,
            created_at=verdict.published_at + timedelta(minutes=1),
            expected_dark_minutes=240.0,
            state=state,
        )

        threshold = core.open_threshold(verdict, deployment, thresholds)

        assert threshold == 20.0, state
        assert core.plan_action(verdict, None, threshold, 20.0).kind == core.ACTION_CREATE
        # 19 minutes is still below it.
        early = core.evaluate(_fetch_at(19), NOW)
        assert core.plan_action(early, None, threshold, 20.0).kind == core.ACTION_NONE


def test_succeeded_deployment_does_not_suppress_beyond_the_base_threshold():
    verdict = core.evaluate(_fetch_at(120), NOW)
    assert verdict.published_at is not None
    deployment = core.DeploymentRecord(
        identifier=45,
        created_at=verdict.published_at + timedelta(minutes=1),
        expected_dark_minutes=400.0,
        state="success",
    )

    assert core.open_threshold(verdict, deployment, core.Thresholds()) == 90.0


def test_deployment_older_than_published_at_is_ignored():
    verdict = core.evaluate(_fetch_at(200), NOW)
    assert verdict.published_at is not None
    # A deployment that finished long before the last successful publish cannot
    # explain the current dark window.
    deployment = core.DeploymentRecord(
        identifier=46,
        created_at=verdict.published_at - timedelta(minutes=21),
        expected_dark_minutes=400.0,
        state="in_progress",
    )

    assert core.open_threshold(verdict, deployment, core.Thresholds()) == 90.0
    # Exactly at the lookback edge it still counts.
    inside = core.DeploymentRecord(
        identifier=47,
        created_at=verdict.published_at - timedelta(minutes=20),
        expected_dark_minutes=400.0,
        state="in_progress",
    )
    assert core.open_threshold(verdict, inside, core.Thresholds()) == 445.0


def test_deployment_record_never_suppresses_beyond_525_minutes():
    verdict = core.evaluate(_fetch_at(600), NOW)
    assert verdict.published_at is not None
    # A deploy record claiming a 100000-minute dark window -- or one that simply
    # never reaches a terminal status -- must not buy unlimited silence.
    deployment = core.DeploymentRecord(
        identifier=48,
        created_at=verdict.published_at + timedelta(minutes=1),
        expected_dark_minutes=100000.0,
        state="in_progress",
    )

    threshold = core.open_threshold(verdict, deployment, core.Thresholds())

    assert threshold == 525.0
    assert core.plan_action(verdict, None, threshold, 20.0).kind == core.ACTION_CREATE

    # The ceiling holds for every claimed window, not just this one value.
    thresholds = core.Thresholds()
    assert thresholds.suppression_ceiling_minutes == 525.0
    for claimed in (0.0, 1.0, 479.0, 480.0, 481.0, 5000.0, 1e9):
        record = core.DeploymentRecord(
            identifier=49,
            created_at=verdict.published_at + timedelta(minutes=1),
            expected_dark_minutes=claimed,
            state="queued",
        )
        bounded = core.open_threshold(verdict, record, thresholds)
        assert bounded <= 525.0, claimed
        # Bounded at both ends: a record may raise the bar, never lower it.
        assert bounded >= thresholds.base_open_minutes, claimed


def test_latest_deployment_returns_none_when_no_records_exist_yet():
    # Nothing writes these records today; the monitor must not crash on that.
    api = FakeGitHub(deployments=[])

    assert github.latest_deployment(api, "owner/repo") is None


def test_latest_deployment_reads_payload_and_status():
    api = FakeGitHub(
        deployments=[
            {
                "id": 77,
                "created_at": "2026-09-15T02:30:00Z",
                "environment": "production-box",
                "payload": {"expected_dark_minutes": 120},
            }
        ],
        statuses=[{"state": "IN_PROGRESS"}],
    )

    record = github.latest_deployment(api, "owner/repo")

    assert record is not None
    assert record.identifier == 77
    assert record.expected_dark_minutes == 120.0
    assert record.state == "in_progress"
    assert record.created_at == datetime(2026, 9, 15, 2, 30, tzinfo=UTC)
    assert "environment=production-box" in api.paths("GET")[0]


def test_latest_deployment_accepts_a_json_encoded_payload_string():
    api = FakeGitHub(
        deployments=[
            {
                "id": 78,
                "created_at": "2026-09-15T02:30:00Z",
                "payload": json.dumps({"expected_dark_minutes": 60}),
            }
        ],
        statuses=[],
    )

    record = github.latest_deployment(api, "owner/repo")

    assert record is not None
    assert record.expected_dark_minutes == 60.0
    assert record.state is None


# --- plan_action() ---


def test_plan_creates_issue_once_and_dedupes_bucket_comments():
    thresholds = core.Thresholds()

    stale = core.evaluate(_fetch_at(120), NOW)
    assert core.plan_action(stale, None, 90.0, 20.0).kind == core.ACTION_CREATE

    # With the issue open, no second create -- and nothing at all before 6 h.
    issue = core.OpenIssue(number=12, body=core.MONITOR_MARKER)
    assert core.plan_action(stale, issue, 90.0, 20.0).kind == core.ACTION_NONE

    six_hours = core.evaluate(_fetch_at(6 * 60 + 5), NOW)
    first = core.plan_action(six_hours, issue, 90.0, 20.0)
    assert first.kind == core.ACTION_COMMENT
    assert first.bucket_hours == 6

    # Once the 6 h marker is on the thread, the next run says nothing more.
    commented = core.OpenIssue(
        number=12,
        body=core.MONITOR_MARKER,
        comment_bodies=(core.BUCKET_MARKER_TEMPLATE.format(bucket=6) + "\nstill stale",),
    )
    later = core.evaluate(_fetch_at(6 * 60 + 40), NOW)
    assert core.plan_action(later, commented, 90.0, 20.0).kind == core.ACTION_NONE

    # 12 h is a new bucket.
    twelve = core.evaluate(_fetch_at(12 * 60 + 1), NOW)
    second = core.plan_action(twelve, commented, 90.0, 20.0)
    assert second.kind == core.ACTION_COMMENT
    assert second.bucket_hours == 12
    assert thresholds.base_open_minutes == 90


def test_escalation_buckets_are_6_12_24_then_daily():
    assert core.escalation_bucket(5 * 60 + 59) is None
    assert core.escalation_bucket(6 * 60) == 6
    assert core.escalation_bucket(11 * 60 + 59) == 6
    assert core.escalation_bucket(12 * 60) == 12
    assert core.escalation_bucket(23 * 60 + 59) == 12
    assert core.escalation_bucket(24 * 60) == 24
    assert core.escalation_bucket(47 * 60) == 24
    assert core.escalation_bucket(48 * 60) == 48
    # The real 46.8 h outage would have produced 6 h, 12 h, 24 h and 48 h pings.
    assert core.escalation_bucket(46.8 * 60) == 24


def test_plan_closes_only_at_or_below_20_minutes():
    issue = core.OpenIssue(number=12, body=core.MONITOR_MARKER)

    at_threshold = core.evaluate(_fetch_at(20), NOW)
    assert at_threshold.state == core.STATE_FRESH
    assert core.plan_action(at_threshold, issue, 90.0, 20.0).kind == core.ACTION_CLOSE

    below = core.evaluate(_fetch_at(4), NOW)
    assert core.plan_action(below, issue, 90.0, 20.0).kind == core.ACTION_CLOSE

    # 21 minutes is stale, not a recovery: the issue stays open.
    above = core.evaluate(_fetch_at(21), NOW)
    assert above.state == core.STATE_STALE
    assert core.plan_action(above, issue, 90.0, 20.0).kind == core.ACTION_NONE

    # Nothing to close when nothing is open.
    assert core.plan_action(below, None, 90.0, 20.0).kind == core.ACTION_NONE

    # plan_action enforces the close threshold on its own, independently of the
    # window evaluate() used to call something fresh. A verdict marked fresh
    # under a wider window is still not a recovery at 45 minutes old.
    wide = core.evaluate(_fetch_at(45), NOW, fresh_within_minutes=90.0)
    assert wide.state == core.STATE_FRESH
    assert core.plan_action(wide, issue, 90.0, 20.0).kind == core.ACTION_NONE
    assert core.plan_action(wide, issue, 90.0, 60.0).kind == core.ACTION_CLOSE


# --- find_open_issue() / apply_action() ---


def test_find_open_issue_falls_back_when_the_label_does_not_exist():
    # The 'production-stale' label is not in the repository yet, so a label
    # query matches nothing even while the issue is open.
    api = FakeGitHub(
        label_exists=False,
        issues=[
            {"number": 5, "title": "unrelated", "body": "nope"},
            {"number": 6, "title": core.ISSUE_TITLE, "body": core.MONITOR_MARKER},
        ],
        comments=[{"body": core.BUCKET_MARKER_TEMPLATE.format(bucket=6)}],
    )
    notes = core.Notes()

    issue = github.find_open_issue(api, "owner/repo", notes=notes)

    assert issue is not None
    assert issue.number == 6
    assert core.posted_buckets(issue) == frozenset({6})
    assert any("label" in line for line in notes.lines)


def test_find_open_issue_never_adopts_someone_elses_labelled_issue():
    # A human issue carrying the label must not be commented on, escalated on,
    # or auto-closed -- and must not suppress the monitor's own alert either.
    api = FakeGitHub(
        issues=[
            {"number": 200, "title": "Track the stale-production follow-ups", "body": "mine"},
            {"number": 150, "title": core.ISSUE_TITLE, "body": core.MONITOR_MARKER},
        ],
        comments=[],
    )

    issue = github.find_open_issue(api, "owner/repo")

    assert issue is not None
    assert issue.number == 150


def test_find_open_issue_reads_every_page_of_issues_and_comments():
    filler = [{"number": n, "title": "unrelated", "body": ""} for n in range(100)]
    ours = {"number": 4242, "title": core.ISSUE_TITLE, "body": core.MONITOR_MARKER}
    comment_pages = [
        [{"body": f"chatter {n}"} for n in range(100)],
        [{"body": core.BUCKET_MARKER_TEMPLATE.format(bucket=24)}],
    ]

    class Paged(FakeGitHub):
        def __call__(self, method: str, path: str, body: Any = None) -> Any:
            if method == "GET" and "/issues?" in path:
                self.calls.append((method, path, body))
                page = _page_number(path)
                return filler if page == 1 else ([ours] if page == 2 else [])
            if method == "GET" and "/comments?" in path:
                self.calls.append((method, path, body))
                index = _page_number(path) - 1
                return comment_pages[index] if index < len(comment_pages) else []
            return super().__call__(method, path, body)

    issue = github.find_open_issue(Paged(), "owner/repo")

    assert issue is not None
    assert issue.number == 4242
    # The newest bucket marker lives on the last comment page; missing it would
    # replay the 24 h escalation comment on every single run.
    assert core.posted_buckets(issue) == frozenset({24})


def test_closing_by_hand_during_an_outage_is_an_acknowledgement_not_an_invitation():
    closed_at = (NOW - timedelta(minutes=10)).isoformat()
    api = FakeGitHub(
        closed_issues=[
            {
                "number": 90,
                "title": core.ISSUE_TITLE,
                "body": core.MONITOR_MARKER,
                "closed_at": closed_at,
            }
        ]
    )
    notes = core.Notes()

    assert github.recently_acknowledged(api, "owner/repo", NOW, notes=notes) is True
    assert any("acknowledgement" in line for line in notes.lines)

    stale = core.evaluate(_fetch_at(300), NOW)
    assert core.plan_action(stale, None, 90.0, 20.0, acknowledged=True).kind == core.ACTION_NONE
    assert core.plan_action(stale, None, 90.0, 20.0).kind == core.ACTION_CREATE

    # The same holds for the states that carry no age -- a broken manifest and a
    # clock fault -- which otherwise re-file on every single tick.
    broken = core.evaluate(_fetch(None, error="HTTP 404 Not Found", structural=True), NOW)
    ahead = core.evaluate(_fetch((NOW + timedelta(days=3)).isoformat()), NOW)
    for verdict in (broken, ahead):
        assert core.plan_action(verdict, None, 90.0, 20.0).kind == core.ACTION_CREATE, verdict.state
        assert core.plan_action(verdict, None, 90.0, 20.0, acknowledged=True).kind == core.ACTION_NONE
        assert core.plan_action(verdict, None, 90.0, 20.0, muted=True).kind == core.ACTION_NONE

    # An old close is not an acknowledgement of today's outage.
    stale_ack = FakeGitHub(
        closed_issues=[
            {
                "number": 90,
                "title": core.ISSUE_TITLE,
                "body": core.MONITOR_MARKER,
                "closed_at": (NOW - timedelta(days=4)).isoformat(),
            }
        ]
    )
    assert github.recently_acknowledged(stale_ack, "owner/repo", NOW) is False

    # Neither is somebody else's closed issue.
    foreign = FakeGitHub(
        closed_issues=[{"number": 91, "title": "unrelated", "body": "", "closed_at": closed_at}]
    )
    assert github.recently_acknowledged(foreign, "owner/repo", NOW) is False


def test_the_monitors_own_recovery_close_is_not_mistaken_for_an_acknowledgement():
    # Publication flaps: it recovers, the monitor auto-closes, and it dies again
    # an hour later. Counting that close as a human acknowledgement would
    # silence the second outage for hours.
    last_publish = NOW - timedelta(hours=1)
    auto_closed = last_publish + timedelta(minutes=5)  # only ever below the close threshold
    by_hand = last_publish + timedelta(minutes=50)

    def api_with(closed_at: datetime) -> Any:
        return FakeGitHub(
            closed_issues=[
                {
                    "number": 92,
                    "title": core.ISSUE_TITLE,
                    "body": core.MONITOR_MARKER,
                    "closed_at": closed_at.isoformat(),
                }
            ]
        )

    # The monitor closes only while the snapshot is younger than the close
    # threshold, so anything later than that cannot have been its own close.
    boundary = last_publish + timedelta(minutes=20)

    assert github.recently_acknowledged(
        api_with(auto_closed), "owner/repo", NOW, human_close_after=boundary
    ) is False
    assert github.recently_acknowledged(
        api_with(by_hand), "owner/repo", NOW, human_close_after=boundary
    ) is True


def test_muted_monitor_still_closes_a_recovered_issue():
    stale = core.evaluate(_fetch_at(300), NOW)
    issue = core.OpenIssue(number=12, body=core.MONITOR_MARKER)

    assert core.plan_action(stale, None, 90.0, 20.0, muted=True).kind == core.ACTION_NONE
    assert core.plan_action(stale, issue, 90.0, 20.0, muted=True).kind == core.ACTION_NONE

    recovered = core.evaluate(_fetch_at(3), NOW)
    assert core.plan_action(recovered, issue, 90.0, 20.0, muted=True).kind == core.ACTION_CLOSE


def test_mute_deadline_reads_the_flag_then_the_environment():
    assert cli.mute_deadline(None, {}) is None
    assert cli.mute_deadline(None, {cli.MUTE_ENV_VAR: "  "}) is None
    assert cli.mute_deadline(None, {cli.MUTE_ENV_VAR: "2026-09-20T18:00:00Z"}) == datetime(
        2026, 9, 20, 18, 0, tzinfo=UTC
    )
    assert cli.mute_deadline("2026-09-21T00:00:00Z", {cli.MUTE_ENV_VAR: "2026-09-20T18:00:00Z"}) == datetime(
        2026, 9, 21, 0, 0, tzinfo=UTC
    )


def test_find_open_issue_skips_pull_requests():
    api = FakeGitHub(
        issues=[
            {"number": 8, "title": core.ISSUE_TITLE, "body": "x", "pull_request": {"url": "..."}},
        ],
        comments=[],
    )

    assert github.find_open_issue(api, "owner/repo") is None


def test_apply_action_calls_rest_api_against_fake_server():
    verdict = core.evaluate(_fetch_at(120), NOW)
    deployment = core.DeploymentRecord(identifier=77, created_at=NOW, state="in_progress")

    # create
    api = FakeGitHub(label_exists=False)
    github.apply_action(
        api,
        "owner/repo",
        core.Action(core.ACTION_CREATE, "stale"),
        verdict,
        deployment,
        None,
        90.0,
        workflow_run_url="https://github.example/run/1",
    )
    posts = [(path, body) for method, path, body in api.calls if method == "POST"]
    assert any(path.endswith("/labels") for path, _ in posts)
    create_path, create_body = next((path, body) for path, body in posts if path.endswith("/repos/owner/repo/issues"))
    assert create_path == "/repos/owner/repo/issues"
    assert create_body["title"] == core.ISSUE_TITLE
    assert create_body["labels"] == [core.ISSUE_LABEL]
    body_text = create_body["body"]
    assert core.MONITOR_MARKER in body_text
    assert "53481cfb43b904dfcf1e7701" in body_text
    assert "2a6432e3bdb29fa1798a4b07e5f5396685b5245b" in body_text
    assert "https://github.example/run/1" in body_text
    assert "@Jaxsonb04" in body_text
    assert "disable_systemd_timers.sh restore" in body_text
    assert "77" in body_text and "in_progress" in body_text

    # comment
    api = FakeGitHub()
    issue = core.OpenIssue(number=12)
    github.apply_action(
        api,
        "owner/repo",
        core.Action(core.ACTION_COMMENT, "escalate", bucket_hours=12),
        verdict,
        None,
        issue,
        90.0,
        workflow_run_url=None,
    )
    assert [(method, path) for method, path, _ in api.calls] == [
        ("POST", "/repos/owner/repo/issues/12/comments")
    ]
    comment_text = api.calls[0][2]["body"]
    assert core.BUCKET_MARKER_TEMPLATE.format(bucket=12) in comment_text
    assert "12 h" in comment_text
    assert verdict.published_at is not None and verdict.published_at.isoformat() in comment_text
    assert "@Jaxsonb04" in comment_text

    # close
    api = FakeGitHub()
    fresh_verdict = core.evaluate(_fetch_at(3), NOW)
    github.apply_action(
        api,
        "owner/repo",
        core.Action(core.ACTION_CLOSE, "recovered"),
        fresh_verdict,
        None,
        issue,
        90.0,
        workflow_run_url=None,
    )
    methods = [(method, path) for method, path, _ in api.calls]
    # Close first, explain second: the other order re-posts an identical
    # "Publication recovered" comment on every run while the PATCH keeps failing.
    assert methods == [
        ("PATCH", "/repos/owner/repo/issues/12"),
        ("POST", "/repos/owner/repo/issues/12/comments"),
    ]
    assert api.calls[0][2] == {"state": "closed", "state_reason": "completed"}

    # none writes nothing at all
    api = FakeGitHub()
    github.apply_action(
        api,
        "owner/repo",
        core.Action(core.ACTION_NONE, "fresh"),
        fresh_verdict,
        None,
        None,
        90.0,
        workflow_run_url=None,
    )
    assert api.calls == []


def test_manifest_strings_cannot_inject_markdown_or_a_counterfeit_marker():
    # The manifest is network input. A crafted snapshot_id that carried its own
    # `<!-- freshness-bucket:24 -->` into the issue body would make posted_buckets
    # believe the 24 h escalation was already sent -- silencing it permanently.
    # 200 minutes: stale, but below the 6 h bucket, so the body carries no
    # legitimate marker of its own and the injected one would stand alone.
    poisoned = _fetch((NOW - timedelta(minutes=200)).isoformat())
    assert poisoned.manifest is not None
    poisoned.manifest["snapshot_id"] = "`\n<!-- freshness-bucket:24 -->\n`"
    poisoned.manifest["provenance"]["source_sha"] = "x" * 5000

    verdict = core.evaluate(poisoned, NOW)
    body = github.issue_body(verdict, None, 90.0, workflow_run_url=None)

    assert "freshness-bucket" not in body
    assert body.count("invalid") == 2
    assert len(body) < 4000
    assert core.posted_buckets(core.OpenIssue(number=1, body=body)) == frozenset()
    # The real thing still renders verbatim.
    clean = core.evaluate(_fetch_at(200), NOW)
    assert "53481cfb43b904dfcf1e7701" in github.issue_body(clean, None, 90.0, workflow_run_url=None)


def test_issue_opened_late_does_not_immediately_comment_about_an_earlier_bucket():
    # A deployment record can raise the threshold past 6 h, so the issue may be
    # born at 9 h old. Without a marker stamped at creation, the very next run
    # posts "still stale at the 6 h mark" fifteen minutes later.
    verdict = core.evaluate(_fetch_at(9 * 60), NOW)
    body = github.issue_body(verdict, None, 525.0, workflow_run_url=None)

    assert core.BUCKET_MARKER_TEMPLATE.format(bucket=6) in body

    issue = core.OpenIssue(number=13, body=body)
    assert core.plan_action(verdict, issue, 525.0, 20.0).kind == core.ACTION_NONE
    # The next genuine bucket still lands.
    twelve = core.evaluate(_fetch_at(12 * 60 + 1), NOW)
    assert core.plan_action(twelve, issue, 525.0, 20.0).bucket_hours == 12


def test_issue_body_cites_the_real_runbook_phase():
    verdict = core.evaluate(_fetch_at(300), NOW)
    body = github.issue_body(verdict, None, 90.0, workflow_run_url=None)
    runbook = REPO_ROOT / "trading" / "deploy" / "aws" / "README.md"
    runbook_text = runbook.read_text(encoding="utf-8")

    assert "trading/deploy/aws/README.md" in body
    assert "Release Deploy And Rollback" in body
    assert "## Release Deploy And Rollback" in runbook_text
    assert "### Phase 0: confirm the host state (read-only)" in runbook_text
    # Phase 0's own instruction: a restore that leaves the marker keeps the
    # scheduler watchdog suppressed, so the box still will not publish.
    assert "/run/weatheredge-deploy-maintenance" in body

    # Each alerting state leads with its own first move: a 404 is not a timer
    # problem, and a bad timestamp is not a publishing problem.
    broken = core.evaluate(_fetch(None, error="HTTP 404 Not Found", structural=True), NOW)
    broken_body = github.issue_body(broken, None, 90.0, workflow_run_url=None)
    assert broken_body.index("pages-build-deployment") < broken_body.index("list-timers")

    ahead = core.evaluate(_fetch((NOW + timedelta(days=3)).isoformat()), NOW)
    ahead_body = github.issue_body(ahead, None, 90.0, workflow_run_url=None)
    assert ahead_body.index("timedatectl") < ahead_body.index("list-timers")

    # Numbering stays contiguous whatever the lead adds.
    for text in (body, broken_body, ahead_body):
        numbers = [int(line.split(".")[0]) for line in text.splitlines() if re.match(r"^\d+\. ", line)]
        assert numbers == list(range(1, len(numbers) + 1)), numbers


def test_write_path_notes_reach_the_log(monkeypatch, capsys):
    monkeypatch.setattr(cli, "fetch_manifest_with_retries", lambda url, now, **kwargs: _fetch_at(1000))
    monkeypatch.setenv("GH_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    class RefusesLabels(FakeGitHub):
        def __call__(self, method: str, path: str, body: Any = None) -> Any:
            if method == "POST" and path.endswith("/labels"):
                self.calls.append((method, path, body))
                raise core.GitHubApiError("POST /labels -> HTTP 403 Resource not accessible")
            return super().__call__(method, path, body)

    api = RefusesLabels(label_exists=False)
    monkeypatch.setattr(cli, "make_api", lambda *args, **kwargs: api)

    exit_code = cli.main(["--manifest-url", MANIFEST_URL])
    out = capsys.readouterr().out

    assert exit_code == 0
    # The issue is filed unlabelled by design -- an unlabelled alert beats no
    # alert -- but the operator must be told, or the next run's fast path is
    # quietly broken with a clean log.
    assert "filing unlabelled" in out
    created = [body for method, path, body in api.calls if method == "POST" and path.endswith("/issues")]
    assert len(created) == 1 and "labels" not in created[0]


def test_main_returns_one_only_on_github_api_failure(monkeypatch, capsys):
    monkeypatch.setattr(cli, "fetch_manifest_with_retries", lambda url, now, **kwargs: _fetch_at(1000))
    monkeypatch.setenv("GH_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    def exploding_api(*args: Any, **kwargs: Any):
        def call(method: str, path: str, body: Any = None) -> Any:
            raise core.GitHubApiError("GET /deployments -> HTTP 502")

        return call

    monkeypatch.setattr(cli, "make_api", exploding_api)

    assert cli.main(["--manifest-url", MANIFEST_URL]) == 1
    assert "GitHub API failure" in capsys.readouterr().out


def test_dry_run_plans_without_writing(monkeypatch, capsys):
    monkeypatch.setattr(cli, "fetch_manifest_with_retries", lambda url, now, **kwargs: _fetch_at(1000))
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(
        cli,
        "make_api",
        lambda *args, **kwargs: pytest.fail("a dry run must not build an API client"),
    )

    exit_code = cli.main(["--manifest-url", MANIFEST_URL, "--dry-run"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "STALE" in out
    assert f"PLAN {core.ACTION_CREATE}" in out
    assert "dry run: nothing was written" in out


# --- fetch_manifest() ---


def test_manifest_request_carries_cache_busting_query():
    seen: dict[str, Any] = {}

    def fake_opener(request, timeout=None):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        seen["headers"] = dict(request.headers)
        body = json.dumps(_manifest("2026-09-15T11:55:00+00:00")).encode("utf-8")
        return _FakeResponse(body, {"Date": "Tue, 15 Sep 2026 12:00:00 GMT"})

    fetch = core.fetch_manifest(MANIFEST_URL, NOW, opener=fake_opener)

    assert fetch.error is None
    assert fetch.manifest is not None
    assert f"{core.CACHE_BUSTER_PARAM}={int(NOW.timestamp())}" in seen["url"]
    assert seen["timeout"] == core.MANIFEST_TIMEOUT_SECONDS
    # urllib title-cases header names.
    assert seen["headers"]["User-agent"] == core.USER_AGENT
    assert fetch.http_date == datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

    # A second call at a later instant must not reuse the first probe value, and
    # must not stack duplicate probe parameters.
    later = NOW + timedelta(minutes=15)
    core.fetch_manifest(seen["url"], later, opener=fake_opener)
    assert seen["url"].count(core.CACHE_BUSTER_PARAM) == 1
    assert f"{core.CACHE_BUSTER_PARAM}={int(later.timestamp())}" in seen["url"]


def test_fetch_manifest_reports_transport_and_decode_failures_as_errors():
    def exploding(request, timeout=None):
        raise OSError("connection reset by peer")

    fetch = core.fetch_manifest(MANIFEST_URL, NOW, opener=exploding)
    assert fetch.manifest is None
    assert fetch.error is not None
    assert core.evaluate(fetch, NOW).state == core.STATE_UNREADABLE

    def html_page(request, timeout=None):
        return _FakeResponse(b"<html>404</html>", {})

    fetch = core.fetch_manifest(MANIFEST_URL, NOW, opener=html_page)
    assert fetch.manifest is None
    assert "not valid JSON" in (fetch.error or "")


# --- workflow wiring ---


def test_workflow_yaml_schedule_permissions_include_deployments_read_and_pinned_checkout():
    # Parsed as text on purpose: PyYAML is not a dependency of this project and
    # the monitor's whole point is to need nothing installed.
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert 'cron: "7,22,37,52 * * * *"' in text
    assert "workflow_dispatch:" in text
    assert "contents: read" in text
    assert "issues: write" in text
    assert "deployments: read" in text
    assert "group: publication-freshness" in text
    assert "runs-on: ubuntu-latest" in text
    assert "timeout-minutes: 5" in text
    assert "persist-credentials: false" in text

    # The checkout action is pinned by SHA, and to the same SHA verify.yml uses.
    # Derived, never hardcoded: Dependabot's weekly github-actions update
    # rewrites both workflows together, and a literal here would turn its own
    # correct PR red.
    verify_text = VERIFY_WORKFLOW_PATH.read_text(encoding="utf-8")
    pin = re.search(r"uses: actions/checkout@([0-9a-f]{40})", verify_text)
    assert pin is not None, "verify.yml no longer pins actions/checkout by SHA"
    assert f"actions/checkout@{pin.group(1)}" in text
    assert "uses: actions/checkout@v" not in text

    # The mute escape hatch the deploy runbook's phase 5.5 tells operators to use.
    assert "FRESHNESS_MUTE_UNTIL:" in text
    assert "vars.FRESHNESS_MUTE_UNTIL" in text

    # The invocation matches the script's own flags.
    assert "python3 scripts/check_publication_freshness.py" in text
    for flag, value in (
        ("--manifest-url", "https://jaxsonb04.github.io/weather_edge/publication_manifest.json"),
        ("--base-open-minutes", "90"),
        ("--deploy-margin-minutes", "45"),
        ("--max-deploy-window-minutes", "480"),
        ("--close-at-or-below-minutes", "20"),
    ):
        assert f"{flag} {value}" in text

    assert "GH_TOKEN:" in text
    assert "GITHUB_REPOSITORY:" in text
    # No dependency install step may stand between an outage and the alert.
    assert "pip install" not in text


def test_workflow_yaml_nesting_is_what_github_will_read():
    # Nothing in CI parses this file as YAML: verify.yml has no workflow linter,
    # and a schedule-only workflow never runs on a pull request. Substring checks
    # alone would pass a mis-indented file that GitHub silently refuses to
    # schedule -- and an unscheduled watchdog is the failure this PR exists for.
    lines = WORKFLOW_PATH.read_text(encoding="utf-8").splitlines()

    def indent_of(needle: str) -> int:
        match = next(line for line in lines if line.strip().startswith(needle))
        return len(match) - len(match.lstrip(" "))

    for top_level in ("on:", "permissions:", "concurrency:", "jobs:"):
        assert indent_of(top_level) == 0, top_level
    # The permission keys belong to the document, not to `on:`.
    for key in ("contents: read", "issues: write", "deployments: read"):
        assert indent_of(key) == 2, key
    assert indent_of("freshness:") == 2
    for job_key in ("runs-on:", "timeout-minutes:", "steps:"):
        assert indent_of(job_key) == 4, job_key
    assert lines[lines.index("jobs:") + 1].startswith("  freshness:")
    assert not any("\t" in line for line in lines), "tabs are invalid YAML indentation"


def _workflow_argv() -> list[str]:
    """The script's argv exactly as the workflow spells it.

    Anchored on the backslash continuations rather than on a blank line, so a
    later edit to the YAML cannot silently truncate the command under test.
    """

    lines = WORKFLOW_PATH.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if "check_publication_freshness.py" in line)
    command: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        command.append(stripped.rstrip("\\").strip())
        if not stripped.endswith("\\"):
            break
    return " ".join(command).split()[2:]


def test_workflow_flags_are_all_accepted_by_the_script_parser():
    argv = _workflow_argv()

    assert argv[0].startswith("--")
    args = cli.build_parser().parse_args(argv)

    assert args.base_open_minutes == 90.0
    assert args.deploy_margin_minutes == 45.0
    assert args.max_deploy_window_minutes == 480.0
    assert args.close_at_or_below_minutes == 20.0
    assert args.manifest_url.endswith("publication_manifest.json")
    assert args.dry_run is False
