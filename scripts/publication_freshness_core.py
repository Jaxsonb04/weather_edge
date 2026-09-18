"""Decision core for the off-box publication staleness monitor.

Pure and offline: this module reads the published manifest over HTTPS and
decides *what should happen*. It never talks to GitHub, so every rule in it --
what counts as stale, how long a deploy may suppress an alert, when to escalate
and when to close -- is testable with plain values and no fakes at all.

See ``scripts/check_publication_freshness.py`` for why this monitor exists.
Standard library only, by design: no ``pip install`` may stand between an
outage and the alert.
"""

from __future__ import annotations

import http.client
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable

DEFAULT_REPO = "Jaxsonb04/weather_edge"
DEFAULT_MANIFEST_URL = "https://jaxsonb04.github.io/weather_edge/publication_manifest.json"

USER_AGENT = "weatheredge-freshness-monitor"
MANIFEST_TIMEOUT_SECONDS = 20
API_TIMEOUT_SECONDS = 20
CACHE_BUSTER_PARAM = "freshness_probe"

# One failed read can mean a Fastly blip or a DNS wobble rather than an outage.
# Retrying inside the run costs seconds of a five-minute budget and keeps a
# single hiccup from throwing away a whole 15-minute detection cycle.
MANIFEST_FETCH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 15.0
# 404/410 is not a hiccup: the manifest path, the Pages build or the branch is
# gone. gh-pages is force-re-rooted roughly every ten days, so this is a routine
# way for the site to disappear -- and a site that is GONE is a worse outage
# than one that is merely stale. Timeouts, DNS and 5xx stay transient.
STRUCTURAL_HTTP_STATUSES = frozenset({404, 410})

# Runner clock and origin clock disagreeing by more than this means one of them
# is wrong; GitHub Pages' own `Date` header is then the better authority.
CLOCK_SKEW_TOLERANCE_SECONDS = 300
# A publish timestamp this far ahead of "now" is not data, it is a broken clock.
FUTURE_TOLERANCE_MINUTES = 5

# An operator closing the issue by hand during an ongoing outage is an
# acknowledgement, not a recovery. Without this the monitor files a duplicate on
# the very next tick and replays every escalation comment onto it.
ACK_AFTER_MANUAL_CLOSE_MINUTES = 360

# The publisher writes and validates both of these itself (publication.py).
# The manifest is network input, so only the exact shape it promises reaches an
# issue body: a crafted `snapshot_id` could otherwise inject Markdown, or a
# counterfeit `<!-- freshness-bucket -->` marker that silences escalation for
# good.
SNAPSHOT_ID_PATTERN = re.compile(r"\A[0-9a-f]{24}\Z")
SOURCE_SHA_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z")

DEPLOYMENT_ENVIRONMENT = "production-box"
# A deployment only explains a dark window it could plausibly have caused: it
# must have started no earlier than shortly before the last successful publish.
DEPLOYMENT_LOOKBACK_MINUTES = 20
# A deployment whose status already says it broke suppresses nothing -- that is
# the "deploy died mid-flight" case, and it wants an issue fast.
FAILED_DEPLOY_OPEN_MINUTES = 20
TERMINAL_FAILURE_STATES = frozenset({"failure", "error"})
TERMINAL_SUCCESS_STATES = frozenset({"success", "inactive"})

ISSUE_TITLE = "Production publication is stale"
ISSUE_LABEL = "production-stale"
ISSUE_LABEL_COLOR = "b60205"
ISSUE_LABEL_DESCRIPTION = "Published site snapshot stopped updating"
# Lets the monitor find its own issue even if the label is missing or renamed.
MONITOR_MARKER = "<!-- freshness-monitor -->"
BUCKET_MARKER_TEMPLATE = "<!-- freshness-bucket:{bucket} -->"
BUCKET_MARKER_PATTERN = re.compile(r"<!--\s*freshness-bucket:(\d+)\s*-->")

# Escalate at 6 h, 12 h, 24 h, then once a day.
EARLY_ESCALATION_BUCKET_HOURS = (6, 12)
DAILY_ESCALATION_BUCKET_HOURS = 24

