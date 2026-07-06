"""Shared exit math for the paper monitor and the dashboard mirror.

The live monitor (``cli.py``) and the Strategy Lab payload builder
(``strategy_research.py``) must agree on how an open position is exited, or the
dashboard shows a target the monitor will never act on. This module is the
single source of truth for both.

Key correctness fix (2026-06): the take-profit was a fixed percentage of entry
cost, e.g. ``net_exit >= cost * 1.35``. A binary contract caps ``net_exit`` at
~0.988 (bid 0.99 minus fee), so for any favorite with cost > ~0.74 that target
exceeds $1.00 and is **physically unreachable** -- the favorite silently rides
to settlement and never banks intraday convergence. The take-profit is now
*edge-based*: sell once the net exit reaches the model's fair value for the held
side (price has converged to where the model thinks it belongs). That target is
always reachable, holds a still-mispriced favorite to settlement, and books a
0.40 -> 0.65 convergence at fair value rather than scalping or riding past it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .fees import quadratic_fee_average_per_contract


# Side-aware percentage thresholds. The take-profit percentages are retained as
# the disabled-edge-based fallback and for dashboard display defaults; the
# stop-loss percentages still define the reachable downside price floor.
DEFAULT_TAKE_PROFIT_PCT = 40.0
DEFAULT_STOP_LOSS_PCT = 35.0
DEFAULT_YES_TAKE_PROFIT_PCT = 50.0
DEFAULT_YES_STOP_LOSS_PCT = 25.0
DEFAULT_NO_TAKE_PROFIT_PCT = 35.0
DEFAULT_NO_STOP_LOSS_PCT = 35.0
# Research-only settlement-first guard for expensive NO favorites. The prior
# paper book clustered above this cost, where a binary's residual upside is tiny
# and profitable early exits mostly clip the remaining settlement spread.
DEFAULT_RESEARCH_NO_SETTLEMENT_FIRST_MIN_COST = 0.73


@dataclass(frozen=True)
class SideThresholds:
    take_profit_pct: float
    stop_loss_pct: float


def thresholds_for_side(
    side: str,
    *,
    yes_take_profit_pct: float,
    yes_stop_loss_pct: float,
    no_take_profit_pct: float,
    no_stop_loss_pct: float,
    take_profit_pct: float,
    stop_loss_pct: float,
) -> SideThresholds:
    """Resolve the percentage thresholds for a side from already-parsed values.

    Both call sites carry the six configured percentages (YES/NO/generic) in
    their own container (argparse namespace vs. a monitor dict); they pass the
    values in so this stays free of either dependency.
    """

    normalized = side.upper()
    if normalized == "YES":
        return SideThresholds(float(yes_take_profit_pct), float(yes_stop_loss_pct))
    if normalized == "NO":
        return SideThresholds(float(no_take_profit_pct), float(no_stop_loss_pct))
    return SideThresholds(float(take_profit_pct), float(stop_loss_pct))


def net_exit_per_contract(bid: float, contracts: float) -> float:
    """Net proceeds per contract after the quadratic exit (taker) fee."""

    if bid <= 0 or bid >= 1 or contracts <= 0:
        return 0.0
    return bid - quadratic_fee_average_per_contract(bid, contracts)


def exit_bid_for_net(target_net: float, contracts: float, *, round_to: int = 4) -> float | None:
    """Smallest sellable bid whose net proceeds reach ``target_net``.

    Returns ``None`` when the target is unreachable inside the [0.01, 0.99]
    sellable range -- the dashboard renders that as "no reachable exit" rather
    than a phantom price above $1.
    """

    if contracts <= 0:
        return None
    if target_net <= net_exit_per_contract(0.01, contracts):
        return round(0.01, round_to)
    if target_net > net_exit_per_contract(0.99, contracts):
        return None
    lo = 0.01
    hi = 0.99
    for _ in range(48):
        mid = (lo + hi) / 2.0
        if net_exit_per_contract(mid, contracts) >= target_net:
            hi = mid
        else:
            lo = mid
    return round(hi, round_to)


def convergence_take_profit_net(
    model_side_probability: float | None,
    *,
    buffer: float = 0.0,
) -> float | None:
    """Edge-based take-profit: the net-exit level at which to bank the position.

    Sell once the net exit reaches the model's fair value for the held side --
    the price has converged to where the model thinks it belongs, so the
    remaining edge is gone. ``buffer`` lets a slightly-early exit through (a
    small tolerance to cover the exit fee).

    Returns ``None`` when no current model probability is available: holding a
    still-favoured position to settlement pays no exit fee and realizes the full
    fair value, so "no model read" means "ride to settlement", not "scalp".
    """

    if model_side_probability is None:
        return None
    return max(0.0, min(1.0, float(model_side_probability) - buffer))


@dataclass(frozen=True)
class ExitSignal:
    action: str  # "HOLD" | "HOLD_SETTLEMENT_FIRST" | "TAKE_PROFIT" | "STOP_LOSS"
    reason: str


def decide_exit(
    *,
    side: str,
    entry_cost: float,
    net_exit: float,
    stop_loss_net: float,
    model_side_probability: float | None,
    convergence_buffer: float = 0.0,
    model_veto_enabled: bool = True,
    model_veto_buffer: float = 0.0,
    model_veto_max_loss_roi: float | None = None,
    edge_based_take_profit: bool = True,
    legacy_take_profit_net: float | None = None,
    stop_loss_pct: float | None = None,
    settlement_first_no_min_cost: float | None = None,
) -> ExitSignal:
    """Decide whether to hold, take profit, or stop out an open position.

    Take-profit (``edge_based_take_profit=True``): sell when ``net_exit`` reaches
    the model's fair value (``convergence_take_profit_net``). This is provably no
    worse than riding to settlement -- it only sells when the market already pays
    fair value or better -- and it banks intraday convergence instead of forgoing
    it. When no model probability is available, the position rides to settlement.

    Stop-loss: a reachable downside price floor (``stop_loss_net``), preserved
    with the existing NO-side model veto: if a fresh model snapshot says the side
    still clears the entry cost and net exit (plus a buffer), hold rather than
    realize intraday noise. Once the loss crosses the catastrophic floor
    (``model_veto_max_loss_roi``) the veto lapses and discipline wins.

    Fail-safe: for a NO side with NO fresh model read available, the stop now
    HOLDS (``HOLD_NO_MODEL_READ``) instead of firing. A daily weather high is
    monotonic and settles at a known time, so an intraday NO mark is noise until
    the high is set; without a model read to confirm the thesis is dead, selling
    just crystallizes that noise. Max loss is already bounded by entry cost, and
    the catastrophic floor still cuts a true disaster. (When the model read IS
    present and has dropped below the veto floor, the thesis IS confirmed dead
    and the stop fires as before.)
    """

    # Prefer the edge-based convergence target when a fresh model read exists;
    # otherwise fall back to the legacy %-of-cost target (reachable for cheap
    # positions, and harmlessly unreachable for an expensive favorite, which then
    # simply rides to settlement).
    if edge_based_take_profit and model_side_probability is not None:
        tp_net = convergence_take_profit_net(model_side_probability, buffer=convergence_buffer)
    else:
        tp_net = legacy_take_profit_net
    # The EV-optimal exit: sell once the market pays at or above the model's fair
    # value. This single condition captures BOTH upward convergence (price rose to
    # fair value -> bank the gain) and deterioration (the model fell below the
    # sellable price -> the edge reversed, cut while you can). Label by whether the
    # exit is a gain or a loss relative to entry cost.
    if tp_net is not None and net_exit >= tp_net:
        if net_exit >= entry_cost:
            if (
                side.upper() == "NO"
                and settlement_first_no_min_cost is not None
                and entry_cost >= settlement_first_no_min_cost
            ):
                return ExitSignal(
                    "HOLD_SETTLEMENT_FIRST",
                    f"settlement-first NO favorite: entry cost {entry_cost:.3f} >= "
                    f"{settlement_first_no_min_cost:.3f}; hold instead of clipping "
                    f"residual settlement upside at net exit {net_exit:.3f}",
                )
            return ExitSignal(
                "TAKE_PROFIT",
                f"edge captured: net exit {net_exit:.3f} >= fair value {tp_net:.3f}",
            )
        return ExitSignal(
            "STOP_LOSS",
            f"edge reversed: fair value {tp_net:.3f} fell to/below net exit "
            f"{net_exit:.3f}, under entry {entry_cost:.3f}",
        )

    if net_exit <= stop_loss_net:
        roi = (net_exit - entry_cost) / entry_cost if entry_cost > 0 else 0.0
        # A catastrophic drawdown stops unconditionally -- past this floor the
        # position is a genuine disaster, not intraday noise, and discipline wins
        # even with no model read.
        catastrophic = (
            model_veto_max_loss_roi is not None and roi <= -model_veto_max_loss_roi
        )
        if model_veto_enabled and side.upper() == "NO" and not catastrophic:
            if model_side_probability is None:
                # FAIL-SAFE: the daily high is monotonic and settles at a known
                # time, so an intraday NO mark is noise until the high is set. With
                # no fresh model read we cannot confirm the thesis is dead, so do
                # NOT crystallize a price-noise loss -- hold (max loss is already
                # bounded by entry cost) until a model read returns or it settles.
                # This is the failure mode that drained the book once the veto's
                # data source (probability_snapshots) went stale: the naked
                # %-of-cost stop whipsawed NO favorites the model still expected to
                # win. The catastrophic floor above still cuts a true disaster.
                return ExitSignal(
                    "HOLD_NO_MODEL_READ",
                    f"stop-loss held: net exit {net_exit:.3f} <= floor "
                    f"{stop_loss_net:.3f} but no fresh model read to confirm the "
                    f"thesis is dead (fail-safe hold on a monotonic weather high)",
                )
            veto_floor = max(entry_cost, net_exit + model_veto_buffer)
            if model_side_probability >= veto_floor:
                return ExitSignal(
                    "HOLD_MODEL_VETO",
                    f"stop-loss vetoed: model p={model_side_probability:.2f} at settlement is "
                    f"above entry cost {entry_cost:.2f} and net exit {net_exit:.2f} "
                    f"+ {model_veto_buffer:.2f} buffer",
                )
        pct_suffix = f" ({stop_loss_pct:.1f}%)" if stop_loss_pct is not None else ""
        return ExitSignal(
            "STOP_LOSS",
            f"{side} stop-loss: net exit {net_exit:.3f} <= floor {stop_loss_net:.3f}{pct_suffix}",
        )

    return ExitSignal("HOLD", "inside exit bands")
