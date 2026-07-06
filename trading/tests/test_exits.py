"""Unit tests for the shared exit math (trading/sfo_kalshi_quant/exits.py)."""

from sfo_kalshi_quant.exits import (
    convergence_take_profit_net,
    decide_exit,
    exit_bid_for_net,
    net_exit_per_contract,
)


# --- the CRITICAL bug: %-of-cost take-profit was unreachable for favorites ---

def test_legacy_percent_take_profit_is_unreachable_for_no_favorite():
    """A NO favorite at cost 0.86 with a 35% take-profit targets 0.86*1.35=1.161,
    which exceeds the $1 payout: there is no reachable sell bid. This is the bug."""
    legacy_target = 0.86 * 1.35
    assert exit_bid_for_net(legacy_target, contracts=5.0) is None


def test_edge_based_take_profit_is_reachable_for_the_same_favorite():
    """The edge-based target (the model's fair value) is always inside [0,1] and
    therefore has a reachable sell bid."""
    target = convergence_take_profit_net(0.97)
    bid = exit_bid_for_net(target, contracts=5.0)
    assert bid is not None
    assert 0.97 < bid <= 0.99


# --- convergence take-profit semantics ---

def test_convergence_target_is_the_model_fair_value():
    assert convergence_take_profit_net(0.66) == 0.66
    assert convergence_take_profit_net(0.66, buffer=0.01) == 0.65
    # No model read -> ride to settlement (no scalp, no exit fee paid).
    assert convergence_take_profit_net(None) is None


def test_still_mispriced_favorite_holds_to_settlement():
    """NO bought at 0.86, model still 0.97: do NOT sell at 0.94 -- the contract is
    worth more than the sale proceeds, so hold (rides toward settlement)."""
    signal = decide_exit(
        side="NO",
        entry_cost=0.86,
        net_exit=0.94,
        stop_loss_net=0.86 * 0.65,
        model_side_probability=0.97,
    )
    assert signal.action == "HOLD"


def test_favorite_banks_when_price_reaches_fair_value():
    signal = decide_exit(
        side="NO",
        entry_cost=0.86,
        net_exit=0.975,
        stop_loss_net=0.86 * 0.65,
        model_side_probability=0.97,
    )
    assert signal.action == "TAKE_PROFIT"


def test_settlement_first_no_favorite_holds_instead_of_clipping_residual_upside():
    signal = decide_exit(
        side="NO",
        entry_cost=0.86,
        net_exit=0.975,
        stop_loss_net=0.86 * 0.65,
        model_side_probability=0.97,
        settlement_first_no_min_cost=0.73,
    )
    assert signal.action == "HOLD_SETTLEMENT_FIRST"


def test_settlement_first_no_does_not_block_edge_reversal_stop():
    signal = decide_exit(
        side="NO",
        entry_cost=0.86,
        net_exit=0.50,
        stop_loss_net=0.86 * 0.65,
        model_side_probability=0.45,
        settlement_first_no_min_cost=0.73,
    )
    assert signal.action == "STOP_LOSS"


def test_converged_no_books_profit_at_fair_value_not_at_ceiling():
    """NO entered at 0.40, model now 0.66: hold at 0.60 (still below fair value),
    bank at 0.67 -- the 0.40 -> 0.65 convergence the old %-rule never captured."""
    hold = decide_exit(
        side="NO",
        entry_cost=0.40,
        net_exit=0.60,
        stop_loss_net=0.40 * 0.65,
        model_side_probability=0.66,
    )
    assert hold.action == "HOLD"
    bank = decide_exit(
        side="NO",
        entry_cost=0.40,
        net_exit=0.67,
        stop_loss_net=0.40 * 0.65,
        model_side_probability=0.66,
    )
    assert bank.action == "TAKE_PROFIT"


def test_no_model_read_rides_to_settlement():
    signal = decide_exit(
        side="NO",
        entry_cost=0.86,
        net_exit=0.95,
        stop_loss_net=0.86 * 0.65,
        model_side_probability=None,
    )
    assert signal.action == "HOLD"


