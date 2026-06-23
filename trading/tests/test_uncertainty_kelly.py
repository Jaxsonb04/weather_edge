"""Phase 3 — side-agnostic Baker-McHale uncertainty shrink on Kelly.

Guards: the extracted _confidence_shrink behaves correctly, the YES helper is a
bit-identical refactor (shrink x payout), and the feature is inert by default.
"""

from sfo_kalshi_quant.config import StrategyConfig, strategy_config_for_profile
from sfo_kalshi_quant.risk import _confidence_shrink, _yes_sizing_factor


def test_confidence_shrink_is_identity_when_win_prob_is_certain():
    cfg = StrategyConfig()
    # p == p_lcb -> sigma 0 -> no uncertainty -> no haircut.
    assert _confidence_shrink(0.90, 0.90, 0.50, cfg) == 1.0


def test_confidence_shrink_decreases_as_uncertainty_widens():
    cfg = StrategyConfig()
    certain = _confidence_shrink(0.90, 0.90, 0.50, cfg)
    tight = _confidence_shrink(0.90, 0.86, 0.50, cfg)
    wide = _confidence_shrink(0.90, 0.70, 0.50, cfg)
    assert certain == 1.0
    assert 1.0 > tight > wide > 0.0


def test_confidence_shrink_zero_on_non_tradeable_or_no_edge():
    cfg = StrategyConfig()
    assert _confidence_shrink(0.9, 0.8, 0.0, cfg) == 0.0  # cost <= 0
    assert _confidence_shrink(0.9, 0.8, 1.0, cfg) == 0.0  # cost >= 1
    assert _confidence_shrink(0.40, 0.30, 0.50, cfg) == 0.0  # prob <= cost -> no edge


def test_yes_sizing_factor_equals_shrink_times_payout():
    # The refactor must be bit-identical: _yes_sizing_factor == shrink * cost.
    cfg = StrategyConfig()
    for p, lcb, cost in [(0.90, 0.85, 0.50), (0.20, 0.10, 0.08), (0.95, 0.95, 0.30), (0.4, 0.3, 0.5)]:
        assert abs(
            _yes_sizing_factor(p, lcb, cost, cfg) - _confidence_shrink(p, lcb, cost, cfg) * cost
        ) < 1e-12


def test_uncertainty_kelly_is_off_by_default_on_every_profile():
    assert StrategyConfig().uncertainty_kelly_enabled is False
    for profile in ("live", "research"):
        assert strategy_config_for_profile(profile).uncertainty_kelly_enabled is False


def test_shrink_sizes_down_an_uncertain_expensive_no_favorite():
    # An expensive NO favorite (cost ~0.90 -> small edge_e) with an HONEST lower-bound
    # gap is sized down hard; with p == p_lcb (no honest gap) it is untouched. (The
    # degenerate p == p_lcb == 1.0 case is the min_probability_uncertainty floor's job,
    # not the shrink's.)
    cfg = StrategyConfig()
    no_honest_gap = _confidence_shrink(0.99, 0.99, 0.90, cfg)  # p == LCB -> identity
    honest_uncertainty = _confidence_shrink(0.99, 0.80, 0.90, cfg)  # wide LCB -> strong shrink
    assert no_honest_gap == 1.0
    assert honest_uncertainty < 0.1  # expensive favorite + real uncertainty -> tiny bet