DEFAULT_BASE_OPEN_MINUTES = 90
DEFAULT_DEPLOY_MARGIN_MINUTES = 45
DEFAULT_MAX_DEPLOY_WINDOW_MINUTES = 480
DEFAULT_CLOSE_AT_OR_BELOW_MINUTES = 20

STATE_FRESH = "fresh"
STATE_STALE = "stale"
# Transient: one read failed, the next one may not. Warn, do not alert.
STATE_UNREADABLE = "unreadable"
# Structural: the manifest is gone or unusable. A dead site is a worse outage
# than a stale one, so this alerts rather than warning forever on a green job.
STATE_BROKEN = "broken"
STATE_FUTURE = "future"

ACTION_CREATE = "create"
ACTION_COMMENT = "comment"
ACTION_CLOSE = "close"
ACTION_NONE = "none"


class GitHubApiError(RuntimeError):
    """A GitHub REST call failed. This is the only condition that exits 1."""


@dataclass(frozen=True)
class Thresholds:
    """Tunables, threaded through explicitly so tests never touch argv."""

    base_open_minutes: float = DEFAULT_BASE_OPEN_MINUTES
    deploy_margin_minutes: float = DEFAULT_DEPLOY_MARGIN_MINUTES
    max_deploy_window_minutes: float = DEFAULT_MAX_DEPLOY_WINDOW_MINUTES
    close_at_or_below_minutes: float = DEFAULT_CLOSE_AT_OR_BELOW_MINUTES

    @property
    def suppression_ceiling_minutes(self) -> float:
        """No deployment record may buy silence past this. Default 525 min."""

        return self.max_deploy_window_minutes + self.deploy_margin_minutes


@dataclass(frozen=True)
class ManifestFetch:
    """Outcome of one manifest read: the payload plus the origin's own clock."""

    url: str
    manifest: dict[str, Any] | None = None
    http_date: datetime | None = None
    http_age_seconds: float | None = None
    cache_hit: bool = False
    error: str | None = None
    # True when the failure cannot heal on its own: the manifest is missing or
    # is not the document we publish.
    structural: bool = False


@dataclass(frozen=True)
class Verdict:
    state: str
    detail: str
    age_minutes: float | None = None
    published_at: datetime | None = None
    snapshot_id: str | None = None
    source_sha: str | None = None
    reference_now: datetime | None = None
    reference_source: str = "runner-clock"


@dataclass(frozen=True)
class DeploymentRecord:
    identifier: int | None = None
    created_at: datetime | None = None
    expected_dark_minutes: float | None = None
    state: str | None = None
    environment: str | None = None

    @property
    def phase(self) -> str:
        return self.state or "no status recorded"


@dataclass(frozen=True)
class OpenIssue:
    number: int
    body: str = ""
    comment_bodies: tuple[str, ...] = ()

    @property
    def texts(self) -> tuple[str, ...]:
        return (self.body, *self.comment_bodies)


@dataclass(frozen=True)
class Action:
    kind: str
    reason: str
    bucket_hours: int | None = None


@dataclass
class Notes:
    """Non-fatal observations worth printing but not worth failing over."""

    lines: list[str] = field(default_factory=list)

    def add(self, line: str) -> None:
        self.lines.append(line)


# --- Parsing helpers ---


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def parse_http_date(value: str | None) -> datetime | None:
    """Parse an RFC 7231 ``Date`` header; the origin's clock beats the runner's."""

    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def coerce_float(value: Any) -> float | None:
    """Accept the int, float or numeric string a deploy payload might carry."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


# --- Manifest ---


def cache_busting_url(url: str, now: datetime) -> str:
    """Append a per-run probe so no CDN edge can answer with a stale body.

    GitHub Pages serves `cache-control: max-age=600` through Fastly, so a
    15-minute poll could otherwise be shown a manifest that is already gone.
    """

    parts = urllib.parse.urlsplit(url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key != CACHE_BUSTER_PARAM
    ]
    query.append((CACHE_BUSTER_PARAM, str(int(now.timestamp()))))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def response_clock(headers: Any) -> tuple[datetime | None, float | None, bool]:
    """Pull the origin's clock out of the response: ``Date``, ``Age``, hit/miss."""

    if headers is None:
        return None, None, False
    http_date = parse_http_date(headers.get("Date"))
    age_seconds = coerce_float(headers.get("Age"))
    if age_seconds is not None and age_seconds < 0:
        age_seconds = None
    cache_hit = any(
        "hit" in str(headers.get(name) or "").lower()
        for name in ("X-Cache", "X-Proxy-Cache")
    )
    return http_date, age_seconds, cache_hit


