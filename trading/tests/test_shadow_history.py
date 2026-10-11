"""Historical onboarding must not become invented prospective evidence."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

import pytest

LOCAL = Path(__file__).resolve().parents[2] / 'scripts/local_compute'
sys.path.insert(0, str(LOCAL))
import shadow_history as history
shadow = history.shadow


@pytest.fixture
def database():
    with sqlite3.connect(':memory:') as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        shadow.ensure_schema(conn)
        yield conn


def encoded(value, url):
    raw = json.dumps(value).encode()
    stamp = datetime.now(timezone.utc).isoformat()
    return raw, {'url': url, 'sha256': shadow.sha(raw), 'bytes': len(raw),
                 'request_started_at': stamp, 'retrieved_at': stamp}


def forecast(city, start, end, *, incomplete=False):
    times = []
    for index in range((end-start).days+1):
        times += [f'{start+timedelta(days=index)}T{hour:02d}:00' for hour in range(24)]
    hourly, units = {'time': times}, {'time': 'iso8601'}
    for index, model in enumerate(shadow.nwp_archive.NWP_MODELS):
        for lead in history.LEADS:
            name = shadow.nwp_archive.previous_day_variable(lead) + '_' + model
            hourly[name] = [60+index+hour%24/24+lead for hour in range(len(times))]
            if incomplete:
                hourly[name][0] = None
            units[name] = '°F'
    return {'utc_offset_seconds': city.standard_utc_offset_hours*3600, 'hourly': hourly, 'hourly_units': units}


def test_complete_fixed_leads_preserve_current_receipts_and_never_issue(database):
    city = shadow.parse_city_slugs('lv')[0]
    start, end = date(2026, 6, 1), date(2026, 8, 31)
    raw, meta = encoded(forecast(city, start, end), history.previous_runs_url(city, start, end))
    result = history.import_previous_runs(database, city, raw, meta, start, end)
    assert result['new_rows_by_lead'] == {'1': 736, '2': 736}
    assert database.execute('select count(*) from forecast_emos_live_vintages').fetchone()[0] == 0
    rows = database.execute('select fetched_at,source from nwp_model_forecasts').fetchall()
    assert {row[0] for row in rows} == {meta['retrieved_at']}
    assert {row[1] for row in rows} == {shadow.nwp_archive.DEFAULT_SOURCE}
    lineage = database.execute('select provider_initialized_at,is_original_vintage,availability_scope from shadow_training_lineage').fetchall()
    assert all(row[0] is None and row[1] == 0 and 'current retrieval' in row[2] for row in lineage)
    saved = database.execute('select * from nwp_model_forecasts').fetchall()
    raw2, meta2 = encoded(forecast(city, start, end), history.previous_runs_url(city, start, end))
    assert history.import_previous_runs(database, city, raw2, meta2, start, end)['new_rows_by_lead'] == {'1': 0, '2': 0}
    assert database.execute('select * from nwp_model_forecasts').fetchall() == saved
    for table in ('shadow_training_lineage', 'shadow_truth_lineage'):
        if table == 'shadow_truth_lineage':
            continue
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            database.execute('delete from ' + table)


def test_partial_day_and_freshest_variable_cannot_fill_fixed_lead(database):
    city = shadow.parse_city_slugs('min')[0]
    start, end = date(2026, 8, 1), date(2026, 8, 2)
    payload = forecast(city, start, end, incomplete=True)
    payload['hourly']['temperature_2m'] = [900]*48
    raw, meta = encoded(payload, history.previous_runs_url(city, start, end))
    result = history.import_previous_runs(database, city, raw, meta, start, end)
    assert result['new_rows_by_lead'] == {'1': 8, '2': 8}
    assert database.execute('select distinct target_date from nwp_model_forecasts').fetchall() == [('2026-08-02',)]


@pytest.mark.parametrize('bad', ['station', 'units', 'hash', 'timezone'])
def test_ambiguous_historical_identity_is_rejected(database, bad):
    city = shadow.parse_city_slugs('satx')[0]
    start = end = date(2026, 8, 1)
    payload = forecast(city, start, end)
    if bad == 'timezone':
        payload['utc_offset_seconds'] = 0
    if bad == 'units':
        payload['hourly_units'][next(k for k in payload['hourly_units'] if k != 'time')] = '°C'
    raw, meta = encoded(payload, history.previous_runs_url(city, start, end))
    if bad == 'station':
        meta['url'] = history.previous_runs_url(shadow.parse_city_slugs('lv')[0], start, end)
    if bad == 'hash':
        meta['sha256'] = 'x'*64
    with pytest.raises(ValueError):
        history.import_previous_runs(database, city, raw, meta, start, end)
    assert database.execute('select count(*) from nwp_model_forecasts').fetchone()[0] == 0


def test_iem_archive_retains_final_source_and_does_not_invent_old_availability(database):
    city = shadow.parse_city_slugs('dc')[0]
    old = '2026-08-01'
    payload = {'station': city.nws_station_id, 'results': [{'valid': old, 'high': 88, 'product': 'archived-id'},
        {'valid': '2026-08-02', 'high': 89, 'raw_text': 'AS OF 500 PM'},
        {'valid': '2026-08-03', 'high': 90, 'is_final': False}, {'valid': '2026-08-04', 'high': 'M'}]}
    raw, meta = encoded(payload, history.iem_url(city, 2026))
    result = history.import_iem_truth(database, city, raw, meta, 2026)
    assert result['new_final_days'] == 1 and result['missing_nonnumeric_dates'] == ['2026-08-04']
    assert database.execute('select local_date,source,fetched_at,is_final from cli_settlements').fetchall() == [
        (old, 'iem_cli', meta['retrieved_at'], 1)]
    assert database.execute('select archived_product_id from shadow_truth_lineage').fetchone()[0] == 'archived-id'
    assert history.import_iem_truth(database, city, raw, meta, 2026)['new_final_days'] == 0
    assert not shadow.city_truth._iem_is_final({'high': 1}, city,
        datetime.now(timezone.utc).astimezone(city.fixed_standard_timezone()).date(), datetime.now(timezone.utc))


def test_archive_cannot_overwrite_conflicting_final_truth(database):
    city = shadow.parse_city_slugs('lax')[0]
    shadow.city_truth.upsert_settlement(database, city.nws_station_id, '2026-10-05', 78,
                                      fetched_at='2026-10-06T12:00:00+00:00')
    database.commit()
    raw, meta = encoded({'results': [{'valid': '2026-10-05', 'high': 79}]}, history.iem_url(city, 2026))
    with pytest.raises(ValueError, match='conflicts'):
        history.import_iem_truth(database, city, raw, meta, 2026)
    assert database.execute('select max_temperature_f,source from cli_settlements').fetchone() == (78, 'nws_cli')


def test_matching_archive_appends_corroboration_without_rewriting_original_final(database):
    city = shadow.parse_city_slugs('lax')[0]
    shadow.city_truth.upsert_settlement(database, city.nws_station_id, '2026-10-05', 78,
                                      fetched_at='2026-10-06T12:00:00+00:00')
    database.commit()
    before = database.execute('SELECT * FROM cli_settlements').fetchall()
    raw, meta = encoded({'results': [{'valid': '2026-10-05', 'high': 78}]}, history.iem_url(city, 2026))
    result = history.import_iem_truth(database, city, raw, meta, 2026)
    assert result['new_final_days'] == 0
    assert result['new_preserved_final_corroborations'] == 1
    assert database.execute('SELECT * FROM cli_settlements').fetchall() == before
    assert database.execute('SELECT source,retrieved_at FROM shadow_truth_lineage').fetchone() == ('iem_cli', meta['retrieved_at'])
    repeated = history.import_iem_truth(database, city, raw, meta, 2026)
    assert repeated['new_final_days'] == repeated['new_preserved_final_corroborations'] == 0
    assert database.execute('SELECT count(*) FROM shadow_truth_lineage').fetchone()[0] == 1


def test_guarded_entry_point_requires_wrapper(monkeypatch, tmp_path):
    monkeypatch.delenv('WEATHEREDGE_LOCAL_BUDGET_ACTIVE', raising=False)
    monkeypatch.setattr(sys, 'argv', ['shadow_history.py', '--execute', '--state-dir', str(tmp_path),
        '--start', '2026-08-01', '--end', '2026-08-31', '--year', '2026'])
    with pytest.raises(SystemExit) as error:
        history.main()
    assert error.value.code == 2


@pytest.mark.parametrize('bad', ['preliminary', 'station', 'date', 'missing'])
def test_archived_text_requires_exact_numeric_final_station_day(database, bad):
    city = shadow.parse_city_slugs('phx')[0]
    product = '202609280900-KPSR-CDUS45-CLIPHX'
    target = date(2026, 9, 27)
    text = 'CLIPHX\nCLIMATE SUMMARY FOR SEPTEMBER 27 2026\nTEMPERATURE (F)\nMAXIMUM 97 1259 PM\n'
    if bad == 'preliminary': text = 'AS OF 500 PM\n' + text
    if bad == 'station': text = text.replace('CLIPHX', 'CLISFO')
    if bad == 'date': text = text.replace('27 2026', '26 2026')
    if bad == 'missing': text = text.replace('MAXIMUM 97', 'MAXIMUM M')
    raw = text.encode()
    stamp = datetime.now(timezone.utc).isoformat()
    meta = {'url': history.archived_product_url(city, product), 'sha256': shadow.sha(raw), 'bytes': len(raw),
            'request_started_at': stamp, 'retrieved_at': stamp}
    with pytest.raises(ValueError):
        history.import_archived_product(database, city, raw, meta, target, product)
    assert database.execute('select count(*) from cli_settlements').fetchone()[0] == 0


def test_archived_text_strengthens_atomic_source_with_raw_final_report(database):
    city = shadow.parse_city_slugs('lax')[0]
    product = '202610060840-KLOX-CDUS46-CLILAX'
    target = date(2026, 10, 5)
    raw = b'CLILAX\nCLIMATE SUMMARY FOR OCTOBER 5 2026\nTEMPERATURE (F)\nMAXIMUM 101R 1259 PM\n'
    stamp = datetime.now(timezone.utc).isoformat()
    meta = {'url': history.archived_product_url(city, product), 'sha256': shadow.sha(raw), 'bytes': len(raw),
            'request_started_at': stamp, 'retrieved_at': stamp}
    assert history.import_archived_product(database, city, raw, meta, target, product)['high_f'] == 101
    assert database.execute('select source from cli_settlements').fetchone()[0] == 'nws_cli_iem_archive'
    with pytest.raises(sqlite3.IntegrityError, match='append-only'):
        database.execute('delete from shadow_truth_lineage')


@pytest.mark.parametrize('token,expected', [('101R', 101), ('87R', 87), ('-4R', -4),
    ('101X', None), ('101.5', None), ('MM', None), ('9999', None)])
def test_forecaster_record_flag_parser_matches_official_integer_token(token, expected):
    assert shadow.parse_cli_report('TEMPERATURE (F)\nMAXIMUM ' + token + ' 1259 PM\n').max_temperature_f == expected
