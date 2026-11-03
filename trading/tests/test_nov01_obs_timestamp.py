"""The intraday snapshot's latest observation is the newest station reading.

Audit finding 1a: ``SfoForecasterAdapter.intraday_snapshot`` replaced the
newest observation time with ``nws_daily_high_ground_truth.high_observed_at``,
the first time the day's maximum was reached. Once a city peaked, or settled
onto a flat maximum, that timestamp stopped advancing. About 90 minutes later
the same-day heartbeat's observation-freshness gate refused to journal model
reads, open NO positions fell to HOLD_NO_MODEL_READ, and the intraday weight
and clock ran on the hour of the peak instead of the current hour.

The weather databases are written by the forecaster's production writer
(``nws_ground_truth.archive_observations`` and ``update_daily_high``) from
NWS-API-shaped readings; only the legacy-schema case writes its daily row by
hand, because that writer requires last_observed_at.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

# The forecaster modules use project-relative imports; expose them on the path.
FORECASTER_DIR = Path(__file__).resolve().parents[2] / "forecaster"
if str(FORECASTER_DIR) not in sys.path:
    sys.path.insert(0, str(FORECASTER_DIR))

import nws_ground_truth  # noqa: E402

from sfo_kalshi_quant import monitor  # noqa: E402
from sfo_kalshi_quant.cities import CityConfig, get_city  # noqa: E402
from sfo_kalshi_quant.config import intraday_timezone_for_city  # noqa: E402
from sfo_kalshi_quant.db import PaperStore  # noqa: E402
from sfo_kalshi_quant.forecast import SfoForecasterAdapter, _intraday_weight  # noqa: E402
from sfo_kalshi_quant.models import (  # noqa: E402
    EventSnapshot,
    ForecastSnapshot,
    IntradaySnapshot,
    format_event_date_token,
)
from sfo_kalshi_quant.probability import (  # noqa: E402
    _local_decimal_hour,
    post_peak_cap_engaged,
)
from sfo_kalshi_quant.standard_bins import fallback_bins  # noqa: E402

BOSTON = get_city("bos")
EST = BOSTON.fixed_standard_timezone()
NOV_1 = date(2026, 11, 1)
# One EMOS city per US standard offset: whatever the UTC hour, one of them is
# at least three hours into its fixed-standard settlement day.
HEARTBEAT_CITY_SLUGS = ("bos", "chi", "den", "sea")


def _est(hour: int, minute: int) -> datetime:
    return datetime.combine(NOV_1, time(hour, minute), tzinfo=EST)


def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _feature(observed_at: datetime, temp_f: float) -> dict:
    """One NWS API observation; the API reports temperature in Celsius."""

    return {
        "properties": {
            "timestamp": observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "temperature": {"value": (temp_f - 32.0) * 5.0 / 9.0, "unitCode": "wmoUnit:degC"},
        }
    }


def _write_station_day(db_path: Path, station_id: str, local_date: date, readings) -> None:
    with sqlite3.connect(db_path) as conn:
        nws_ground_truth.init_db(conn)
        nws_ground_truth.archive_observations(
            conn, [_feature(observed_at, temp_f) for observed_at, temp_f in readings], station_id
        )
        nws_ground_truth.update_daily_high(conn, local_date.isoformat(), station_id)


def _daily_row(db_path: Path, station_id: str, local_date: date) -> tuple:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            """
            SELECT high_f, high_observed_at, last_observed_at
            FROM nws_daily_high_ground_truth
            WHERE station_id = ? AND local_date = ?
            """,
            (station_id, local_date.isoformat()),
        ).fetchone()


def _intraday_update(adapter: SfoForecasterAdapter, intraday, target: date) -> dict:
    forecast = ForecastSnapshot(
        target_date=target,
        predicted_high_f=59.0,
        station_id=adapter.station_id,
        method="obs-timestamp-test",
    )
    return adapter.apply_intraday_update(forecast, intraday).raw["intraday_update"]


# Boston, Nov 1: the maximum arrives at 15:50 EST, then the evening cools.
COOLING_EVENING = [
    (_est(9, 54), 50.0),
    (_est(12, 54), 55.0),
    (_est(15, 50), 57.0),
    (_est(17, 54), 53.0),
    (_est(19, 54), 50.0),
    (_est(21, 20), 48.0),
]


def test_cooling_evening_reports_the_newest_reading_not_the_time_of_the_maximum(tmp_path):
    _write_station_day(tmp_path / "weather.db", "KBOS", NOV_1, COOLING_EVENING)
    assert _daily_row(tmp_path / "weather.db", "KBOS", NOV_1) == (
        57.0,
        _utc_iso(_est(15, 50)),
        _utc_iso(_est(21, 20)),
    )
    adapter = SfoForecasterAdapter(tmp_path, city=BOSTON)

    intraday = adapter.intraday_snapshot(NOV_1)

    assert intraday is not None
    assert intraday.observed_high_f == 57.0
    assert intraday.latest_temp_f == pytest.approx(48.0)
    assert intraday.latest_observed_at == _utc_iso(_est(21, 20))
    # The intraday clock and weight run on the 21:20 reading, not the 15:50 peak.
    tz = intraday_timezone_for_city(BOSTON)
    assert _local_decimal_hour(intraday.latest_observed_at, tz) == pytest.approx(21 + 20 / 60)
    update = _intraday_update(adapter, intraday, NOV_1)
    assert update["latest_observed_at"] == _utc_iso(_est(21, 20))
    assert update["intraday_weight"] == 0.90


def test_flat_maximum_does_not_freeze_the_latest_observation(tmp_path):
    # 61F is first reached at 12:54 EST and holds through the 16:54 reading.
    readings = [(_est(9, 54), 56.0)] + [(_est(hour, 54), 61.0) for hour in range(12, 17)]
    _write_station_day(tmp_path / "weather.db", "KBOS", NOV_1, readings)
    assert _daily_row(tmp_path / "weather.db", "KBOS", NOV_1)[1] == _utc_iso(_est(12, 54))
    adapter = SfoForecasterAdapter(tmp_path, city=BOSTON)

    intraday = adapter.intraday_snapshot(NOV_1)

    assert intraday is not None
    assert intraday.observed_high_f == 61.0
    assert intraday.latest_observed_at == _utc_iso(_est(16, 54))
    tz = intraday_timezone_for_city(BOSTON)
    assert _local_decimal_hour(intraday.latest_observed_at, tz) == pytest.approx(16 + 54 / 60)
    assert _intraday_update(adapter, intraday, NOV_1)["intraday_weight"] == 0.80


def test_daily_row_newest_reading_is_kept_when_station_rows_lag_behind_it(tmp_path):
    db_path = tmp_path / "weather.db"
    _write_station_day(db_path, "KBOS", NOV_1, COOLING_EVENING)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "DELETE FROM nws_station_observations WHERE observed_at > ?",
            (_utc_iso(_est(17, 54)),),
        )

    intraday = SfoForecasterAdapter(tmp_path, city=BOSTON).intraday_snapshot(NOV_1)

    assert intraday is not None
    assert intraday.observed_high_f == 57.0
    assert intraday.latest_temp_f == pytest.approx(53.0)
    assert intraday.latest_observed_at == _utc_iso(_est(21, 20))


def test_daily_table_without_last_observed_at_falls_back_to_station_rows(tmp_path):
    db_path = tmp_path / "weather.db"
    with sqlite3.connect(db_path) as conn:
        # An older daily-high schema that never carried last_observed_at.
        conn.execute(
            """
            CREATE TABLE nws_daily_high_ground_truth (
                station_id TEXT, local_date TEXT, high_f REAL, high_observed_at TEXT,
                observation_count INTEGER, is_complete INTEGER, source TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO nws_daily_high_ground_truth VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("KBOS", NOV_1.isoformat(), 57.0, _utc_iso(_est(15, 50)), 6, 0, "legacy"),
        )
        nws_ground_truth.init_db(conn)
        nws_ground_truth.archive_observations(
            conn, [_feature(at, temp_f) for at, temp_f in COOLING_EVENING], "KBOS"
        )

    intraday = SfoForecasterAdapter(tmp_path, city=BOSTON).intraday_snapshot(NOV_1)

    assert intraday is not None
    assert intraday.observed_high_f == 57.0
    assert intraday.observed_high_source == "legacy"
    assert intraday.latest_observed_at == _utc_iso(_est(21, 20))


