"""GitHub side of the off-box publication staleness monitor.

Everything that touches api.github.com lives here: the REST client, the two
read paths the monitor needs (the latest ``production-box`` deployment and its
own open issue), the issue prose, and the writes. The decision rules live in
``publication_freshness_core`` and never reach the network.

Standard library only, by design.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable, Iterator, Sequence

from publication_freshness_core import (
    ACK_AFTER_MANUAL_CLOSE_MINUTES,
    API_TIMEOUT_SECONDS,
    BUCKET_MARKER_TEMPLATE,
    DEPLOYMENT_ENVIRONMENT,
    ISSUE_LABEL,
    ISSUE_LABEL_COLOR,
    ISSUE_LABEL_DESCRIPTION,
    ISSUE_TITLE,
    MONITOR_MARKER,
    SNAPSHOT_ID_PATTERN,
    SOURCE_SHA_PATTERN,
    STATE_BROKEN,
    STATE_FUTURE,
    STATE_STALE,
    USER_AGENT,
    ACTION_CLOSE,
    ACTION_COMMENT,
    ACTION_CREATE,
    ACTION_NONE,
    Action,
    DeploymentRecord,
    GitHubApiError,
    Notes,
    OpenIssue,
    Verdict,
    coerce_float,
    escalation_bucket,
    parse_timestamp,
)

ApiCall = Callable[[str, str, Any], Any]

PAGE_SIZE = 100
# A repository with more than 500 open issues would hide this monitor's own
# issue from an unlabelled scan, and a hidden issue means a new duplicate every
# fifteen minutes for the length of the outage.
ISSUE_SCAN_MAX_PAGES = 5
# Comments come back oldest-first, so the newest bucket markers are on the last
# page; missing them replays an escalation comment every run.
COMMENT_SCAN_MAX_PAGES = 10


def make_api(token: str, *, opener: Callable[..., Any] = urllib.request.urlopen) -> ApiCall:
    """Return a ``(method, path, body) -> payload`` callable over api.github.com."""

    def call(method: str, path: str, body: Any = None) -> Any:
        url = path if path.startswith("http") else f"https://api.github.com{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {token}",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with opener(request, timeout=API_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except (OSError, ValueError, UnicodeDecodeError):
                detail = ""
            raise GitHubApiError(f"{method} {path} -> HTTP {exc.code} {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise GitHubApiError(f"{method} {path} -> {type(exc).__name__}: {exc}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubApiError(f"{method} {path} -> unreadable response: {exc}") from exc

    return call


# --- Reads ---


def _payload_object(raw: Any) -> dict[str, Any]:
    """A deployment ``payload`` arrives as an object, or as a JSON string."""

    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def latest_deployment(api: ApiCall, repo: str, *, notes: Notes | None = None) -> DeploymentRecord | None:
    """Most recent ``production-box`` deployment record, or ``None``.

    Nothing creates these records yet -- the deploy script learns to in a later
    PR -- so the common answer today is ``None``, and every caller must cope
    with that by falling back to the base threshold.
    """

    environment = urllib.parse.quote(DEPLOYMENT_ENVIRONMENT)
    payload = api("GET", f"/repos/{repo}/deployments?environment={environment}&per_page=1", None)
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return None
    record = payload[0]

    identifier = record.get("id") if isinstance(record.get("id"), int) else None
    expected = coerce_float(_payload_object(record.get("payload")).get("expected_dark_minutes"))
    environment_name = record.get("environment")

    state: str | None = None
    if identifier is not None:
        statuses = api("GET", f"/repos/{repo}/deployments/{identifier}/statuses?per_page=1", None)
        if isinstance(statuses, list) and statuses and isinstance(statuses[0], dict):
            raw_state = statuses[0].get("state")
            state = raw_state.strip().lower() if isinstance(raw_state, str) else None

    if notes is not None and expected is None:
        notes.add(
            f"deployment {identifier} carries no payload.expected_dark_minutes; "
            "assuming the full deploy window"
        )
    return DeploymentRecord(
        identifier=identifier,
        created_at=parse_timestamp(record.get("created_at")),
        expected_dark_minutes=expected,
        state=state,
        environment=environment_name if isinstance(environment_name, str) else None,
    )


def _issue_from_payload(payload: Any) -> OpenIssue | None:
    if not isinstance(payload, dict) or "pull_request" in payload:
        return None
    number = payload.get("number")
    if not isinstance(number, int):
        return None
    body = payload.get("body")
    return OpenIssue(number=number, body=body if isinstance(body, str) else "")


def _first_issue(payload: Any, predicate: Callable[[dict[str, Any]], bool]) -> OpenIssue | None:
    if not isinstance(payload, list):
        return None
    for entry in payload:
        if isinstance(entry, dict) and predicate(entry):
            issue = _issue_from_payload(entry)
            if issue is not None:
                return issue
    return None


def _looks_like_our_issue(entry: dict[str, Any]) -> bool:
    body, title = entry.get("body"), entry.get("title")
    if isinstance(body, str) and MONITOR_MARKER in body:
        return True
    return isinstance(title, str) and title.strip() == ISSUE_TITLE


def _pages(api: ApiCall, path: str, *, max_pages: int) -> Iterator[list[Any]]:
    """Walk a paginated list endpoint until it runs short or the cap is hit."""

    joiner = "&" if "?" in path else "?"
    for page in range(1, max_pages + 1):
        payload = api("GET", f"{path}{joiner}per_page={PAGE_SIZE}&page={page}", None)
        if not isinstance(payload, list) or not payload:
            return
        yield payload
        if len(payload) < PAGE_SIZE:
            return


def _scan_issues(api: ApiCall, path: str) -> OpenIssue | None:
    for page in _pages(api, path, max_pages=ISSUE_SCAN_MAX_PAGES):
        issue = _first_issue(page, _looks_like_our_issue)
        if issue is not None:
            return issue
    return None


def find_open_issue(api: ApiCall, repo: str, *, notes: Notes | None = None) -> OpenIssue | None:
    """Find this monitor's *own* open issue.

    The ``production-stale`` label does not exist in the repository yet, and a
    label filter naming a missing label simply matches nothing. So the label is
    the fast path and a title+marker scan is the fallback: the monitor must not
    lose track of its own issue -- and start filing duplicates every 15 minutes
    -- merely because a label was renamed or deleted.

    Identity is checked on *both* paths. Adopting whatever open issue happens to
    carry the label would let the monitor comment on, and eventually close, a
    human's issue, while its own stayed open and suppressed every later alert.
    """

    label = urllib.parse.quote(ISSUE_LABEL)
    found = _scan_issues(
        api, f"/repos/{repo}/issues?state=open&labels={label}&sort=created&direction=desc"
    )
    if found is None:
        found = _scan_issues(api, f"/repos/{repo}/issues?state=open&sort=created&direction=desc")
        if found is not None and notes is not None:
            notes.add(
                f"issue #{found.number} matched without the '{ISSUE_LABEL}' label; "
                "the label may be missing"
            )
    if found is None:
        return None

    bodies: list[str] = []
    for page in _pages(api, f"/repos/{repo}/issues/{found.number}/comments", max_pages=COMMENT_SCAN_MAX_PAGES):
        bodies.extend(
            comment["body"]
            for comment in page
            if isinstance(comment, dict) and isinstance(comment.get("body"), str)
        )
    return OpenIssue(number=found.number, body=found.body, comment_bodies=tuple(bodies))


def recently_acknowledged(
    api: ApiCall,
    repo: str,
    now: datetime,
    *,
    human_close_after: datetime | None = None,
    within_minutes: float = ACK_AFTER_MANUAL_CLOSE_MINUTES,
    notes: Notes | None = None,
) -> bool:
    """True when a *human* closed this monitor's issue in the recent past.

    Closing the issue during an ongoing outage is an acknowledgement -- "seen,
    I am on it" -- not a recovery. Without this the monitor files a fresh
    duplicate on the next tick and replays every escalation comment onto it.

    ``human_close_after`` is what keeps the monitor's own auto-closes out of
    that count, which matters when publication flaps: the monitor only ever
    closes while the snapshot is younger than the close threshold, so a close
    later than ``published_at + close_threshold`` cannot have been its own. With
    no publish timestamp to reckon from (an unreadable manifest) the time window
    stands alone, and a recovery inside it can suppress for up to that window.

    Called only when the plan is otherwise to create, so a healthy run pays
    nothing for it.
    """

    label = urllib.parse.quote(ISSUE_LABEL)
    cutoff = now - timedelta(minutes=within_minutes)
    for path in (
        f"/repos/{repo}/issues?state=closed&labels={label}&sort=updated&direction=desc",
        f"/repos/{repo}/issues?state=closed&sort=updated&direction=desc",
    ):
        for page in _pages(api, path, max_pages=1):
            for entry in page:
                if not isinstance(entry, dict) or "pull_request" in entry:
                    continue
                if not _looks_like_our_issue(entry):
                    continue
                closed_at = parse_timestamp(entry.get("closed_at"))
                if closed_at is None or closed_at < cutoff:
                    continue
                if human_close_after is not None and closed_at <= human_close_after:
                    # This monitor's own recovery close, not a human's.
                    continue
                if notes is not None:
                    notes.add(
                        f"issue #{entry.get('number')} was closed at {closed_at.isoformat()}; "
                        "treating that as an acknowledgement rather than filing a duplicate"
                    )
                return True
    return False


# --- Issue prose ---


def format_age(age_minutes: float | None) -> str:
    if age_minutes is None:
        return "unknown"
    hours, minutes = divmod(int(round(age_minutes)), 60)
    return f"{hours} h {minutes} min ({age_minutes:.0f} min)"


def _published(verdict: Verdict) -> str:
    return verdict.published_at.isoformat() if verdict.published_at else "unknown"


def _code(value: str | None, pattern: Any) -> str:
    """Render a manifest-sourced identifier, or refuse it.

    The manifest is network input. Only the exact shape the publisher promises
    reaches an issue body: an unvalidated string could break out of its code
    span to inject Markdown, or smuggle a counterfeit
    ``<!-- freshness-bucket -->`` marker that silences escalation permanently.
    """

    if value is None:
        return "unknown"
    if isinstance(value, str) and pattern.match(value):
        return value
    return "invalid"


def _safe_text(value: str | None, *, limit: int = 200) -> str:
    """Bound and de-fang free text (an error string) before it is published."""

    if not value:
        return "unknown"
    return value.replace("<!--", "<!-").replace("`", "'")[:limit]


HEADLINES = {
    STATE_STALE: (
        "The published site snapshot has stopped updating."
    ),
    STATE_BROKEN: (
        "The published manifest could not be read at all -- the Pages build, the "
        "`gh-pages` branch or the manifest path may be gone. The site being *missing* "
        "is a worse outage than the site being stale."
    ),
    STATE_FUTURE: (
        "The published manifest is stamped in the future, so the freshness signal "
        "cannot be trusted: the publishing box's clock is wrong. The box cannot catch "
        "this itself -- it validates its own timestamps against that same clock."
    ),
}


# Each state's own first move, before the shared quiesced-box checks. A 404 is
# not a timer problem, and a bad timestamp is not a publishing problem.
TRIAGE_LEADS = {
    STATE_BROKEN: (
        "Open the manifest URL yourself, then check the repository's Pages build "
        "(the `pages-build-deployment` run). `gh-pages` is force-re-rooted roughly "
        "every ten days (`SFO_PAGES_HISTORY_MAX_COMMITS`), and the deploy runbook's "
        "post-deploy watch says to re-verify that the manifest still loads after one.",
        "If Pages is serving but the document is not ours, the last publish wrote "
        "something unexpected: read `sfo-operational-publish` on the box.",
    ),
    STATE_FUTURE: (
        "Check the box's clock first: `timedatectl` on the host. The publisher "
        "validates its own timestamps against that same clock, so it cannot catch "
        "this itself -- every on-box freshness check will look healthy.",
    ),
}

SHARED_TRIAGE_STEPS = (
    "Assume a deploy died mid-flight and left the box quiesced -- that is what "
    "caused both multi-hour outages this month. Check the timers and the "
    "maintenance marker: `systemctl list-timers 'sfo-*'` and "
    "`test -e /run/weatheredge-deploy-maintenance` on the box.",
    "If the timers are disabled, restore them with "
    "`bash trading/deploy/aws/disable_systemd_timers.sh restore` -- producers "
    "first, `sfo-scheduler-health.timer` last -- and then "
    "`sudo rm -f /run/weatheredge-deploy-maintenance`. Leaving the marker in place "
    "keeps the scheduler watchdog suppressed, so the box still will not publish.",
    "If the box itself is unreachable, confirm the instance is running before "
    "assuming a software fault.",
    "Once publishing resumes, this issue closes itself on the next run.",
)


def issue_body(
    verdict: Verdict,
    deployment: DeploymentRecord | None,
    threshold_minutes: float,
    *,
    workflow_run_url: str | None,
) -> str:
    lines = [MONITOR_MARKER]
    # Stamp the bucket the outage is already in, so an issue opened at, say, 9 h
    # (a deployment record having raised the threshold) does not post a "still
    # stale at the 6 h mark" comment fifteen minutes later.
    opening_bucket = escalation_bucket(verdict.age_minutes) if verdict.state == STATE_STALE else None
    if opening_bucket is not None:
        lines.append(BUCKET_MARKER_TEMPLATE.format(bucket=opening_bucket))
    lines += [
        HEADLINES.get(verdict.state, HEADLINES[STATE_STALE])
        + " This check runs on GitHub Actions, off the production box, so it still "
        "reports when the box, the build Mac and the owner's laptop are all unreachable.",
        "",
        f"- **Age**: {format_age(verdict.age_minutes)}",
        f"- **manifest `published_at`**: `{_published(verdict)}`",
        f"- **Open threshold**: {threshold_minutes:g} min",
        f"- **Clock used**: {verdict.reference_source}",
        f"- **snapshot_id**: `{_code(verdict.snapshot_id, SNAPSHOT_ID_PATTERN)}`",
        f"- **provenance.source_sha**: `{_code(verdict.source_sha, SOURCE_SHA_PATTERN)}`",
    ]
    if verdict.state in (STATE_BROKEN, STATE_FUTURE):
        lines.append(f"- **Detail**: {_safe_text(verdict.detail)}")
    if deployment is not None:
        lines.append(
            f"- **Deployment**: id `{deployment.identifier}` "
            f"({deployment.environment or DEPLOYMENT_ENVIRONMENT}), phase `{deployment.phase}`"
        )
    else:
        lines.append("- **Deployment**: no `production-box` deployment record covers this window")
    if workflow_run_url:
        lines.append(f"- **Workflow run**: {workflow_run_url}")

    lines += [
        "",
        "### Triage",
        "",
        'Follow `trading/deploy/aws/README.md`, "Release Deploy And Rollback", '
        "**Phase 0: confirm the host state (read-only)** -- it classifies the host as "
        "healthy, stranded or deliberately paused, and gives the ordering constraints "
        "a bare restore gets wrong.",
        "",
    ]
    steps = [*TRIAGE_LEADS.get(verdict.state, ()), *SHARED_TRIAGE_STEPS]
    lines += [f"{number}. {step}" for number, step in enumerate(steps, start=1)]
    lines += ["", "cc @Jaxsonb04"]
    return "\n".join(lines)


def comment_body(bucket_hours: int, verdict: Verdict, *, workflow_run_url: str | None) -> str:
    lines = [
        BUCKET_MARKER_TEMPLATE.format(bucket=bucket_hours),
        f"Still stale at the **{bucket_hours} h** mark: age {format_age(verdict.age_minutes)}, "
        f"manifest `published_at` `{_published(verdict)}`.",
    ]
    if workflow_run_url:
        lines.append(f"Workflow run: {workflow_run_url}")
    lines += ["", "cc @Jaxsonb04"]
    return "\n".join(lines)


def close_comment_body(verdict: Verdict, *, workflow_run_url: str | None) -> str:
    lines = [
        MONITOR_MARKER,
        f"Publication recovered: age {format_age(verdict.age_minutes)}, "
        f"`published_at` `{_published(verdict)}`, "
        f"snapshot `{_code(verdict.snapshot_id, SNAPSHOT_ID_PATTERN)}`. Closing automatically.",
    ]
    if workflow_run_url:
        lines.append(f"Workflow run: {workflow_run_url}")
    return "\n".join(lines)


# --- Writes ---


def ensure_label(api: ApiCall, repo: str, *, notes: Notes | None = None) -> bool:
    """Create the ``production-stale`` label when it is missing.

    Returns True when the label exists and may be attached to a new issue. A
    repository where label creation is refused still gets its issue, just
    unlabelled: an unlabelled alert beats no alert.
    """

    try:
        existing = api("GET", f"/repos/{repo}/labels/{urllib.parse.quote(ISSUE_LABEL)}", None)
    except GitHubApiError:
        existing = None
    if isinstance(existing, dict) and existing.get("name"):
        return True
    try:
        api(
            "POST",
            f"/repos/{repo}/labels",
            {"name": ISSUE_LABEL, "color": ISSUE_LABEL_COLOR, "description": ISSUE_LABEL_DESCRIPTION},
        )
        return True
    except GitHubApiError as exc:
        if notes is not None:
            notes.add(f"could not create the '{ISSUE_LABEL}' label ({exc}); filing unlabelled")
        return False


def apply_action(
    api: ApiCall,
    repo: str,
    action: Action,
    verdict: Verdict,
    deployment: DeploymentRecord | None,
    issue: OpenIssue | None,
    threshold_minutes: float,
    *,
    workflow_run_url: str | None,
    notes: Notes | None = None,
) -> None:
    """Perform the planned mutation. Called only outside ``--dry-run``."""

    if action.kind == ACTION_NONE:
        return

    if action.kind == ACTION_CREATE:
        body = issue_body(verdict, deployment, threshold_minutes, workflow_run_url=workflow_run_url)
        payload: dict[str, Any] = {"title": ISSUE_TITLE, "body": body}
        if ensure_label(api, repo, notes=notes):
            payload["labels"] = [ISSUE_LABEL]
        api("POST", f"/repos/{repo}/issues", payload)
        return

    if issue is None:
        if notes is not None:
            notes.add(f"planned '{action.kind}' but no open issue was resolved; skipping")
        return

    if action.kind == ACTION_COMMENT and action.bucket_hours is not None:
        body = comment_body(action.bucket_hours, verdict, workflow_run_url=workflow_run_url)
        api("POST", f"/repos/{repo}/issues/{issue.number}/comments", {"body": body})
        return

    if action.kind == ACTION_CLOSE:
        # Close first, explain second. The other order re-posts an identical
        # "Publication recovered" comment on every run for as long as the PATCH
        # keeps failing; this way a failed comment leaves a closed issue and the
        # next run finds nothing open to repeat itself on.
        api("PATCH", f"/repos/{repo}/issues/{issue.number}", {"state": "closed", "state_reason": "completed"})
        api(
            "POST",
            f"/repos/{repo}/issues/{issue.number}/comments",
            {"body": close_comment_body(verdict, workflow_run_url=workflow_run_url)},
        )
        return

    raise ValueError(f"unknown action kind: {action.kind}")


__all__: Sequence[str] = (
    "ApiCall",
    "apply_action",
    "close_comment_body",
    "comment_body",
    "ensure_label",
    "find_open_issue",
    "format_age",
    "issue_body",
    "latest_deployment",
    "make_api",
    "recently_acknowledged",
)
