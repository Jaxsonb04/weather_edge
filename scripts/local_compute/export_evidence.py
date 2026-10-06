#!/usr/bin/env python3
"""Bounded, read-only AWS evidence export for the Mac research worker."""
import json
import sqlite3
import time
from datetime import datetime, timezone

LIMIT = 250000


def collect(paper_path, weather_path):
    result = {}
    for kind, path, tables in (
        ('paper', paper_path, [('paper_accounts', None), ('paper_orders', None)]),
        ('weather', weather_path, [('nwp_model_forecasts', 'target_date'),
                                  ('cli_settlements', 'local_date'),
                                  ('forecast_emos_daily_high', 'target_date')]),
    ):
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            # Establish the snapshot before recording its observation clock.
            conn.execute('SELECT count(*) FROM sqlite_master').fetchone()
            captured = datetime.now(timezone.utc).isoformat()
            group = {'captured_at': captured}
            deadline = time.monotonic() + 60
            conn.set_progress_handler(lambda: time.monotonic() > deadline, 10000)
            for table, column in tables:
                where = f' WHERE {column} >= ?' if column else ''
                args = ('2025-10-05',) if column else ()
                rows = [dict(row) for row in conn.execute(
                    f'SELECT * FROM {table}{where} LIMIT {LIMIT + 1}', args)]
                if len(rows) > LIMIT:
                    raise ValueError(f'{table} exceeded bounded export limit')
                group[table] = {'rows': rows, 'truncated': False}
            result[kind] = group
    return {
        'paper': {'captured_at': result['paper'].pop('captured_at'),
                  'paper': result['paper']},
        'weather': result['weather'],
    }


if __name__ == '__main__':
    print(json.dumps(collect('/opt/weatheredge/trading/data/paper_trading.db',
                             '/opt/weatheredge/forecaster/weather.db'), allow_nan=False))
