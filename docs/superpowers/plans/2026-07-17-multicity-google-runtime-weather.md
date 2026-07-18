# Multi-City Google Runtime Weather Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Store fresh Google Weather content in an expiring city-aware SQLite runtime database, produce a fixed research-only Google-conditioned forecast for all 15 cities, and remain below WeatherEdge's 8,000-event monthly budget.

**Architecture:** Keep permanent non-Google baselines and request accounting in `weather.db`; store parsed Google content in `/run/weatheredge/google_runtime.db` with enforced TTLs. SFO dual-runs through a compatibility adapter without changing the served live forecast. Other cities produce a paired EMOS baseline and fixed 15%-share, ±1.5°F-capped research challenger; only derived challenger outputs persist for settlement scoring.

**Tech Stack:** Python 3.13+, SQLite, Google Weather REST API, systemd, pytest

---

## File structure

- Create `forecaster/google_weather_store.py` — permanent event ledger, expiring runtime schema, TTL queries, and purge.
- Create `forecaster/google_runtime_blend.py` — fixed-standard daily-high coverage and fixed challenger formula.
- Modify `forecaster/weather_cache_config.py` — paths, immutable TTL maxima, page cap, and soft/hard budgets.
- Modify `forecaster/google_api.py` — city-aware request/parse functions and per-dispatch accounting hooks.
- Modify `forecaster/google_weather_cache.py` — budget-aware all-city orchestration.
- Modify `forecaster/blend_sources.py` and `forecaster/blend_archive.py` — paired baseline/runtime shadow output.
- Modify trading forecast/decision persistence to keep Google raw fields ephemeral and derived evidence versioned.
- Modify publication validators and AWS systemd templates.
- Add `forecaster/tests/test_google_weather_store.py`, `test_google_multicity.py`, `test_google_api.py`, `test_settlement_calendar.py`, `test_blend_sources.py`, and `test_blend_archive.py`, plus focused trading/publication tests.

### Task 1: Define immutable Google runtime configuration

**Files:**
- Modify: `forecaster/weather_cache_config.py`
- Test: `forecaster/tests/test_google_weather_store.py`

- [ ] **Step 1: Add configuration-boundary tests**

Add named tests that set larger environment TTLs and assert the official maxima
win, request a fourth hourly page and assert dispatch stops at three, and assert
the daily/monthly/soft budget constants equal 260/8,000/7,800.

- [ ] **Step 2: Verify constants/path policy do not exist**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_weather_store.py`

Expected: FAIL.

- [ ] **Step 3: Add fixed maxima and injected paths**

```python
GOOGLE_RUNTIME_DB_PATH = Path(os.getenv("GOOGLE_RUNTIME_DB_PATH", "/run/weatheredge/google_runtime.db"))
GOOGLE_HOURLY_TTL = timedelta(hours=1)
GOOGLE_CURRENT_TTL = timedelta(hours=1)
GOOGLE_TODAY_DAILY_TTL = timedelta(days=30)
GOOGLE_FUTURE_DAILY_TTL = timedelta(hours=24)
GOOGLE_HOURLY_MAX_PAGES = 3
GOOGLE_WEATHER_DAILY_EVENT_BUDGET = 260
GOOGLE_WEATHER_MONTHLY_EVENT_BUDGET = 8000
GOOGLE_WEATHER_SOFT_MONTHLY_CEILING = 7800
```

Environment values may reduce TTL/budget values but `min(configured, official_max)` prevents extension. Production startup validates that the runtime DB is under `/run/weatheredge`; unit tests inject a temporary path explicitly.

- [ ] **Step 4: Run configuration tests and commit**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_weather_store.py`

Expected: PASS.

```bash
git add forecaster/weather_cache_config.py forecaster/tests/test_google_weather_store.py
git commit -m "feat: define Google runtime limits"
```

### Task 2: Build transactional permanent event accounting

**Files:**
- Create: `forecaster/google_weather_store.py`
- Test: `forecaster/tests/test_google_weather_store.py`

- [ ] **Step 1: Add reservation concurrency and lifecycle tests**

