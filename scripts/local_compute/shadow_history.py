#!/usr/bin/env python3
"""Bounded source-preserving historical onboarding for the separate Mac shadow.

Open-Meteo previous-runs values are reconstructed fixed-lead weather training,
never original decision-time vintages. IEM CLI atomic data is retained with its
actual retrieval time and the existing conservative archive finality policy.
No AWS API, paid endpoint, paper ledger or public artifact is written.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import urlencode

import shadow_collect as shadow
from guards import Budget, host_snapshot, policy

HISTORY_HOSTS = frozenset({'previous-runs-api.open-meteo.com', 'mesonet.agron.iastate.edu'})
MAXIMUM_HISTORY_DAYS = 120
LEADS = (1, 2)


def validate_metadata(raw, metadata):
    if metadata['sha256'] != shadow.sha(raw) or metadata['bytes'] != len(raw):
        raise ValueError('raw response hash/length mismatch')
    if not shadow.utc_clock(metadata['request_started_at']) <= shadow.utc_clock(metadata['retrieved_at']) <= datetime.now(timezone.utc):
        raise ValueError('invalid current retrieval clocks')


def retain_http(conn, provider, city, raw, metadata):
    """A parser repair can reuse the exact immutable response without a GET."""
    evidence = shadow.sha(shadow.canonical({**metadata, 'provider': provider,
                                           'station_id': city.nws_station_id}).encode())
    if conn.execute('SELECT 1 FROM shadow_http_evidence WHERE evidence_id=?', (evidence,)).fetchone():
        return evidence
    return shadow.save_http(conn, provider, city, raw, metadata)


def previous_runs_url(city, start, end):
    return shadow.nwp_archive.OPEN_METEO_PREVIOUS_RUNS_URL + '?' + urlencode({
        'latitude': city.latitude, 'longitude': city.longitude,
        'hourly': ','.join(shadow.nwp_archive.previous_day_variable(lead) for lead in LEADS),
        'temperature_unit': 'fahrenheit', 'timezone': city.settlement_tz_name,
        'start_date': start.isoformat(), 'end_date': end.isoformat(),
        'models': ','.join(shadow.nwp_archive.NWP_MODELS)})


def import_previous_runs(conn, city, raw, metadata, start, end):
    validate_metadata(raw, metadata)
    if metadata['url'] != previous_runs_url(city, start, end):
        raise ValueError('historical request does not bind registered station/range/models')
    payload = json.loads(raw)
    if payload.get('utc_offset_seconds') != city.standard_utc_offset_hours * 3600:
        raise ValueError('historical climate-day timezone mismatch')
    hourly, units = payload.get('hourly') or {}, payload.get('hourly_units') or {}
    times = hourly.get('time') or []
    if not isinstance(times, list) or len(times) > MAXIMUM_HISTORY_DAYS * 24:
        raise ValueError('historical hourly series exceeds fixed bound')
    evidence = retain_http(conn, 'open_meteo_previous_runs', city, raw, metadata)
    imported_at = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    counts = {str(lead): 0 for lead in LEADS}
    with conn:
        for model in shadow.nwp_archive.NWP_MODELS:
            for lead in LEADS:
                variable = shadow.nwp_archive.previous_day_variable(lead)
                name = variable + '_' + model
                values = hourly.get(name)
                if values is None:
                    continue
                if units.get(name) != '°F' or not isinstance(values, list):
                    raise ValueError('named historical temperatures lack Fahrenheit units')
                reconstructed = shadow.nwp_archive.reconstruct_daily_max(
                    {'hourly': {'time': times, variable: values}}, lead)
                for target, high in reconstructed.items():
                    if not start <= date.fromisoformat(target) <= end:
                        raise ValueError('historical output escapes requested date range')
                    counts[str(lead)] += shadow.save_training_row(conn,
                        (city.nws_station_id, target, model, lead, high, metadata['retrieved_at'],
                         shadow.nwp_archive.DEFAULT_SOURCE), evidence, imported_at,
                        'reconstructed fixed-lead previous-runs; first available locally at current retrieval; original initialization unknown')
    return {'new_rows_by_lead': counts, 'evidence_id': evidence,
            'original_vintages': False, 'source': shadow.nwp_archive.DEFAULT_SOURCE}


def iem_url(city, year):
    return shadow.city_truth.IEM_CLI_URL.format(station=city.nws_station_id, year=year)


def import_iem_truth(conn, city, raw, metadata, year):
    validate_metadata(raw, metadata)
    if metadata['url'] != iem_url(city, year):
        raise ValueError('archive request does not bind registered station/year')
    payload = json.loads(raw)
    if payload.get('station', city.nws_station_id) != city.nws_station_id:
        raise ValueError('archive response station mismatch')
    rows = payload.get('results')
    if not isinstance(rows, list) or len(rows) > 400:
        raise ValueError('archive truth response missing or unbounded')
    evidence = retain_http(conn, 'iem_cli', city, raw, metadata)
    observed = shadow.utc_clock(metadata['retrieved_at'])
    written, corroborated, missing = 0, 0, []
    with conn:
        for row in rows:
            if row.get('station', city.nws_station_id) != city.nws_station_id:
                raise ValueError('archive truth row station mismatch')
            valid, high = row.get('valid'), row.get('high')
            if not valid:
                continue
            target = date.fromisoformat(str(valid)[:10])
            if target.year != year:
                raise ValueError('archive truth date outside requested year')
            if high is None or high == 'M':
                missing.append(target.isoformat())
                continue
            if type(high) not in (int, float) or not math.isfinite(high) or int(high) != high:
                raise ValueError('archive CLI maximum must be an integer Fahrenheit value or explicit M/null missing marker')
            if not shadow.city_truth._iem_is_final(row, city, target, observed):
                continue
            # Existing confirmed NWS truth and its original receipt are retained.
            prior = conn.execute('SELECT max_temperature_f FROM cli_settlements '
                'WHERE station_id=? AND local_date=? AND is_final=1',
                (city.nws_station_id, target.isoformat())).fetchone()
            if prior is not None:
                if prior[0] != high:
                    raise ValueError('archive CLI conflicts with retained final truth')
            basis = 'IEM CLI atomic archive; existing two-standard-day stability finality rule'
            if prior is None:
                shadow.city_truth.upsert_settlement(conn, city.nws_station_id, target.isoformat(),
                    int(high), source='iem_cli', fetched_at=metadata['retrieved_at'], is_final=True)
                written += 1
            fields = [evidence, city.nws_station_id, target.isoformat(), int(high),
                      metadata['retrieved_at'], 'iem_cli', basis, row.get('product')]
            identity = shadow.sha(shadow.canonical(fields).encode())
            if not conn.execute('SELECT 1 FROM shadow_truth_lineage WHERE truth_id=?', (identity,)).fetchone():
                conn.execute('INSERT INTO shadow_truth_lineage VALUES (?,?,?,?,?,?,?,?,?)', (identity, *fields))
                corroborated += int(prior is not None)
    return {'new_final_days': written, 'new_preserved_final_corroborations': corroborated,
            'evidence_id': evidence, 'finality_basis': basis if written or corroborated else None,
            'missing_nonnumeric_dates': missing}


def archived_product_url(city, product_id):
    if re.fullmatch(r'\d{12}-K' + re.escape(city.cli_site) + r'-[A-Z0-9]{6}-CLI' + re.escape(city.cli_issuedby), product_id) is None:
        raise ValueError('archive product ID does not bind the registered CLI office/station')
    return 'https://mesonet.agron.iastate.edu/api/1/nwstext/' + product_id


def import_archived_product(conn, city, raw, metadata, target, product_id):
    validate_metadata(raw, metadata)
    if metadata['url'] != archived_product_url(city, product_id):
        raise ValueError('archive product request identity mismatch')
    text = raw.decode('utf-8', errors='strict')
    evidence = retain_http(conn, 'nws_cli_iem_archive', city, raw, metadata)
    if re.search(r'\bCLI' + re.escape(city.cli_issuedby) + r'\b', text, re.I) is None:
        raise ValueError('archived NWS CLI station identity mismatch')
    report = shadow.parse_cli_report(text)
    if report.report_date != target or report.max_temperature_f is None or report.is_preliminary:
        raise ValueError('archived NWS CLI is missing numeric final truth for exact requested day')
    if not shadow.city_truth.settlement_is_final(city, target, shadow.utc_clock(metadata['retrieved_at'])):
        raise ValueError('archived climate day unfinished at retrieval')
    prior = conn.execute('SELECT max_temperature_f FROM cli_settlements '
        'WHERE station_id=? AND local_date=? AND is_final=1', (city.nws_station_id, target.isoformat())).fetchone()
    if prior and prior[0] != report.max_temperature_f:
        raise ValueError('archived final CLI conflicts with retained final truth')
    basis = 'numeric complete-day original NWS CLI product retained from IEM text archive'
    with conn:
        shadow.city_truth.upsert_settlement(conn, city.nws_station_id, target.isoformat(),
            report.max_temperature_f, source='nws_cli_iem_archive', fetched_at=metadata['retrieved_at'], is_final=True)
        fields = [evidence, city.nws_station_id, target.isoformat(), report.max_temperature_f,
                  metadata['retrieved_at'], 'nws_cli_iem_archive', basis, product_id]
        identity = shadow.sha(shadow.canonical(fields).encode())
        if not conn.execute('SELECT 1 FROM shadow_truth_lineage WHERE truth_id=?', (identity,)).fetchone():
            conn.execute('INSERT INTO shadow_truth_lineage VALUES (?,?,?,?,?,?,?,?,?)', (identity, *fields))
    return {'date': target.isoformat(), 'high_f': report.max_temperature_f,
            'source': 'nws_cli_iem_archive', 'final': True, 'evidence_id': evidence}


def coverage(conn, cities):
    result = {}
    for city in cities:
        result[city.slug] = {str(lead): conn.execute('SELECT COUNT(*) FROM ('
            'SELECT f.target_date FROM nwp_model_forecasts f JOIN cli_settlements t '
            'ON t.station_id=f.station_id AND t.local_date=f.target_date AND t.is_final=1 '
            'WHERE f.station_id=? AND f.lead_days=? AND f.source=? '
            'GROUP BY f.target_date HAVING COUNT(DISTINCT f.model)>=?)',
            (city.nws_station_id, lead, shadow.nwp_archive.DEFAULT_SOURCE,
             shadow.emos_forecast.MIN_MODELS)).fetchone()[0] for lead in LEADS}
    return result


def recover(state, forecast_cities, truth_cities, start, end, *, year, client=None, products=(), retained_truth=(), retained_products=()):
    state = state.expanduser().resolve()
    local = (shadow.ROOT / '.local').resolve()
    if not state.is_relative_to(local) or state == local:
        raise ValueError('history state must be separate under ignored .local')
    cities = {city.slug: city for city in (*forecast_cities, *truth_cities, *retained_truth,
                                          *(p[0] for p in (*products, *retained_products)))}
    requests = len(forecast_cities) + len(truth_cities) + len(products)
    if not 1 <= len(cities) <= shadow.MAXIMUM_CITIES or not 0 <= requests <= shadow.MAXIMUM_HTTP_REQUESTS or (not requests and not retained_truth and not retained_products):
        raise ValueError('historical batch requires at most four cities/eight requests')
    if len({c.slug for c in forecast_cities}) != len(forecast_cities) or len({c.slug for c in truth_cities}) != len(truth_cities):
        raise ValueError('duplicate historical cities')
    if not 1 <= (end - start).days + 1 <= MAXIMUM_HISTORY_DAYS or not start.year == end.year == year:
        raise ValueError('historical range must be at most 120 days within selected year')
    if any(not start <= target <= end for _, target, _ in (*products, *retained_products)):
        raise ValueError('archived target outside explicit historical range')
    for city in cities.values():
        if not shadow.city_truth.settlement_is_final(city, end, datetime.now(timezone.utc)):
            raise ValueError('historical training range includes an unfinished climate day')
    prospective = state / 'prospective'
    if not (prospective / 'shadow-weather.db').is_file():
        raise ValueError('existing separate seeded shadow store required')
    for name in ('shadow-weather.db', 'collector.lock', 'public-http-budget.json', 'history-receipt.json', 'weather-export.json'):
        if (prospective / name).is_symlink():
            raise ValueError('historical state files cannot redirect elsewhere')
    with (prospective / 'collector.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        source = shadow.source_identity()
        started = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        client = client or (shadow.PublicClient(prospective, requests, allowed_hosts=HISTORY_HOSTS) if requests else None)
        results = []
        with sqlite3.connect(prospective / 'shadow-weather.db', timeout=5) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            shadow.ensure_schema(conn)
            for city, target, product_id in retained_products:
                row = conn.execute('SELECT raw_text,url,request_started_at,retrieved_at,sha256,bytes '
                    'FROM shadow_http_evidence WHERE provider=? AND station_id=? AND url=? '
                    'ORDER BY retrieved_at DESC LIMIT 1', ('nws_cli_iem_archive', city.nws_station_id,
                                                          archived_product_url(city, product_id))).fetchone()
                if row is None:
                    raise ValueError('no exact retained NWS text response exists for local parser replay')
                raw = row[0].encode('utf-8')
                metadata = dict(zip(('url', 'request_started_at', 'retrieved_at', 'sha256', 'bytes'), row[1:]))
                value = import_archived_product(conn, city, raw, metadata, target, product_id)
                results.append({'city': city.slug, 'stage': 'retained_product', 'status': 'complete', **value})
            for city in retained_truth:
                row = conn.execute('SELECT raw_text,url,request_started_at,retrieved_at,sha256,bytes '
                    'FROM shadow_http_evidence WHERE provider=? AND station_id=? AND url=? '
                    'ORDER BY retrieved_at DESC LIMIT 1', ('iem_cli', city.nws_station_id, iem_url(city, year))).fetchone()
                if row is None:
                    raise ValueError('no exact retained CLI response exists for local parser replay')
                raw = row[0].encode('utf-8')
                metadata = dict(zip(('url', 'request_started_at', 'retrieved_at', 'sha256', 'bytes'), row[1:]))
                value = import_iem_truth(conn, city, raw, metadata, year)
                results.append({'city': city.slug, 'stage': 'retained_truth', 'status': 'complete', **value})
            for stage, batch in (('forecast', forecast_cities), ('truth', truth_cities)):
                for city in batch:
                    url = previous_runs_url(city, start, end) if stage == 'forecast' else iem_url(city, year)
                    try:
                        raw, metadata = client.get(url)
                        value = (import_previous_runs(conn, city, raw, metadata, start, end)
                                 if stage == 'forecast' else import_iem_truth(conn, city, raw, metadata, year))
                        results.append({'city': city.slug, 'stage': stage, 'status': 'complete', **value})
                    except Exception as error:
                        results.append({'city': city.slug, 'stage': stage, 'status': 'failed',
                                        'error_type': type(error).__name__, 'error': str(error)[:200]})
            for city, target, product_id in products:
                try:
                    raw, metadata = client.get(archived_product_url(city, product_id))
                    value = import_archived_product(conn, city, raw, metadata, target, product_id)
                    results.append({'city': city.slug, 'stage': 'archived_product', 'status': 'complete', **value})
                except Exception as error:
                    results.append({'city': city.slug, 'stage': 'archived_product', 'status': 'failed',
                                    'error_type': type(error).__name__, 'error': str(error)[:200]})
            if shadow.source_identity() != source:
                raise ValueError('source changed during historical collection; receipt withheld')
            finished = datetime.now(timezone.utc).isoformat(timespec='microseconds')
            exported = shadow.bounded_export(conn, finished)
            shadow.write_json(prospective / 'weather-export.json', exported)
            result = {'schema_version': 1, 'status': 'complete' if all(v['status']=='complete' for v in results) else 'partial',
                'started_at': started, 'finished_at': finished, **source, 'http_requests': client.used if client else 0,
                'maximum_http_requests': requests, 'research_identity': shadow.IDENTITY,
                'execution_location': 'local_mac', 'results': results,
                'matched_training_days': coverage(conn, cities.values()),
                'training_minimum_days': shadow.emos_forecast.EMOS_MIN_TRAIN,
                'weather_export_sha256': shadow.sha((prospective / 'weather-export.json').read_bytes()),
                'attribution': 'Weather data by Open-Meteo.com (CC BY 4.0); NWS CLI atomic archive provided by Iowa Environmental Mesonet',
                'live_orders_enabled': False, 'promotion_eligible': False,
                'boundary': 'current-retrieval reconstructed training; never historical original vintages, AWS evidence or trading profit'}
            path = prospective / 'history-runs' / started.replace(':', '').replace('+', '_')
            path.mkdir(parents=True, mode=0o700)
            shadow.write_json(path / 'receipt.json', result)
            shadow.write_json(prospective / 'history-receipt.json', result)
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--forecasts', default='')
    parser.add_argument('--truth', default='')
    parser.add_argument('--retained-truth', default='', help='reparse exact retained IEM response without a network call')
    parser.add_argument('--products', default='', help='comma-separated city:date:exact-IEM-product-ID')
    parser.add_argument('--retained-products', default='', help='reparse exact retained archived NWS text without a GET')
    parser.add_argument('--start', type=date.fromisoformat, required=True)
    parser.add_argument('--end', type=date.fromisoformat, required=True)
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    state = args.state_dir.expanduser().resolve()
    if args.execute:
        if os.getenv('WEATHEREDGE_LOCAL_BUDGET_ACTIVE') != '1':
            parser.error('historical collection requires native resource budget wrapper')
        products = []
        for token in filter(None, args.products.split(',')):
            slug, target, product_id = token.split(':', 2)
            products.append((shadow.parse_city_slugs(slug)[0], date.fromisoformat(target), product_id))
        retained_products = []
        for token in filter(None, args.retained_products.split(',')):
            slug, target, product_id = token.split(':', 2)
            retained_products.append((shadow.parse_city_slugs(slug)[0], date.fromisoformat(target), product_id))
        result = recover(state, shadow.parse_city_slugs(args.forecasts) if args.forecasts else (),
            shadow.parse_city_slugs(args.truth) if args.truth else (), args.start, args.end, year=args.year,
            products=products, retained_truth=shadow.parse_city_slugs(args.retained_truth) if args.retained_truth else (),
            retained_products=retained_products)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] == 'complete' else 1
    if not state.is_relative_to((shadow.ROOT / '.local').resolve()) or state == (shadow.ROOT / '.local').resolve():
        parser.error('history state must be separate under ignored .local')
    limits = policy({'limits': {'wall_seconds': 300, 'cpu_seconds': 120}})
    with (state / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        host_snapshot(state, limits)
        budget = Budget(state, limits)
        with (state / 'history-supervisor.log').open('w') as log:
            budget.run([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], '--execute'],
                       stdout=log, stderr=subprocess.STDOUT)
        receipt = state / 'prospective' / 'history-receipt.json'
        value = json.loads(receipt.read_text())
        value['resources'] = budget.receipt()
        shadow.write_json(receipt, value)
        print(json.dumps({'status': value['status'], 'http_requests': value['http_requests'],
                          'matched_training_days': value['matched_training_days'], 'resources': value['resources']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
