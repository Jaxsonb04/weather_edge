"""Only attributable reconstructed weather rows may cross into AWS history."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

import pytest

LOCAL = Path(__file__).resolve().parents[2] / 'scripts/local_compute'
sys.path.insert(0, str(LOCAL))
import shadow_history_bundle as bundle

history, shadow = bundle.history, bundle.shadow
START, END = date(2026, 8, 1), date(2026, 8, 2)


def encoded(value, url):
    raw = json.dumps(value).encode()
    stamp = datetime.now(timezone.utc).isoformat()
    return raw, {'url': url, 'request_started_at': stamp, 'retrieved_at': stamp,
                 'sha256': shadow.sha(raw), 'bytes': len(raw)}


def seed(conn):
    shadow.ensure_schema(conn)
    city = shadow.parse_city_slugs('lv')[0]
    hourly, units = {'time': [f'{day}T{hour:02d}:00' for day in (START, END) for hour in range(24)]}, {}
    for model in shadow.nwp_archive.NWP_MODELS:
        for lead in history.LEADS:
            key = shadow.nwp_archive.previous_day_variable(lead) + '_' + model
            hourly[key], units[key] = [70 + hour % 24 / 24 for hour in range(48)], '°F'
    raw, meta = encoded({'hourly': hourly, 'hourly_units': units,
                         'utc_offset_seconds': city.standard_utc_offset_hours * 3600},
                        history.previous_runs_url(city, START, END))
    history.import_previous_runs(conn, city, raw, meta, START, END)
    raw, meta = encoded({'station': city.nws_station_id, 'results': [
        {'valid': day.isoformat(), 'high': 70, 'product': 'retained-product'} for day in (START, END)]},
        history.iem_url(city, 2026))
    history.import_iem_truth(conn, city, raw, meta, 2026)
    return city


@pytest.fixture
def payload():
    with sqlite3.connect(':memory:') as conn:
        city = seed(conn)
        return bundle.build_bundle(conn, [city], START, END)


def target(conn):
    shadow.nwp_archive.ensure_schema(conn)
    shadow.city_truth.ensure_schema(conn)
    conn.commit()


def rehash(value):
    value['content_sha256'] = bundle.bundle_hash(value)
    return value


def test_export_retains_raw_attribution_current_clocks_and_excludes_issued_weather(payload):
    assert set(payload['tables']) == set(bundle.TABLES)
    assert len(payload['tables']['nwp_model_forecasts']) == 32
    assert len(payload['tables']['cli_settlements']) == 2
    assert len(payload['tables']['shadow_http_evidence']) == 2
    assert payload['promotion_eligible'] is False
    assert all(row['provider_initialized_at'] is None and row['is_original_vintage'] == 0
               for row in payload['tables']['shadow_training_lineage'])
    observed_clocks = {row['retrieved_at'] for row in payload['tables']['shadow_http_evidence']
                       if row['provider'] == 'open_meteo_previous_runs'}
    assert {row['fetched_at'] for row in payload['tables']['nwp_model_forecasts']} == observed_clocks
    assert 'Open-Meteo.com (CC BY 4.0)' in payload['provider_attribution']
    assert bundle.matched_training_days(payload) == {'KLAS': {'1': 2, '2': 2}}


def test_verified_import_is_atomic_idempotent_and_receipt_is_immutable(payload):
    with sqlite3.connect(':memory:') as conn:
        target(conn)
        result = bundle.import_bundle(conn, payload)
        assert result['new_rows'] == {'nwp_model_forecasts': 32, 'cli_settlements': 2}
        assert result['issued_forecast_rows_imported'] == 0
        assert bundle.import_bundle(conn, payload)['new_rows'] == {'nwp_model_forecasts': 0, 'cli_settlements': 0}
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='forecast_emos_live_vintages'").fetchone() is None
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='paper_orders'").fetchone() is None
        saved = json.loads(conn.execute('SELECT receipt_json FROM history_import_receipts').fetchone()[0])
        assert saved == payload
        for statement in ('DELETE FROM history_import_receipts',
                          "UPDATE history_import_receipts SET imported_at='changed'",
                          'INSERT OR REPLACE INTO history_import_receipts SELECT * FROM history_import_receipts'):
            with pytest.raises(sqlite3.IntegrityError, match='append-only'):
                conn.execute(statement)
        conn.rollback()


def test_rehashed_wrong_training_values_cannot_bypass_raw_source_replay(payload):
    changed = deepcopy(payload)
    changed['tables']['nwp_model_forecasts'][0]['predicted_high_f'] += 10
    with pytest.raises(ValueError, match='raw attributed'):
        bundle.validate_bundle(rehash(changed))


@pytest.mark.parametrize('field', ['imported_at', 'row_sha256', 'is_original_vintage'])
def test_rehashed_training_lineage_mutations_are_rejected(payload, field):
    changed = deepcopy(payload)
    changed['tables']['shadow_training_lineage'][0][field] = 'invented'
    with pytest.raises(ValueError, match='lineage'):
        bundle.validate_bundle(rehash(changed))


def test_issued_table_injection_and_orphan_raw_sources_are_rejected(payload):
    changed = deepcopy(payload)
    changed['tables']['forecast_emos_live_vintages'] = []
    with pytest.raises(ValueError, match='scope'):
        bundle.validate_bundle(rehash(changed))
    changed = deepcopy(payload)
    changed['tables']['shadow_truth_lineage'] = []
    changed['tables']['cli_settlements'] = []
    with pytest.raises(ValueError, match='unreferenced'):
        bundle.validate_bundle(rehash(changed))


def test_conflicting_existing_weather_aborts_every_row_and_receipt(payload):
    with sqlite3.connect(':memory:') as conn:
        target(conn)
        conn.execute("INSERT INTO cli_settlements VALUES ('KLAS','2026-08-02',999,'prior-clock','nws_cli',1)")
        conn.commit()
        with pytest.raises(ValueError, match='conflicts'):
            bundle.import_bundle(conn, payload)
        assert conn.execute('SELECT count(*) FROM nwp_model_forecasts').fetchone()[0] == 0
        assert conn.execute('SELECT max_temperature_f FROM cli_settlements').fetchall() == [(999,)]
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='history_import_receipts'").fetchone() is None


def test_agreeing_existing_values_keep_their_original_clocks_and_sources(payload):
    with sqlite3.connect(':memory:') as conn:
        target(conn)
        conn.execute("INSERT INTO cli_settlements VALUES ('KLAS','2026-08-02',70,'prior-clock','nws_cli',1)")
        conn.commit()
        assert bundle.import_bundle(conn, payload)['new_rows']['cli_settlements'] == 1
        assert conn.execute("SELECT fetched_at,source FROM cli_settlements WHERE local_date='2026-08-02'").fetchone() == ('prior-clock', 'nws_cli')


def test_bundle_exports_independent_corroboration_without_relabeling_original_nws_rows():
    with sqlite3.connect(':memory:') as conn:
        shadow.ensure_schema(conn)
        shadow.city_truth.upsert_settlement(conn, 'KLAS', START.isoformat(), 70,
                                           fetched_at='2026-08-02T20:00:00Z')
        conn.commit()
        before = conn.execute('SELECT * FROM cli_settlements').fetchall()
        city = seed(conn)
        value = bundle.build_bundle(conn, [city], START, END)
        original = conn.execute('SELECT * FROM cli_settlements WHERE local_date=?', (START.isoformat(),)).fetchall()
        assert original == before
        exported = next(row for row in value['tables']['cli_settlements'] if row['local_date'] == START.isoformat())
        assert exported['source'] == 'iem_cli'
        assert exported['fetched_at'] != '2026-08-02T20:00:00Z'
        assert bundle.matched_training_days(value) == {'KLAS': {'1': 2, '2': 2}}
        bundle.validate_bundle(value)
