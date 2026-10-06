#!/usr/bin/env python3
"""Collect original local paper-research weather vintages from free public APIs.

This separate disclosed educational shadow is never an AWS V7 ledger, a trading
feed, or permission for commercial API use. The CLI requires the reviewed Mac
worker's resource budget. Historical seed rows remain reconstructed history.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler

for _name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
              'VECLIB_MAXIMUM_THREADS', 'NUMEXPR_NUM_THREADS', 'BLIS_NUM_THREADS'):
    os.environ[_name] = '1'
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'forecaster'))
from cities import CITIES, city_for_station, parse_city_slugs
import city_truth
from clisfo import parse_cli_report
import emos_forecast
from live_forecast_evidence import LiveModelForecast, ensure_schema as ensure_vintages
import nwp_archive

IDENTITY = 'local-shadow-v7-v1'
MAXIMUM_CITIES = 4
MAXIMUM_HTTP_REQUESTS = 8
DAILY_HTTP_REQUESTS = 96
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_SEED_BYTES = 100_000_000
MAX_TABLE_ROWS = 250_000
MAX_RUNTIME_SECONDS = 300
ALLOWED_HOSTS = {'api.open-meteo.com', 'forecast.weather.gov'}
USER_AGENT = 'WeatherEdge-disclosed-student-paper-research/0.7 (https://github.com/Jaxsonb04/weather_edge)'
SOURCE_FILES = ('scripts/local_compute/shadow_collect.py', 'forecaster/cities.py',
    'forecaster/emos_forecast.py', 'forecaster/live_forecast_evidence.py',
    'forecaster/postproc_models.py', 'forecaster/emos_recalibration.py',
    'forecaster/truth_store.py', 'forecaster/city_truth.py', 'forecaster/nwp_archive.py',
    'forecaster/clisfo.py', 'forecaster/settlement_calendar.py', 'forecaster/scores.py',
    'forecaster/emos_sources.py')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def utc_clock(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('local evidence clocks require a timezone')
    return stamp.astimezone(timezone.utc)


def write_json(path, value):
    encoded = json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n'
    descriptor, token = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    stage = Path(token)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(encoded)
        stage.replace(path)
    finally:
        stage.unlink(missing_ok=True)


def source_identity():
    return {'source_commit': subprocess.check_output(
        ['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
        'source_dirty': bool(subprocess.check_output(
            ['git', '-C', str(ROOT), 'status', '--porcelain'], text=True).strip()),
        'implementation_sha256': {name: sha((ROOT / name).read_bytes()) for name in SOURCE_FILES}}


class PublicOnlyRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect is another request. Refuse it so the reserved eight-call
        # ceiling cannot silently grow even within an allowed host.
        raise ValueError('public redirects are disabled by the fixed request budget')


def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443)):
        raise ValueError('only the two documented unauthenticated public providers are allowed')


class PublicClient:
    def __init__(self, state, maximum, *, now=None, opener=None):
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= MAXIMUM_HTTP_REQUESTS:
            raise ValueError('HTTP request ceiling must be 1..8')
        self.maximum, self.used = maximum, 0
        self.day = (now or datetime.now(timezone.utc)).date().isoformat()
        self.counter = state / 'public-http-budget.json'
        self.deadline = time.monotonic() + MAX_RUNTIME_SECONDS
        if opener is None:
            # Trust standard TLS certificates; use existing certifi where the
            # Python installation lacks a populated system certificate store.
            try:
                import certifi
                context = ssl.create_default_context(cafile=certifi.where())
            except ImportError:
                context = ssl.create_default_context()
            opener = build_opener(PublicOnlyRedirects(), HTTPSHandler(context=context))
        self.opener = opener

    def get(self, url):
        validate_url(url)
        if self.used >= self.maximum or time.monotonic() >= self.deadline:
            raise RuntimeError('bounded public request/run allowance exhausted')
        saved = json.loads(self.counter.read_text()) if self.counter.exists() else {}
        total = saved.get('requests', 0) if saved.get('day') == self.day else 0
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise ValueError('public request budget counter is invalid')
        if total >= DAILY_HTTP_REQUESTS:
            raise RuntimeError('daily public request allowance exhausted')
        # Failed calls count; there is no retry storm or paid fallback.
        write_json(self.counter, {'day': self.day, 'requests': total + 1})
        self.used += 1
        started = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        request = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json,text/plain'})
        with self.opener.open(request, timeout=15) as response:
            validate_url(response.geturl())
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError('public response exceeded the fixed byte limit')
        return raw, {'url': url, 'request_started_at': started,
                     'retrieved_at': datetime.now(timezone.utc).isoformat(timespec='microseconds'),
                     'sha256': sha(raw), 'bytes': len(raw)}


def ensure_schema(conn):
    nwp_archive.ensure_schema(conn)
    city_truth.ensure_schema(conn)
    emos_forecast.ensure_schema(conn)
    ensure_vintages(conn)
    conn.execute('CREATE TABLE IF NOT EXISTS shadow_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    conn.execute('''CREATE TABLE IF NOT EXISTS shadow_http_evidence (
        evidence_id TEXT PRIMARY KEY, provider TEXT NOT NULL, station_id TEXT NOT NULL,
        request_started_at TEXT NOT NULL, retrieved_at TEXT NOT NULL, url TEXT NOT NULL,
        sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, raw_text TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS shadow_forecast_lineage (
        snapshot_id TEXT PRIMARY KEY REFERENCES forecast_emos_live_vintages(snapshot_id),
        research_identity TEXT NOT NULL, execution_location TEXT NOT NULL,
        model_policy_fingerprint_sha256 TEXT NOT NULL, lineage_sha256 TEXT NOT NULL,
        source_commit TEXT NOT NULL, source_dirty INTEGER NOT NULL, policy_json TEXT NOT NULL)''')
    for table in ('shadow_http_evidence', 'shadow_forecast_lineage'):
        for operation in ('UPDATE', 'DELETE'):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_reject_{operation.lower()} "
                         f"BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'shadow evidence is append-only'); END")
        key = 'evidence_id' if table == 'shadow_http_evidence' else 'snapshot_id'
        conn.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_reject_replacement BEFORE INSERT ON {table} '
                     f'WHEN EXISTS(SELECT 1 FROM {table} WHERE {key}=NEW.{key}) '
                     "BEGIN SELECT RAISE(ABORT, 'shadow evidence is append-only'); END")
    conn.commit()


def seed_history(conn, seed_path):
    prior = conn.execute("SELECT value FROM shadow_metadata WHERE key='seed'").fetchone()
    if prior:
        # An input path may change on a later worker run. It does not replace
        # or relabel the already-imported history or its original byte hash.
        return json.loads(prior[0])
    with seed_path.open('rb') as stream:
        raw = stream.read(MAX_SEED_BYTES + 1)
    if len(raw) > MAX_SEED_BYTES:
        raise ValueError('historical seed exceeds the fixed byte limit')
    export = json.loads(raw)
    captured = utc_clock(export['captured_at'])
    if captured > datetime.now(timezone.utc):
        raise ValueError('historical seed capture is in the future')
    with conn:
        for table in ('nwp_model_forecasts', 'cli_settlements', 'forecast_emos_daily_high'):
            block = export[table]
            if block.get('available', True) is not True or block.get('truncated') is not False or len(block['rows']) > MAX_TABLE_ROWS:
                raise ValueError('a complete bounded historical seed is required')
            columns = [row[1] for row in conn.execute(f'PRAGMA table_info({table})')]
            required = {
                'nwp_model_forecasts': ('station_id', 'target_date', 'model', 'lead_days',
                                       'predicted_high_f', 'fetched_at', 'source'),
                'cli_settlements': ('station_id', 'local_date', 'max_temperature_f',
                                    'fetched_at', 'source', 'is_final'),
                'forecast_emos_daily_high': ('station_id', 'target_date', 'lead_days',
                    'predicted_high_f', 'sigma_f', 'n_models', 'fetched_at', 'method', 'source'),
            }[table]
            for row in block['rows']:
                if any(field not in row for field in required):
                    raise ValueError('historical seed lacks explicit station/source/forecast provenance')
                if utc_clock(row['fetched_at']) > captured:
                    raise ValueError('historical seed row follows its capture')
                if table == 'cli_settlements' and row.get('is_final') == 1:
                    station = city_for_station(row['station_id'])
                    if not city_truth.settlement_is_final(station, date.fromisoformat(row['local_date']), captured):
                        raise ValueError('historical seed marks an unfinished station day final')
                values = {key: value for key, value in row.items() if key in columns}
                conn.execute(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                             tuple(values.values()))
        receipt = {'sha256': sha(raw), 'captured_at': export['captured_at'],
                   'scope': 'retained reconstructed training history; never original local vintages'}
        with seed_path.open('rb') as stream:
            final_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
        if final_hash != receipt['sha256']:
            raise ValueError('historical seed changed during import')
        conn.execute("INSERT INTO shadow_metadata VALUES ('seed',?)", (canonical(receipt),))
    return receipt


def save_http(conn, provider, city, raw, metadata):
    value = {**metadata, 'provider': provider, 'station_id': city.nws_station_id}
    identity = sha(canonical(value).encode())
    conn.execute('INSERT INTO shadow_http_evidence VALUES (?,?,?,?,?,?,?,?,?)',
                 (identity, provider, city.nws_station_id, metadata['request_started_at'],
                  metadata['retrieved_at'], metadata['url'], metadata['sha256'],
                  metadata['bytes'], raw.decode('utf-8', errors='replace')))
    conn.commit()


def model_payload(payload, city, retrieved_at):
    if payload.get('utc_offset_seconds') != city.standard_utc_offset_hours * 3600:
        raise ValueError('provider climate-day timezone does not match the registered station')
    daily = payload.get('daily') or {}
    units = payload.get('daily_units') or {}
    times = daily.get('time') or []
    if len(times) > 3 or len(set(times)) != len(times):
        raise ValueError('daily model dates are ambiguous or unbounded')
    outputs = {}
    for index, token in enumerate(times):
        target = date.fromisoformat(token)
        values = {}
        for model in nwp_archive.NWP_MODELS:
            name = f'temperature_2m_max_{model}'
            array = daily.get(name)
            if not isinstance(array, list) or len(array) != len(times):
                continue
            if units.get(name) != '°F':
                raise ValueError('named provider daily maximum is not explicitly Fahrenheit')
            value = array[index]
            if type(value) in (int, float) and math.isfinite(value):
                values[model] = float(value)
        if values:
            outputs[target] = LiveModelForecast(values, retrieved_at=retrieved_at)
    return outputs


def refresh_cli(conn, city, raw, metadata):
    text = raw.decode('utf-8', errors='replace')
    if re.search(r'\bCLI' + re.escape(city.cli_issuedby) + r'\b', text, re.I) is None:
        raise ValueError('NWS product identity does not match the registered CLI station')
    report = parse_cli_report(text)
    if report.report_date is None or report.max_temperature_f is None:
        raise ValueError('NWS product has no verified numeric station-day report')
    final = not report.is_preliminary and city_truth.settlement_is_final(
        city, report.report_date, utc_clock(metadata['retrieved_at']))
    city_truth.upsert_settlement(conn, city.nws_station_id, report.report_date.isoformat(),
        report.max_temperature_f, source='nws_cli', fetched_at=metadata['retrieved_at'], is_final=final)
    conn.commit()
    return {'date': report.report_date.isoformat(), 'final': final}


def bounded_export(conn, captured_at):
    output = {'captured_at': captured_at, 'research_identity': IDENTITY,
              'execution_location': 'local_mac', 'live_orders_enabled': False}
    for table in ('nwp_model_forecasts', 'cli_settlements', 'forecast_emos_daily_high',
                  'forecast_emos_live_vintages', 'nwp_live_forecast_members', 'shadow_forecast_lineage'):
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info({table})')]
        rows = [dict(zip(columns, values)) for values in conn.execute(f'SELECT * FROM {table} LIMIT ?', (MAX_TABLE_ROWS + 1,))]
        if len(rows) > MAX_TABLE_ROWS:
            raise ValueError('local shadow export exceeded the table ceiling')
        output[table] = {'available': True, 'rows': rows, 'truncated': False}
    if len(canonical(output).encode()) > MAX_SEED_BYTES:
        raise ValueError('local shadow export exceeded the byte ceiling')
    return output


def collect(seed_path, state, cities, *, maximum_requests=8, client=None, rotate_registry=False):
    state = state.expanduser().resolve()
    # Canonical runtime stores and arbitrary directories are not collection destinations.
    local = (ROOT / '.local').resolve()
    if not state.is_relative_to(local) or state == local:
        raise ValueError('shadow state must have its own directory inside ignored .local')
    if not 1 <= len(cities) <= MAXIMUM_CITIES or len({c.slug for c in cities}) != len(cities):
        raise ValueError('choose one to four distinct registered cities')
    if isinstance(maximum_requests, bool) or not isinstance(maximum_requests, int) or not 1 <= maximum_requests <= MAXIMUM_HTTP_REQUESTS:
        raise ValueError('HTTP request ceiling must be 1..8')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    for name in ('shadow-weather.db', 'collector.lock', 'public-http-budget.json',
                 'rotation.json', 'weather-export.json', 'latest-receipt.json'):
        if (state / name).is_symlink():
            raise ValueError('shadow state files must not redirect outside their directory')
    with (state / 'collector.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rotation = state / 'rotation.json'
        cursor = 0
        if rotate_registry:
            saved = json.loads(rotation.read_text()) if rotation.exists() else {}
            cursor = saved.get('next_city_index', 0)
            if isinstance(cursor, bool) or not isinstance(cursor, int) or not 0 <= cursor < len(CITIES):
                raise ValueError('registry rotation cursor is invalid')
            cities = tuple(CITIES[(cursor + i) % len(CITIES)] for i in range(len(cities)))
        client = client or PublicClient(state, maximum_requests)
        deadline = time.monotonic() + MAX_RUNTIME_SECONDS
        def check_time():
            if time.monotonic() >= deadline:
                raise TimeoutError('local prospective collection exceeded its fixed runtime ceiling')
        source = source_identity()
        started = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        with sqlite3.connect(state / 'shadow-weather.db', timeout=5) as conn:
            os.chmod(state / 'shadow-weather.db', 0o600)
            conn.execute('PRAGMA foreign_keys=ON')
            ensure_schema(conn)
            seed = seed_history(conn, seed_path)
            model_policy = {**source, 'research_identity': IDENTITY, 'seed_sha256': seed['sha256'],
                'method': 'v7 inverse-variance EMOS with existing bias-only correction',
                'models': list(nwp_archive.NWP_MODELS), 'purpose': 'disclosed_noncommercial_educational_paper_research',
                'provider_initialized_at': None, 'constituent_hour_completeness': None,
                'provider_attribution': 'Weather data by Open-Meteo.com (CC BY 4.0); final CLI reports by NWS',
                'provider_terms_url': 'https://open-meteo.com/en/terms'}
            fingerprint = sha(canonical(model_policy).encode())
            results, forecasts = [], {}
            # Forecast calls have priority so a truth outage cannot suppress a new vintage.
            for city in cities:
                check_time()
                params = urlencode({'latitude': city.latitude, 'longitude': city.longitude,
                    'daily': 'temperature_2m_max', 'temperature_unit': 'fahrenheit',
                    'timezone': city.settlement_tz_name, 'forecast_days': 3,
                    'models': ','.join(nwp_archive.NWP_MODELS)})
                try:
                    raw, meta = client.get('https://api.open-meteo.com/v1/forecast?' + params)
                    save_http(conn, 'open_meteo', city, raw, meta)
                    forecasts[city.slug] = model_payload(json.loads(raw), city, meta['retrieved_at'])
                except Exception as error:
                    results.append({'city': city.slug, 'stage': 'forecast', 'error_type': type(error).__name__})
            for city in cities:
                check_time()
                try:
                    raw, meta = client.get(city.cli_product_url)
                    save_http(conn, 'nws_cli', city, raw, meta)
                    truth = refresh_cli(conn, city, raw, meta)
                    results.append({'city': city.slug, 'stage': 'truth', **truth})
                except Exception as error:
                    results.append({'city': city.slug, 'stage': 'truth', 'error_type': type(error).__name__})
            snapshot_ids = []
            for city in cities:
                check_time()
                today = datetime.now(timezone.utc).astimezone(city.fixed_standard_timezone()).date()
                for target, members in sorted(forecasts.get(city.slug, {}).items()):
                    check_time()
                    lead = (target - today).days
                    if lead not in (0, 1, 2):
                        continue
                    recorded = datetime.now(timezone.utc).isoformat(timespec='microseconds')
                    result = emos_forecast.serve_live_emos(conn, target, city=city,
                        lead_days=max(lead, 1), store_lead_days=lead,
                        live_models=members, fetched_at=recorded)
                    if result is None:
                        results.append({'city': city.slug, 'target_date': target.isoformat(),
                                        'stage': 'serve', 'state': 'insufficient_training_or_members'})
                        continue
                    row = conn.execute('SELECT snapshot_id FROM forecast_emos_live_vintages '
                        'WHERE station_id=? AND target_date=? AND recorded_at=?',
                        (city.nws_station_id, target.isoformat(), recorded)).fetchone()
                    if row is None:
                        raise ValueError('served forecast lacks immutable vintage evidence')
                    fields = {'snapshot_id': row[0], 'research_identity': IDENTITY,
                        'execution_location': 'local_mac', 'model_policy_fingerprint_sha256': fingerprint}
                    conn.execute('INSERT INTO shadow_forecast_lineage VALUES (?,?,?,?,?,?,?,?)',
                        (*fields.values(), sha(canonical(fields).encode()), source['source_commit'],
                         int(source['source_dirty']), canonical(model_policy)))
                    conn.commit()
                    snapshot_ids.append(row[0])
            if source_identity() != source:
                raise ValueError('collection source changed during the run; no successful receipt promoted')
            check_time()
            finished = datetime.now(timezone.utc).isoformat(timespec='microseconds')
            exported = bounded_export(conn, finished)
            weather_export = state / 'weather-export.json'
            write_json(weather_export, exported)
            receipt = {'schema_version': 1, 'status': 'complete' if snapshot_ids else 'no_issued_distribution',
                'started_at': started, 'finished_at': finished, **source,
                'research_identity': IDENTITY, 'execution_location': 'local_mac', 'seed': seed,
                'new_issued_vintages': len(snapshot_ids), 'new_snapshot_ids': snapshot_ids,
                'http_requests': client.used, 'results': results,
                'cities': [city.slug for city in cities], 'source_registry_cities': len(CITIES),
                'rotating_registry': rotate_registry, 'maximum_http_requests': maximum_requests,
                'maximum_runtime_seconds': MAX_RUNTIME_SECONDS,
                'model_policy_fingerprint_sha256': fingerprint,
                'weather_export_sha256': sha(weather_export.read_bytes()),
                'live_orders_enabled': False, 'promotion_eligible': False,
                'purpose': 'disclosed noncommercial educational research; commercial/real-money provider use unapproved',
                'attribution': model_policy['provider_attribution'],
                'unknown_fields': ['provider_initialized_at', 'complete_hour_count'],
                'boundary': 'local shadow issued weather; never AWS V7, paper fills, trading profit or readiness'}
            run = state / 'runs' / started.replace(':', '').replace('+', '_')
            run.mkdir(parents=True, mode=0o700)
            write_json(run / 'receipt.json', receipt)
            write_json(state / 'latest-receipt.json', receipt)
            if rotate_registry:
                write_json(rotation, {'next_city_index': (cursor + len(cities)) % len(CITIES),
                    'registry': [city.slug for city in CITIES],
                    'policy': 'fixed batch per bounded attempt; unavailable forecasts/truth remain explicit gaps'})
            return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed-export', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--cities', default='sfo')
    parser.add_argument('--maximum-cities', type=int, default=1)
    parser.add_argument('--maximum-http-requests', type=int, default=8)
    parser.add_argument('--rotate-registry', action='store_true', help='rotate the batch over the twenty source cities')
    args = parser.parse_args()
    if os.getenv('WEATHEREDGE_LOCAL_BUDGET_ACTIVE') != '1':
        parser.error('run this collector through the reviewed Mac resource-budget wrapper')
    cities = parse_city_slugs(args.cities)
    if not 1 <= args.maximum_cities <= MAXIMUM_CITIES or len(cities) > args.maximum_cities:
        parser.error('registered city ceiling exceeded')
    result = collect(args.seed_export.resolve(), args.state_dir, cities,
                     maximum_requests=args.maximum_http_requests, rotate_registry=args.rotate_registry)
    print(json.dumps({key: result[key] for key in ('status', 'research_identity', 'new_issued_vintages',
                                                   'http_requests', 'live_orders_enabled', 'promotion_eligible')}))
    return 0 if result['new_issued_vintages'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