def _city_furthest_into_its_day(now: datetime) -> CityConfig:
    def elapsed(city: CityConfig) -> timedelta:
        local = now.astimezone(city.fixed_standard_timezone())
        return local - local.replace(hour=0, minute=0, second=0, microsecond=0)

    return max((get_city(slug) for slug in HEARTBEAT_CITY_SLUGS), key=elapsed)


def _write_live_emos(db_path: Path, station_id: str, target: date, fetched_at: datetime) -> None:
    """A fresh multi-model live EMOS row plus a scored rolling-origin record."""

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE forecast_emos_daily_high (
                station_id TEXT, target_date TEXT, lead_days INTEGER,
                predicted_high_f REAL, sigma_f REAL, n_models INTEGER,
                model_spread_f REAL, fetched_at TEXT, method TEXT, source TEXT,
                actual_high_f REAL,
                PRIMARY KEY (station_id, target_date, lead_days, source)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE cli_settlements (
                station_id TEXT, local_date TEXT, max_temperature_f INTEGER,
                fetched_at TEXT, source TEXT, is_final INTEGER,
                PRIMARY KEY (station_id, local_date)
            )
            """
        )
        for idx in range(60):
            day = (target - timedelta(days=61 - idx)).isoformat()
            conn.execute(
                "INSERT INTO forecast_emos_daily_high VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (station_id, day, 1, 55.0 + idx % 5, 2.0, 8, 3.0, fetched_at.isoformat(),
                 "emos_wmean", "rolling_origin_v2", None),
            )
            conn.execute(
                "INSERT INTO cli_settlements VALUES (?, ?, ?, ?, ?, ?)",
                (station_id, day, 55 + idx % 5 + (idx % 7 - 3), fetched_at.isoformat(),
                 "nws_cli", 1),
            )
        conn.execute(
            "INSERT INTO forecast_emos_daily_high VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (station_id, target.isoformat(), 0, 57.6, 1.6, 8, 3.0, fetched_at.isoformat(),
             "emos_wmean", "live", None),
        )


def _heartbeat_day(tmp_path: Path, *, peak_age_minutes: int, cooling_f_per_hour: float):
    """A same-day position built around the real clock.

    The freshness gate and the EMOS age check read the real clock, so readings
    run every ten minutes up to ten minutes ago, with the 57F maximum
    ``peak_age_minutes`` before the newest reading and a steady cooling after
    it. Returns the heartbeat's inputs and the newest reading's time.
    """

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    city = _city_furthest_into_its_day(now)
    newest = now - timedelta(minutes=10)
    peak = newest - timedelta(minutes=peak_age_minutes)
    target = newest.astimezone(city.fixed_standard_timezone()).date()
    local_midnight = datetime.combine(
        target, time(0, 0), tzinfo=city.fixed_standard_timezone()
    )
    readings = []
    observed_at = newest
    while observed_at >= local_midnight:
        hours_from_peak = (observed_at - peak).total_seconds() / 3600.0
        if observed_at == peak:
            temp_f = 57.0
        elif hours_from_peak < 0:
            temp_f = 56.5 + hours_from_peak
        else:
            temp_f = 56.5 - cooling_f_per_hour * hours_from_peak
        readings.append((observed_at, temp_f))
        observed_at -= timedelta(minutes=10)
    assert peak in [at for at, _temp_f in readings]
    root = tmp_path / "forecaster"
    root.mkdir()
    _write_station_day(root / "weather.db", city.nws_station_id, target, readings)
    _write_live_emos(root / "weather.db", city.nws_station_id, target, newest)
    assert _daily_row(root / "weather.db", city.nws_station_id, target)[1] == _utc_iso(peak)

    event_ticker = f"{city.series_ticker}-{format_event_date_token(target)}"
    markets = fallback_bins(event_ticker, 58.0)
    store = PaperStore(tmp_path / "paper.db")
    store.record_market(
        EventSnapshot(
            event_ticker=event_ticker,
            title="obs timestamp heartbeat test",
            target_date=target,
            markets=markets,
            raw={
                "event_ticker": event_ticker,
                "title": "obs timestamp heartbeat test",
                "markets": [market.raw for market in markets],
            },
        )
    )
    return root, store, city, target, markets, newest


def _run_heartbeat(store, root, target, held, logs) -> int:
    return monitor._refresh_same_day_model_reads(
        store,
        [{"market_ticker": held.ticker, "target_date": target.isoformat(), "risk_profile": "live"}],
        forecaster_root=root,
        log=logs.append,
        clock=lambda *, city: datetime.combine(
            target, time(15, 0), tzinfo=city.fixed_standard_timezone()
        ),
        adapter_factory=SfoForecasterAdapter,
    )


def test_heartbeat_keeps_journaling_reads_hours_after_the_daily_maximum(tmp_path, monkeypatch):
    # The maximum is two hours old (past the 90-minute window) while the newest
    # reading and the EMOS row are ten minutes old, and the day has cooled
    # 3.5F. Whether the post-peak cap engages depends on the real clock's local
    # hour (it needs 14:00 standard), so it is pinned open here; the gate's own
    # hour logic is tested directly below.
    monkeypatch.setattr(monitor, "post_peak_cap_engaged", lambda *_args, **_kwargs: True)
    root, store, city, target, markets, newest = _heartbeat_day(
        tmp_path, peak_age_minutes=120, cooling_f_per_hour=1.5
    )
    held = markets[2]
    logs: list[str] = []

    written = _run_heartbeat(store, root, target, held, logs)

    assert logs == []
    assert written == len(markets)
    assert store.latest_model_probability_read(target.isoformat(), held.ticker) is not None

    # The snapshot the heartbeat priced from carries the newest reading's time,
    # so the intraday hour and weight come from it rather than from the peak.
    adapter = SfoForecasterAdapter(root, city=city)
    intraday = adapter.intraday_snapshot(target)
    assert intraday is not None
    assert intraday.observed_high_f == 57.0
    assert intraday.latest_observed_at == _utc_iso(newest)
    tz = intraday_timezone_for_city(city)
    local_newest = newest.astimezone(tz)
    assert _local_decimal_hour(intraday.latest_observed_at, tz) == pytest.approx(
        local_newest.hour + local_newest.minute / 60
    )
    update = adapter.apply_intraday_update(adapter.latest_emos_snapshot(target), intraday)
    assert update.raw["intraday_update"]["latest_observed_at"] == _utc_iso(newest)
    assert update.raw["intraday_update"]["intraday_weight"] == _intraday_weight(
        _utc_iso(newest), tz
    )


def test_heartbeat_holds_without_a_read_when_the_maximum_is_old_and_the_cap_is_off(
    tmp_path, monkeypatch
):
    # Integration review of the Nov 1 fixes: with F1 the newest reading stays
    # fresh all evening, but below the post-peak cap the read is the
    # forecast-centred model. Once the maximum is past the 90-minute window,
    # the heartbeat journals nothing, so open positions hold as before F1.
    monkeypatch.setattr(monitor, "post_peak_cap_engaged", lambda *_args, **_kwargs: False)
    root, store, _city, target, markets, _newest = _heartbeat_day(
        tmp_path, peak_age_minutes=120, cooling_f_per_hour=1.5
    )
    held = markets[2]
    logs: list[str] = []

    written = _run_heartbeat(store, root, target, held, logs)

    assert written == 0
    assert store.latest_model_probability_read(target.isoformat(), held.ticker) is None
    assert len(logs) == 1
    assert "post-peak cap has not engaged" in logs[0]


def test_heartbeat_holds_through_a_sub_2f_evening_plateau(tmp_path):
    # Two hours past a 57F maximum the newest reading is 56.0F, only 1F lower:
    # the cap cannot engage at any hour, so the heartbeat writes no
    # forecast-centred read for the stop or the veto to act on.
    root, store, _city, target, markets, _newest = _heartbeat_day(
        tmp_path, peak_age_minutes=120, cooling_f_per_hour=0.25
    )
    held = markets[2]
    logs: list[str] = []

    written = _run_heartbeat(store, root, target, held, logs)

    assert written == 0
    assert store.latest_model_probability_read(target.isoformat(), held.ticker) is None
    assert "post-peak cap has not engaged" in logs[0]


def test_heartbeat_reads_while_the_maximum_is_recent(tmp_path):
    # Within 90 minutes of the maximum the heartbeat reads exactly as it did
    # before F1, whatever the post-peak cap says.
    root, store, _city, target, markets, _newest = _heartbeat_day(
        tmp_path, peak_age_minutes=30, cooling_f_per_hour=0.5
    )
    held = markets[2]
    logs: list[str] = []

    written = _run_heartbeat(store, root, target, held, logs)

    assert logs == []
    assert written == len(markets)
    assert store.latest_model_probability_read(target.isoformat(), held.ticker) is not None


def test_snapshot_keeps_the_time_of_the_maximum_beside_the_newest_reading(tmp_path):
    _write_station_day(tmp_path / "weather.db", "KBOS", NOV_1, COOLING_EVENING)

    intraday = SfoForecasterAdapter(tmp_path, city=BOSTON).intraday_snapshot(NOV_1)

    assert intraday is not None
    assert intraday.observed_high_at == _utc_iso(_est(15, 50))
    assert intraday.latest_observed_at == _utc_iso(_est(21, 20))


def test_station_only_day_reports_when_the_maximum_was_first_reached(tmp_path):
    # No daily-high row yet, so the snapshot reads the station rows alone. The
    # 61F maximum is first reached at 12:54 and held until 16:54.
    readings = (
        [(_est(9, 54), 56.0)]
        + [(_est(hour, 54), 61.0) for hour in range(12, 17)]
        + [(_est(18, 54), 58.0)]
    )
    with sqlite3.connect(tmp_path / "weather.db") as conn:
        nws_ground_truth.init_db(conn)
        nws_ground_truth.archive_observations(
            conn, [_feature(at, temp_f) for at, temp_f in readings], "KBOS"
        )

    intraday = SfoForecasterAdapter(tmp_path, city=BOSTON).intraday_snapshot(NOV_1)

    assert intraday is not None
    assert intraday.observed_high_source == "nws_station_observations"
    assert intraday.observed_high_f == pytest.approx(61.0)
    assert intraday.observed_high_at == _utc_iso(_est(12, 54))
    assert intraday.latest_observed_at == _utc_iso(_est(18, 54))


def _snapshot(latest_at: datetime, latest_temp_f: float, *, complete: bool = False):
    return IntradaySnapshot(
        target_date=NOV_1,
        observed_high_f=57.0,
        latest_temp_f=latest_temp_f,
        latest_observed_at=_utc_iso(latest_at),
        remaining_forecast_high_f=None,
        forecast_fetched_at=None,
        is_complete=complete,
        observed_high_at=_utc_iso(_est(15, 50)),
    )


@pytest.mark.parametrize(
    ("latest_at", "latest_temp_f", "engaged"),
    [
        (_est(21, 20), 55.4, False),  # 1.6F under the maximum: forecast-centred
        (_est(18, 54), 54.9, True),  # 2.1F drop after 14:00 standard: capped
        (_est(13, 30), 54.0, False),  # 3F drop, but before the 14:00 gate hour
    ],
)
def test_post_peak_cap_engages_only_after_a_2f_drop_past_the_gate_hour(
    latest_at, latest_temp_f, engaged, monkeypatch
):
    monkeypatch.delenv("PAPER_SAME_DAY_ENTRY_CUTOFF_HOUR", raising=False)

    assert post_peak_cap_engaged(
        _snapshot(latest_at, latest_temp_f), standard_timezone=EST
    ) is engaged


def test_post_peak_cap_is_never_engaged_on_a_complete_day():
    assert (
        post_peak_cap_engaged(_snapshot(_est(18, 54), 50.0, complete=True), standard_timezone=EST)
        is False
    )
