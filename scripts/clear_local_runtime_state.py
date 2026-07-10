#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

RUNTIME_PATHS = (
    "forecaster/weather.db",
    "forecaster/weather.db-journal",
    "forecaster/google_weather_cache.json",
    "forecaster/trading_signal.json",
    "forecaster/strategy_research.json",
    "forecaster/cities_data.json",
    "forecaster/publication_manifest.json",
    ".google_weather_usage.json",
    "trading/data",
)

PLACEHOLDER_PATHS = (
    "forecaster/google_weather_cache.json",
    "forecaster/trading_signal.json",
    "forecaster/strategy_research.json",
)


def _project_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"refusing path outside project root: {relative}") from exc
    return path


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _placeholder(relative: str) -> dict[str, object]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "available": False,
        "local_runtime_placeholder": True,
        "source": "AWS runtime only",
        "source_of_truth": "AWS Lightsail after sync and refresh",
        "generated_at": now,
        "reason": (
            "Live WeatherEdge API/cache data is generated on AWS after sync. "
            "This local placeholder prevents stale MacBook data from confusing "
            "dashboard design smoke tests."
        ),
        "artifact": relative,
    }


def _write_placeholder(path: Path, relative: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_placeholder(relative), indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Clear local WeatherEdge runtime data so dashboard smoke tests do "
            "not mistake stale MacBook artifacts for AWS state."
        )
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="WeatherEdge root")
    parser.add_argument("--confirm", action="store_true", help="actually remove files; otherwise dry-run")
    parser.add_argument(
        "--no-placeholders",
        action="store_true",
        help="do not write local AWS-placeholder JSON files after cleanup",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    found = []
    for relative in RUNTIME_PATHS:
        path = _project_path(root, relative)
        if path.exists() or path.is_symlink():
            found.append((relative, path))

    verb = "removed" if args.confirm else "would remove"
    if found:
        for relative, path in found:
            if args.confirm:
                _remove(path)
            print(f"{verb}: {relative}")
    else:
        print("no local runtime data found")

    if args.confirm and not args.no_placeholders:
        for relative in PLACEHOLDER_PATHS:
            _write_placeholder(_project_path(root, relative), relative)
            print(f"wrote AWS runtime placeholder: {relative}")
    elif not args.confirm:
        print("dry-run only; pass --confirm to clear local runtime data")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
