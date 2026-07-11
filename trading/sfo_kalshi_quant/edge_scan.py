"""Measure the favorite-band Maker opportunity on live books, honestly.

The research case for this system rests on a transaction-level finding
(Burgi, Deng & Whelan, SSRN 5502658): Kalshi contracts priced above ~70c
earned small positive post-fee returns, Makers beat Takers, and the per-trade
return standard deviation (~33%) dwarfs the mean (~2.6%), so distinguishing
skill from luck takes hundreds of independent bets. That finding predates the
current fee schedule, so this module re-measures the OPPORTUNITY on today's
books and today's fees rather than trusting the paper's number.

What a snapshot CAN measure (and this reports):
* how many favorite-band maker posts the fifteen city ladders offer right now,
* the model edge of each post under the city's live EMOS Gaussian, after the
  CURRENT maker fee (25% of the 0.07 quadratic taker rate, fee_multiplier 1 --
  verified against the live series API on 2026-07-06),
* spreads, displayed depth, and how many independent city-days that breadth
  yields per day.

What a snapshot CANNOT measure (stated, not papered over):
* realized post-fee returns (needs settled outcomes over months),
* fill rates for resting maker orders (queue position and hidden flow are
  invisible; the paper engine's displayed-ask fill proxy is itself a model),
* adverse selection against faster traders.

The sample-size block quantifies the variance problem instead of hiding it:
with per-trade sd sigma and mean edge mu, a two-sided t at 95% needs roughly
n = (1.96 * sigma / mu)^2 settled, independent trades.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError

from .cities import CityConfig, parse_city_slugs
from .config import DEFAULT_FORECASTER_ROOT
from .emos_sources import forecast_source_precedence
from .fees import quadratic_fee_average_per_contract
from .kalshi import KalshiPublicClient
from .models import EventSnapshot, MarketBin

FAVORITE_MIN = 0.70
FAVORITE_MAX = 0.97
TICK = 0.01
# Literature anchors (SSRN 5502658), used ONLY for the sample-size arithmetic.
LITERATURE_MEAN_RETURN = 0.026
LITERATURE_SD_RETURN = 0.33


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def _bin_yes_probability(market: MarketBin, mu: float, sigma: float) -> float:
    lo, hi = market.continuous_interval()
    lo_v = -math.inf if lo is None else float(lo)
    hi_v = math.inf if hi is None else float(hi)
    return max(
        0.0,
        min(1.0, _normal_cdf(hi_v, mu, sigma) - _normal_cdf(lo_v, mu, sigma)),
    )


def _load_live_emos(
    weather_db: Path, station_id: str
) -> dict[str, tuple[float, float]]:
    """target_date -> freshest (mu, sigma) for a station, any source."""

    if not weather_db.exists():
        return {}
    preferred: dict[str, tuple[tuple[int, str], tuple[float, float]]] = {}
    with sqlite3.connect(weather_db) as conn:
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='forecast_emos_daily_high'"
            ).fetchone()
            is None
        ):
            return {}
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(forecast_emos_daily_high)")
        }
        if "station_id" not in columns:
            return {}
        for target_date, mu, sigma, fetched_at, source in conn.execute(
            "SELECT target_date, predicted_high_f, sigma_f, fetched_at, source "
            "FROM forecast_emos_daily_high WHERE station_id = ?",
            (station_id,),
        ):
            target = str(target_date)
            rank = (forecast_source_precedence(str(source)), str(fetched_at))
            current = preferred.get(target)
            if current is None or rank > current[0]:
                preferred[target] = (rank, (float(mu), float(sigma)))
    return {target: value for target, (_rank, value) in preferred.items()}


def _maker_post_price(side_bid: float, side_ask: float) -> float | None:
    """The resting price a maker would post on this side: join the bid, or
    improve one tick when the spread allows price improvement without crossing."""

    if side_ask <= 0.0 or side_ask >= 1.0:
        return None
    bid = max(0.0, side_bid)
    if bid <= 0.0:
        # An empty bid side gives the post no market anchor; a "join" there
        # would manufacture opportunity out of a dead book.
        return None
    if side_ask - bid > TICK + 1e-9:
        return round(bid + TICK, 2)
    return round(bid, 2)


def scan_city(
    client: KalshiPublicClient,
    city: CityConfig,
    emos_by_date: dict[str, tuple[float, float]],
    *,
    limit: int = 10,
) -> dict:
    """Scan one city's open events for favorite-band maker candidates."""

    result: dict = {
        "city": city.slug,
        "series_ticker": city.series_ticker,
        "events": 0,
        "candidates": [],
        "skipped_no_emos": 0,
        "error": None,
    }
    try:
        events: list[EventSnapshot] = client.list_event_snapshots(
            series_ticker=city.series_ticker, limit=limit, with_nested_markets=True
        )
    except (URLError, OSError, TimeoutError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    for event in events:
        target = event.target_date
        markets = event.active_markets
        if target is None or not markets:
            continue
        result["events"] += 1
        emos = emos_by_date.get(target.isoformat())
        for market in markets:
            for side in ("YES", "NO"):
                side_bid = market.side_bid(side)
                side_ask = market.side_ask(side)
                post = _maker_post_price(side_bid, side_ask)
                if post is None or not (FAVORITE_MIN <= post <= FAVORITE_MAX):
                    continue
                maker_fee = quadratic_fee_average_per_contract(post, 1.0, maker=True)
                taker_fee = quadratic_fee_average_per_contract(
                    side_ask, 1.0, maker=False
                )
                candidate = {
                    "target_date": target.isoformat(),
                    "ticker": market.ticker,
                    "side": side,
                    "side_bid": side_bid,
                    "side_ask": side_ask,
                    "spread": round(side_ask - side_bid, 4),
                    "post_price": post,
                    "maker_fee": maker_fee,
                    "maker_cost": round(post + maker_fee, 4),
                    "taker_cost": round(side_ask + taker_fee, 4),
                    "maker_vs_taker_saving": round(
                        (side_ask + taker_fee) - (post + maker_fee), 4
                    ),
                    "displayed_bid_size": market.side_bid_size(side),
                    "model_probability": None,
                    "model_edge_after_maker_fee": None,
                }
                if emos is not None:
                    mu, sigma = emos
                    p_yes = _bin_yes_probability(market, mu, sigma)
                    p_side = p_yes if side == "YES" else 1.0 - p_yes
                    candidate["model_probability"] = round(p_side, 4)
                    candidate["model_edge_after_maker_fee"] = round(
                        p_side - (post + maker_fee), 4
                    )
                else:
                    result["skipped_no_emos"] += 1
                result["candidates"].append(candidate)
    return result


def required_sample_size(mean_return: float, sd_return: float, z: float = 1.96) -> int:
    if mean_return <= 0:
        return -1
    return math.ceil((z * sd_return / mean_return) ** 2)


def summarize(city_results: list[dict]) -> dict:
    candidates = [c for r in city_results for c in r["candidates"]]
    with_model = [
        c for c in candidates if c["model_edge_after_maker_fee"] is not None
    ]
    positive = [c for c in with_model if c["model_edge_after_maker_fee"] > 0]
    edges = sorted(c["model_edge_after_maker_fee"] for c in with_model)

    def _pct(values: list[float], q: float) -> float | None:
        if not values:
            return None
        idx = min(len(values) - 1, max(0, int(q * (len(values) - 1))))
        return values[idx]

    city_days = {
        (r["city"], c["target_date"]) for r in city_results for c in r["candidates"]
    }
    mean_edge = sum(edges) / len(edges) if edges else None
    return {
        "cities_scanned": len(city_results),
        "cities_with_errors": [r["city"] for r in city_results if r["error"]],
        "events_seen": sum(r["events"] for r in city_results),
        "favorite_band_candidates": len(candidates),
        "candidates_with_model": len(with_model),
        "candidates_positive_model_edge": len(positive),
        "distinct_city_days": len(city_days),
        "mean_spread": (
            round(sum(c["spread"] for c in candidates) / len(candidates), 4)
            if candidates
            else None
        ),
        "mean_maker_vs_taker_saving": (
            round(
                sum(c["maker_vs_taker_saving"] for c in candidates) / len(candidates),
                4,
            )
            if candidates
            else None
        ),
        "model_edge_mean": round(mean_edge, 4) if mean_edge is not None else None,
        "model_edge_p10": _pct(edges, 0.10),
        "model_edge_p50": _pct(edges, 0.50),
        "model_edge_p90": _pct(edges, 0.90),
        "sample_size_note": {
            "explanation": (
                "n ~= (1.96 * sd / mean)^2 settled independent trades to "
                "distinguish the mean return from zero at 95% confidence"
            ),
            "n_at_literature_priors": required_sample_size(
                LITERATURE_MEAN_RETURN, LITERATURE_SD_RETURN
            ),
            "n_at_measured_mean_edge": (
                required_sample_size(mean_edge, LITERATURE_SD_RETURN)
                if mean_edge is not None and mean_edge > 0
                else None
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cities", default="all")
    parser.add_argument(
        "--weather-db",
        default=str(Path(DEFAULT_FORECASTER_ROOT) / "weather.db"),
        help="weather.db holding per-station EMOS rows (for model probabilities)",
    )
    parser.add_argument("--out", default=None, help="write full JSON here")
    args = parser.parse_args(argv)

    cities = parse_city_slugs(args.cities)
    client = KalshiPublicClient()
    weather_db = Path(args.weather_db)

    city_results = []
    for city in cities:
        emos = _load_live_emos(weather_db, city.nws_station_id)
        result = scan_city(client, city, emos)
        city_results.append(result)
        n = len(result["candidates"])
        pos = sum(
            1
            for c in result["candidates"]
            if (c["model_edge_after_maker_fee"] or 0) > 0
        )
        status = f"error: {result['error']}" if result["error"] else (
            f"{result['events']} events, {n} favorite-band posts, "
            f"{pos} positive-model-edge"
        )
        print(f"[{city.slug}] {status}")

    summary = summarize(city_results)
    print()
    print("=== favorite-band maker opportunity (live books, current fees) ===")
    for key, value in summary.items():
        if key != "sample_size_note":
            print(f"  {key}: {value}")
    note = summary["sample_size_note"]
    print(f"  sample size: {note['explanation']}")
    print(
        f"    at literature priors (mu=2.6%, sd=33%): "
        f"n ~= {note['n_at_literature_priors']}"
    )
    if note["n_at_measured_mean_edge"]:
        print(
            f"    at measured mean model edge: n ~= {note['n_at_measured_mean_edge']}"
        )

    if args.out:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fee_model": {
                "taker": "0.07 * p * (1-p), ceil to cent",
                "maker": "25% of taker (verified vs live series API 2026-07-06)",
                "fee_multiplier": 1,
            },
            "favorite_band": [FAVORITE_MIN, FAVORITE_MAX],
            "summary": summary,
            "cities": city_results,
        }
        Path(args.out).write_text(json.dumps(payload, indent=2, sort_keys=True))
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
