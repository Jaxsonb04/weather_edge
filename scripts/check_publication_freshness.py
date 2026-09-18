#!/usr/bin/env python3
"""Off-box staleness monitor for the published WeatherEdge site snapshot.

Every existing publication watcher runs *on the EC2 box*. That is the wrong
place: both multi-hour outages this month began when a deploy launched from a
laptop died mid-flight and left the box quiesced, which disabled the watchers
along with the publisher. Production was dark 46.8 h and then 22.3 h, untold.

This is the off-box half. It runs on GitHub Actions, reads only the published
artifact over HTTPS, and opens/comments/closes a GitHub issue -- so it still
reports when the box, the build Mac and the owner's laptop are all unreachable.
It is standard library only: no `pip install` may stand between an outage and
the alert. A deploy legitimately blanks publication for a while, so a
`production-box` deployment record raises the alert threshold -- but only up to
a cap, because a deploy that never finishes is the very failure being watched
for. A `published_at` in the future means a clock is wrong, and is never read
as freshness in either direction.

Three things are deliberately never silent: a stale snapshot, a manifest that
is *structurally* gone (a 404, or a body that is not our document -- a dead
site is a worse outage than a stale one), and a clock fault. Only a transient
read failure warns and exits 0, and only after the read has been retried. An
attended maintenance window that stops publishing on purpose can set
`FRESHNESS_MUTE_UNTIL` to suppress new issues without suppressing recovery.

The decision rules live in `publication_freshness_core` (pure, offline) and the
REST calls in `publication_freshness_github`. This file is the wiring: parse
flags, read, decide, print, and -- outside `--dry-run` -- write.

Usage (read-only, safe against production):

    python3 scripts/check_publication_freshness.py --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Sequence

# Running this file directly already puts scripts/ on sys.path; doing it
# explicitly means the module also imports cleanly under a test loader.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from publication_freshness_core import (  # noqa: E402 - must follow the sys.path line
    ACTION_CREATE,
    DEFAULT_BASE_OPEN_MINUTES,
    DEFAULT_CLOSE_AT_OR_BELOW_MINUTES,
    DEFAULT_DEPLOY_MARGIN_MINUTES,
    DEFAULT_MANIFEST_URL,
    DEFAULT_MAX_DEPLOY_WINDOW_MINUTES,
    DEFAULT_REPO,
    STATE_BROKEN,
    STATE_FUTURE,
    STATE_UNREADABLE,
    DeploymentRecord,
    GitHubApiError,
    Notes,
    OpenIssue,
    Thresholds,
    evaluate,
    fetch_manifest_with_retries,
    open_threshold,
    parse_timestamp,
    plan_action,
)
from publication_freshness_github import (  # noqa: E402 - must follow the sys.path line
    apply_action,
    find_open_issue,
    latest_deployment,
    make_api,
    recently_acknowledged,
)

MUTE_ENV_VAR = "FRESHNESS_MUTE_UNTIL"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Watch the published WeatherEdge manifest from off the production box."
    )
    add = parser.add_argument
    add("--manifest-url", default=DEFAULT_MANIFEST_URL, help="published manifest URL")
    add("--repo", default=None, help="owner/repo; defaults to $GITHUB_REPOSITORY")
    add(
        "--base-open-minutes",
        type=float,
        default=DEFAULT_BASE_OPEN_MINUTES,
        help="staleness that opens an issue with no deploy in flight (default: %(default)s)",
    )
    add(
        "--deploy-margin-minutes",
        type=float,
        default=DEFAULT_DEPLOY_MARGIN_MINUTES,
        help="grace added to a declared deploy window (default: %(default)s)",
    )
    add(
        "--max-deploy-window-minutes",
        type=float,
        default=DEFAULT_MAX_DEPLOY_WINDOW_MINUTES,
        help="longest deploy window a record may claim (default: %(default)s)",
    )
    add(
        "--close-at-or-below-minutes",
        type=float,
        default=DEFAULT_CLOSE_AT_OR_BELOW_MINUTES,
        help="age at or below which an open issue is closed (default: %(default)s)",
    )
    add(
        "--mute-until",
        default=None,
        help=(
            f"ISO-8601 instant until which no issue is opened; defaults to ${MUTE_ENV_VAR}. "
            "For an attended maintenance window that stops publishing on purpose "
            "(deploy runbook phase 5.5). Recovery still closes an open issue."
        ),
    )
    add("--dry-run", action="store_true", help="print the planned action; write nothing")
    return parser


def mute_deadline(raw: str | None, env: dict[str, str] | None = None) -> datetime | None:
    """Resolve the mute deadline from the flag, then the environment."""

    source = raw or (env if env is not None else dict(os.environ)).get(MUTE_ENV_VAR)
    if not source or not source.strip():
        return None
    return parse_timestamp(source)


def workflow_run_url(env: dict[str, str] | None = None) -> str | None:
    source = env if env is not None else dict(os.environ)
    repo, run_id = source.get("GITHUB_REPOSITORY"), source.get("GITHUB_RUN_ID")
    if not repo or not run_id:
        return None
    server = source.get("GITHUB_SERVER_URL") or "https://github.com"
    return f"{server}/{repo}/actions/runs/{run_id}"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    notes = Notes()
    try:
        return _run(args, notes)
    finally:
        # Printed here, and only here, so that observations recorded by the
        # write path -- a refused label, an unresolved issue -- reach the log
        # too. Flushing them before apply_action silently discarded them.
        for line in notes.lines:
            print(f"  note: {line}")


def _run(args: argparse.Namespace, notes: Notes) -> int:
    thresholds = Thresholds(
        base_open_minutes=args.base_open_minutes,
        deploy_margin_minutes=args.deploy_margin_minutes,
        max_deploy_window_minutes=args.max_deploy_window_minutes,
        close_at_or_below_minutes=args.close_at_or_below_minutes,
    )
    now = datetime.now(UTC)

    fetch = fetch_manifest_with_retries(args.manifest_url, now, notes=notes)
    verdict = evaluate(fetch, now, fresh_within_minutes=thresholds.close_at_or_below_minutes)
    print(f"{verdict.state.upper()}: {verdict.detail}")
    if verdict.published_at is not None:
        print(f"  published_at   {verdict.published_at.isoformat()} (clock: {verdict.reference_source})")
        print(f"  snapshot_id    {verdict.snapshot_id or 'unknown'}")
        print(f"  source_sha     {verdict.source_sha or 'unknown'}")

    if verdict.state == STATE_UNREADABLE:
        # Transient only: a monitor that paged on every Fastly hiccup would be
        # turned off within a week. The read was already retried; warn, exit 0,
        # and let the next run decide. A *structural* failure is STATE_BROKEN
        # and falls through to the alerting path below.
        print(f"::warning::publication manifest unreadable: {verdict.detail}")
        return 0
    if verdict.state == STATE_BROKEN:
        print(f"::error::published manifest is unusable: {verdict.detail}")
    if verdict.state == STATE_FUTURE:
        # Never silent: this state files nothing below the open threshold, and
        # an unannotated green run is how an outage goes unnoticed.
        print(f"::warning::publication clock fault: {verdict.detail}")

    deadline = mute_deadline(args.mute_until)
    muted = deadline is not None and now < deadline
    if muted and deadline is not None:
        notes.add(f"alerting is muted until {deadline.isoformat()} (${MUTE_ENV_VAR})")

    repo = args.repo or os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    deployment: DeploymentRecord | None = None
    issue: OpenIssue | None = None
    api = None
    if token:
        try:
            api = make_api(token)
            deployment = latest_deployment(api, repo, notes=notes)
            issue = find_open_issue(api, repo, notes=notes)
        except GitHubApiError as exc:
            print(f"::error::GitHub API failure: {exc}")
            return 1
    else:
        notes.add("no GH_TOKEN/GITHUB_TOKEN; skipping deployment and issue lookups")

    threshold = open_threshold(verdict, deployment, thresholds)
    action = plan_action(
        verdict, issue, threshold, thresholds.close_at_or_below_minutes, muted=muted
    )
    if action.kind == ACTION_CREATE and api is not None:
        # Only now is the extra read worth making: an issue closed by hand
        # minutes ago was an acknowledgement, not an invitation to file again.
        human_close_after = None
        if verdict.published_at is not None:
            # Later than this, a close cannot have been the monitor's own
            # recovery close -- which only ever fires below the close threshold.
            human_close_after = verdict.published_at + timedelta(
                minutes=thresholds.close_at_or_below_minutes
            )
        try:
            if recently_acknowledged(api, repo, now, human_close_after=human_close_after, notes=notes):
                action = plan_action(
                    verdict,
                    issue,
                    threshold,
                    thresholds.close_at_or_below_minutes,
                    muted=muted,
                    acknowledged=True,
                )
        except GitHubApiError as exc:
            print(f"::error::GitHub API failure: {exc}")
            return 1

    print(f"  open threshold {threshold:g} min")
    if deployment is not None:
        print(f"  deployment     id={deployment.identifier} phase={deployment.phase}")
    print(f"  open issue     {('#' + str(issue.number)) if issue else 'none'}")
    print(f"PLAN {action.kind}: {action.reason}")

    if args.dry_run:
        print("dry run: nothing was written")
        return 0
    if not token:
        if verdict.state == STATE_BROKEN:
            # Nothing can be filed, so the only remaining way to be heard is to
            # fail the run and let GitHub's own failure notification fire.
            print("::error::no GH_TOKEN available to report an unusable manifest")
            return 1
        print("::warning::no GH_TOKEN available; nothing was written")
        return 0

    try:
        apply_action(
            make_api(token),
            repo,
            action,
            verdict,
            deployment,
            issue,
            threshold,
            workflow_run_url=workflow_run_url(),
            notes=notes,
        )
    except GitHubApiError as exc:
        print(f"::error::GitHub API failure: {exc}")
        return 1
    if action.kind == ACTION_CREATE:
        print(f"opened '{repo}' issue: {action.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