def fetch_manifest(
    url: str,
    now: datetime,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> ManifestFetch:
    """Read the published manifest. Never raises; failures come back as data."""

    probe_url = cache_busting_url(url, now)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    request = urllib.request.Request(probe_url, headers=headers)
    try:
        with opener(request, timeout=MANIFEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
            http_date, age_seconds, cache_hit = response_clock(getattr(response, "headers", None))
    except urllib.error.HTTPError as exc:
        # Caught ahead of URLError, its own base class, to read the status:
        # 404/410 says the site is gone, everything else may still heal.
        return ManifestFetch(
            url=probe_url,
            error=f"HTTP {exc.code} {exc.reason}",
            structural=exc.code in STRUCTURAL_HTTP_STATUSES,
        )
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        # http.client.HTTPException (BadStatusLine, LineTooLong, InvalidURL) is
        # neither OSError nor ValueError; uncaught it would crash the job.
        return ManifestFetch(url=probe_url, error=f"{type(exc).__name__}: {exc}")

    clock = {"http_date": http_date, "http_age_seconds": age_seconds, "cache_hit": cache_hit}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # A failed Pages build serves its 404 page with a 200, so a body that is
        # not JSON is the site being broken, not a transport hiccup.
        error = f"manifest is not valid JSON: {type(exc).__name__}: {exc}"
        return ManifestFetch(url=probe_url, error=error, structural=True, **clock)
    if not isinstance(payload, dict):
        error = f"manifest root is {type(payload).__name__}, expected object"
        return ManifestFetch(url=probe_url, error=error, structural=True, **clock)
    return ManifestFetch(url=probe_url, manifest=payload, **clock)


def fetch_manifest_with_retries(
    url: str,
    now: datetime,
    *,
    attempts: int = MANIFEST_FETCH_ATTEMPTS,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleeper: Callable[[float], Any] = time.sleep,
    notes: Notes | None = None,
) -> ManifestFetch:
    """Read the manifest, retrying a transient failure inside the same run.

    A structural failure is returned immediately: a 404 will still be a 404 in
    fifteen seconds, and the monitor alerts on it rather than retrying it.
    """

    total = max(1, attempts)
    fetch = fetch_manifest(url, now, opener=opener)
    for attempt in range(2, total + 1):
        if fetch.error is None or fetch.structural:
            return fetch
        if notes is not None:
            notes.add(f"manifest read {attempt - 1}/{total} failed ({fetch.error}); retrying")
        sleeper(RETRY_BACKOFF_SECONDS)
        # Nudge the probe value so a retry cannot be answered from whatever
        # answered the attempt that just failed.
        fetch = fetch_manifest(url, now + timedelta(seconds=attempt), opener=opener)
    return fetch


def origin_clock(fetch: ManifestFetch) -> datetime | None:
    """The origin's own "now", corrected for CDN caching, or ``None``.

    ``Date`` on a cache HIT is when the object was cached, not now. Believing it
    under-reports the age by the whole cache lifetime -- enough to close a live
    outage's issue -- so ``Age`` is added back, and a hit that cannot be
    corrected is refused as a clock rather than trusted.
    """

    if fetch.http_date is None:
        return None
    if fetch.http_age_seconds is not None:
        return fetch.http_date + timedelta(seconds=fetch.http_age_seconds)
    return None if fetch.cache_hit else fetch.http_date


def evaluate(
    fetch: ManifestFetch,
    runner_now: datetime,
    *,
    fresh_within_minutes: float = DEFAULT_CLOSE_AT_OR_BELOW_MINUTES,
) -> Verdict:
    """Classify the published snapshot as fresh / stale / unreadable / future.

    The authoritative publish timestamp is the manifest's top-level
    ``published_at``. It is deliberately *not* any artifact's ``generated_at``:
    several artifacts (``forecast_data.json``, ``weather_story_data.json``) are
    legitimately months old and preserved across publishes, so reading those
    would report a two-month outage on a perfectly healthy site.
    """

    if fetch.error is not None:
        state = STATE_BROKEN if fetch.structural else STATE_UNREADABLE
        return Verdict(state=state, detail=fetch.error)

    manifest = fetch.manifest or {}
    published_at = parse_timestamp(manifest.get("published_at"))
    if published_at is None:
        # The document parsed but is not the manifest we publish. That is the
        # site being broken, and no later run can heal it on its own.
        return Verdict(
            state=STATE_BROKEN,
            detail="manifest has no parseable top-level published_at",
        )

    snapshot_id = manifest.get("snapshot_id")
    provenance = manifest.get("provenance")
    source_sha = provenance.get("source_sha") if isinstance(provenance, dict) else None

    reference_now = runner_now.astimezone(UTC)
    reference_source = "runner-clock"
    origin_now = origin_clock(fetch)
    if origin_now is not None:
        if abs((reference_now - origin_now).total_seconds()) > CLOCK_SKEW_TOLERANCE_SECONDS:
            reference_now = origin_now
            reference_source = "http-date"

    age_minutes = (reference_now - published_at).total_seconds() / 60.0
    common = {
        "age_minutes": age_minutes,
        "published_at": published_at,
        "snapshot_id": snapshot_id if isinstance(snapshot_id, str) else None,
        "source_sha": source_sha if isinstance(source_sha, str) else None,
        "reference_now": reference_now,
        "reference_source": reference_source,
    }

    if age_minutes < -FUTURE_TOLERANCE_MINUTES:
        detail = (
            f"published_at is {abs(age_minutes):.1f} min in the future against the "
            f"{reference_source}; refusing to act on an untrusted clock"
        )
        return Verdict(state=STATE_FUTURE, detail=detail, **common)

    state = STATE_FRESH if age_minutes <= fresh_within_minutes else STATE_STALE
    return Verdict(state=state, detail=f"published {age_minutes:.1f} min ago", **common)


# --- Thresholds ---


def deployment_covers_window(verdict: Verdict, deployment: DeploymentRecord | None) -> bool:
    """True when this record could plausibly explain the current dark window."""

    if deployment is None or deployment.created_at is None or verdict.published_at is None:
        return False
    earliest = verdict.published_at - timedelta(minutes=DEPLOYMENT_LOOKBACK_MINUTES)
    return deployment.created_at >= earliest


def open_threshold(
    verdict: Verdict,
    deployment: DeploymentRecord | None,
    thresholds: Thresholds,
) -> float:
    """Minutes of staleness required before an issue is opened."""

    base = thresholds.base_open_minutes
    if deployment is None or not deployment_covers_window(verdict, deployment):
        return base
    if deployment.state in TERMINAL_FAILURE_STATES:
        # The deploy already told us it broke. Do not wait out its dark window.
        return float(FAILED_DEPLOY_OPEN_MINUTES)
    if deployment.state in TERMINAL_SUCCESS_STATES:
        # It finished. Anything still dark afterwards is an ordinary outage.
        return base

    expected = deployment.expected_dark_minutes
    if expected is None or expected < 0:
        # A record that declares nothing gets a typical window, never the
        # maximum: defaulting to the ceiling would hand the exact failure this
        # monitor exists for -- a deploy that dies without ever posting a
        # terminal status -- 8 h 45 min of silence instead of 90 minutes.
        expected = base
    # Clamped, then floored at the base, so a record may only ever RAISE the
    # bar. The clamp bounds the result at `thresholds.suppression_ceiling_
    # minutes` (525 min by default) whatever the payload claims; the floor stops
    # a deploy that honestly declares a 5-minute window from LOWERING the bar to
    # 50 minutes and filing an issue the base threshold never would have.
    claimed = min(expected, thresholds.max_deploy_window_minutes)
    return max(base, claimed + thresholds.deploy_margin_minutes)


# --- Planning ---


def escalation_bucket(age_minutes: float) -> int | None:
    """Escalation bucket in whole hours: 6, 12, 24, then one per further day."""

    age_hours = age_minutes / 60.0
    if age_hours >= DAILY_ESCALATION_BUCKET_HOURS:
        return int(age_hours // DAILY_ESCALATION_BUCKET_HOURS) * DAILY_ESCALATION_BUCKET_HOURS
    for bucket in reversed(EARLY_ESCALATION_BUCKET_HOURS):
        if age_hours >= bucket:
            return bucket
    return None


def markers_in(text: str) -> Iterable[int]:
    return (int(match) for match in BUCKET_MARKER_PATTERN.findall(text or ""))


def posted_buckets(issue: OpenIssue | None) -> frozenset[int]:
    if issue is None:
        return frozenset()
    return frozenset(bucket for text in issue.texts for bucket in markers_in(text))


def _plan_alert_only(
    issue: OpenIssue | None,
    *,
    reason: str,
    muted: bool,
    acknowledged: bool,
) -> Action:
    """File one issue, then stay quiet.

    Used for the states that carry no usable age: there is nothing to escalate
    on and, crucially, nothing that counts as a recovery, so these never close.
    """

    if issue is not None:
        return Action(ACTION_NONE, f"{reason}; issue already open")
    if muted:
        return Action(ACTION_NONE, f"{reason}; alerting is muted")
    if acknowledged:
        return Action(ACTION_NONE, f"{reason}; recently closed by hand")
    return Action(ACTION_CREATE, reason)


def plan_action(
    verdict: Verdict,
    issue: OpenIssue | None,
    threshold_minutes: float,
    close_at_or_below_minutes: float,
    *,
    muted: bool = False,
    acknowledged: bool = False,
) -> Action:
    """Decide what to do. Pure: no I/O, so every branch is cheap to test."""

    if verdict.state == STATE_UNREADABLE:
        return Action(ACTION_NONE, "manifest unreadable; warning only")

    if verdict.state == STATE_BROKEN:
        # The published manifest is gone or is not our document. Warning about
        # that forever on a green job is the "nobody noticed" failure again.
        return _plan_alert_only(
            issue,
            reason=f"published manifest is unusable: {verdict.detail}",
            muted=muted,
            acknowledged=acknowledged,
        )

    if verdict.state == STATE_FUTURE:
        # Never acted on as freshness -- the clock is not trustworthy -- but a
        # publish timestamp further ahead than the open threshold is itself a
        # fault worth an issue, and the box cannot catch it with its own clock.
        ahead = -(verdict.age_minutes or 0.0)
        if ahead < threshold_minutes:
            return Action(ACTION_NONE, "published_at is in the future; refusing to act")
        return _plan_alert_only(
            issue,
            reason=f"published_at is {ahead:.1f} min in the future; the publishing clock is wrong",
            muted=muted,
            acknowledged=acknowledged,
        )

    age = verdict.age_minutes
    if age is None:
        return Action(ACTION_NONE, "no age could be computed")

    if verdict.state == STATE_FRESH:
        if issue is None:
            return Action(ACTION_NONE, "publication is fresh and no issue is open")
        if age <= close_at_or_below_minutes:
            # Recovery closes even while muted: a mute silences alarms, it does
            # not keep a resolved issue open.
            return Action(ACTION_CLOSE, f"recovered: age {age:.1f} min <= {close_at_or_below_minutes:g} min")
        return Action(ACTION_NONE, f"age {age:.1f} min is above the close threshold")

    if issue is None:
        if age < threshold_minutes:
            return Action(ACTION_NONE, f"stale {age:.1f} min, below open threshold {threshold_minutes:g} min")
        if muted:
            return Action(ACTION_NONE, f"stale {age:.1f} min but alerting is muted")
        if acknowledged:
            return Action(ACTION_NONE, f"stale {age:.1f} min; a monitor issue was closed by hand recently")
        return Action(ACTION_CREATE, f"stale {age:.1f} min >= open threshold {threshold_minutes:g} min")

    bucket = escalation_bucket(age)
    if bucket is None:
        return Action(ACTION_NONE, "issue already open; no escalation bucket reached")
    if bucket in posted_buckets(issue):
        return Action(ACTION_NONE, f"issue already open; {bucket}h escalation already posted")
    if muted:
        return Action(ACTION_NONE, f"{bucket}h escalation suppressed while alerting is muted")
    return Action(ACTION_COMMENT, f"escalating to the {bucket}h bucket", bucket_hours=bucket)
