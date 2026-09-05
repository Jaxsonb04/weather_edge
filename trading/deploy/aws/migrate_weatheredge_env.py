#!/usr/bin/env python3
"""Apply narrow, idempotent migrations to the WeatherEdge runtime env file."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path


LEGACY_LIVE_RISK_DEFAULTS = (
    "SFO_LIVE_PILOT_MAX_LOSS=50",
    "SFO_LIVE_DAILY_LOSS=20",
    "SFO_LIVE_PER_TRADE_RISK=10",
)
RELATIVE_LIVE_RISK_DEFAULTS = (
    "SFO_LIVE_RISK_CAPITAL=1000",
    "SFO_LIVE_PILOT_MAX_LOSS_PCT=0.05",
    "SFO_LIVE_DAILY_LOSS_PCT=0.02",
    "SFO_LIVE_PER_TRADE_RISK_PCT=0.01",
)
AUDIT_RUNTIME_DEFAULTS = {
    "PAPER_RESEARCH_TAKE_PROFIT_MARGIN": "0.05",
    "PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED": "true",
}


def migrate_legacy_live_risk_defaults(text: str) -> tuple[str, bool]:
    """Replace only the complete historical 50/20/10 default policy.

    Any custom legacy value, duplicate, or existing relative-policy key leaves
    the file untouched so an installer cannot reinterpret operator intent.
    """

    lines = text.splitlines()
    assignments: dict[str, list[tuple[int, str]]] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        assignments.setdefault(key, []).append((index, line))

    relative_keys = tuple(line.split("=", 1)[0] for line in RELATIVE_LIVE_RISK_DEFAULTS)
    if any(key in assignments for key in relative_keys):
        return text, False

    legacy_indices: list[int] = []
    for expected in LEGACY_LIVE_RISK_DEFAULTS:
        key = expected.split("=", 1)[0]
        matches = assignments.get(key, [])
        if len(matches) != 1 or matches[0][1] != expected:
            return text, False
        legacy_indices.append(matches[0][0])

    migrated: list[str] = []
    legacy_index_set = set(legacy_indices)
    for index, line in enumerate(lines):
        if index == legacy_indices[0]:
            migrated.extend(RELATIVE_LIVE_RISK_DEFAULTS)
        elif index in legacy_index_set:
            continue
        else:
            migrated.append(line)
    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(migrated) + suffix, True


def migrate_audit_runtime_defaults(text: str) -> tuple[str, bool]:
    """Install the audited paper-only defaults without overriding operators.

    The heartbeat's former shipped default was exactly ``false``. That one
    historical value is migrated to ``true``; any other explicit value is
    treated as an operator choice. The new research-only take-profit margin is
    appended only when absent. Duplicate assignments are never rewritten.
    """

    lines = text.splitlines()
    assignments: dict[str, list[tuple[int, str]]] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        assignments.setdefault(key, []).append((index, line))

    changed = False
    heartbeat_key = "PAPER_SAME_DAY_MODEL_HEARTBEAT_ENABLED"
    heartbeat_matches = assignments.get(heartbeat_key, [])
    if len(heartbeat_matches) == 1 and heartbeat_matches[0][1] == f"{heartbeat_key}=false":
        lines[heartbeat_matches[0][0]] = f"{heartbeat_key}=true"
        changed = True

    for key, value in AUDIT_RUNTIME_DEFAULTS.items():
        if key not in assignments:
            lines.append(f"{key}={value}")
            changed = True

    if not changed:
        return text, False
    return "\n".join(lines) + "\n", True


def _atomic_write(path: Path, text: str) -> None:
    existing = path.stat()
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), stat.S_IMODE(existing.st_mode))
            if hasattr(os, "fchown"):
                os.fchown(handle.fileno(), existing.st_uid, existing.st_gid)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} /path/to/weatheredge.env", file=sys.stderr)
        return 2
    path = Path(argv[1])
    original = path.read_text(encoding="utf-8")
    migrated, risk_changed = migrate_legacy_live_risk_defaults(original)
    migrated, audit_changed = migrate_audit_runtime_defaults(migrated)
    if risk_changed or audit_changed:
        _atomic_write(path, migrated)
    if risk_changed:
        print("migrated legacy live risk defaults")
    else:
        print("live risk defaults unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
