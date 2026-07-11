import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from sfo_kalshi_quant.forecast_scorecards import build_forecast_scorecards


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE forecast_emos_daily_high (
            station_id TEXT NOT NULL, target_date TEXT NOT NULL,
            lead_days INTEGER NOT NULL, predicted_high_f REAL NOT NULL,
            sigma_f REAL NOT NULL, n_models INTEGER, model_spread_f REAL,
            fetched_at TEXT NOT NULL, method TEXT NOT NULL,
            source TEXT NOT NULL, actual_high_f REAL,
            PRIMARY KEY (station_id, target_date, lead_days, source)
        );
        CREATE TABLE cli_settlements (
            station_id TEXT NOT NULL, local_date TEXT NOT NULL,
            max_temperature_f REAL NOT NULL, fetched_at TEXT,
            source TEXT, PRIMARY KEY (station_id, local_date)
        );
        """
    )


def test_scorecards_join_truth_by_station_and_date() -> None:
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "weather.db"
        with sqlite3.connect(db) as conn:
            _schema(conn)
            conn.executemany(
                "INSERT INTO cli_settlements VALUES (?, ?, ?, 't', 'cli')",
                [("KSFO", "2026-07-01", 68), ("KNYC", "2026-07-01", 88)],
            )
            conn.executemany(
                "INSERT INTO forecast_emos_daily_high VALUES (?, '2026-07-01', 1, ?, 2, 8, 4, 't', 'emos_ngr', 'rolling_origin', NULL)",
                [("KSFO", 68), ("KNYC", 88)],
            )

        payload = build_forecast_scorecards(db)

    assert payload["available"] is True
    cards = {(row["station_id"], row["lead_days"]): row for row in payload["scorecards"]}
    assert cards[("KSFO", 1)]["mae_f"] == 0.0
    assert cards[("KNYC", 1)]["mae_f"] == 0.0
    assert len(cards) == 2


def test_scorecards_publish_probabilistic_metrics_and_fail_closed_gates() -> None:
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "weather.db"
        with sqlite3.connect(db) as conn:
            _schema(conn)
            for day in range(1, 11):
                target = f"2026-06-{day:02d}"
                conn.execute(
                    "INSERT INTO cli_settlements VALUES ('KSFO', ?, ?, 't', 'cli')",
                    (target, 60 + day),
                )
                conn.execute(
                    "INSERT INTO forecast_emos_daily_high VALUES ('KSFO', ?, 0, ?, 2, 8, 4, 't', 'emos_ngr', 'rolling_origin', NULL)",
                    (target, 60 + day),
                )

        payload = build_forecast_scorecards(db)
    card = payload["scorecards"][0]

    assert card["cases"] == 10
    assert card["crps"] > 0
    assert card["log_score"] > 0
    assert card["pit_mean"] == 0.5
    assert card["interval_coverage"]["80"] == 1.0
    gates = {row["key"]: row for row in payload["challenger_gates"]}
    assert gates["minimum_crps_emos"]["promotion_eligible"] is False
    assert "nested" in " ".join(gates["minimum_crps_emos"]["block_reasons"]).lower()
    assert gates["time_series_emos"]["required_cases_per_city"] == 180
    assert gates["analog_ensemble"]["required_cases_per_city"] == 365
    assert gates["pooled_distributional"]["required_pooled_station_days"] == 5000


def test_scorecards_do_not_use_embedded_non_authoritative_actuals() -> None:
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "weather.db"
        with sqlite3.connect(db) as conn:
            _schema(conn)
            conn.execute(
                "INSERT INTO forecast_emos_daily_high VALUES ('KSFO', '2026-07-01', 1, 70, 2, 8, 4, 't', 'emos_ngr', 'rolling_origin', 70)"
            )

        payload = build_forecast_scorecards(db)

    assert payload["available"] is False
    assert payload["matched_cases"] == 0


def test_scorecards_exclude_preliminary_settlement_rows() -> None:
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "weather.db"
        with sqlite3.connect(db) as conn:
            _schema(conn)
            conn.execute(
                "ALTER TABLE cli_settlements "
                "ADD COLUMN is_final INTEGER NOT NULL DEFAULT 1"
            )
            conn.executemany(
                "INSERT INTO cli_settlements VALUES (?, ?, ?, 't', 'cli', ?)",
                [
                    ("KSFO", "2026-07-01", 68, 1),
                    ("KSFO", "2026-07-02", 71, 0),
                ],
            )
            conn.executemany(
                "INSERT INTO forecast_emos_daily_high VALUES "
                "('KSFO', ?, 1, ?, 2, 8, 4, 't', 'emos_ngr', 'rolling_origin', NULL)",
                [("2026-07-01", 68), ("2026-07-02", 71)],
            )

        payload = build_forecast_scorecards(db)

    assert payload["matched_cases"] == 1


def test_scorecards_prefer_v2_per_city_lead_method_without_double_counting() -> None:
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "weather.db"
        with sqlite3.connect(db) as conn:
            _schema(conn)
            conn.executemany(
                "INSERT INTO cli_settlements VALUES (?, ?, ?, 't', 'cli')",
                [
                    ("KSFO", "2026-07-01", 68),
                    ("KSFO", "2026-07-02", 69),
                    ("KNYC", "2026-07-01", 88),
                ],
            )
            conn.executemany(
                "INSERT INTO forecast_emos_daily_high VALUES (?, ?, 1, ?, 2, 8, 4, 't', 'emos_ngr', ?, NULL)",
                [
                    ("KSFO", "2026-07-01", 80, "rolling_origin"),
                    ("KSFO", "2026-07-02", 81, "rolling_origin"),
                    ("KSFO", "2026-07-02", 69, "rolling_origin_v2"),
                    ("KNYC", "2026-07-01", 88, "rolling_origin"),
                ],
            )

        payload = build_forecast_scorecards(db)

    assert payload["matched_cases"] == 2
    cards = {(row["station_id"], row["source"]): row for row in payload["scorecards"]}
    assert cards[("KSFO", "rolling_origin_v2")]["cases"] == 1
    assert cards[("KSFO", "rolling_origin_v2")]["mae_f"] == 0.0
    assert cards[("KNYC", "rolling_origin")]["cases"] == 1
    assert not any(station == "KSFO" and source == "rolling_origin" for station, source in cards)


def test_scorecards_do_not_fall_back_when_scope_has_unscored_v2_rows() -> None:
    with TemporaryDirectory() as tmp:
        db = Path(tmp) / "weather.db"
        with sqlite3.connect(db) as conn:
            _schema(conn)
            conn.execute(
                "INSERT INTO cli_settlements VALUES ('KSFO', '2026-07-01', 68, 't', 'cli')"
            )
            conn.execute(
                "INSERT INTO forecast_emos_daily_high VALUES "
                "('KSFO', '2026-07-01', 1, 80, 2, 8, 4, 't', 'emos_ngr', "
                "'rolling_origin', NULL)"
            )
            # The v2 rebuild has begun for this exact city/lead/method scope,
            # but its target is not settled yet. Reusing v1 would silently
            # report a different model version while labelling v2 as current.
            conn.execute(
                "INSERT INTO forecast_emos_daily_high VALUES "
                "('KSFO', '2026-07-02', 1, 69, 2, 8, 4, 't', 'emos_ngr', "
                "'rolling_origin_v2', NULL)"
            )

        payload = build_forecast_scorecards(db)

    assert payload["available"] is False
    assert payload["matched_cases"] == 0
