#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = "Jaxsonb04/weather_edge"
DEFAULT_BRANCHES = ("main", "gh-pages")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    details: str


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _fetch_json(url: str, token: str | None = None) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "WeatherEdge-github-hygiene-check",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def check_branch_protection(
    repo: str,
    branches: tuple[str, ...],
    *,
    token: str | None = None,
    fetch_json: Callable[[str, str | None], Any] = _fetch_json,
) -> CheckResult:
    unprotected: list[str] = []
    protected: list[str] = []
    errors: list[str] = []
    for branch in branches:
        url = f"https://api.github.com/repos/{repo}/branches/{branch}"
        try:
            payload = fetch_json(url, token)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            errors.append(f"{branch}: {type(exc).__name__}: {exc}")
            continue
        if payload.get("protected"):
            protected.append(branch)
        else:
            sha = (payload.get("commit") or {}).get("sha")
            suffix = f" ({sha[:7]})" if isinstance(sha, str) else ""
            unprotected.append(f"{branch}{suffix}")
    if unprotected:
        return CheckResult(
            "GitHub branch protection",
            "FAIL",
            "unprotected branch(es): " + ", ".join(unprotected),
        )
    if errors:
        return CheckResult(
            "GitHub branch protection",
            "WARN",
            "could not verify: " + "; ".join(errors),
        )
    return CheckResult(
        "GitHub branch protection",
        "PASS",
        "protected branch(es): " + ", ".join(protected),
    )


def check_open_prs(
    repo: str,
    *,
    now: datetime | None = None,
    stale_days: int = 7,
    token: str | None = None,
    fetch_json: Callable[[str, str | None], Any] = _fetch_json,
) -> CheckResult:
    url = f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=100"
    current = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        payload = fetch_json(url, token)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return CheckResult("GitHub open PR hygiene", "WARN", f"could not verify: {type(exc).__name__}: {exc}")
    stale: list[str] = []
    stacked: list[str] = []
    for pr in payload:
        number = pr.get("number")
        title = str(pr.get("title") or "").strip()
        updated_at = str(pr.get("updated_at") or "")
        try:
            age_days = (current - _parse_timestamp(updated_at)).days
        except ValueError:
            age_days = 0
        if age_days >= stale_days:
            stale.append(f"#{number} {age_days}d {title}")
        base = ((pr.get("base") or {}).get("ref")) or ""
        if base and base != "main":
            head = ((pr.get("head") or {}).get("ref")) or "unknown"
            stacked.append(f"#{number} {head}->{base}")
    if stale or stacked:
        pieces = []
        if stale:
            pieces.append("stale: " + "; ".join(stale[:8]))
        if stacked:
            pieces.append("stacked: " + "; ".join(stacked[:8]))
        return CheckResult("GitHub open PR hygiene", "WARN", " | ".join(pieces))
    return CheckResult("GitHub open PR hygiene", "PASS", "no stale or stacked open PRs found")


def _remote_branch_rows(root: Path) -> list[tuple[str, str]]:
    output = subprocess.check_output(
        [
            "git",
            "for-each-ref",
            "refs/remotes/origin",
            "--format=%(refname:short)\t%(committerdate:iso-strict)",
        ],
        cwd=root,
        text=True,
        stderr=subprocess.DEVNULL,
    )
    rows: list[tuple[str, str]] = []
    for line in output.splitlines():
        if "\t" not in line:
            continue
        branch, timestamp = line.split("\t", 1)
        rows.append((branch, timestamp))
    return rows


def stale_remote_branches(
    rows: list[tuple[str, str]],
    *,
    now: datetime,
    stale_days: int,
) -> list[str]:
    stale: list[str] = []
    ignored = {"origin/HEAD", "origin/main", "origin/gh-pages"}
    for branch, timestamp in rows:
        if branch in ignored:
            continue
        try:
            age_days = (now - _parse_timestamp(timestamp)).days
        except ValueError:
            continue
        if age_days >= stale_days:
            stale.append(f"{branch} ({age_days}d)")
    return stale


def check_remote_branches(root: Path, *, now: datetime | None = None, stale_days: int = 7) -> CheckResult:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        rows = _remote_branch_rows(root)
    except (OSError, subprocess.CalledProcessError) as exc:
        return CheckResult("GitHub remote branch hygiene", "WARN", f"could not inspect local refs: {exc}")
    stale = stale_remote_branches(rows, now=current, stale_days=stale_days)
    if stale:
        return CheckResult(
            "GitHub remote branch hygiene",
            "WARN",
            "stale remote branch(es): " + ", ".join(stale[:12]),
        )
    return CheckResult("GitHub remote branch hygiene", "PASS", "no stale remote branches found")


def run_checks(
    *,
    repo: str = DEFAULT_REPO,
    root: Path = PROJECT_ROOT,
    branches: tuple[str, ...] = DEFAULT_BRANCHES,
    stale_days: int = 7,
    token: str | None = None,
) -> list[CheckResult]:
    return [
        check_branch_protection(repo, branches, token=token),
        check_open_prs(repo, stale_days=stale_days, token=token),
        check_remote_branches(root, stale_days=stale_days),
    ]


def print_results(results: list[CheckResult]) -> None:
    for result in results:
        print(f"{result.status:4} {result.name}: {result.details}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check WeatherEdge GitHub hygiene without mutating repository settings.")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="GitHub owner/repo, default: %(default)s")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="local repository root")
    parser.add_argument("--branch", action="append", dest="branches", help="branch to require protected; may repeat")
    parser.add_argument("--stale-days", type=int, default=7, help="age threshold for stale PRs/remote branches")
    parser.add_argument("--strict", action="store_true", help="treat WARN as nonzero")
    parser.add_argument("--json", action="store_true", help="emit JSON results")
    args = parser.parse_args()

    branches = tuple(args.branches) if args.branches else DEFAULT_BRANCHES
    token = os.getenv("GITHUB_TOKEN")
    results = run_checks(
        repo=args.repo,
        root=args.root,
        branches=branches,
        stale_days=args.stale_days,
        token=token,
    )
    if args.json:
        print(json.dumps([result.__dict__ for result in results], indent=2))
    else:
        print_results(results)

    has_failure = any(result.status == "FAIL" for result in results)
    has_warning = any(result.status == "WARN" for result in results)
    if has_failure or (args.strict and has_warning):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
