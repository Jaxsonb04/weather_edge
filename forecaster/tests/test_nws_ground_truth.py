import sqlite3

import nws_ground_truth


def test_observation_completions_and_corrections_recompute_high():
    with sqlite3.connect(":memory:") as conn:
        nws_ground_truth.init_db(conn)
        for index, (temperature, expected_high) in enumerate(
            [(None, None), (38, 100.4), (20, 68.0), (None, None)]
        ):
            feature = {"properties": {
                "timestamp": "2026-07-01T20:00:00Z",
                "temperature": {"value": temperature, "unitCode": "wmoUnit:degC"},
                "dewpoint": {"value": 10, "unitCode": "wmoUnit:degC"},
                "windSpeed": {"value": 8, "unitCode": "wmoUnit:km_h-1"},
            }}
            assert nws_ground_truth.archive_observations(conn, [feature]) == (1, 1)
            if index == 0:
                # Preserve the first ingestion receipt across all revisions.
                conn.execute("UPDATE nws_station_observations SET inserted_at = 'first'")
            assert nws_ground_truth.archive_observations(conn, [feature]) == (1, 0)
            nws_ground_truth.update_daily_high(conn, "2026-07-01")
            high, count, high_at, first_at, last_at = conn.execute(
                "SELECT high_f, observation_count, high_observed_at, first_observed_at, last_observed_at "
                "FROM nws_daily_high_ground_truth"
            ).fetchone()
            assert high == expected_high
            assert count == int(temperature is not None)
            if temperature is None:
                assert (high_at, first_at, last_at) == (None, None, None)
            assert conn.execute("SELECT inserted_at FROM nws_station_observations").fetchone() == ("first",)


def test_observation_daily_high_never_claims_settlement_finality():
    with sqlite3.connect(":memory:") as conn:
        nws_ground_truth.init_db(conn)
        conn.execute(
            """
            INSERT INTO nws_station_observations (
                station_id, observed_at, local_date, temp_f, raw_json, inserted_at
            ) VALUES ('KSFO', '2020-01-01T20:00:00+00:00', '2020-01-01', 70.4, '{}', 't')
            """
        )

        nws_ground_truth.update_daily_high(conn, "2020-01-01", "KSFO")

        row = conn.execute(
            """
            SELECT high_f, is_complete, source
            FROM nws_daily_high_ground_truth
            WHERE station_id='KSFO' AND local_date='2020-01-01'
            """
        ).fetchone()

    assert row == (70.4, 0, "NWS station observations")