Test that concurrent reservations cannot exceed 260/day or 8,000/month; a reservation cancelled before dispatch releases capacity; dispatch makes it consumed; timeout, HTTP error, or parse failure remains consumed; success counts one event; stale undispatched reservations are cancellable.

- [ ] **Step 2: Verify missing store fails**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_weather_store.py -k 'reservation or budget'`

Expected: FAIL.

- [ ] **Step 3: Create the permanent ledger**

```sql
CREATE TABLE IF NOT EXISTS google_weather_usage_events (
    id INTEGER PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    billing_month TEXT NOT NULL,
    billing_date_pacific TEXT NOT NULL,
    city_slug TEXT NOT NULL,
    station_id TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    reserved_at TEXT NOT NULL,
    dispatched_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('reserved','consumed','success','cancelled')),
    billable_events INTEGER NOT NULL CHECK(billable_events IN (0,1)),
    response_status_class INTEGER,
    error_kind TEXT,
    UNIQUE(reservation_id, endpoint, page_number)
);
```

Implement `reserve_event`, `mark_dispatched`, `complete_event`, and `cancel_before_dispatch`. Every admission uses `BEGIN IMMEDIATE`; count `reserved`, `consumed`, and `success` toward limits. Never store a request URL or key.

- [ ] **Step 4: Run concurrent budget tests and commit**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_weather_store.py -k 'reservation or budget'`

Expected: PASS.

```bash
git add forecaster/google_weather_store.py forecaster/tests/test_google_weather_store.py
git commit -m "feat: account for Google events transactionally"
```

### Task 3: Build expiring runtime content tables

**Files:**
- Modify: `forecaster/google_weather_store.py`
- Test: `forecaster/tests/test_google_weather_store.py`

- [ ] **Step 1: Add exact expiry, purge, and no-raw-content tests**

Add named tests for the exact microsecond expiry boundary, distinct today/future
daily TTLs, transactional physical purge, and a `PRAGMA table_info` allowlist
that rejects raw JSON, URL, key, token, response-body, and Google-gap columns.

- [ ] **Step 2: Verify tests fail**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_weather_store.py -k 'expiry or purge or raw'`

Expected: FAIL.

- [ ] **Step 3: Add minimal city-aware tables**

Create `google_hourly_runtime`, `google_daily_runtime`, `google_current_runtime`, and `google_runtime_high` keyed by city/station/issue/valid time with `expires_at`. Implement writes, `active_*` reads with `expires_at > now`, `purge_expired`, and `next_expiry`. Composite expiry is the minimum constituent expiry.

- [ ] **Step 4: Add production path assertion and test injection**

```python
def assert_runtime_path(path: Path, *, production: bool) -> None:
    if production and not path.resolve().is_relative_to(Path("/run/weatheredge")):
        raise RuntimeError("Google runtime content must live under /run/weatheredge")
```

- [ ] **Step 5: Run store tests and commit**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_weather_store.py`

Expected: PASS.

```bash
git add forecaster/google_weather_store.py forecaster/tests/test_google_weather_store.py
git commit -m "feat: store expiring Google runtime weather"
```

### Task 4: Make Google fetching city-aware and strictly paginated

**Files:**
- Modify: `forecaster/google_api.py`
- Create: `forecaster/tests/test_google_multicity.py`
- Modify: `forecaster/tests/test_google_api.py`
- Create: `forecaster/tests/test_settlement_calendar.py`

- [ ] **Step 1: Add all-city, pagination, and secret-safety tests**

Test every `CityConfig` coordinate/station, three hourly pages exactly, underfilled pages marked incomplete without a fourth call, independent endpoint failures, and sanitized exceptions with no API key/full URL.

- [ ] **Step 2: Verify SFO-global behavior fails**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_multicity.py forecaster/tests/test_google_api.py`

Expected: FAIL.

- [ ] **Step 3: Add explicit city request APIs**

```python
@dataclass(frozen=True)
class GoogleFetchResult:
    city_slug: str
    station_id: str
    issued_at: datetime
    hourly_rows: tuple[GoogleHourlyRow, ...]
    daily_rows: tuple[GoogleDailyRow, ...]
    current_row: GoogleCurrentRow | None
    dispatched_events: int


