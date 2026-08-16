import sqlite3

import nws_ground_truth


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
