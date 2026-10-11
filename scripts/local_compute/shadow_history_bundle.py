#!/usr/bin/env python3
"""Transfer attributed reconstructed training, without importing issued vintages.

Bundles retain actual current retrieval clocks and immutable raw source receipts.
Import replays those sources locally, verifies the derived rows, refuses existing
conflicts and appends provenance in one transaction. Production apply requires
the canonical root-owned deployment maintenance marker and a pinned bundle hash.
There are no provider requests, paper-ledger writes or promotion changes here.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import fcntl
import json
from pathlib import Path
import sqlite3
import stat
from urllib.parse import parse_qs, urlsplit

import shadow_history as history

shadow = history.shadow
DEFAULT_CITIES = 'lv,min,satx,nola,dc'
MAX_BYTES = 64 * 1024**2
MAX_ROWS = 25000
MAINTENANCE_MARKER = Path('/run/weatheredge-deploy-maintenance')
TABLES = ('nwp_model_forecasts', 'cli_settlements', 'shadow_training_lineage',
          'shadow_truth_lineage', 'shadow_http_evidence')
FORECAST_COLUMNS = ('station_id', 'target_date', 'model', 'lead_days',
                    'predicted_high_f', 'fetched_at', 'source')
TRUTH_COLUMNS = ('station_id', 'local_date', 'max_temperature_f', 'fetched_at', 'source', 'is_final')


def rows(conn, query, args=()):
    cursor = conn.execute(query, args)
    names = [item[0] for item in cursor.description]
    values = cursor.fetchmany(MAX_ROWS + 1)
    if len(values) > MAX_ROWS:
        raise ValueError('history bundle table exceeds bounded row count')
    return [dict(zip(names, value)) for value in values]


def bundle_hash(value):
    return shadow.sha(shadow.canonical({key: item for key, item in value.items()
                                       if key != 'content_sha256'}).encode())


def matched_training_days(value):
    truth = {(row['station_id'], row['local_date']) for row in value['tables']['cli_settlements']}
    models = {}
    for row in value['tables']['nwp_model_forecasts']:
        if (row['station_id'], row['target_date']) in truth:
            models.setdefault((row['station_id'], row['lead_days'], row['target_date']), set()).add(row['model'])
    return {station: {str(lead): sum(key[0] == station and key[1] == lead
                and len(names) >= shadow.emos_forecast.MIN_MODELS for key, names in models.items())
                for lead in history.LEADS} for station in value['station_ids']}


def build_bundle(conn, cities, start, end):
    if not cities or len({city.slug for city in cities}) != len(cities):
        raise ValueError('distinct registered cities required')
    if not 1 <= (end - start).days + 1 <= history.MAXIMUM_HISTORY_DAYS:
        raise ValueError('bundle range must be at most 120 days')
    stations = sorted(city.nws_station_id for city in cities)
    placeholders = ','.join('?' for _ in stations)
    scope = f'station_id IN ({placeholders})'
    nwp = rows(conn, f'SELECT * FROM nwp_model_forecasts WHERE {scope} '
               'AND target_date BETWEEN ? AND ? AND source=? ORDER BY station_id,target_date,model,lead_days',
               (*stations, start.isoformat(), end.isoformat(), shadow.nwp_archive.DEFAULT_SOURCE))
    training = rows(conn, f'SELECT * FROM shadow_training_lineage WHERE {scope} '
                    'AND target_date BETWEEN ? AND ? AND source=? ORDER BY history_id',
                    (*stations, start.isoformat(), end.isoformat(), shadow.nwp_archive.DEFAULT_SOURCE))
    candidates = rows(conn, f'SELECT * FROM shadow_truth_lineage WHERE {scope} '
                  "AND local_date BETWEEN ? AND ? AND source='iem_cli' ORDER BY retrieved_at,truth_id",
                  (*stations, start.isoformat(), end.isoformat()))
    # A current archive response can corroborate an earlier final NWS report
    # without replacing its canonical source or clock. Export that independent
    # IEM receipt as IEM evidence, using its own actual retrieval time. Repeated
    # agreeing receipts select the earliest retained corroboration deterministically.
    truth, truths, selected = [], [], {}
    for row in candidates:
        key = (row['station_id'], row['local_date'])
        if key in selected:
            if selected[key] != row['max_temperature_f']:
                raise ValueError('conflicting retained final truth corroborations')
            continue
        canonical = conn.execute('SELECT max_temperature_f,is_final FROM cli_settlements '
                                 'WHERE station_id=? AND local_date=?', key).fetchone()
        if canonical is None or tuple(canonical) != (row['max_temperature_f'], 1):
            raise ValueError('corroboration does not agree with retained canonical final truth')
        selected[key] = row['max_temperature_f']
        truths.append(row)
        truth.append({'station_id': row['station_id'], 'local_date': row['local_date'],
                      'max_temperature_f': row['max_temperature_f'], 'fetched_at': row['retrieved_at'],
                      'source': row['source'], 'is_final': 1})
    truth.sort(key=lambda row: (row['station_id'], row['local_date']))
    references = sorted({item['evidence_id'] for item in (*training, *truths)})
    if not references:
        raise ValueError('bundle has no attributable history')
    evidence = []
    for identity in references:
        found = rows(conn, 'SELECT * FROM shadow_http_evidence WHERE evidence_id=?', (identity,))
        if len(found) != 1:
            raise ValueError('history bundle has missing raw source receipt')
        evidence.extend(found)
    result = {'schema_version': 1, 'kind': 'weatheredge_reconstructed_history',
        'captured_at': datetime.now(timezone.utc).isoformat(timespec='microseconds'),
        'station_ids': stations, 'start_date': start.isoformat(), 'end_date': end.isoformat(),
        'tables': dict(zip(TABLES, (nwp, truth, training, truths, evidence))),
        'provider_attribution': 'Weather data by Open-Meteo.com (CC BY 4.0); NWS CLI archive by Iowa Environmental Mesonet',
        'scope': 'reconstructed historical training first available at retained current retrieval; no original issued vintages',
        'promotion_eligible': False}
    result['content_sha256'] = bundle_hash(result)
    validate_bundle(result)
    if len(shadow.canonical(result).encode()) > MAX_BYTES:
        raise ValueError('history bundle exceeds byte ceiling')
    return result


def _key(row, columns):
    return tuple(row[column] for column in columns)


def validate_bundle(value):
    """Re-derive each exported value from its exact retained provider response."""
    if (type(value.get('schema_version')) is not int or value['schema_version'] != 1 or value.get('kind') != 'weatheredge_reconstructed_history'
            or value.get('promotion_eligible') is not False or set(value.get('tables', {})) != set(TABLES)
            or value.get('content_sha256') != bundle_hash(value)):
        raise ValueError('history bundle identity/hash/table scope invalid')
    if len(shadow.canonical(value).encode()) > MAX_BYTES:
        raise ValueError('history bundle exceeds byte ceiling')
    captured = shadow.utc_clock(value['captured_at'])
    if captured > datetime.now(timezone.utc):
        raise ValueError('history bundle capture is in the future')
    start, end = date.fromisoformat(value['start_date']), date.fromisoformat(value['end_date'])
    if not 1 <= (end - start).days + 1 <= history.MAXIMUM_HISTORY_DAYS:
        raise ValueError('history bundle date range invalid')
    stations = value['station_ids']
    if not isinstance(stations, list) or not 1 <= len(stations) <= 20 or len(set(stations)) != len(stations):
        raise ValueError('history bundle station scope invalid')
    cities = {station: shadow.city_for_station(station) for station in stations}
    tables = value['tables']
    if any(not isinstance(items, list) or len(items) > MAX_ROWS for items in tables.values()):
        raise ValueError('history bundle tables exceed row ceiling')
    if len(tables['shadow_http_evidence']) > 64:
        raise ValueError('history bundle raw source count exceeds bound')
    source_training, source_truth = {}, {}
    evidence_ids = set()
    evidence_clocks = {}
    for evidence in tables['shadow_http_evidence']:
        identity = evidence['evidence_id']
        if identity in evidence_ids or evidence['station_id'] not in cities:
            raise ValueError('duplicate or wrong-station raw history evidence')
        evidence_ids.add(identity)
        raw = evidence['raw_text'].encode('utf-8')
        metadata = {key: evidence[key] for key in ('url', 'request_started_at', 'retrieved_at', 'sha256', 'bytes')}
        if (len(raw) > shadow.MAX_RESPONSE_BYTES or shadow.utc_clock(metadata['retrieved_at']) > captured
                or identity != shadow.sha(shadow.canonical({**metadata, 'provider': evidence['provider'],
                                                           'station_id': evidence['station_id']}).encode())):
            raise ValueError('raw history source receipt identity/clock invalid')
        evidence_clocks[identity] = shadow.utc_clock(metadata['retrieved_at'])
        city = cities[evidence['station_id']]
        query = parse_qs(urlsplit(metadata['url']).query)
        # Independent scratch stores avoid giving a later response precedence
        # over the exact earlier response named by a row's immutable lineage.
        with sqlite3.connect(':memory:') as source:
            source.execute('PRAGMA foreign_keys=ON')
            shadow.ensure_schema(source)
            if evidence['provider'] == 'open_meteo_previous_runs':
                first = date.fromisoformat(query['start_date'][0])
                last = date.fromisoformat(query['end_date'][0])
                if not 1 <= (last - first).days + 1 <= history.MAXIMUM_HISTORY_DAYS:
                    raise ValueError('raw historical source range invalid')
                history.import_previous_runs(source, city, raw, metadata, first, last)
            elif evidence['provider'] == 'iem_cli':
                history.import_iem_truth(source, city, raw, metadata, int(query['year'][0]))
            else:
                raise ValueError('historical bundle source provider is outside allowlist')
            for row in rows(source, 'SELECT * FROM shadow_training_lineage'):
                record = rows(source, 'SELECT * FROM nwp_model_forecasts WHERE '
                    'station_id=? AND target_date=? AND model=? AND lead_days=? AND source=?',
                    _key(row, ('station_id', 'target_date', 'model', 'lead_days', 'source')))[0]
                source_training[row['history_id']] = (row, record)
            for row in rows(source, 'SELECT * FROM shadow_truth_lineage'):
                record = rows(source, 'SELECT * FROM cli_settlements WHERE station_id=? AND local_date=?',
                              _key(row, ('station_id', 'local_date')))[0]
                source_truth[row['truth_id']] = (row, record)
    expected_training, expected_truth, referenced = {}, {}, set()
    for table, originals, expected, identity, key in (
        ('shadow_training_lineage', source_training, expected_training, 'history_id',
         ('station_id', 'target_date', 'model', 'lead_days', 'source')),
        ('shadow_truth_lineage', source_truth, expected_truth, 'truth_id', ('station_id', 'local_date')),
    ):
        seen = set()
        for row in tables[table]:
            compared = row
            if table == 'shadow_training_lineage' and row[identity] in originals:
                # Replaying retained bytes happens later than original import.
                # Preserve and validate that original local import clock rather
                # than requiring a newly generated replay timestamp to match it.
                try:
                    imported = shadow.utc_clock(row['imported_at'])
                    if not evidence_clocks[row['evidence_id']] <= imported <= captured:
                        raise ValueError('outside capture')
                except (KeyError, ValueError):
                    raise ValueError('history lineage import clock invalid') from None
                compared = {**row, 'imported_at': originals[row[identity]][0]['imported_at']}
            if row[identity] in seen or row[identity] not in originals or compared != originals[row[identity]][0]:
                raise ValueError('history lineage does not match re-derived raw source')
            seen.add(row[identity])
            record = originals[row[identity]][1]
            record_key = _key(record, key)
            if record_key in expected and expected[record_key] != record:
                raise ValueError('conflicting raw history sources')
            expected[record_key] = record
            referenced.add(row['evidence_id'])
    if referenced != evidence_ids:
        raise ValueError('history bundle contains unreferenced raw evidence')
    for table, expected, key, daykey in (
        ('nwp_model_forecasts', expected_training, ('station_id', 'target_date', 'model', 'lead_days', 'source'), 'target_date'),
        ('cli_settlements', expected_truth, ('station_id', 'local_date'), 'local_date'),
    ):
        seen = set()
        for row in tables[table]:
            row_key = _key(row, key)
            if (row_key in seen or row_key not in expected or row != expected[row_key]
                    or row['station_id'] not in cities or not start <= date.fromisoformat(row[daykey]) <= end):
                raise ValueError('training/truth rows do not match raw attributed source/range')
            seen.add(row_key)
        if seen != set(expected):
            raise ValueError('history bundle has orphaned training/truth lineage')
    if not expected_training or not expected_truth:
        raise ValueError('history bundle requires both attributed training and final truth')
    return value


def import_bundle(conn, value):
    """Append validated data and provenance atomically, preserving all conflicts."""
    validate_bundle(value)
    conn.execute('BEGIN IMMEDIATE')
    try:
        # Retain the original immutable provenance tables, but do not initialize
        # any forecast, distribution, research-account or trading schema here.
        for table in TABLES:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
                if table in ('nwp_model_forecasts', 'cli_settlements'):
                    raise ValueError('canonical target weather tables must already exist')
        conn.execute('CREATE TABLE IF NOT EXISTS history_import_receipts ('
                     'bundle_sha256 TEXT PRIMARY KEY, imported_at TEXT NOT NULL, receipt_json TEXT NOT NULL)')
        for operation in ('UPDATE', 'DELETE'):
            conn.execute(f'CREATE TRIGGER IF NOT EXISTS history_import_receipts_reject_{operation.lower()} '
                         f"BEFORE {operation} ON history_import_receipts BEGIN SELECT RAISE(ABORT, 'history receipts are append-only'); END")
        conn.execute('CREATE TRIGGER IF NOT EXISTS history_import_receipts_reject_replacement '
                     'BEFORE INSERT ON history_import_receipts WHEN EXISTS(SELECT 1 FROM history_import_receipts '
                     "WHERE bundle_sha256=NEW.bundle_sha256) BEGIN SELECT RAISE(ABORT, 'history receipts are append-only'); END")
        # One complete receipt contains the immutable raw sources and derived
        # row lineage. It is separate from issued-weather forecast evidence.
        receipt = shadow.canonical(value)
        found = conn.execute('SELECT receipt_json FROM history_import_receipts WHERE bundle_sha256=?',
                             (value['content_sha256'],)).fetchone()
        if found and found[0] != receipt:
            raise ValueError('existing history import receipt conflicts')
        written = {}
        for table, columns, key in (
            ('nwp_model_forecasts', FORECAST_COLUMNS, FORECAST_COLUMNS[:4] + ('source',)),
            ('cli_settlements', TRUTH_COLUMNS, ('station_id', 'local_date')),
        ):
            written[table] = 0
            for row in value['tables'][table]:
                prior = rows(conn, f"SELECT {','.join(columns)} FROM {table} WHERE " +
                             ' AND '.join(f'{column}=?' for column in key), _key(row, key))
                if prior:
                    # Existing values retain their original provenance, even
                    # when this independently retrieved source agrees.
                    compare = ('predicted_high_f',) if table == 'nwp_model_forecasts' else ('max_temperature_f', 'is_final')
                    if any(prior[0][field] != row[field] for field in compare):
                        raise ValueError('canonical weather value conflicts with history bundle')
                    continue
                conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                             tuple(row[column] for column in columns))
                written[table] += 1
        if not found:
            conn.execute('INSERT INTO history_import_receipts VALUES (?,?,?)',
                         (value['content_sha256'], datetime.now(timezone.utc).isoformat(), receipt))
        conn.commit()
        return {'bundle_sha256': value['content_sha256'], 'new_rows': written,
                'matched_training_days': matched_training_days(value),
                'promotion_eligible': False, 'issued_forecast_rows_imported': 0}
    except Exception:
        conn.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('export', 'verify', 'import'))
    parser.add_argument('--db', type=Path)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--cities', default=DEFAULT_CITIES)
    parser.add_argument('--start', type=date.fromisoformat)
    parser.add_argument('--end', type=date.fromisoformat)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.mode == 'export':
        if args.db is None or args.start is None or args.end is None:
            parser.error('export requires database/start/end')
        with sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as conn:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            value = build_bundle(conn, shadow.parse_city_slugs(args.cities), args.start, args.end)
        shadow.write_json(args.bundle, value)
    else:
        if args.bundle.is_symlink() or args.bundle.stat().st_size > MAX_BYTES:
            parser.error('bundle must be a bounded regular file')
        value = json.loads(args.bundle.read_text())
        if args.expected_sha256 != value.get('content_sha256'):
            parser.error('exact expected content SHA256 is required')
        validate_bundle(value)
        if args.mode == 'import' and args.apply:
            marker = MAINTENANCE_MARKER.lstat()
            if not stat.S_ISREG(marker.st_mode) or marker.st_uid != 0 or stat.S_IMODE(marker.st_mode) != 0o600:
                parser.error('canonical root-owned deployment maintenance marker required')
            if args.db is None or args.db.is_symlink() or not args.db.is_file():
                parser.error('existing canonical weather database required')
            with args.db.with_suffix(args.db.suffix + '.history-import.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with sqlite3.connect(args.db, timeout=5) as conn:
                    print(json.dumps(import_bundle(conn, value), sort_keys=True))
                    return 0
    print(json.dumps({'bundle_sha256': value['content_sha256'], 'verified': True,
                      'rows': {table: len(items) for table, items in value['tables'].items()},
                      'matched_training_days': matched_training_days(value),
                      'promotion_eligible': False}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