def fetch_hourly_pages(
    city: CityConfig,
    *,
    usage: GoogleUsageLedger,
    runtime: GoogleRuntimeStore,
    max_pages: int = 3,
    transport=urlopen,
) -> tuple[GoogleHourlyRow, ...]:
    return _fetch_paginated_hourly(
        city=city,
        usage=usage,
        runtime=runtime,
        max_pages=min(max_pages, GOOGLE_HOURLY_MAX_PAGES),
        transport=transport,
    )
```

Reserve before each page; mark consumed immediately before transport dispatch; complete afterward. Use `city.latitude`, `city.longitude`, and civil timezone only for API metadata. Do not persist the response body.

- [ ] **Step 4: Bucket UTC hours by fixed-standard station time**

Use `city.fixed_standard_timezone()` explicitly; never rely on `SFO_TZ` or a default settlement-calendar timezone.

- [ ] **Step 5: Run API/multicity tests and commit**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_multicity.py forecaster/tests/test_google_api.py forecaster/tests/test_settlement_calendar.py`

Expected: PASS.

```bash
git add forecaster/google_api.py forecaster/tests/test_google_multicity.py forecaster/tests/test_google_api.py forecaster/tests/test_settlement_calendar.py
git commit -m "feat: fetch Google weather for every city"
```

### Task 5: Derive complete station-day highs and fixed challengers

**Files:**
- Create: `forecaster/google_runtime_blend.py`
- Test: `forecaster/tests/test_google_multicity.py`

- [ ] **Step 1: Add coverage and challenger formula tests**

```python
def test_challenger_uses_fifteen_percent_share_capped_at_one_point_five():
    assert google_challenger(80, 3, 84).mu == pytest.approx(80.6)
    assert google_challenger(80, 3, 95).mu == pytest.approx(81.5)
```

Also add `test_complete_high_requires_all_24_fixed_standard_hours`,
`test_partial_same_day_is_remaining_heat_not_complete_high`, and
`test_seven_degree_gap_emits_block_not_probability`; assert incomplete coverage
returns no final high and a 7°F absolute gap returns the block action with no mean.

- [ ] **Step 2: Verify tests fail**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_multicity.py -k 'complete or challenger or block'`

Expected: FAIL.

- [ ] **Step 3: Implement the fixed formula**

```python
@dataclass(frozen=True)
class GoogleChallenger:
    mu: float | None
    sigma: float
    action: str
    policy_version: str = "google-runtime-fixed-v1"


def google_challenger(baseline_mu: float, baseline_sigma: float, google_high: float) -> GoogleChallenger:
    gap = google_high - baseline_mu
    if abs(gap) >= 7.0:
        return GoogleChallenger(None, baseline_sigma, "external_runtime_corroboration_block")
    adjustment = max(-1.5, min(1.5, 0.15 * gap))
    return GoogleChallenger(baseline_mu + adjustment, baseline_sigma, "forecast")
```

Compute bracket probabilities from this mean and unchanged sigma in memory.

- [ ] **Step 4: Run blend tests and commit**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_multicity.py`

Expected: PASS.

```bash
git add forecaster/google_runtime_blend.py forecaster/tests/test_google_multicity.py
git commit -m "feat: derive fixed Google research challenger"
```

### Task 6: Orchestrate a budget-safe 15-city refresh

**Files:**
- Modify: `forecaster/google_weather_cache.py`
- Modify: `forecaster/weather_cache_config.py`
- Test: `forecaster/tests/test_google_multicity.py`
- Test: `forecaster/tests/test_google_weather_cache.py`

- [ ] **Step 1: Add budget arithmetic and priority tests**

Assert SFO uses 190/day; 14 cities use 56/day; total is 246/day and 7,626/31-day month; 200 events remain reserved under the 7,800 soft ceiling; SFO reservations happen before non-SFO; one city failure leaves the other 14 intact.

