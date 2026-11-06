"""A prediction-market outage must degrade the public daily report, not abort it.

Audit 2026-11-01, finding 7 (F12 in the report). ``daily-report`` builds
trading_signal.json inside the operational publication cycle, which runs under
``set -euo pipefail``. Once its retries are exhausted the Kalshi client raises
``KalshiUnavailable``, an ``OSError`` that is not a ``URLError``, and it lets a
garbled status line (``http.client.HTTPException``) through unconverted. The
report caught only ``URLError``, so a refused connection, a timeout, a reset or
an HTML maintenance page aborted every target, the CLI exited 1, and the public
site stopped refreshing until the exchange recovered.
"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date, timedelta
from http.client import BadStatusLine
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from sfo_kalshi_quant import kalshi, report
from sfo_kalshi_quant.cli import main
from sfo_kalshi_quant.kalshi import KalshiUnavailable
from test_daily_report import _write_forecaster_fixture

TARGET = date(2026, 6, 4)
MARKET_DOWN_WARNING = (
    "Live prediction market lookup failed; using probability-only fallback ladder."
)


@pytest.mark.parametrize(
    "outage",
    [
        pytest.param(
            lambda: KalshiUnavailable(
                "Kalshi request failed after 3 attempts: <urlopen error [SSL: "
                "CERTIFICATE_VERIFY_FAILED] certificate is not valid for "
                "'external-api.kalshi.com'>"
            ),
            id="retries-exhausted",
        ),
        pytest.param(lambda: TimeoutError("The read operation timed out"), id="read-timeout"),
        pytest.param(
            lambda: ConnectionResetError(54, "Connection reset by peer"),
            id="connection-reset",
        ),
        pytest.param(lambda: BadStatusLine("Kalshi maintenance"), id="garbled-status-line"),
        # Already caught before the fix, but the reason phrase is server text.
        pytest.param(
            lambda: HTTPError(
                "https://external-api.kalshi.com/trade-api/v2/events",
                503,
                "Kalshi Service Unavailable",
                {},
                None,
            ),
            id="http-503",
        ),
    ],
)
def test_market_outage_returns_neutral_fallback_warning(monkeypatch, outage):
    def _down(self, target, series_ticker):
        raise outage()

    monkeypatch.setattr(report.KalshiPublicClient, "find_event_by_date", _down)

    event, warning = report._event_for_report(
        TARGET, offline_events=None, allow_live_market=True
    )

    assert event is None
    # Exception text can name the venue or its host; the public warning must not.
    assert warning == MARKET_DOWN_WARNING
    assert "kalshi" not in warning.lower()


def _refused(request, timeout):
    raise URLError(ConnectionRefusedError(61, "Connection refused"))


def _read_timeout(request, timeout):
    raise TimeoutError("The read operation timed out")


def _maintenance_page(request, timeout):
    return io.BytesIO(b"<html>Down for maintenance</html>")


def _garbled_status_line(request, timeout):
    raise BadStatusLine("NOT-HTTP garbage\r\n")


@pytest.mark.parametrize(
    "urlopen", [_refused, _read_timeout, _maintenance_page, _garbled_status_line]
)
def test_real_client_transport_failures_degrade_the_report(monkeypatch, urlopen):
    # The real client with its default retries: only the transport and the
    # backoff sleep are replaced, so whatever it actually raises reaches the report.
    monkeypatch.delenv("KALSHI_ENV", raising=False)
    monkeypatch.setattr(kalshi, "urlopen", urlopen)
    monkeypatch.setattr(kalshi.time, "sleep", lambda _seconds: None)

    assert report._event_for_report(
        TARGET, offline_events=None, allow_live_market=True
    ) == (None, MARKET_DOWN_WARNING)


def _add_forecast_target(root: Path, target: date) -> None:
    """Give the fixture forecaster a second target day with the same blend row."""

    with closing(sqlite3.connect(root / "weather.db")) as conn:
        conn.execute("CREATE TEMP TABLE next_day AS SELECT * FROM forecast_blend_daily_high")
        conn.execute("UPDATE next_day SET target_date = ?", (target.isoformat(),))
        conn.execute("INSERT INTO forecast_blend_daily_high SELECT * FROM next_day")
        conn.commit()


def test_daily_report_still_writes_the_artifact_while_the_market_is_down(monkeypatch):
    monkeypatch.delenv("KALSHI_ENV", raising=False)
    attempts: list[str] = []

    def _refused_and_counted(request, timeout):
        attempts.append(request.full_url)
        _refused(request, timeout)

    monkeypatch.setattr(kalshi, "urlopen", _refused_and_counted)
    monkeypatch.setattr(kalshi.time, "sleep", lambda _seconds: None)
    targets = [TARGET, TARGET + timedelta(days=1)]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "forecaster"
        _write_forecaster_fixture(root, targets[0])
        _add_forecast_target(root, targets[1])
        output = root / "trading_signal.json"
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            # build_public_trading_signal.sh's arguments: the live market lookup
            # stays on; only the ensemble is off, to keep Open-Meteo out of it.
            code = main(
                [
                    "--no-color",
                    "--forecaster-root",
                    str(root),
                    "--db-path",
                    str(Path(tmp) / "paper_trading.db"),
                    "daily-report",
                    "--target-date",
                    ",".join(target.isoformat() for target in targets),
                    "--side",
                    "both",
                    "--calibration-source",
                    "lstm",
                    "--format",
                    "json",
                    "--ensemble-timeout",
                    "12",
                    "--output",
                    str(output),
                    "--no-ensemble",
                ]
            )

        assert code == 0, err.getvalue()
        assert output.exists()
        payload = json.loads(output.read_text(encoding="utf-8"))

    assert attempts, "the live market lookup must actually run"
    # One failed lookup no longer aborts the targets after it.
    assert [row["target_date"] for row in payload["targets"]] == [
        target.isoformat() for target in targets
    ]
    for row in payload["targets"]:
        assert row["market_available"] is False
        assert row["market_data_at"] is None
        assert MARKET_DOWN_WARNING in row["warnings"]
        assert row["decisions"]
    assert payload["live_orders_enabled"] is False
    assert "kalshi" not in json.dumps(payload).lower()
    # The cause stays visible to operators in the service log, which is not published.
    assert "Connection refused" in err.getvalue()
