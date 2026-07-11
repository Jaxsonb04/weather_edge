"""Rolling-origin replay harness for the serve-time EMOS recalibration.

Acceptance gate for ``emos_recalibration.py``: replays the last N days of
scored rolling-origin EMOS rows and, for each historical target, computes the
trailing correction using ONLY rows whose truth was published before that
target's serve date (serve date = target - lead), applies it, and scores the
corrected Gaussian against ``cli_settlements``.

Ship rule (checked and printed by this tool):

* pooled Gaussian CRPS must improve, AND
* no city's CRPS may degrade by more than 5% at either lead.

Run with corrections toggled to attribute the gain::

    python recalibration_replay.py --db weather.db --days 60             # both
    python recalibration_replay.py --db weather.db --days 60 --no-sigma  # bias only
    python recalibration_replay.py --db weather.db --days 60 --no-bias   # sigma only

The "before" columns with both toggles off reproduce the uncorrected
scoreboard (baseline). Pure standard library.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import date, timedelta
from statistics import fmean

from cities import CITIES
from emos_recalibration import (
    BIAS_DEADBAND_T,
    SHRINKAGE_K,
    TRAILING_WINDOW_DAYS,
    compute_correction,
    load_scored_series,
    window_rows,
)

DEFAULT_EVAL_DAYS = 60
LEADS = (1, 2)

# Central 80% interval half-width in sigmas: Phi^-1(0.90).
Z_80 = 1.2815515655446004
MAX_CITY_CRPS_DEGRADATION = 1.05  # per city x lead acceptance ceiling


def gaussian_crps(mu: float, sigma: float, x: float) -> float:
    """Closed-form CRPS of a Gaussian forecast (Gneiting & Raftery 2007)."""

    if sigma <= 0.0:
        return abs(x - mu)
    z = (x - mu) / sigma
    pdf = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    cdf = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return sigma * (z * (2.0 * cdf - 1.0) + 2.0 * pdf - 1.0 / math.sqrt(math.pi))


def score_rows(rows: list[tuple[float, float, float]]) -> dict[str, float]:
    """MAE / bias / CRPS / cov80 over (mu, sigma, truth) rows."""

    if not rows:
        return {"n": 0, "mae": 0.0, "bias": 0.0, "crps": 0.0, "cov80": 0.0}
    errors = [mu - truth for mu, _sigma, truth in rows]
    covered = [
        1.0 if abs(truth - mu) <= Z_80 * sigma else 0.0 for mu, sigma, truth in rows
    ]
    return {
        "n": len(rows),
        "mae": fmean(abs(e) for e in errors),
        "bias": fmean(errors),
        "crps": fmean(gaussian_crps(mu, sigma, truth) for mu, sigma, truth in rows),
        "cov80": fmean(covered),
    }


def replay_city_lead(
    series: list[tuple[date, float, float, float]],
    eval_start: date,
    eval_end: date,
    lead: int,
    *,
    window_days: int,
    k: float,
    apply_bias: bool,
    apply_sigma: bool,
    sigma_net_of_bias: bool,
    bias_deadband_t: float,
) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]], list[int]]:
    """(before_rows, after_rows, window_sizes) for one station x lead."""

    before: list[tuple[float, float, float]] = []
    after: list[tuple[float, float, float]] = []
    sizes: list[int] = []
    for target, mu, sigma, truth in series:
        if not (eval_start <= target <= eval_end):
            continue
        serve_date = target - timedelta(days=lead)
        window = window_rows(series, serve_date, window_days=window_days)
        correction = compute_correction(
            window,
            k=k,
            apply_bias=apply_bias,
            apply_sigma=apply_sigma,
            sigma_net_of_bias=sigma_net_of_bias,
            bias_deadband_t=bias_deadband_t,
        )
        corrected_mu, corrected_sigma = correction.apply(mu, sigma)
        before.append((mu, sigma, truth))
        after.append((corrected_mu, corrected_sigma, truth))
        sizes.append(correction.n_window)
    return before, after, sizes


def run_replay(
    db_path: str,
    *,
    days: int = DEFAULT_EVAL_DAYS,
    window_days: int = TRAILING_WINDOW_DAYS,
    k: float = SHRINKAGE_K,
    apply_bias: bool = True,
    apply_sigma: bool = True,
    sigma_net_of_bias: bool = True,
    bias_deadband_t: float = BIAS_DEADBAND_T,
    leads: tuple[int, ...] = LEADS,
) -> dict:
    with sqlite3.connect(db_path) as conn:
        settlement_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(cli_settlements)")
        }
        final_filter = "AND is_final = 1" if "is_final" in settlement_columns else ""
        latest = conn.execute(
            "SELECT MAX(local_date) FROM cli_settlements "
            f"WHERE max_temperature_f IS NOT NULL {final_filter}"
        ).fetchone()[0]
        if latest is None:
            raise SystemExit("cli_settlements holds no scored truth")
        eval_end = date.fromisoformat(latest)
        eval_start = eval_end - timedelta(days=days - 1)

        cities_out: list[dict] = []
        pooled_before: list[tuple[float, float, float]] = []
        pooled_after: list[tuple[float, float, float]] = []
        for city in CITIES:
            for lead in leads:
                series = load_scored_series(conn, city.nws_station_id, lead)
                before, after, sizes = replay_city_lead(
                    series,
                    eval_start,
                    eval_end,
                    lead,
                    window_days=window_days,
                    k=k,
                    apply_bias=apply_bias,
                    apply_sigma=apply_sigma,
                    sigma_net_of_bias=sigma_net_of_bias,
                    bias_deadband_t=bias_deadband_t,
                )
                pooled_before.extend(before)
                pooled_after.extend(after)
                score_before = score_rows(before)
                score_after = score_rows(after)
                cities_out.append(
                    {
                        "city": city.slug,
                        "station": city.nws_station_id,
                        "lead": lead,
                        "before": score_before,
                        "after": score_after,
                        "mean_window_n": fmean(sizes) if sizes else 0.0,
                    }
                )

    pooled = {"before": score_rows(pooled_before), "after": score_rows(pooled_after)}
    violations = [
        f"{row['city']}@lead{row['lead']}: CRPS {row['before']['crps']:.3f} -> "
        f"{row['after']['crps']:.3f} (+{100 * (row['after']['crps'] / row['before']['crps'] - 1):.1f}%)"
        for row in cities_out
        if row["before"]["n"] > 0
        and row["after"]["crps"] > row["before"]["crps"] * MAX_CITY_CRPS_DEGRADATION
    ]
    passed = (
        pooled["after"]["crps"] < pooled["before"]["crps"] and not violations
    )
    return {
        "eval_start": eval_start.isoformat(),
        "eval_end": eval_end.isoformat(),
        "days": days,
        "window_days": window_days,
        "k": k,
        "apply_bias": apply_bias,
        "apply_sigma": apply_sigma,
        "sigma_net_of_bias": sigma_net_of_bias,
        "bias_deadband_t": bias_deadband_t,
        "cities": cities_out,
        "pooled": pooled,
        "violations": violations,
        "passed": passed,
    }


def _print_table(result: dict) -> None:
    print(
        f"replay {result['eval_start']}..{result['eval_end']} "
        f"window={result['window_days']}d k={result['k']} "
        f"bias={'on' if result['apply_bias'] else 'off'} "
        f"sigma={'on' if result['apply_sigma'] else 'off'} "
        f"(net_of_bias={'on' if result['sigma_net_of_bias'] else 'off'}) "
        f"deadband_t={result['bias_deadband_t']}"
    )
    header = (
        f"{'city':5s} {'ld':>2s} {'n':>4s} "
        f"{'MAE b':>6s} {'MAE a':>6s} {'bias b':>7s} {'bias a':>7s} "
        f"{'CRPS b':>7s} {'CRPS a':>7s} {'d%':>6s} {'cov80 b':>8s} {'cov80 a':>8s}"
    )
    print(header)
    for row in result["cities"]:
        b, a = row["before"], row["after"]
        if b["n"] == 0:
            print(f"{row['city']:5s} {row['lead']:>2d}    0  (no scored rows)")
            continue
        delta = 100.0 * (a["crps"] / b["crps"] - 1.0) if b["crps"] else 0.0
        print(
            f"{row['city']:5s} {row['lead']:>2d} {b['n']:>4d} "
            f"{b['mae']:>6.2f} {a['mae']:>6.2f} {b['bias']:>+7.2f} {a['bias']:>+7.2f} "
            f"{b['crps']:>7.3f} {a['crps']:>7.3f} {delta:>+6.1f} "
            f"{100 * b['cov80']:>7.0f}% {100 * a['cov80']:>7.0f}%"
        )
    b, a = result["pooled"]["before"], result["pooled"]["after"]
    print(
        f"{'ALL':5s} {'':>2s} {b['n']:>4d} "
        f"{b['mae']:>6.2f} {a['mae']:>6.2f} {b['bias']:>+7.2f} {a['bias']:>+7.2f} "
        f"{b['crps']:>7.3f} {a['crps']:>7.3f} "
        f"{100 * (a['crps'] / b['crps'] - 1):>+6.1f} "
        f"{100 * b['cov80']:>7.0f}% {100 * a['cov80']:>7.0f}%"
    )
    for violation in result["violations"]:
        print(f"GATE VIOLATION: {violation}")
    print(f"acceptance: {'PASS' if result['passed'] else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="weather.db")
    parser.add_argument("--days", type=int, default=DEFAULT_EVAL_DAYS)
    parser.add_argument("--window", type=int, default=TRAILING_WINDOW_DAYS)
    parser.add_argument("--k", type=float, default=SHRINKAGE_K)
    parser.add_argument("--deadband", type=float, default=BIAS_DEADBAND_T)
    parser.add_argument("--no-bias", dest="bias", action="store_false")
    parser.add_argument("--no-sigma", dest="sigma", action="store_false")
    parser.add_argument(
        "--sigma-raw",
        action="store_true",
        help="dispersion factor from raw residuals instead of net-of-bias",
    )
    parser.add_argument("--json", action="store_true", help="emit the full result as JSON")
    args = parser.parse_args(argv)

    result = run_replay(
        args.db,
        days=args.days,
        window_days=args.window,
        k=args.k,
        apply_bias=args.bias,
        apply_sigma=args.sigma,
        sigma_net_of_bias=not args.sigma_raw,
        bias_deadband_t=args.deadband,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_table(result)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