- [ ] **Step 2: Verify current single-city orchestrator fails**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_multicity.py forecaster/tests/test_google_weather_cache.py -k 'budget or priority or isolation'`

Expected: FAIL.

- [ ] **Step 3: Implement `--cities` orchestration**

Default the CLI to SFO for backward compatibility; systemd passes all cities. SFO schedules 3 hourly + daily + current. Non-SFO schedules 3 hourly + daily once daily. Sort optional extra bundles by active exposure, soonest close, oldest corroboration, then configured market-volume order. Never exceed three pages or hard budgets.

- [ ] **Step 4: Stop writing Google values to JSON cache**

The compatibility JSON may contain only non-content status such as availability, last attempt, and budget counts. Consumers move to runtime SQLite.

- [ ] **Step 5: Run orchestrator tests and commit**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_multicity.py forecaster/tests/test_google_weather_cache.py`

Expected: PASS.

```bash
git add forecaster/google_weather_cache.py forecaster/weather_cache_config.py forecaster/tests/test_google_multicity.py forecaster/tests/test_google_weather_cache.py
git commit -m "feat: refresh Google weather within budget"
```

### Task 7: Dual-run paired baseline and Google challenger

**Files:**
- Modify: `forecaster/blend_sources.py`
- Modify: `forecaster/blend_archive.py`
- Modify: `trading/sfo_kalshi_quant/forecast.py`
- Modify: `trading/sfo_kalshi_quant/db.py`
- Modify: `trading/sfo_kalshi_quant/models.py`
- Modify: `trading/sfo_kalshi_quant/prediction_features.py`
- Modify: `trading/sfo_kalshi_quant/report.py`
- Modify: `trading/sfo_kalshi_quant/store/diagnostics.py`
- Modify: `trading/sfo_kalshi_quant/store/schema.py`
- Create: `forecaster/tests/test_blend_sources.py`
- Create: `forecaster/tests/test_blend_archive.py`
- Test: `forecaster/tests/test_google_multicity.py`
- Test: `trading/tests/test_prediction_features.py`

- [ ] **Step 1: Add live-invariance and persistence tests**

Assert the served SFO/live forecast is identical with the new adapter disabled/enabled in shadow mode; non-SFO baseline is unchanged; the research challenger follows the fixed formula; raw Google values/gaps never enter permanent forecast/context/decision JSON.

- [ ] **Step 2: Add a durable derived-evidence schema**

```sql
CREATE TABLE IF NOT EXISTS google_challenger_snapshots (
  station_id TEXT NOT NULL,
  target_date TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  policy_version TEXT NOT NULL,
  baseline_mu REAL NOT NULL,
  baseline_sigma REAL NOT NULL,
  challenger_mu REAL,
  challenger_sigma REAL NOT NULL,
  baseline_probabilities_json TEXT NOT NULL,
  challenger_probabilities_json TEXT,
  action TEXT NOT NULL,
  PRIMARY KEY(station_id, target_date, issued_at, policy_version)
);
```

Validate JSON contains only bracket keys and numeric probabilities. Do not store Google high, gap, raw response, conditions, URL, key, or token.

- [ ] **Step 3: Split permanent baseline from ephemeral runtime application**

Build/archive the non-Google baseline first. Apply Google in memory only for a research challenger. Persist only the derived evidence above. Keep the served live `ForecastSnapshot` and strategy fingerprint unchanged.

- [ ] **Step 4: Exclude Google from learning boundaries**

Changing runtime Google rows must not alter LSTM/EMOS training, adaptive weights, MOS, residual de-bias, or historical baseline scorecards.

