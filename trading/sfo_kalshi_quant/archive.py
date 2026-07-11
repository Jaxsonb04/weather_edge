"""Append-only archival + feature layer for the paper-trading journal.

The retention prune (``PaperStore.prune_decision_snapshots``) keeps the 1 GB
box healthy by deleting redundant rejection ticks, but those ticks are the
training data this project compounds on: intra-day book/probability
evolution, per-tick rejection reasons, ladders and config snapshots inside
``diagnostics_json``, and the never-yet-read ``prediction_features_json``
audit trail.  This module guarantees the prune can never destroy signal:

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
    created_at TEXT NOT NULL,
    uploaded_at TEXT,
    upload_target TEXT,
    local_deleted_at TEXT,
    PRIMARY KEY (table_name, day, kind)
);
"""


def open_manifest(archive_dir: Path) -> sqlite3.Connection:
    archive_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(archive_dir / "manifest.db", timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(MANIFEST_DDL)
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


def _table_columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    return [str(r[1]) for r in rows]


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
        select_cols = ", ".join(
            (f"{schema}.{table}.{c}" if c in have else f"NULL AS {c}")
            for c in columns
        )
        where = [f"{schema}.{table}.created_at >= ?", f"{schema}.{table}.created_at < ?"]
        params.extend([day, next_day])
        if id_floor is not None and schema == "main" and not merge_schemas:
            where.append(f"{schema}.{table}.id > ?")
            params.append(id_floor)
        for prev in schemas[:idx]:
            if not _table_columns(conn, prev, table):
                continue
            where.append(
                f"{schema}.{table}.id NOT IN ("
                f"SELECT id FROM {prev}.{table} WHERE created_at >= ? AND created_at < ?)"
            )
            params.extend([day, next_day])
        parts.append(
            f"SELECT {select_cols} FROM {schema}.{table} WHERE {' AND '.join(where)}"
        )
    sql = " UNION ALL ".join(parts) + " ORDER BY id"
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

    columns = _table_columns(conn, "main", table)
    out_dir = archive_dir / table
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / f"dt={day}.jsonl.gz"
    tmp_path = out_dir / f"dt={day}.jsonl.gz.tmp"

    digest = hashlib.sha256()
    rows = 0
    min_id: int | None = None
    max_id: int | None = None
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
    )


def _record(manifest: sqlite3.Connection, result: ExportResult, archive_dir: Path, kind: str) -> None:
    manifest.execute(
        "INSERT OR REPLACE INTO archive_files "
        "(table_name, day, kind, path, rows, bytes, sha256, min_id, max_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            result.table, result.day, kind,
            str(result.path.relative_to(archive_dir)),
            result.rows, result.bytes, result.sha256,
            result.min_id, result.max_id, _now_iso(),
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


def archive_pending(
    db_path: Path,
    archive_dir: Path,
    merge_dbs: list[Path] | None = None,
    include_full: bool = True,
    log=print,
) -> int:
    """Export every unarchived complete UTC day; returns count of new files."""

    manifest = open_manifest(archive_dir)
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
    conn = connect_readonly(db_path)
    yesterday = _utc_today() - timedelta(days=1)
    missing: list[tuple[str, str]] = []
    try:
        manifest_columns = {
            str(row[1]) for row in manifest.execute("PRAGMA table_info(archive_files)")
        }
        if "rows" not in manifest_columns:
            raise RuntimeError(
                "archive manifest lacks verified row counts; re-export complete UTC-day "
                "partitions before running --check-gate"
            )
        for table in STREAM_TABLES:
            first = _first_day(conn, table, [])
            if first is None:
                continue
            coverage = {
                str(day): int(rows)
                for day, rows in manifest.execute(
                    "SELECT day, rows FROM archive_files "
                    "WHERE table_name = ? AND kind = 'day'",
                    (table,),
                )
                if rows is not None
            }
            for day in _day_range(date.fromisoformat(first), yesterday):
                source_rows = _source_day_count(conn, table, day)
                if day not in coverage or coverage[day] != source_rows:
                    missing.append((table, day))
    finally:
        conn.close()
        manifest.close()
    return missing


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
