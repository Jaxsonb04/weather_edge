#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_PATHS = (
    "README.md",
    "CONTEXT.md",
    ".gitignore",
    "docs/architecture.md",
    "src/App.tsx",
    "forecaster/forecast_data.json",
    "forecaster/weather_story_data.json",
    "scripts/run_tests.sh",
    "trading/sfo_kalshi_quant/config.py",
)

EXCLUDED_DIR_PARTS = {
    ".git",
    ".local",
    ".venv",
    ".venv-dev",
    ".venv-test",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    "dist",
    "2016-2026 weather data",
    "models",
    "plots",
    "logs",
    "data",
}

TEXT_SUFFIXES = {
    "",
    ".css",
    ".env",
    ".example",
    ".html",
    ".js",
    ".json",
    ".md",
    ".service",
    ".sh",
    ".timer",
    ".toml",
    ".txt",
    ".py",
    ".yaml",
    ".yml",
}

SECRET_FILE_NAMES = {".env", ".google_weather_usage.json"}
SECRET_FILE_SUFFIXES = (".pem", ".key", ".p8")

SECRET_PATTERNS = (
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub token", re.compile(r"ghp_[A-Za-z0-9_]{36}")),
    ("AI API token", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("AI provider token", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
)

LOCAL_RUNTIME_ARTIFACTS = {
    "forecaster/weather.db": "local forecast SQLite archive",
    "forecaster/google_weather_cache.json": "local Google Weather cache",
    "forecaster/trading_signal.json": "local public trading signal",
    "forecaster/strategy_research.json": "local Strategy Lab research artifact",
    "forecaster/cities_data.json": "local multi-city public snapshot",
    "forecaster/publication_manifest.json": "local publication manifest",
    "trading/data": "local paper-trading state directory",
}

LOCAL_RUNTIME_PLACEHOLDERS = {
    "forecaster/google_weather_cache.json",
    "forecaster/trading_signal.json",
    "forecaster/strategy_research.json",
}

# Task 8 item 4 (defense in depth): the authoritative gate is
# sfo_kalshi_quant.publication's publish-time scan (which this script
# cannot import -- it is deliberately zero-dependency, loaded standalone via
# importlib.util in trading/tests/test_weatheredge_health_check.py), but a
# local operator should see the same class of leak before ever running a
# publish cycle. Kept in sync by hand with
# trading/sfo_kalshi_quant/publication.py's _RAW_GOOGLE_FIELD_KEYS /
# _RAW_GOOGLE_VALUE_PATTERNS -- deliberately excludes google_high_f/
# sources.google.*, the pre-existing legacy SFO live blend's own derived
# field (spec section 7.5: a known, accepted exception, not a new leak).
RAW_GOOGLE_ARTIFACTS = (
    "forecaster/trading_signal.json",
    "forecaster/cities_data.json",
    "forecaster/strategy_research.json",
    "forecaster/forecast_data.json",
    "forecaster/weather_story_data.json",
    "forecaster/google_weather_cache.json",
)

RAW_GOOGLE_FIELD_KEYS = frozenset(
    (
        "highF",
        "weatherCondition",
        "maxTemperature",
        "minTemperature",
        "feelsLikeTemperature",
        "temperatureChange",
        "displayDateTime",
        "forecastDays",
        "forecastHours",
        "currentConditionsHistory",
        "iconBaseUri",
        "nextPageToken",
        "google_current_conditions",
        "google_daily_forecast_highs",
    )
)
RAW_GOOGLE_VALUE_PATTERNS = (
    re.compile(r"weather\.googleapis\.com"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    details: str


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _is_excluded(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    return any(part in EXCLUDED_DIR_PARTS for part in relative.parts[:-1])


def _iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _is_excluded(root, path):
            continue
        yield path


def _iter_text_files(root: Path):
    for path in _iter_files(root):
        if path.suffix not in TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > 1_000_000:
                continue
        except OSError:
            continue
        yield path


def check_required_paths(root: Path) -> CheckResult:
    missing = [relative for relative in REQUIRED_PATHS if not (root / relative).exists()]
    if missing:
        return CheckResult("required project paths", "FAIL", "missing: " + ", ".join(missing))
    return CheckResult("required project paths", "PASS", "all expected WeatherEdge source paths exist")


def check_config_defaults(root: Path) -> CheckResult:
    config_path = root / "trading" / "sfo_kalshi_quant" / "config.py"
    if not config_path.exists():
        return CheckResult("WeatherEdge config defaults", "FAIL", "trading config is missing")
    text = config_path.read_text(encoding="utf-8", errors="replace")
    required_terms = ("DEFAULT_FORECASTER_ROOT", "forecaster")
    missing = [term for term in required_terms if term not in text]
    if missing:
        return CheckResult(
            "WeatherEdge config defaults",
            "FAIL",
            "config.py does not mention: " + ", ".join(missing),
        )
    return CheckResult(
        "WeatherEdge config defaults",
        "PASS",
        "trading config still points at a forecaster root",
    )


def check_local_secret_files(root: Path) -> CheckResult:
    hits = []
    for path in _iter_files(root):
        if path.name in SECRET_FILE_NAMES or path.suffix in SECRET_FILE_SUFFIXES:
            hits.append(_relative(root, path))
    if hits:
        return CheckResult("local secret files", "FAIL", "remove or keep untracked: " + ", ".join(sorted(hits)))
    return CheckResult("local secret files", "PASS", "no .env, key, pem, or Google usage ledger files found")


def check_secret_patterns(root: Path) -> CheckResult:
    hits = []
    for path in _iter_text_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append(f"{_relative(root, path)} ({label})")
                break
    if hits:
        return CheckResult("secret pattern scan", "FAIL", "high-confidence secret pattern(s): " + ", ".join(sorted(hits)))
    return CheckResult("secret pattern scan", "PASS", "no high-confidence token patterns found")


def _is_runtime_placeholder(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("local_runtime_placeholder") is True


def check_local_runtime_artifacts(root: Path) -> CheckResult:
    stale = []
    placeholders = []
    for relative, label in LOCAL_RUNTIME_ARTIFACTS.items():
        path = root / relative
        if not path.exists():
            continue
        if relative in LOCAL_RUNTIME_PLACEHOLDERS and _is_runtime_placeholder(path):
            placeholders.append(relative)
            continue
        stale.append(f"{relative} ({label})")

    if stale:
        return CheckResult(
            "local runtime data",
            "WARN",
            "may be stale during dashboard smoke tests: "
            + ", ".join(sorted(stale))
            + "; live API/cache state is AWS-side after sync. Run "
            "`python3 scripts/clear_local_runtime_state.py --confirm` before design verification.",
        )
    if placeholders:
        return CheckResult(
            "local runtime data",
            "PASS",
            "AWS runtime placeholder(s) present: " + ", ".join(sorted(placeholders)),
        )
    return CheckResult(
        "local runtime data",
        "PASS",
        "no local DB/cache/generated dashboard artifacts found; AWS remains live source after sync",
    )


def _raw_google_markers(value: object, *, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if key in RAW_GOOGLE_FIELD_KEYS:
                hits.append(key_path)
            hits.extend(_raw_google_markers(nested, path=key_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hits.extend(_raw_google_markers(item, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        for pattern in RAW_GOOGLE_VALUE_PATTERNS:
            if pattern.search(value):
                hits.append(path)
                break
    return hits


def check_no_raw_google_content_in_public_artifacts(root: Path) -> CheckResult:
    hits = []
    for relative in RAW_GOOGLE_ARTIFACTS:
        path = root / relative
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        for marker in _raw_google_markers(payload):
            hits.append(f"{relative} ({marker})")
    if hits:
        return CheckResult(
            "raw Google content scan",
            "FAIL",
            "raw Google field(s) found in public artifacts: " + ", ".join(sorted(hits)),
        )
    return CheckResult(
        "raw Google content scan",
        "PASS",
        "no raw Google API field markers found in tracked public artifacts",
    )


def check_git_state(root: Path) -> CheckResult:
    if (root / ".git").exists():
        return CheckResult("git repository state", "PASS", "local Git repository is initialized")
    return CheckResult(
        "git repository state",
        "WARN",
        "WeatherEdge is not initialized as a Git repository yet",
    )


def check_optional_tools(root: Path) -> CheckResult:
    missing = []
    user_semgrep = (
        Path.home()
        / "Library"
        / "Python"
        / f"{sys.version_info.major}.{sys.version_info.minor}"
        / "bin"
        / "semgrep"
    )
    if shutil.which("semgrep") is None and not user_semgrep.exists():
        missing.append("semgrep")
    if missing:
        return CheckResult("optional quality tools", "WARN", "not installed on PATH: " + ", ".join(missing))
    return CheckResult("optional quality tools", "PASS", "optional quality tools are available")


def run_checks(root: Path = PROJECT_ROOT) -> list[CheckResult]:
    root = root.resolve()
    return [
        check_required_paths(root),
        check_config_defaults(root),
        check_local_secret_files(root),
        check_secret_patterns(root),
        check_local_runtime_artifacts(root),
        check_no_raw_google_content_in_public_artifacts(root),
        check_git_state(root),
        check_optional_tools(root),
    ]


def print_results(results: list[CheckResult]) -> None:
    for result in results:
        print(f"{result.status:4} {result.name}: {result.details}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local WeatherEdge safety and readiness checks.")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="WeatherEdge root to check")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args()

    results = run_checks(args.root)
    print_results(results)

    has_failure = any(result.status == "FAIL" for result in results)
    has_warning = any(result.status == "WARN" for result in results)
    if has_failure or (args.strict and has_warning):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