# --- stop-loss + model veto (preserved behavior) ---

def test_stop_loss_fires_when_model_no_longer_favors_the_side():
    signal = decide_exit(
        side="NO",
        entry_cost=0.86,
        net_exit=0.50,
        stop_loss_net=0.86 * 0.65,  # 0.559 floor
        model_side_probability=0.45,
    )
    assert signal.action == "STOP_LOSS"


def test_no_side_stop_is_vetoed_while_model_still_favors_settlement():
    """The June-2026 failure mode: a NO on track to win is sold into intraday
    noise. If the model still says it wins, hold instead of realizing the dip."""
    signal = decide_exit(
        side="NO",
        entry_cost=0.86,
        net_exit=0.50,
        stop_loss_net=0.86 * 0.65,
        model_side_probability=0.95,
    )
    assert signal.action == "HOLD_MODEL_VETO"


def test_veto_lapses_past_the_hard_loss_floor():
    signal = decide_exit(
        side="NO",
        entry_cost=0.86,
        net_exit=0.50,
        stop_loss_net=0.86 * 0.65,
        model_side_probability=0.95,
        model_veto_max_loss_roi=0.30,  # roi here is ~-0.42, past the floor
    )
    assert signal.action == "STOP_LOSS"


def test_yes_side_has_no_stop_veto():
    """The cheap-YES failure mode closes on the stop instead of vetoing."""
    signal = decide_exit(
        side="YES",
        entry_cost=0.30,
        net_exit=0.10,
        stop_loss_net=0.30 * 0.75,  # 0.225 floor
        model_side_probability=0.95,
    )
    assert signal.action == "STOP_LOSS"


# --- fail-safe: a NO stop must not fire on price noise with no model read ---

def test_no_side_stop_held_when_no_fresh_model_read():
    """The 2026-06-18 failure mode reproduced: a NO favorite drops to its hard
    %-of-cost floor while no fresh model read is available (the veto's data
    source had gone stale). It must HOLD, not crystallize the price-noise loss --
    the daily high is monotonic and the loss is already bounded by entry cost."""
    signal = decide_exit(
        side="NO",
        entry_cost=0.916,
        net_exit=0.573,
        stop_loss_net=0.916 * 0.65,  # 0.595 floor; net_exit is below it
        model_side_probability=None,
        model_veto_max_loss_roi=0.60,
    )
    assert signal.action == "HOLD_NO_MODEL_READ"


def test_no_side_failsafe_still_cuts_a_catastrophic_loss():
    """Even with no model read, a drawdown past the catastrophic floor stops --
    the fail-safe is for intraday noise, not for an unbounded disaster."""
    signal = decide_exit(
        side="NO",
        entry_cost=0.916,
        net_exit=0.30,  # roi ~ -0.67, past the 60% floor
        stop_loss_net=0.916 * 0.65,
        model_side_probability=None,
        model_veto_max_loss_roi=0.60,
    )
    assert signal.action == "STOP_LOSS"


def test_no_side_stop_fires_when_model_read_present_and_below_floor():
    """With a fresh model read that has dropped below the veto floor, the thesis
    IS confirmed dead and the stop fires (the fail-safe only covers the
    no-read case, never a read that says the side now loses)."""
    signal = decide_exit(
        side="NO",
        entry_cost=0.916,
        net_exit=0.573,
        stop_loss_net=0.916 * 0.65,
        model_side_probability=0.40,
        model_veto_max_loss_roi=0.60,
    )
    assert signal.action == "STOP_LOSS"


# --- helpers ---

def test_net_exit_per_contract_guards_invalid_bids():
    assert net_exit_per_contract(0.0, 5.0) == 0.0
    assert net_exit_per_contract(1.0, 5.0) == 0.0
    assert net_exit_per_contract(0.50, 0.0) == 0.0
    assert net_exit_per_contract(0.50, 5.0) < 0.50  # fee deducted


def test_exit_bid_for_net_returns_floor_bid_for_tiny_targets():
    assert exit_bid_for_net(0.0, 5.0) == 0.01
