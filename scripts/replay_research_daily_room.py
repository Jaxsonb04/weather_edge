#!/usr/bin/env python3
"""Replay the research daily-room arithmetic over published gh-pages snapshots.

Source for the figures quoted next to TARGET_OPEN_RISK_CHARGE_FRACTION in
trading/sfo_kalshi_quant/research_entry_risk.py. Samples the last
strategy_research.json published at or before each two-hour boundary
(America/Los_Angeles) in the requested window and reports the share of
snapshots with zero room at several open-risk charges, using the same
arithmetic as target_remaining_daily_risk():

    room = max(0, 150 + min(realized_today, 0) - charge * (open + pending))

Public data only: it needs a local fetch of the gh-pages branch
(`git fetch origin gh-pages`) and nothing else. Example:

    python scripts/replay_research_daily_room.py --start 2026-08-31 --end 2026-09-12

Reproduced 2026-09-13 for that window: 154 samples, open+pending mean $170,
median $148, p90 $388; zero room in 52.6% of samples at the 1.00 charge,
20.1% at 0.60, 0.6% at 0.35; no sample was under the $150 realized pause.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ACCOUNT = "paper-research-roi-v6"
BUDGET = 150.0
TZ = ZoneInfo("America/Los_Angeles")
FRACTIONS = (1.0, 0.60, 0.35)
SAMPLE_STEP = timedelta(hours=2)
PENDING_COST_KEYS = ("reserved_cost", "initial_cost", "risk", "max_loss")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def commits(repo: Path, start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    out = git(
        repo,
        "log",
        "--format=%H %cI",
        "origin/gh-pages",
        f"--since={(start - timedelta(days=1)).isoformat()}",
        f"--until={end.isoformat()}",
        "--",
        "strategy_research.json",
    )
    rows = []
    for line in out.splitlines():
        sha, iso = line.split()
        rows.append((datetime.fromisoformat(iso), sha))
    return sorted(rows)


def pending_cost(row: dict) -> float:
    for key in PENDING_COST_KEYS:
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    contracts = row.get("contracts") or 0.0
    price = row.get("limit_cost_per_contract") or row.get("limit_price") or 0.0
    return float(contracts) * float(price)


def snapshot_state(repo: Path, sha: str) -> dict:
    data = json.loads(git(repo, "show", f"{sha}:strategy_research.json"))
    trading = data["paper_trading"]
    open_cost = sum(
        float(row["initial_cost"])
        for row in trading.get("open_positions") or []
        if row.get("account_id") == ACCOUNT
    )
    pending = sum(
        pending_cost(row)
        for row in trading.get("pending_limit_orders") or []
        if row.get("account_id") == ACCOUNT
    )
    daily = trading.get("research_daily_target") or {}
    objective_day = daily.get("objective_day")
    realized = 0.0
    for day in daily.get("days") or []:
        if day.get("objective_day") == objective_day:
            realized = float(day.get("realized_pnl") or 0.0)
    return {
        "generated": data.get("generated_at"),
        "open": open_cost,
        "pending": pending,
        "realized": realized,
        "objective_day": objective_day,
    }


def room(sample: dict, charge: float) -> float:
    active = sample["open"] + sample["pending"]
    return max(0.0, BUDGET + min(sample["realized"], 0.0) - charge * active)


def collect(repo: Path, start: datetime, end: datetime) -> list[dict]:
    rows = commits(repo, start, end)
    samples = []
    index = 0
    boundary = start
    while boundary < end:
        last = None
        while index < len(rows) and rows[index][0] <= boundary:
            last = rows[index]
            index += 1
        if last is not None and boundary - last[0] <= SAMPLE_STEP:
            state = snapshot_state(repo, last[1])
            samples.append({**state, "boundary": boundary.isoformat(), "sha": last[1][:9]})
            print(
                f"{boundary.strftime('%m-%d %H:%M')} {last[1][:9]} "
                f"open={state['open']:.2f} pending={state['pending']:.2f} "
                f"realized={state['realized']:.2f}",
                file=sys.stderr,
            )
        boundary += SAMPLE_STEP
    return samples


def report(samples: list[dict]) -> None:
    count = len(samples)
    print(f"snapshots={count}")
    if not count:
        return
    active = sorted(s["open"] + s["pending"] for s in samples)
    p90 = active[min(count - 1, int(round(0.9 * (count - 1))))]
    print(
        f"open+pending mean={sum(active) / count:.2f} "
        f"median={active[count // 2]:.2f} p90={p90:.2f}"
    )
    for charge in FRACTIONS:
        zero = sum(1 for s in samples if room(s, charge) <= 0.0)
        print(f"charge={charge:.2f} zero_room={zero}/{count} ({100.0 * zero / count:.1f}%)")
    paused = sum(1 for s in samples if s["realized"] <= -BUDGET)
    print(f"realized_pause_snapshots={paused}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 8, 31))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 12))
    parser.add_argument("--samples-out", type=Path, default=None)
    args = parser.parse_args()
    start = datetime.combine(args.start, datetime.min.time(), tzinfo=TZ)
    end = datetime.combine(args.end + timedelta(days=1), datetime.min.time(), tzinfo=TZ)
    samples = collect(args.repo, start, end)
    report(samples)
    if args.samples_out is not None:
        args.samples_out.write_text(json.dumps(samples, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
