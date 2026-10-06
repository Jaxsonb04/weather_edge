#!/usr/bin/env python3
"""Copy a live AWS SQLite database to the Mac with SQLite's transaction-aware tool.

This is an off-host replica, not authorization to bypass deployment's durable
S3 backup/restore gate. A failed copy or validation never publishes a receipt.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from datetime import datetime, timezone

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--origin', required=True)
parser.add_argument('--output', required=True, type=Path)
parser.add_argument('--tool', required=True, type=Path)
parser.add_argument('--remote-tool', required=True)
parser.add_argument('--ssh', required=True, type=Path)
args = parser.parse_args()
if args.output.exists():
    parser.error('choose a new output path; verified historical replicas are never overwritten')
args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
started = datetime.now(timezone.utc).isoformat()
subprocess.run([str(args.tool.resolve()), args.origin, str(args.output.resolve()),
                '--exe', args.remote_tool, '--ssh', str(args.ssh.resolve()), '-v'],
               check=True, timeout=7200)
with sqlite3.connect(args.output.resolve().as_uri() + '?mode=ro', uri=True) as conn:
    conn.execute('PRAGMA query_only=ON')
    if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise ValueError('replica integrity check failed')
    if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('replica foreign-key check failed')
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
with args.output.open('rb') as stream:
    sha = hashlib.file_digest(stream, 'sha256').hexdigest()
receipt = {'schema_version': 1, 'started_at': started,
           'verified_at': datetime.now(timezone.utc).isoformat(),
           'method': 'sqlite3_rsync consistent origin snapshot',
           'integrity_check': 'ok', 'foreign_key_check': 'ok',
           'sha256': sha, 'bytes': args.output.stat().st_size, 'tables': tables,
           'durable_s3_restore_gate': 'not satisfied by this local replica'}
args.output.with_suffix('.receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
print('Local replica verified; durable deployment backup gate remains separate.')
