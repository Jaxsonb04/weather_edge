"""Task 7: SFO/city Google research-challenger shadow dual-run.

Composes the already-served ``SfoForecasterAdapter.latest_blend`` with the
forecaster-owned durable paired baseline/Google-challenger evidence
(``SfoForecasterAdapter.latest_google_challenger_baseline``, a plain SQL read
-- this module never imports forecaster's Python modules; the two projects
deliberately do not import each other), computes bracket probabilities
against the live Kalshi ladder, and persists ONLY the derived evidence via
``PaperStore.record_google_challenger_snapshot``.

This module is research-only shadow output. It is never imported by the live
decision-recording path or any live trading-loop entrypoint -- see
``trading/tests/test_google_challenger_shadow.py``'s isolation tests -- and
calling it never changes what ``latest_blend``/``latest_emos_snapshot``
return: ``run_sfo_google_shadow`` calls ``latest_blend`` exactly once and
returns that same object unmodified, proven byte-identical to a bare
``latest_blend`` call by
``test_served_forecast_is_byte_identical_with_shadow_disabled_and_enabled``.
"""

from __future__ import annotations

from datetime import date
from typing import Sequence

from .db import PaperStore
from .forecast import SfoForecasterAdapter
from .models import ForecastSnapshot, GoogleChallengerSnapshot, MarketBin
from .prediction_features import build_google_challenger_bracket_probabilities


def build_google_challenger_snapshot(
    adapter: SfoForecasterAdapter,
    target: date,
    *,
    markets: Sequence[MarketBin] = (),
) -> GoogleChallengerSnapshot | None:
    """Build one paired evidence snapshot from the durable baseline row.

    Fails closed (returns ``None``) when no paired-evidence row exists yet
    (e.g. Google was unavailable, budget-blocked, or the row has not been
    derived for this target) or when there are no markets to price -- never
    fabricates a probability payload.
    """

    row = adapter.latest_google_challenger_baseline(target)
    if row is None:
        return None
    baseline_probabilities = build_google_challenger_bracket_probabilities(
        row["baseline_mu"], row["baseline_sigma"], markets
    )
    if baseline_probabilities is None:
        return None
    challenger_probabilities = build_google_challenger_bracket_probabilities(
        row["challenger_mu"], row["challenger_sigma"], markets
    )
    return GoogleChallengerSnapshot(
        station_id=row["station_id"],
        target_date=date.fromisoformat(row["target_date"]),
        issued_at=row["issued_at"],
        policy_version=row["policy_version"],
        baseline_mu=row["baseline_mu"],
        baseline_sigma=row["baseline_sigma"],
        challenger_mu=row["challenger_mu"],
        challenger_sigma=row["challenger_sigma"],
        baseline_probabilities=baseline_probabilities,
        challenger_probabilities=challenger_probabilities,
        action=row["action"],
    )


def run_sfo_google_shadow(
    adapter: SfoForecasterAdapter,
    target: date,
    *,
    paper_store: PaperStore,
    markets: Sequence[MarketBin] = (),
) -> tuple[ForecastSnapshot, GoogleChallengerSnapshot | None]:
    """Dual-run the SFO shadow challenger without touching the served forecast.

    Always calls ``adapter.latest_blend`` exactly as the live path does and
    returns that same object unmodified; the shadow snapshot is computed and
    persisted independently and can never feed back into it. Persists
    nothing when no paired evidence is available.
    """

    served = adapter.latest_blend(target)
    snapshot = build_google_challenger_snapshot(adapter, target, markets=markets)
    if snapshot is not None:
        paper_store.record_google_challenger_snapshot(snapshot)
    return served, snapshot
