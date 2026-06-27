from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "github_hygiene_check.py"
    spec = importlib.util.spec_from_file_location("github_hygiene_check", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


github_hygiene = _load_module()


def test_branch_protection_flags_unprotected_branch():
    def fake_fetch(url: str, token: str | None):
        branch = url.rsplit("/", 1)[-1]
        return {
            "protected": branch == "gh-pages",
            "commit": {"sha": "862056cfa58687ef4389a8fd32baee9274a54adb"},
        }

    result = github_hygiene.check_branch_protection(
        "Jaxsonb04/weather_edge",
        ("main", "gh-pages"),
        fetch_json=fake_fetch,
    )

    assert result.status == "FAIL"
    assert "main" in result.details
    assert "862056c" in result.details


def test_open_pr_hygiene_flags_stale_and_stacked_prs():
    def fake_fetch(url: str, token: str | None):
        return [
            {
                "number": 17,
                "title": "old stacked work",
                "updated_at": "2026-06-20T09:40:40Z",
                "base": {"ref": "feat/base"},
                "head": {"ref": "feat/head"},
            },
            {
                "number": 21,
                "title": "fresh main work",
                "updated_at": "2026-06-27T09:40:40Z",
                "base": {"ref": "main"},
                "head": {"ref": "feat/fresh"},
            },
        ]

    result = github_hygiene.check_open_prs(
        "Jaxsonb04/weather_edge",
        now=datetime(2026, 6, 27, 10, 0, tzinfo=UTC),
        stale_days=7,
        fetch_json=fake_fetch,
    )

    assert result.status == "WARN"
    assert "#17" in result.details
    assert "feat/head->feat/base" in result.details


def test_stale_remote_branch_classifier_ignores_main_and_pages():
    rows = [
        ("origin/main", "2026-06-01T00:00:00+00:00"),
        ("origin/gh-pages", "2026-06-01T00:00:00+00:00"),
        ("origin/feat/old", "2026-06-01T00:00:00+00:00"),
        ("origin/feat/fresh", "2026-06-26T00:00:00+00:00"),
    ]

    stale = github_hygiene.stale_remote_branches(
        rows,
        now=datetime(2026, 6, 27, 0, 0, tzinfo=UTC),
        stale_days=7,
    )

    assert stale == ["origin/feat/old (26d)"]
