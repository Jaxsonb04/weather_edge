"""Append-only archival + feature layer for the paper-trading journal.

The retention prune (``PaperStore.prune_decision_snapshots``) keeps the 1 GB
box healthy by deleting redundant rejection ticks, but those ticks are the
training data this project compounds on: intra-day book/probability
evolution, per-tick rejection reasons, ladders and config snapshots inside
the normalized ``scan_context_snapshots`` rows, and legacy embedded diagnostic
payloads.  This module guarantees the prune can never destroy signal:

* Every complete UTC day of every snapshot table is exported losslessly to
  ``<archive_dir>/<table>/dt=YYYY-MM-DD.jsonl.gz`` (one JSON object per row,
  every column, JSON blobs kept as their exact stored strings).
* A manifest (``<archive_dir>/manifest.db``) records each verified export;
  a file is only recorded after it has been re-read from disk and its row
  count and payload sha256 re-checked.
* The prune gate (``--check-gate``) exits non-zero unless every complete UTC
  day from the oldest surviving row through yesterday is archived+verified.
  The systemd wrapper runs the gate between archive and prune, so a failed
  or missing archive blocks deletion instead of losing data.
* Small tables (orders, ledger, dataset_*) are snapshotted in full nightly.
* ``--upload`` pushes unuploaded files to S3 via the AWS CLI and records the
  verified remote copy; ``--cleanup`` deletes local files only when they are
  older than the ring-buffer window AND verifiably uploaded.
* The feature rollup distills each (target_date, market_ticker, side,
  risk_profile) into one ``market_side_day`` row — entry snapshot chosen with
  the same first-approved-else-first semantics as ``backtest_rescore`` —
  joined to realized settlement labels, so calibration/microstructure
  research stays one query away even after raw rows leave the box.

Everything here is stdlib-only and streams row batches, matching the trading
module's no-dependency rule and the box's 911 MB RAM budget.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .cities import city_for_market_ticker
from .settlement_truth import (
    load_cli_settlement_truth,
    settlement_key_for_market,
)

# Tables partitioned into per-UTC-day files by created_at (all have INTEGER
# PRIMARY KEY id + TEXT created_at written at insert time, so a completed UTC
# day can never gain rows afterwards).
STREAM_TABLES: tuple[str, ...] = (
    "decision_snapshots",
    "scan_context_snapshots",
    "probability_snapshots",
    "paper_monitor_snapshots",
    "market_snapshots",
    "forecast_snapshots",
)

# Small/label-spine tables copied in full every night for durability.
FULL_TABLES: tuple[str, ...] = (
    "paper_orders",
    "research_shadow_orders",
    "research_shadow_monitor_snapshots",
    "paper_accounts",
    "paper_account_ledger",
    "strategy_versions",
    "schema_migrations",
    "dataset_runs",
    "dataset_station_observations",
    "dataset_forecast_features",
    "dataset_kalshi_markets",
    "dataset_kalshi_candles",
    "dataset_kalshi_trades",
)

FETCH_BATCH = 2000
DEFAULT_KEEP_DAYS = 30
DEFAULT_FEATURE_WINDOW_DAYS = 9
# Ticks for target_date T are created while the market trades: from the
# day-ahead scan (lead ~2 days) through close on T (early hours of T+1 UTC).
FEATURE_CREATED_LOOKBACK_DAYS = 3
FEATURE_CREATED_LOOKAHEAD_DAYS = 1


def _utc_today() -> date:
    return datetime.now(UTC).date()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _day_range(first: date, last: date) -> list[str]:
    return [
        (first + timedelta(days=i)).isoformat()
        for i in range((last - first).days + 1)
    ]


@contextmanager
def _gzip_writer(path: Path):
    """Deterministic gzip writer (mtime=0 so byte-identical re-exports hash equal)."""
    raw = open(path, "wb")
    try:
        gz = gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0)
        try:
            yield gz
        finally:
            gz.close()
    finally:
        raw.close()


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    # uri=True so backup DBs can later be ATTACHed read-only via file: URIs.
    conn = sqlite3.connect(
        f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=30.0
    )
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA query_only = ON")
    return conn


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

MANIFEST_DDL = """
CREATE TABLE IF NOT EXISTS archive_files (
    table_name TEXT NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,               -- 'day' (UTC-day partition) | 'full' (whole-table snapshot)
    path TEXT NOT NULL,               -- relative to the archive dir
    rows INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,             -- of the uncompressed JSONL payload
    min_id INTEGER,
    max_id INTEGER,
    id_coverage_json TEXT,           -- exact inclusive id ranges for subset proof after prune
    context_ref_coverage_json TEXT, -- exact scan_context_id ranges referenced by decisions
    created_at TEXT NOT NULL,
    uploaded_at TEXT,
    upload_target TEXT,
    local_deleted_at TEXT,
    PRIMARY KEY (table_name, day, kind)
);
"""

MANIFEST_COLUMN_TYPES = {
    "table_name": "TEXT",
    "day": "TEXT",
    "kind": "TEXT",
    "path": "TEXT",
    "rows": "INTEGER",
    "bytes": "INTEGER",
    "sha256": "TEXT",
    "min_id": "INTEGER",
    "max_id": "INTEGER",
    "id_coverage_json": "TEXT",
    "context_ref_coverage_json": "TEXT",
    "created_at": "TEXT",
    "uploaded_at": "TEXT",
    "upload_target": "TEXT",
    "local_deleted_at": "TEXT",
}


def open_manifest(archive_dir: Path) -> sqlite3.Connection:
    archive_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(archive_dir / "manifest.db", timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(MANIFEST_DDL)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(archive_files)")}
    migrated = False
    for name, column_type in MANIFEST_COLUMN_TYPES.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE archive_files ADD COLUMN {name} {column_type}"
            )
            migrated = True
    if migrated:
        conn.commit()
    return conn


def _manifest_days(manifest: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[0]
        for row in manifest.execute(
            "SELECT day FROM archive_files WHERE table_name = ? AND kind = 'day'",
            (table,),
        )
    }


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


@dataclass
class ExportResult:
    table: str
    day: str
    path: Path
    rows: int
    bytes: int
    sha256: str
    min_id: int | None
    max_id: int | None
    id_ranges: list[tuple[int, int]]
    context_ref_ranges: list[tuple[int, int]]


def _ranges_for_ids(ids: set[int]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for row_id in sorted(ids):
        if ranges and row_id == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], row_id)
        else:
            ranges.append((row_id, row_id))
    return ranges


def _table_columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(
        f"PRAGMA {_quote_identifier(schema)}.table_info({_quote_identifier(table)})"
    ).fetchall()
    return [str(r[1]) for r in rows]


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _table_union_columns(
    conn: sqlite3.Connection,
    schemas: list[str],
    table: str,
) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for schema in schemas:
        for column in _table_columns(conn, schema, table):
            if column not in seen:
                seen.add(column)
                columns.append(column)
    return columns


def _json_default(value: object) -> object:
    if isinstance(value, bytes):  # defensive: no BLOB columns exist today
        import base64

        return {"__b64__": base64.b64encode(value).decode("ascii")}
    raise TypeError(f"unserializable value: {type(value)!r}")


def _day_select(
    conn: sqlite3.Connection,
    table: str,
    day: str,
    merge_schemas: list[str],
    columns: list[str],
    id_floor: int | None = None,
) -> sqlite3.Cursor:
    """Stream one UTC day of ``table`` across main + merge sources, id-sorted.

    Merge sources (attached backup DBs) only contribute rows whose id is
    absent from every earlier source, so re-exporting after a prune from a
    backup that still holds the deleted rows yields their union exactly once.
    Sources may predate schema migrations; missing columns come back NULL.

    ``id_floor`` (nightly incremental path only): ids are assigned at insert
    time and created_at is always "now", so id order matches day order; rows
    of a new day always have ids above every previously archived max_id.
    Seeding the scan there turns the unindexed created_at filter into a
    cheap rowid-range scan instead of a full-table scan of the ever-growing
    snapshot tables.
    """

    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    parts: list[str] = []
    params: list[object] = []
    schemas = ["main", *merge_schemas]
    for idx, schema in enumerate(schemas):
        have = set(_table_columns(conn, schema, table))
        if not have:
            continue
        source = f"{_quote_identifier(schema)}.{_quote_identifier(table)}"
        select_cols = ", ".join(
            (
                f"{source}.{_quote_identifier(c)}"
                if c in have
                else f"NULL AS {_quote_identifier(c)}"
            )
            for c in columns
        )
        where = [f'{source}."created_at" >= ?', f'{source}."created_at" < ?']
        params.extend([day, next_day])
        if id_floor is not None and schema == "main" and not merge_schemas:
            where.append(f'{source}."id" > ?')
            params.append(id_floor)
        for prev in schemas[:idx]:
            if not _table_columns(conn, prev, table):
                continue
            previous = f"{_quote_identifier(prev)}.{_quote_identifier(table)}"
            where.append(
                f'{source}."id" NOT IN ('
                f'SELECT "id" FROM {previous} '
                f'WHERE "created_at" >= ? AND "created_at" < ?)'
            )
            params.extend([day, next_day])
        parts.append(
            f"SELECT {select_cols} FROM {source} WHERE {' AND '.join(where)}"
        )
    sql = " UNION ALL ".join(parts) + ' ORDER BY "id"'
    return conn.execute(sql, params)


def export_day(
    conn: sqlite3.Connection,
    table: str,
    day: str,
    archive_dir: Path,
    merge_schemas: list[str] | None = None,
    id_floor: int | None = None,
) -> ExportResult:
    """Write one complete UTC day of ``table`` to JSONL.gz, atomically."""

    columns = _table_union_columns(conn, ["main", *(merge_schemas or [])], table)
    out_dir = archive_dir / table
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / f"dt={day}.jsonl.gz"
    tmp_path = out_dir / f"dt={day}.jsonl.gz.tmp"

    digest = hashlib.sha256()
    rows = 0
    min_id: int | None = None
    max_id: int | None = None
    id_ranges: list[tuple[int, int]] = []
    context_refs: set[int] = set()
    cursor = _day_select(conn, table, day, merge_schemas or [], columns, id_floor)
    with _gzip_writer(tmp_path) as out:
        while True:
            batch = cursor.fetchmany(FETCH_BATCH)
            if not batch:
                break
            for row in batch:
                record = dict(zip(columns, row))
                line = json.dumps(
                    record, ensure_ascii=False, separators=(",", ":"),
                    default=_json_default,
                ).encode("utf-8") + b"\n"
                digest.update(line)
                out.write(line)
                rows += 1
                row_id = record.get("id")
                if isinstance(row_id, int):
                    min_id = row_id if min_id is None else min(min_id, row_id)
                    max_id = row_id if max_id is None else max(max_id, row_id)
                    if id_ranges and row_id == id_ranges[-1][1] + 1:
                        id_ranges[-1] = (id_ranges[-1][0], row_id)
                    else:
                        id_ranges.append((row_id, row_id))
                context_ref = record.get("scan_context_id")
                if table == "decision_snapshots" and isinstance(context_ref, int):
                    context_refs.add(context_ref)

    # Integrity check: re-read the finished file from disk and require the
    # same row count and payload hash before the manifest may record it.
    check = hashlib.sha256()
    check_rows = 0
    with gzip.open(tmp_path, "rb") as inp:
        for line in inp:
            check.update(line)
            check_rows += 1
    if check_rows != rows or check.hexdigest() != digest.hexdigest():
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"archive verify failed for {table} {day}: "
            f"wrote {rows} rows, re-read {check_rows}"
        )
    os.replace(tmp_path, final_path)
    return ExportResult(
        table=table,
        day=day,
        path=final_path,
        rows=rows,
        bytes=final_path.stat().st_size,
        sha256=digest.hexdigest(),
        min_id=min_id,
        max_id=max_id,
        id_ranges=id_ranges,
        context_ref_ranges=_ranges_for_ids(context_refs),
    )


def export_full_table(
    conn: sqlite3.Connection, table: str, day: str, archive_dir: Path
) -> ExportResult:
    """Whole-table snapshot (small label-spine tables), same file format."""

    columns = _table_columns(conn, "main", table)
    out_dir = archive_dir / table
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / f"full-{day}.jsonl.gz"
    tmp_path = out_dir / f"full-{day}.jsonl.gz.tmp"
    digest = hashlib.sha256()
    rows = 0
    cursor = conn.execute(f"SELECT * FROM {table}")
    with _gzip_writer(tmp_path) as out:
        while True:
            batch = cursor.fetchmany(FETCH_BATCH)
            if not batch:
                break
            for row in batch:
                line = json.dumps(
                    dict(zip(columns, row)), ensure_ascii=False,
                    separators=(",", ":"), default=_json_default,
                ).encode("utf-8") + b"\n"
                digest.update(line)
                out.write(line)
                rows += 1
    os.replace(tmp_path, final_path)
    return ExportResult(
        table=table, day=day, path=final_path, rows=rows,
        bytes=final_path.stat().st_size, sha256=digest.hexdigest(),
        min_id=None, max_id=None,
        id_ranges=[],
        context_ref_ranges=[],
    )


def _record(manifest: sqlite3.Connection, result: ExportResult, archive_dir: Path, kind: str) -> None:
    manifest.execute(
        "INSERT OR REPLACE INTO archive_files "
        "(table_name, day, kind, path, rows, bytes, sha256, min_id, max_id, "
        "id_coverage_json, context_ref_coverage_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            result.table, result.day, kind,
            str(result.path.relative_to(archive_dir)),
            result.rows, result.bytes, result.sha256,
            result.min_id, result.max_id,
            json.dumps(result.id_ranges, separators=(",", ":")) if kind == "day" else None,
            (
                json.dumps(result.context_ref_ranges, separators=(",", ":"))
                if kind == "day" and result.table == "decision_snapshots"
                else None
            ),
            _now_iso(),
        ),
    )
    manifest.commit()


def _first_day(conn: sqlite3.Connection, table: str, merge_schemas: list[str]) -> str | None:
    days: list[str] = []
    for schema in ["main", *merge_schemas]:
        if not _table_columns(conn, schema, table):
            continue
        row = conn.execute(
            f"SELECT MIN(created_at) FROM {schema}.{table}"
        ).fetchone()
        if row and row[0]:
            days.append(str(row[0])[:10])
    return min(days) if days else None


def _source_day_count(conn: sqlite3.Connection, table: str, day: str) -> int:
    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    row = conn.execute(
        f"SELECT COUNT(*) FROM main.{table} WHERE created_at >= ? AND created_at < ?",
        (day, next_day),
    ).fetchone()
    return int(row[0] or 0)


def _decode_id_ranges(
    raw: object,
    table: str,
    day: str,
    *,
    archived_rows: int | None = None,
    min_id: int | None = None,
    max_id: int | None = None,
) -> list[tuple[int, int]]:
    if raw is None:
        raise RuntimeError(
            f"archive manifest lacks exact ID coverage for {table} {day}; "
            "re-export the UTC-day partition before running --check-gate"
        )
    try:
        payload = json.loads(str(raw))
        ranges = [(int(start), int(end)) for start, end in payload]
    except (TypeError, ValueError, json.JSONDecodeError):
        raise RuntimeError(
            f"archive manifest has invalid ID coverage for {table} {day}; "
            "re-export the UTC-day partition before running --check-gate"
        )
    previous_end: int | None = None
    for start, end in ranges:
        if start > end or (previous_end is not None and start <= previous_end):
            raise RuntimeError(
                f"archive manifest has invalid ID coverage for {table} {day}; "
                "re-export the UTC-day partition before running --check-gate"
            )
        previous_end = end
    if archived_rows is not None:
        covered_rows = sum(end - start + 1 for start, end in ranges)
        endpoints_match = (
            (archived_rows == 0 and not ranges and min_id is None and max_id is None)
            or (
                archived_rows > 0
                and bool(ranges)
                and ranges[0][0] == min_id
                and ranges[-1][1] == max_id
            )
        )
        if covered_rows != archived_rows or not endpoints_match:
            raise RuntimeError(
                f"archive manifest has invalid ID coverage for {table} {day}; "
                "re-export the UTC-day partition before running --check-gate"
            )
    return ranges


def _ranges_from_verified_archive(
    path: Path,
    *,
    table: str,
    day: str,
    expected_rows: int,
    expected_sha256: str,
) -> list[tuple[int, int]]:
    digest = hashlib.sha256()
    ids: list[int] = []
    try:
        with gzip.open(path, "rb") as inp:
            for line in inp:
                digest.update(line)
                payload = json.loads(line)
                if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
                    raise ValueError("archive row has no integer id")
                ids.append(int(payload["id"]))
    except (OSError, EOFError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"archive coverage recovery found corrupt {table} {day}: {exc}; "
            "restore the original verified partition and rerun paper-archive"
        ) from exc
    if len(ids) != expected_rows or digest.hexdigest() != expected_sha256:
        raise RuntimeError(
            f"archive coverage recovery mismatch for {table} {day}: "
            f"file rows/hash do not match manifest; restore the original verified "
            "partition and rerun paper-archive"
        )
    ranges: list[tuple[int, int]] = []
    for row_id in ids:
        if ranges and row_id == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], row_id)
        elif ranges and row_id <= ranges[-1][1]:
            raise RuntimeError(
                f"archive coverage recovery found unordered/duplicate IDs for {table} {day}; "
                "restore the original verified partition and rerun paper-archive"
            )
        else:
            ranges.append((row_id, row_id))
    return ranges


def _context_ref_ranges_from_verified_archive(
    path: Path,
    *,
    day: str,
    expected_rows: int,
    expected_sha256: str,
) -> list[tuple[int, int]]:
    digest = hashlib.sha256()
    rows = 0
    refs: set[int] = set()
    try:
        with gzip.open(path, "rb") as inp:
            for line in inp:
                digest.update(line)
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("archive row is not an object")
                context_ref = payload.get("scan_context_id")
                if context_ref is not None:
                    if not isinstance(context_ref, int):
                        raise ValueError("scan_context_id is not an integer")
                    refs.add(context_ref)
                rows += 1
    except (OSError, EOFError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"archive context-reference recovery found corrupt decision_snapshots "
            f"{day}: {exc}; restore the original verified partition"
        ) from exc
    if rows != expected_rows or digest.hexdigest() != expected_sha256:
        raise RuntimeError(
            "archive context-reference recovery mismatch for decision_snapshots "
            f"{day}; file rows/hash do not match manifest"
        )
    return _ranges_for_ids(refs)


def backfill_manifest_context_ref_coverage(
    manifest: sqlite3.Connection,
    archive_dir: Path,
    *,
    log=print,
) -> int:
    """Recover decision->context reference ranges from verified archive bytes."""

    pending = manifest.execute(
        "SELECT day, path, rows, sha256, upload_target FROM archive_files "
        "WHERE table_name='decision_snapshots' AND kind='day' "
        "AND context_ref_coverage_json IS NULL ORDER BY day"
    ).fetchall()
    updated = 0
    for day, rel_path, rows, sha256, upload_target in pending:
        if rel_path is None or rows is None or not sha256:
            raise RuntimeError(
                f"decision archive {day} lacks metadata required to recover "
                "scan-context references; restore or re-export it"
            )
        path = archive_dir / str(rel_path)
        temporary: Path | None = None
        source = path
        try:
            if not path.exists():
                if not upload_target:
                    raise RuntimeError(
                        f"decision archive {day} lacks scan-context reference metadata and "
                        f"its verified file is unavailable at {path}; restore it before the gate"
                    )
                with tempfile.NamedTemporaryFile(
                    prefix="weatheredge-context-ref-",
                    suffix=".jsonl.gz",
                    delete=False,
                ) as tmp:
                    temporary = Path(tmp.name)
                try:
                    cp = _aws(
                        [
                            "s3",
                            "cp",
                            str(upload_target),
                            str(temporary),
                            "--only-show-errors",
                        ]
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    raise RuntimeError(
                        "archive context-reference recovery could not fetch verified "
                        f"upload for decision_snapshots {day}: {exc}"
                    ) from exc
                if cp.returncode != 0:
                    raise RuntimeError(
                        "archive context-reference recovery could not fetch verified "
                        f"upload for decision_snapshots {day}: {cp.stderr.strip()}"
                    )
                source = temporary
            ranges = _context_ref_ranges_from_verified_archive(
                source,
                day=str(day),
                expected_rows=int(rows),
                expected_sha256=str(sha256),
            )
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        manifest.execute(
            "UPDATE archive_files SET context_ref_coverage_json=? "
            "WHERE table_name='decision_snapshots' AND day=? AND kind='day'",
            (json.dumps(ranges, separators=(",", ":")), day),
        )
        manifest.commit()
        updated += 1
        log(f"backfilled archive context-reference coverage for dt={day}")
    return updated


def backfill_manifest_id_coverage(
    manifest: sqlite3.Connection,
    archive_dir: Path,
    *,
    log=print,
) -> int:
    """Derive missing ID ranges only from the original verified archive bytes."""

    pending = manifest.execute(
        "SELECT table_name, day, path, rows, sha256, upload_target, min_id, max_id "
        "FROM archive_files WHERE kind='day' AND id_coverage_json IS NULL "
        "ORDER BY table_name, day"
    ).fetchall()
    updated = 0
    for table, day, rel_path, rows, sha256, upload_target, min_id, max_id in pending:
        if rel_path is None or rows is None or not sha256:
            raise RuntimeError(
                f"legacy archive manifest for {table} {day} lacks the verified path, "
                "row count, or SHA-256 required to recover ID coverage; restore the "
                "original partition metadata or re-export that UTC-day partition"
            )
        local = archive_dir / str(rel_path)
        temporary: Path | None = None
        source = local
        if not local.exists():
            if not upload_target:
                raise RuntimeError(
                    f"archive coverage metadata is missing for {table} {day}, and the "
                    f"original file is unavailable. Restore it once to {local} (or set "
                    "its verified upload_target), then rerun paper-archive"
                )
            with tempfile.NamedTemporaryFile(
                prefix="weatheredge-archive-coverage-", suffix=".jsonl.gz", delete=False
            ) as tmp:
                temporary = Path(tmp.name)
            try:
                cp = _aws(
                    ["s3", "cp", str(upload_target), str(temporary), "--only-show-errors"]
                )
            except (OSError, subprocess.SubprocessError) as exc:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(
                    f"archive coverage recovery could not fetch verified upload for "
                    f"{table} {day}: {exc}; restore {local} once and rerun paper-archive"
                ) from exc
            if cp.returncode != 0:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(
                    f"archive coverage recovery could not fetch verified upload for "
                    f"{table} {day}: {cp.stderr.strip()}; restore {local} once and rerun "
                    "paper-archive"
                )
            source = temporary
        try:
            ranges = _ranges_from_verified_archive(
                source,
                table=str(table),
                day=str(day),
                expected_rows=int(rows),
                expected_sha256=str(sha256),
            )
            coverage_json = json.dumps(ranges, separators=(",", ":"))
            _decode_id_ranges(
                coverage_json,
                str(table),
                str(day),
                archived_rows=int(rows),
                min_id=min_id,
                max_id=max_id,
            )
            manifest.execute(
                "UPDATE archive_files SET id_coverage_json=? "
                "WHERE table_name=? AND day=? AND kind='day' "
                "AND id_coverage_json IS NULL",
                (coverage_json, table, day),
            )
            manifest.commit()
            updated += 1
            log(f"backfilled archive ID coverage for {table} dt={day}")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return updated


def _surviving_ids_are_covered(
    conn: sqlite3.Connection,
    table: str,
    day: str,
    ranges: list[tuple[int, int]],
) -> bool:
    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    cursor = conn.execute(
        f"SELECT id FROM main.{table} WHERE created_at >= ? AND created_at < ? ORDER BY id",
        (day, next_day),
    )
    range_idx = 0
    for (row_id,) in cursor:
        value = int(row_id)
        while range_idx < len(ranges) and ranges[range_idx][1] < value:
            range_idx += 1
        if range_idx >= len(ranges):
            return False
        start, end = ranges[range_idx]
        if value < start or value > end:
            return False
    return True


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _ranges_are_covered(
    required: list[tuple[int, int]],
    available: list[tuple[int, int]],
) -> bool:
    available = _merge_ranges(available)
    idx = 0
    for start, end in _merge_ranges(required):
        while idx < len(available) and available[idx][1] < start:
            idx += 1
        if idx >= len(available):
            return False
        parent_start, parent_end = available[idx]
        if start < parent_start or end > parent_end:
            return False
    return True


def archive_pending(
    db_path: Path,
    archive_dir: Path,
    merge_dbs: list[Path] | None = None,
    include_full: bool = True,
    log=print,
) -> int:
    """Export every unarchived complete UTC day; returns count of new files."""

    manifest = open_manifest(archive_dir)
    try:
        backfill_manifest_id_coverage(manifest, archive_dir, log=log)
        backfill_manifest_context_ref_coverage(manifest, archive_dir, log=log)
    except Exception:
        manifest.close()
        raise
    conn = connect_readonly(db_path)
    merge_schemas: list[str] = []
    for idx, merge_path in enumerate(merge_dbs or []):
        schema = f"merge{idx}"
        conn.execute(
            "ATTACH DATABASE ? AS " + schema,
            (f"{Path(merge_path).resolve().as_uri()}?mode=ro",),
        )
        merge_schemas.append(schema)
    yesterday = _utc_today() - timedelta(days=1)
    exported = 0
    try:
        for table in STREAM_TABLES:
            first = _first_day(conn, table, merge_schemas)
            if first is None:
                continue
            done = _manifest_days(manifest, table)
            floor: int | None = None
            for day in _day_range(date.fromisoformat(first), yesterday):
                if day in done:
                    continue
                if not merge_schemas:
                    row = manifest.execute(
                        "SELECT MAX(max_id) FROM archive_files "
                        "WHERE table_name = ? AND kind = 'day' AND day < ?",
                        (table, day),
                    ).fetchone()
                    prior = row[0] if row else None
                    if prior is not None:
                        floor = prior if floor is None else max(floor, prior)
                result = export_day(
                    conn, table, day, archive_dir, merge_schemas, id_floor=floor
                )
                if not merge_schemas:
                    source_rows = _source_day_count(conn, table, day)
                    if result.rows != source_rows and floor is not None:
                        # A backdated/high-id insert disproves the monotone-id
                        # optimization. Re-run this partition without the floor
                        # before the incomplete file can enter the manifest.
                        result = export_day(
                            conn, table, day, archive_dir, merge_schemas, id_floor=None
                        )
                    if result.rows != source_rows:
                        raise RuntimeError(
                            f"archive coverage mismatch for {table} {day}: "
                            f"source has {source_rows} rows, export has {result.rows}; "
                            "refusing to record an incomplete archive"
                        )
                if result.max_id is not None:
                    floor = result.max_id if floor is None else max(floor, result.max_id)
                _record(manifest, result, archive_dir, "day")
                exported += 1
                log(
                    f"archived {table} dt={day}: {result.rows} rows, "
                    f"{result.bytes} bytes, sha256={result.sha256[:12]}…"
                )
        if include_full:
            today = _utc_today().isoformat()
            for table in FULL_TABLES:
                if not _table_columns(conn, "main", table):
                    continue
                already = manifest.execute(
                    "SELECT 1 FROM archive_files WHERE table_name=? AND day=? AND kind='full'",
                    (table, today),
                ).fetchone()
                if already:
                    continue
                result = export_full_table(conn, table, today, archive_dir)
                _record(manifest, result, archive_dir, "full")
                exported += 1
                log(f"snapshotted {table} ({result.rows} rows)")
    finally:
        conn.close()
        manifest.close()
    return exported


# --------------------------------------------------------------------------
# Prune gate
# --------------------------------------------------------------------------


def gate_missing_days(db_path: Path, archive_dir: Path) -> list[tuple[str, str]]:
    """Every (table, day) that MUST be archived before pruning is allowed.

    The requirement spans from the oldest surviving row (approved rows live
    forever, so this reaches back to journal genesis) through yesterday.
    """

    manifest = open_manifest(archive_dir)
    try:
        backfill_manifest_context_ref_coverage(
            manifest, archive_dir, log=lambda *_: None
        )
    except Exception:
        manifest.close()
        raise
    conn = connect_readonly(db_path)
    yesterday = _utc_today() - timedelta(days=1)
    missing: list[tuple[str, str]] = []
    try:
        manifest_columns = {
            str(row[1]) for row in manifest.execute("PRAGMA table_info(archive_files)")
        }
        if "rows" not in manifest_columns or "id_coverage_json" not in manifest_columns:
            raise RuntimeError(
                "archive manifest lacks verified row counts; re-export complete UTC-day "
                "partitions before running --check-gate"
            )
        for table in STREAM_TABLES:
            first = _first_day(conn, table, [])
            if first is None:
                continue
            legacy_day = manifest.execute(
                "SELECT day FROM archive_files WHERE table_name=? AND kind='day' "
                "AND (path IS NULL OR rows IS NULL OR sha256 IS NULL OR sha256='') "
                "LIMIT 1",
                (table,),
            ).fetchone()
            if legacy_day is not None:
                raise RuntimeError(
                    f"legacy archive manifest for {table} {legacy_day[0]} lacks the "
                    "verified path, row count, or SHA-256; restore its original "
                    "metadata or re-export that UTC-day partition before --check-gate"
                )
            coverage = {
                str(day): (int(rows), id_coverage_json, min_id, max_id)
                for day, rows, id_coverage_json, min_id, max_id in manifest.execute(
                    "SELECT day, rows, id_coverage_json, min_id, max_id FROM archive_files "
                    "WHERE table_name = ? AND kind = 'day'",
                    (table,),
                )
                if rows is not None
            }
            for day in _day_range(date.fromisoformat(first), yesterday):
                source_rows = _source_day_count(conn, table, day)
                if day not in coverage:
                    missing.append((table, day))
                    continue
                archived_rows, raw_ranges, min_id, max_id = coverage[day]
                ranges = _decode_id_ranges(
                    raw_ranges,
                    table,
                    day,
                    archived_rows=archived_rows,
                    min_id=min_id,
                    max_id=max_id,
                )
                if (
                    archived_rows < source_rows
                    or not _surviving_ids_are_covered(conn, table, day, ranges)
                ):
                    missing.append((table, day))

        archived_context_ranges: list[tuple[int, int]] = []
        for day, rows, raw_ranges, min_id, max_id in manifest.execute(
            "SELECT day, rows, id_coverage_json, min_id, max_id FROM archive_files "
            "WHERE table_name='scan_context_snapshots' AND kind='day'"
        ):
            archived_context_ranges.extend(
                _decode_id_ranges(
                    raw_ranges,
                    "scan_context_snapshots",
                    str(day),
                    archived_rows=int(rows),
                    min_id=min_id,
                    max_id=max_id,
                )
            )

        for day, raw_refs in manifest.execute(
            "SELECT day, context_ref_coverage_json FROM archive_files "
            "WHERE table_name='decision_snapshots' AND kind='day'"
        ):
            refs = _decode_id_ranges(
                raw_refs,
                "decision_snapshots.scan_context_id",
                str(day),
            )
            if refs and not _ranges_are_covered(refs, archived_context_ranges):
                missing.append(("scan_context_snapshots", str(day)))

        decision_columns = set(
            _table_columns(conn, "main", "decision_snapshots")
        )
        if {"scan_context_id", "created_at"} <= decision_columns:
            for context_id, created_day in conn.execute(
                "SELECT DISTINCT scan_context_id, substr(created_at, 1, 10) "
                "FROM decision_snapshots WHERE scan_context_id IS NOT NULL "
                "AND created_at < ?",
                (_utc_today().isoformat(),),
            ):
                refs = [(int(context_id), int(context_id))]
                if not _ranges_are_covered(refs, archived_context_ranges):
                    missing.append(("scan_context_snapshots", str(created_day)))
    finally:
        conn.close()
        manifest.close()
    return list(dict.fromkeys(missing))


RESTORE_TABLE_ORDER: tuple[str, ...] = (
    "forecast_snapshots",
    "market_snapshots",
    "scan_context_snapshots",
    "decision_snapshots",
    "probability_snapshots",
    "paper_monitor_snapshots",
)


def _archived_context_link_missing_days(manifest: sqlite3.Connection) -> list[str]:
    context_ranges: list[tuple[int, int]] = []
    for day, rows, raw_ranges, min_id, max_id in manifest.execute(
        "SELECT day, rows, id_coverage_json, min_id, max_id FROM archive_files "
        "WHERE table_name='scan_context_snapshots' AND kind='day'"
    ):
        context_ranges.extend(
            _decode_id_ranges(
                raw_ranges,
                "scan_context_snapshots",
                str(day),
                archived_rows=int(rows),
                min_id=min_id,
                max_id=max_id,
            )
        )
    missing: list[str] = []
    for day, raw_refs in manifest.execute(
        "SELECT day, context_ref_coverage_json FROM archive_files "
        "WHERE table_name='decision_snapshots' AND kind='day'"
    ):
        refs = _decode_id_ranges(
            raw_refs,
            "decision_snapshots.scan_context_id",
            str(day),
        )
        if refs and not _ranges_are_covered(refs, context_ranges):
            missing.append(str(day))
    return missing


def _restore_record(
    conn: sqlite3.Connection,
    table: str,
    record: dict[str, object],
    destination_columns: set[str],
) -> bool:
    """Insert one archive row, accepting an existing row only when identical."""

    columns = [name for name in record if name in destination_columns]
    if not columns:
        raise RuntimeError(f"archive restore found no compatible columns for {table}")
    marks = ", ".join("?" for _ in columns)
    values = tuple(record[column] for column in columns)
    try:
        conn.execute(
            f"INSERT INTO {_quote_identifier(table)} "
            f"({', '.join(_quote_identifier(c) for c in columns)}) "
            f"VALUES ({marks})",
            values,
        )
        return True
    except sqlite3.IntegrityError as exc:
        table_info = conn.execute(
            f"PRAGMA main.table_info({_quote_identifier(table)})"
        ).fetchall()
        primary_key = [
            str(row[1])
            for row in sorted(table_info, key=lambda row: int(row[5] or 0))
            if int(row[5] or 0) > 0
        ]
        if not primary_key or any(column not in record for column in primary_key):
            raise RuntimeError(
                f"archive restore constraint failure for {table}: {exc}"
            ) from exc
        where = " AND ".join(
            f"{_quote_identifier(column)} IS ?" for column in primary_key
        )
        existing = conn.execute(
            f"SELECT {', '.join(_quote_identifier(c) for c in columns)} "
            f"FROM {_quote_identifier(table)} WHERE {where}",
            tuple(record[column] for column in primary_key),
        ).fetchone()
        identity = ", ".join(
            f"{column}={record[column]!r}" for column in primary_key
        )
        if existing is None:
            raise RuntimeError(
                f"archive restore constraint failure for {table} ({identity}): {exc}"
            ) from exc
        differing = [
            column
            for column, actual, expected in zip(columns, existing, values)
            if actual != expected
        ]
        if differing:
            raise RuntimeError(
                f"archive restore conflict in {table} ({identity}); differing fields: "
                + ", ".join(differing)
            ) from exc
        return False


def _iter_verified_restore_partition(
    archive_dir: Path,
    table: str,
    file_info: tuple,
) -> Iterator[dict[str, object]]:
    day, rel_path, expected_rows, expected_sha256, _, _, _ = file_info
    path = archive_dir / str(rel_path)
    if not path.exists():
        raise RuntimeError(f"archive restore requires verified local file {path}")
    digest = hashlib.sha256()
    rows = 0
    try:
        with gzip.open(path, "rb") as inp:
            for line in inp:
                digest.update(line)
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("archive row is not an object")
                rows += 1
                yield record
    except (OSError, EOFError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"archive restore found corrupt {table} partition {day}: {exc}"
        ) from exc
    if rows != int(expected_rows) or digest.hexdigest() != str(expected_sha256):
        raise RuntimeError(f"archive restore verification failed for {table} {day}")


def _restore_partition_intersects_ids(
    table: str,
    required_ids: set[int],
    file_info: tuple,
) -> bool:
    if not required_ids:
        return False
    day, _, rows, _, raw_ranges, min_id, max_id = file_info
    ranges = _decode_id_ranges(
        raw_ranges,
        table,
        str(day),
        archived_rows=int(rows),
        min_id=min_id,
        max_id=max_id,
    )
    return any(
        start <= parent_id <= end
        for parent_id in required_ids
        for start, end in ranges
    )


def _verify_restore_parent_ids(
    archive_dir: Path,
    table: str,
    required_ids: set[int],
    files: list[tuple],
) -> None:
    if not required_ids:
        return
    found: set[int] = set()
    for file_info in files:
        if not _restore_partition_intersects_ids(table, required_ids, file_info):
            continue
        for record in _iter_verified_restore_partition(
            archive_dir, table, file_info
        ):
            row_id = record.get("id")
            if isinstance(row_id, int) and row_id in required_ids:
                found.add(row_id)
    missing = sorted(required_ids - found)
    if missing:
        raise RuntimeError(
            f"missing archived {table} parent id(s): "
            + ", ".join(str(value) for value in missing)
        )


def restore_archive_days(
    archive_dir: Path,
    db_path: Path,
    *,
    days: list[str] | None = None,
    log=print,
) -> dict[str, int]:
    """Restore verified day archives with FK parents inserted before children."""

    from .db import PaperStore

    PaperStore(db_path)

    manifest = open_manifest(archive_dir)
    try:
        backfill_manifest_id_coverage(manifest, archive_dir, log=log)
        backfill_manifest_context_ref_coverage(manifest, archive_dir, log=log)
        missing = _archived_context_link_missing_days(manifest)
        if missing:
            raise RuntimeError(
                "archive restore refused: decision scan-context parents are missing "
                f"for day(s) {', '.join(missing)}"
            )
        selected = set(days or [])
        files = {
            table: manifest.execute(
                "SELECT day, path, rows, sha256, id_coverage_json, min_id, max_id "
                "FROM archive_files "
                "WHERE table_name=? AND kind='day' ORDER BY day",
                (table,),
            ).fetchall()
            for table in RESTORE_TABLE_ORDER
        }
    finally:
        manifest.close()

    required_ids: dict[str, set[int]] = {
        "forecast_snapshots": set(),
        "market_snapshots": set(),
        "scan_context_snapshots": set(),
    }
    if selected:
        for file_info in files["decision_snapshots"]:
            if str(file_info[0]) not in selected:
                continue
            for record in _iter_verified_restore_partition(
                archive_dir, "decision_snapshots", file_info
            ):
                context_id = record.get("scan_context_id")
                if isinstance(context_id, int):
                    required_ids["scan_context_snapshots"].add(context_id)

        found_context_ids: set[int] = set()
        for file_info in files["scan_context_snapshots"]:
            selected_partition = str(file_info[0]) in selected
            if not selected_partition and not _restore_partition_intersects_ids(
                "scan_context_snapshots",
                required_ids["scan_context_snapshots"],
                file_info,
            ):
                continue
            for record in _iter_verified_restore_partition(
                archive_dir, "scan_context_snapshots", file_info
            ):
                row_id = record.get("id")
                if not selected_partition and row_id not in required_ids[
                    "scan_context_snapshots"
                ]:
                    continue
                if isinstance(row_id, int):
                    found_context_ids.add(row_id)
                forecast_id = record.get("forecast_snapshot_id")
                market_id = record.get("market_snapshot_id")
                if isinstance(forecast_id, int):
                    required_ids["forecast_snapshots"].add(forecast_id)
                if isinstance(market_id, int):
                    required_ids["market_snapshots"].add(market_id)
        missing_contexts = sorted(
            required_ids["scan_context_snapshots"] - found_context_ids
        )
        if missing_contexts:
            raise RuntimeError(
                "missing archived scan_context_snapshots parent id(s): "
                + ", ".join(str(value) for value in missing_contexts)
            )
        _verify_restore_parent_ids(
            archive_dir,
            "forecast_snapshots",
            required_ids["forecast_snapshots"],
            files["forecast_snapshots"],
        )
        _verify_restore_parent_ids(
            archive_dir,
            "market_snapshots",
            required_ids["market_snapshots"],
            files["market_snapshots"],
        )

    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.execute("PRAGMA foreign_keys=ON")
    restored = {table: 0 for table in RESTORE_TABLE_ORDER}
    try:
        with conn:
            for table in RESTORE_TABLE_ORDER:
                destination_columns = set(_table_columns(conn, "main", table))
                if not destination_columns:
                    continue
                table_required_ids = required_ids.get(table, set())
                for file_info in files[table]:
                    selected_partition = not selected or str(file_info[0]) in selected
                    if not selected_partition and not _restore_partition_intersects_ids(
                        table, table_required_ids, file_info
                    ):
                        continue
                    for record in _iter_verified_restore_partition(
                        archive_dir, table, file_info
                    ):
                        if (
                            not selected_partition
                            and record.get("id") not in table_required_ids
                        ):
                            continue
                        inserted = _restore_record(
                            conn,
                            table,
                            record,
                            destination_columns,
                        )
                        restored[table] += 1 if inserted else 0
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    "archive restore foreign_key_check failed; transaction rolled back: "
                    + "; ".join(str(row) for row in violations[:20])
                )
    finally:
        conn.close()
    return restored


# --------------------------------------------------------------------------
# Upload + ring-buffer cleanup
# --------------------------------------------------------------------------


def _aws(args: list[str]) -> subprocess.CompletedProcess:
    cli = os.getenv("SFO_ARCHIVE_AWS_CLI", "aws")
    return subprocess.run(
        [cli, *args], capture_output=True, text=True, timeout=600
    )


def upload_pending(archive_dir: Path, log=print) -> int:
    """Upload unuploaded manifest files to S3; verify size via head-object.

    No-op (exit clean, loud notice) until SFO_ARCHIVE_S3_BUCKET is set, so
    the pipeline degrades to the local ring buffer instead of failing.
    """

    bucket = os.getenv("SFO_ARCHIVE_S3_BUCKET", "").strip()
    if not bucket:
        log("upload skipped: SFO_ARCHIVE_S3_BUCKET is not set (local ring buffer only)")
        return 0
    prefix = os.getenv("SFO_ARCHIVE_S3_PREFIX", "paper_trading").strip("/")
    manifest = open_manifest(archive_dir)
    uploaded = 0
    try:
        pending = manifest.execute(
            "SELECT table_name, day, kind, path, bytes FROM archive_files "
            "WHERE uploaded_at IS NULL AND local_deleted_at IS NULL ORDER BY day"
        ).fetchall()
        for table, day, kind, rel_path, size in pending:
            local = archive_dir / rel_path
            if not local.exists():
                log(f"WARN: manifest file missing locally, cannot upload: {rel_path}")
                continue
            key = f"{prefix}/{rel_path}"
            target = f"s3://{bucket}/{key}"
            cp = _aws(["s3", "cp", str(local), target, "--only-show-errors"])
            if cp.returncode != 0:
                log(f"WARN: upload failed for {rel_path}: {cp.stderr.strip()}")
                continue
            head = _aws(["s3api", "head-object", "--bucket", bucket, "--key", key])
            remote_size = None
            if head.returncode == 0:
                try:
                    remote_size = json.loads(head.stdout).get("ContentLength")
                except json.JSONDecodeError:
                    remote_size = None
            if remote_size != size:
                log(
                    f"WARN: upload verify failed for {rel_path}: "
                    f"remote size {remote_size} != local {size}"
                )
                continue
            manifest.execute(
                "UPDATE archive_files SET uploaded_at = ?, upload_target = ? "
                "WHERE table_name = ? AND day = ? AND kind = ?",
                (_now_iso(), target, table, day, kind),
            )
            manifest.commit()
            uploaded += 1
            log(f"uploaded {rel_path} -> {target}")
        # Mirror the (small) feature store alongside the raw archive.
        features_db = archive_dir / "features.db"
        if features_db.exists():
            snapshot = archive_dir / "features.db.gz.tmp"
            with open(features_db, "rb") as src, _gzip_writer(snapshot) as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            target = f"s3://{bucket}/{prefix}/features/features-latest.db.gz"
            cp = _aws(["s3", "cp", str(snapshot), target, "--only-show-errors"])
            snapshot.unlink(missing_ok=True)
            if cp.returncode != 0:
                log(f"WARN: features mirror failed: {cp.stderr.strip()}")
            else:
                log(f"mirrored features.db -> {target}")
    finally:
        manifest.close()
    return uploaded


def cleanup_local(archive_dir: Path, keep_days: int = DEFAULT_KEEP_DAYS, log=print) -> int:
    """Delete local files past the ring-buffer window — only if uploaded."""

    manifest = open_manifest(archive_dir)
    cutoff = (_utc_today() - timedelta(days=keep_days)).isoformat()
    deleted = 0
    try:
        rows = manifest.execute(
            "SELECT table_name, day, kind, path FROM archive_files "
            "WHERE day < ? AND uploaded_at IS NOT NULL AND local_deleted_at IS NULL",
            (cutoff,),
        ).fetchall()
        for table, day, kind, rel_path in rows:
            local = archive_dir / rel_path
            local.unlink(missing_ok=True)
            manifest.execute(
                "UPDATE archive_files SET local_deleted_at = ? "
                "WHERE table_name = ? AND day = ? AND kind = ?",
                (_now_iso(), table, day, kind),
            )
            manifest.commit()
            deleted += 1
            log(f"ring-buffer cleanup: removed local {rel_path} (uploaded copy verified)")
    finally:
        manifest.close()
    return deleted


# --------------------------------------------------------------------------
# Feature rollup: market_side_day
# --------------------------------------------------------------------------

FEATURES_DDL = """
CREATE TABLE IF NOT EXISTS market_side_day (
    target_date TEXT NOT NULL,
    market_ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    risk_profile TEXT NOT NULL,
    series_ticker TEXT,
    station_id TEXT,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    n_ticks INTEGER NOT NULL,
    first_created_at TEXT,
    last_created_at TEXT,
    entry_snapshot_id INTEGER,
    entry_created_at TEXT,
    approved_ticks INTEGER,
    signal_approved_ticks INTEGER,
    entry_probability REAL, entry_model_probability REAL,
    first_yes_bid REAL, first_yes_ask REAL,
    last_yes_bid REAL, last_yes_ask REAL,
    min_yes_ask REAL, max_yes_bid REAL,
    avg_spread REAL, min_spread REAL,
    entry_bid_size INTEGER, entry_ask_size INTEGER,
    entry_fee_per_contract REAL, entry_cost_per_contract REAL,
    first_model_prob REAL, last_model_prob REAL,
    first_market_prob REAL, last_market_prob REAL,
    first_edge REAL, last_edge REAL,
    max_edge REAL, max_edge_lcb REAL,
    forecast_predicted_high_f REAL,
    forecast_source_spread_f REAL,
    forecast_lead_hours REAL,
    forecast_method TEXT,
    reasons_histogram_json TEXT,
    entry_block_reasons_json TEXT,
    settlement_high_f REAL,
    side_won INTEGER,
    close_yes_bid REAL, close_yes_ask REAL,
    clv_vs_close REAL,
    traded INTEGER NOT NULL DEFAULT 0,
    order_id INTEGER,
    realized_pnl REAL,
    built_at TEXT NOT NULL,
    PRIMARY KEY (target_date, market_ticker, side, risk_profile)
);
"""


FEATURE_COLUMNS: tuple[str, ...] = (
    "target_date", "market_ticker", "side", "risk_profile",
    "series_ticker", "station_id", "strike_type", "floor_strike", "cap_strike",
    "n_ticks", "first_created_at", "last_created_at",
    "entry_snapshot_id", "entry_created_at",
    "approved_ticks", "signal_approved_ticks",
    "entry_probability", "entry_model_probability",
    "first_yes_bid", "first_yes_ask", "last_yes_bid", "last_yes_ask",
    "min_yes_ask", "max_yes_bid", "avg_spread", "min_spread",
    "entry_bid_size", "entry_ask_size",
    "entry_fee_per_contract", "entry_cost_per_contract",
    "first_model_prob", "last_model_prob",
    "first_market_prob", "last_market_prob",
    "first_edge", "last_edge", "max_edge", "max_edge_lcb",
    "forecast_predicted_high_f", "forecast_source_spread_f",
    "forecast_lead_hours", "forecast_method",
    "reasons_histogram_json", "entry_block_reasons_json",
    "settlement_high_f", "side_won",
    "close_yes_bid", "close_yes_ask", "clv_vs_close",
    "traded", "order_id", "realized_pnl", "built_at",
)


def _coalesce(*values: object) -> object:
    for value in values:
        if value is not None:
            return value
    return None


def _resolves_yes(strike_type: str | None, floor: float | None, cap: float | None, high: float) -> bool:
    strike = str(strike_type or "").lower()
    if strike == "less":
        return cap is not None and high < float(cap)
    if strike == "greater":
        return floor is not None and high > float(floor)
    if strike == "between":
        return floor is not None and cap is not None and float(floor) <= high <= float(cap)
    return False


class _GroupAccumulator:
    """Streaming first/last/min/max over one (target, market, side, profile)."""

    __slots__ = (
        "first", "last", "entry", "entry_approved", "n_ticks",
        "approved", "signal_approved", "min_yes_ask", "max_yes_bid",
        "spread_sum", "spread_n", "min_spread", "max_edge", "max_edge_lcb",
        "last_pre_close", "reasons", "block_reasons",
    )

    def __init__(self) -> None:
        self.first = None
        self.last = None
        self.entry = None
        self.entry_approved = False
        self.n_ticks = 0
        self.approved = 0
        self.signal_approved = 0
        self.min_yes_ask = None
        self.max_yes_bid = None
        self.spread_sum = 0.0
        self.spread_n = 0
        self.min_spread = None
        self.max_edge = None
        self.max_edge_lcb = None
        self.last_pre_close = None
        self.reasons: dict[str, int] = {}
        self.block_reasons: dict[str, int] = {}

    def add(self, row: dict) -> None:
        self.n_ticks += 1
        if self.first is None or (row.get("id") or 0) < (self.first.get("id") or 0):
            self.first = row
        if self.last is None or (row.get("id") or 0) > (self.last.get("id") or 0):
            self.last = row
        approved = bool(row.get("approved"))
        if approved:
            self.approved += 1
        if row.get("signal_approved"):
            self.signal_approved += 1
        # Entry semantics mirror backtest_rescore: only pre-resolution ticks
        # qualify (market still open, intraday high not complete), and the
        # FIRST approved snapshot wins; otherwise the first snapshot at all.
        close_time = row.get("market_close_time")
        created = row.get("created_at")
        pre_resolution = (
            not row.get("intraday_is_complete")
            and close_time
            and str(created or "") < str(close_time)
        )
        if pre_resolution:
            row_id = row.get("id") or 0
            if approved and not self.entry_approved:
                self.entry = row
                self.entry_approved = True
            elif approved == self.entry_approved:
                if self.entry is None or row_id < (self.entry.get("id") or 0):
                    self.entry = row
        yes_ask = row.get("yes_ask")
        if yes_ask is not None:
            self.min_yes_ask = yes_ask if self.min_yes_ask is None else min(self.min_yes_ask, yes_ask)
        yes_bid = row.get("yes_bid")
        if yes_bid is not None:
            self.max_yes_bid = yes_bid if self.max_yes_bid is None else max(self.max_yes_bid, yes_bid)
        spread = row.get("spread")
        if spread is not None:
            self.spread_sum += float(spread)
            self.spread_n += 1
            self.min_spread = spread if self.min_spread is None else min(self.min_spread, spread)
        edge = row.get("edge")
        if edge is not None:
            self.max_edge = edge if self.max_edge is None else max(self.max_edge, edge)
        edge_lcb = row.get("edge_lcb")
        if edge_lcb is not None:
            self.max_edge_lcb = edge_lcb if self.max_edge_lcb is None else max(self.max_edge_lcb, edge_lcb)
        if close_time and created and str(created) < str(close_time):
            if self.last_pre_close is None or str(created) > str(self.last_pre_close.get("created_at") or ""):
                self.last_pre_close = row
        for reason in _parse_reasons(row.get("reasons_json")):
            self.reasons[reason] = self.reasons.get(reason, 0) + 1
        block = row.get("entry_block_reason")
        if block:
            self.block_reasons[str(block)] = self.block_reasons.get(str(block), 0) + 1


def _parse_reasons(raw: object) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(parsed, list):
        return [str(r) for r in parsed]
    return []


def _mid(bid: object, ask: object) -> float | None:
    if bid is None or ask is None:
        return None
    return (float(bid) + float(ask)) / 2.0


def _iter_archive_days(archive_dir: Path, table: str, days: list[str]):
    for day in days:
        path = archive_dir / table / f"dt={day}.jsonl.gz"
        if not path.exists():
            continue
        with gzip.open(path, "rt", encoding="utf-8") as inp:
            for line in inp:
                yield json.loads(line)


def build_features(
    archive_dir: Path,
    features_db: Path,
    weather_db: Path | None,
    paper_db: Path | None,
    window_days: int = DEFAULT_FEATURE_WINDOW_DAYS,
    log=print,
) -> int:
    """(Re)build market_side_day for the trailing target-date window.

    Reads the raw archive files (NOT the live journal — post-prune the
    journal no longer holds the ticks), which doubles as a nightly proof
    that the archive is sufficient to reconstruct the signal.  Idempotent:
    each target_date in the window is deleted and rebuilt, so late-arriving
    CLI settlements fill labels on the next run.
    """

    today = _utc_today()
    targets = [
        (today - timedelta(days=i)).isoformat() for i in range(1, window_days + 1)
    ]
    target_set = set(targets)
    created_first = date.fromisoformat(min(targets)) - timedelta(days=FEATURE_CREATED_LOOKBACK_DAYS)
    created_last = min(
        date.fromisoformat(max(targets)) + timedelta(days=FEATURE_CREATED_LOOKAHEAD_DAYS),
        today - timedelta(days=1),
    )
    created_days = _day_range(created_first, created_last)

    groups: dict[tuple[str, str, str, str], _GroupAccumulator] = {}
    for row in _iter_archive_days(archive_dir, "decision_snapshots", created_days):
        target = str(row.get("target_date") or "")
        if target not in target_set:
            continue
        key = (
            target,
            str(row.get("market_ticker") or ""),
            str(row.get("side") or ""),
            str(row.get("risk_profile") or ""),
        )
        acc = groups.get(key)
        if acc is None:
            acc = groups[key] = _GroupAccumulator()
        acc.add(row)

    # Labels come only from confirmed-final CLI truth. Booked paper settlement
    # values may be manual, legacy, or MISSING_FINAL and must never train models.
    truth: dict[tuple[str, str], float] = {}
    if weather_db is not None and Path(weather_db).exists():
        wconn = connect_readonly(Path(weather_db))
        try:
            truth.update(load_cli_settlement_truth(wconn))
        except sqlite3.DatabaseError as exc:
            log(f"WARN: could not load cli_settlements from {weather_db}: {exc}")
        finally:
            wconn.close()
    orders: dict[tuple[str, str, str, str], tuple[int, float | None]] = {}
    if paper_db is not None and Path(paper_db).exists():
        pconn = connect_readonly(Path(paper_db))
        try:
            for tick, target, side, profile, order_id, pnl in pconn.execute(
                "SELECT market_ticker, target_date, side, COALESCE(risk_profile,''), "
                "MIN(id), SUM(realized_pnl) FROM paper_orders "
                "WHERE target_date >= ? GROUP BY market_ticker, target_date, side, risk_profile",
                (min(targets),),
            ):
                orders[(str(target), str(tick), str(side), str(profile))] = (order_id, pnl)
        finally:
            pconn.close()

    fconn = sqlite3.connect(features_db, timeout=30.0)
    fconn.execute("PRAGMA journal_mode = WAL")
    fconn.executescript(FEATURES_DDL)
    written = 0
    try:
        with fconn:
            for target in targets:
                fconn.execute("DELETE FROM market_side_day WHERE target_date = ?", (target,))
            for (target, ticker, side, profile), acc in groups.items():
                entry = acc.entry or {}
                first = acc.first or {}
                last = acc.last or {}
                city = city_for_market_ticker(ticker)
                skey = settlement_key_for_market(ticker, target)
                high = truth.get(skey) if skey is not None else None
                strike_type = _coalesce(entry.get("strike_type"), first.get("strike_type"))
                floor_strike = _coalesce(entry.get("floor_strike"), first.get("floor_strike"))
                cap_strike = _coalesce(entry.get("cap_strike"), first.get("cap_strike"))
                side_won = None
                if high is not None:
                    yes_wins = _resolves_yes(strike_type, floor_strike, cap_strike, float(high))
                    side_won = int(yes_wins if side.upper() == "YES" else not yes_wins)
                entry_mid = _mid(entry.get("yes_bid"), entry.get("yes_ask"))
                close_row = acc.last_pre_close or {}
                close_mid = _mid(close_row.get("yes_bid"), close_row.get("yes_ask"))
                clv = None
                if entry_mid is not None and close_mid is not None:
                    clv = (close_mid - entry_mid) if side.upper() == "YES" else (entry_mid - close_mid)
                order = orders.get((target, ticker, side, profile))
                fconn.execute(
                    "INSERT OR REPLACE INTO market_side_day ("
                    + ", ".join(FEATURE_COLUMNS)
                    + ") VALUES (" + ",".join("?" * len(FEATURE_COLUMNS)) + ")",
                    (
                        target, ticker, side, profile,
                        city.series_ticker if city else None,
                        city.nws_station_id if city else None,
                        strike_type, floor_strike, cap_strike,
                        acc.n_ticks,
                        first.get("created_at"), last.get("created_at"),
                        entry.get("id"), entry.get("created_at"),
                        acc.approved, acc.signal_approved,
                        entry.get("probability"), entry.get("model_probability"),
                        first.get("yes_bid"), first.get("yes_ask"),
                        last.get("yes_bid"), last.get("yes_ask"),
                        acc.min_yes_ask, acc.max_yes_bid,
                        (acc.spread_sum / acc.spread_n) if acc.spread_n else None,
                        acc.min_spread,
                        entry.get("entry_bid_size"), entry.get("entry_ask_size"),
                        entry.get("fee_per_contract"), entry.get("cost_per_contract"),
                        first.get("model_probability"), last.get("model_probability"),
                        first.get("market_probability"), last.get("market_probability"),
                        first.get("edge"), last.get("edge"),
                        acc.max_edge, acc.max_edge_lcb,
                        entry.get("forecast_predicted_high_f"),
                        entry.get("forecast_source_spread_f"),
                        entry.get("forecast_lead_hours"),
                        entry.get("forecast_method"),
                        json.dumps(acc.reasons, ensure_ascii=False, sort_keys=True),
                        json.dumps(acc.block_reasons, ensure_ascii=False, sort_keys=True),
                        high, side_won,
                        close_row.get("yes_bid"), close_row.get("yes_ask"),
                        clv,
                        1 if order else 0,
                        order[0] if order else None,
                        order[1] if order else None,
                        _now_iso(),
                    ),
                )
                written += 1
    finally:
        fconn.close()
    log(
        f"features: rebuilt {written} market_side_day rows across "
        f"{len(targets)} target dates ({targets[-1]}..{targets[0]})"
    )
    return written