- [ ] **Step 5: Run forecast/persistence tests and commit**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_multicity.py forecaster/tests/test_blend_sources.py forecaster/tests/test_blend_archive.py trading/tests/test_prediction_features.py trading/tests/test_trade_diagnostics.py`

Expected: PASS.

```bash
git add forecaster/blend_sources.py forecaster/blend_archive.py forecaster/tests/test_blend_sources.py forecaster/tests/test_blend_archive.py trading/sfo_kalshi_quant/forecast.py trading/sfo_kalshi_quant/db.py trading/sfo_kalshi_quant/models.py trading/sfo_kalshi_quant/prediction_features.py trading/sfo_kalshi_quant/report.py trading/sfo_kalshi_quant/store/diagnostics.py trading/sfo_kalshi_quant/store/schema.py trading/tests/test_prediction_features.py
git commit -m "feat: record paired Google research forecasts"
```

### Task 8: Enforce publication, attribution, and runtime expiry operations

**Files:**
- Modify: `trading/sfo_kalshi_quant/publication.py`
- Modify: `scripts/weatheredge_health_check.py`
- Modify: `trading/deploy/aws/publish_forecaster_pages.sh`
- Modify: `trading/deploy/aws/systemd/sfo-forecaster-refresh.service.in`
- Create: `trading/deploy/aws/systemd/weatheredge-google-runtime-purge.service.in`
- Create: `trading/deploy/aws/systemd/weatheredge-google-runtime-purge.timer`
- Test: `trading/tests/test_publication.py`
- Test: `trading/tests/test_weatheredge_health_check.py`
- Test: `trading/tests/test_aws_deploy.py`
- Test: `trading/tests/test_deploy_shell_behavior.py`

- [ ] **Step 1: Add artifact leakage tests**

Fail any Git-published artifact containing raw Google field names/values. Verify the runtime endpoint includes the required adjacent attribution and a response cache lifetime no longer than the row's remaining TTL.

- [ ] **Step 2: Add startup purge and expiry service**

Every collector/consumer runs `purge_expired` before reading. The purge service sleeps/schedules to the earliest expiry, deletes transactionally, and the runtime DB stays under `/run` with no backup/sync unit.

- [ ] **Step 3: Update SFO/all-city service invocation**

Pass `--cities all`; preserve the existing refresh lock and SFO priority. Add environment defaults for the runtime path and budgets without exposing the API key.

- [ ] **Step 4: Run publication/deployment tests and shell validation**

Run focused pytest tests and `bash -n` for changed scripts. Render a fixture and confirm no TTL-bound content reaches Git artifacts.

- [ ] **Step 5: Commit Task 8**

```bash
git add trading/sfo_kalshi_quant/publication.py scripts/weatheredge_health_check.py trading/deploy/aws/publish_forecaster_pages.sh trading/deploy/aws/systemd/sfo-forecaster-refresh.service.in trading/deploy/aws/systemd/weatheredge-google-runtime-purge.service.in trading/deploy/aws/systemd/weatheredge-google-runtime-purge.timer trading/tests/test_publication.py trading/tests/test_weatheredge_health_check.py trading/tests/test_aws_deploy.py trading/tests/test_deploy_shell_behavior.py
git commit -m "feat: operate expiring Google runtime data"
```

### Task 9: Google runtime verification

- [ ] **Step 1: Run all Google and settlement tests**

Run: `./.venv-dev/bin/pytest -q forecaster/tests/test_google_weather_store.py forecaster/tests/test_google_multicity.py forecaster/tests/test_google_api.py forecaster/tests/test_google_weather_cache.py forecaster/tests/test_settlement_calendar.py`

Expected: PASS.

- [ ] **Step 2: Run forecast/trading boundary tests**

Run the blend, archive, prediction-feature, decision-diagnostic, publication, and live-fingerprint tests.

Expected: PASS with no live output change.

- [ ] **Step 3: Run the full Python/frontend/build suites**

Run: `./.venv-dev/bin/pytest -q`, `bun test`, `bun run lint`, and `bun run build`.

Expected: PASS.

- [ ] **Step 4: Perform a no-network dry run with all 15 city fixtures**

Verify fixed-standard buckets, exact expiries, derived challenger rows, 246/day admission math, and per-city failure isolation without a real API call.

- [ ] **Step 5: Perform an AWS canary only after local verification**

Deploy with Google challenger consumption disabled, verify SFO output equality and usage ledger counts, then enable non-SFO research challenger collection. Real-money orders remain disabled.
