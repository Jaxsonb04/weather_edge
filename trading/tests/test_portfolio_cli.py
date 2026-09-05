from __future__ import annotations

import io
import inspect
from contextlib import ExitStack, contextmanager, redirect_stdout
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import sfo_kalshi_quant.cli as cli_module
from sfo_kalshi_quant._cli import scan as scan_module

from sfo_kalshi_quant.cli import (
    _place_portfolio_orders,
    _print_portfolio_scan,
    build_parser,
    cmd_analyze,
    cmd_portfolio_scan,
)
from sfo_kalshi_quant.colors import Color
from sfo_kalshi_quant.cities import get_city
from sfo_kalshi_quant.config import (
    StrategyConfig,
    config_for_city,
    strategy_config_for_profile,
)
from sfo_kalshi_quant.forecast import ForecastDataError
from sfo_kalshi_quant.models import ForecastSnapshot
from sfo_kalshi_quant.portfolio import PortfolioLimits, PortfolioPlan
from sfo_kalshi_quant.paper import ArbitrageContainmentError


def test_portfolio_scan_parser_is_paper_only_by_default() -> None:
    args = build_parser().parse_args(
        [
            "--bankroll",
            "1000",
            "--risk-profile",
            "live",
            "portfolio-scan",
        ]
    )

    assert args.func is cmd_portfolio_scan
    assert args.target_date == "rolling"
    assert args.side == "both"
    assert args.place_paper is False
    assert args.place_research_target is False
    assert args.place_research_motion is False
    assert args.paper_entry_mode == "market"
    assert args.max_arb_spend == 12.0
    assert args.min_profit == 0.01


def test_portfolio_scan_parser_exposes_active_research_target_placement() -> None:
    args = build_parser().parse_args(
        [
            "--risk-profile",
            "research",
            "portfolio-scan",
            "--place-research-target",
        ]
    )

    assert args.place_paper is False
    assert args.place_research_target is True
    assert args.place_research_motion is False


def test_archived_motion_placement_flag_is_a_compatibility_noop() -> None:
    args = build_parser().parse_args(
        [
            "--risk-profile",
            "research",
            "portfolio-scan",
            "--place-research-motion",
        ]
    )

    assert args.place_research_motion is True
    assert scan_module._research_placement_flags(args) == (False, False)
    assert scan_module._paper_placement_requested(args) is False


def test_research_placement_switches_cannot_request_live_placement() -> None:
    args = build_parser().parse_args(
        [
            "--risk-profile",
            "live",
            "portfolio-scan",
            "--place-research-target",
            "--place-research-motion",
        ]
    )

    assert scan_module._research_placement_flags(args) == (False, False)
    assert scan_module._paper_placement_requested(args) is False


def test_paper_prune_help_marks_command_low_level_and_points_to_scheduled_service() -> None:
    help_text = build_parser().format_help()

    assert "Low-level/manual" in help_text
    assert "archive-gated service" in help_text


def test_portfolio_scan_parser_keeps_diagnostics_flags_available() -> None:
    args = build_parser().parse_args(
        [
            "--risk-profile",
            "research",
            "portfolio-scan",
            "--target-date",
            "both",
            "--side",
            "no",
            "--max-arb-spend",
            "20",
            "--min-profit",
            "0.05",
            "--paper-entry-mode",
            "limit",
            "--place-paper",
        ]
    )

    assert args.func is cmd_portfolio_scan
    assert args.target_date == "both"
    assert args.side == "no"
    assert args.max_arb_spend == 20.0
    assert args.min_profit == 0.05
    assert args.paper_entry_mode == "limit"
    assert args.place_paper is True


def test_portfolio_scan_prints_blocked_status_when_pause_prevents_placement() -> None:
    plan = PortfolioPlan(
        run_id="PF-test",
        risk_profile="research",
        approved=True,
        legs=[],
        arbitrage_opportunities=[],
        total_spend=12.34,
        worst_case_loss=12.34,
        expected_profit=1.23,
        reasons=[],
        limits=PortfolioLimits(
            risk_profile="research",
            bankroll=1000.0,
            max_daily_loss=250.0,
            yes_sleeve=50.0,
            explore_sleeve=12.5,
        ),
    )
    forecast = ForecastSnapshot(
        target_date=date(2026, 6, 20),
        predicted_high_f=68.0,
        method="fixture",
    )

    out = io.StringIO()
    with redirect_stdout(out):
        _print_portfolio_scan(
            "fixture event",
            forecast,
            plan,
            [],
            placed_ids=[],
            market_available=True,
            color=Color.from_no_color(True),
            entry_block_reason="research paused: daily loss cap reached",
        )

    text = out.getvalue()
    assert "research paused: daily loss cap reached" in text
    assert "portfolio=BLOCKED_BY_PAUSE" in text
    assert "portfolio=APPROVED" not in text


def test_portfolio_order_placement_contains_one_arbitrage_failure_and_continues() -> None:
    class _Trader:
        def __init__(self):
            self.arb_calls = 0
            self.directional_called = False

        def place_arbitrage(self, target_date, opportunity, *, bankroll):
            self.arb_calls += 1
            if self.arb_calls == 1:
                raise RuntimeError("simulated box race")
            return [22]

        def place_approved(self, target_date, decisions, *, bankroll):
            self.directional_called = True
            return [33]

    trader = _Trader()
    plan = SimpleNamespace(
        arbitrage_opportunities=[object(), object()],
        legs=[SimpleNamespace(sleeve="directional", decision=object())],
    )
    warnings: list[str] = []

    placed = _place_portfolio_orders(
        trader,
        "2026-06-03",
        plan,
        bankroll=1000.0,
        warn=warnings.append,
    )

    assert placed == [22, 33]
    assert trader.arb_calls == 2
    assert trader.directional_called
    assert warnings and "simulated box race" in warnings[0]


def test_portfolio_order_placement_stops_target_on_fatal_arbitrage_containment():
    class _Trader:
        directional_called = False

        def place_arbitrage(self, target_date, opportunity, *, bankroll):
            raise ArbitrageContainmentError("naked filled leg remains")

        def place_approved(self, target_date, decisions, *, bankroll):
            self.directional_called = True
            return [99]

    trader = _Trader()
    plan = SimpleNamespace(
        arbitrage_opportunities=[object()],
        legs=[SimpleNamespace(sleeve="directional", decision=object())],
    )

    with pytest.raises(ArbitrageContainmentError):
        _place_portfolio_orders(
            trader,
            "2026-06-03",
            plan,
            bankroll=1000.0,
        )

    assert trader.directional_called is False


def test_portfolio_scan_returns_nonzero_after_fatal_containment_but_continues_city_loop():
    args = build_parser().parse_args(
        ["--risk-profile", "live", "portfolio-scan", "--cities", "sfo,nyc"]
    )
    target = date(2026, 7, 10)
    adapter = Mock()
    adapter.load_calibration_outcomes.return_value = [object()] * 30

    with (
        patch(
            "sfo_kalshi_quant.cli._cities_for_args",
            return_value=(get_city("sfo"), get_city("nyc")),
        ),
        patch(
            "sfo_kalshi_quant.cli._resolve_analysis_targets",
            return_value=([target], {}),
        ),
        patch("sfo_kalshi_quant.cli.SfoForecasterAdapter", return_value=adapter),
        patch("sfo_kalshi_quant.cli.ResidualCalibrator", return_value=object()),
        patch("sfo_kalshi_quant.cli.KalshiPublicClient"),
        patch(
            "sfo_kalshi_quant.cli._config",
            return_value=StrategyConfig(emos_distribution_enabled=True),
        ),
        patch(
            "sfo_kalshi_quant.cli._portfolio_scan_one_target",
            side_effect=[ArbitrageContainmentError("residual exposure"), None],
        ) as scan_target,
    ):
        code = cmd_portfolio_scan(args)

    assert code == 1
    assert scan_target.call_count == 2


def test_portfolio_scan_returns_nonzero_when_no_city_has_an_eligible_target():
    args = build_parser().parse_args(
        ["--risk-profile", "live", "portfolio-scan", "--cities", "sfo,nyc"]
    )
    with (
        patch(
            "sfo_kalshi_quant.cli._cities_for_args",
            return_value=(get_city("sfo"), get_city("nyc")),
        ),
        patch(
            "sfo_kalshi_quant.cli._resolve_analysis_targets",
            return_value=([], {}),
        ),
        patch("sfo_kalshi_quant.cli.KalshiPublicClient"),
        patch("sfo_kalshi_quant.cli.PaperStore"),
    ):
        code = cmd_portfolio_scan(args)

    assert code == 1


def test_analyze_returns_nonzero_when_no_city_has_an_eligible_target():
    args = build_parser().parse_args(
        ["--risk-profile", "live", "analyze", "--cities", "sfo,nyc"]
    )
    with (
        patch(
            "sfo_kalshi_quant.cli._cities_for_args",
            return_value=(get_city("sfo"), get_city("nyc")),
        ),
        patch(
            "sfo_kalshi_quant.cli._resolve_analysis_targets",
            return_value=([], {}),
        ),
        patch("sfo_kalshi_quant.cli.KalshiPublicClient"),
        patch("sfo_kalshi_quant.cli.PaperStore"),
    ):
        code = cmd_analyze(args)

    assert code == 1


def test_portfolio_scan_hoists_emos_and_sizing_model_once_per_city():
    args = build_parser().parse_args(
        ["--risk-profile", "live", "portfolio-scan", "--cities", "sfo"]
    )
    targets = [date(2026, 7, 10), date(2026, 7, 11), date(2026, 7, 12)]
    adapter = Mock()
    adapter.load_calibration_outcomes.return_value = [object()] * 30
    emos = {target: (70.0, 2.5) for target in targets}
    adapter.load_emos_mu_sigma.return_value = emos
    sizing_model = object()

    with (
        patch("sfo_kalshi_quant.cli._cities_for_args", return_value=(get_city("sfo"),)),
        patch(
            "sfo_kalshi_quant.cli._resolve_analysis_targets",
            return_value=(targets, {}),
        ),
        patch("sfo_kalshi_quant.cli.SfoForecasterAdapter", return_value=adapter),
        patch("sfo_kalshi_quant.cli.ResidualCalibrator", return_value=object()),
        patch("sfo_kalshi_quant.cli.KalshiPublicClient"),
        patch(
            "sfo_kalshi_quant.cli._config",
            return_value=StrategyConfig(emos_distribution_enabled=True),
        ),
        patch(
            "sfo_kalshi_quant.cli._build_sizing_model", return_value=sizing_model
        ) as build_model,
        patch("sfo_kalshi_quant.cli._portfolio_scan_one_target") as scan_target,
    ):
        code = cmd_portfolio_scan(args)

    assert code == 0
    adapter.load_emos_mu_sigma.assert_called_once_with(lead_days=None)
    build_model.assert_called_once()
    assert scan_target.call_count == len(targets)
    assert all(call.kwargs["emos_lookup"] is emos for call in scan_target.call_args_list)
    assert all(
        call.kwargs["sizing_model"] is sizing_model for call in scan_target.call_args_list
    )
    pause_caches = [call.kwargs["pause_reasons"] for call in scan_target.call_args_list]
    assert pause_caches[0] is pause_caches[1] is pause_caches[2]


def test_pause_reason_cache_is_keyed_by_exact_profile_and_target():
    store = Mock()
    store.paper_entry_pause_reason.side_effect = [None, "paused", None]
    cache = {}

    first = cli_module._cached_paper_entry_pause_reason(
        store, "live", bankroll=1000.0, target_date="2026-07-10", cache=cache
    )
    repeated = cli_module._cached_paper_entry_pause_reason(
        store, "live", bankroll=1000.0, target_date="2026-07-10", cache=cache
    )
    next_target = cli_module._cached_paper_entry_pause_reason(
        store, "live", bankroll=1000.0, target_date="2026-07-11", cache=cache
    )
    other_profile = cli_module._cached_paper_entry_pause_reason(
        store, "research", bankroll=1000.0, target_date="2026-07-10", cache=cache
    )

    assert first is repeated is None
    assert next_target == "paused"
    assert other_profile is None
    assert store.paper_entry_pause_reason.call_count == 3


def test_analysis_and_portfolio_scans_share_one_context_builder() -> None:
    assert hasattr(cli_module, "ScanContext")
    assert hasattr(cli_module, "build_scan_context")

    analyze_source = inspect.getsource(scan_module._analyze_one_target)
    portfolio_source = inspect.getsource(scan_module._portfolio_scan_one_target)
    assert analyze_source.count("build_scan_context(") == 1
    assert portfolio_source.count("build_scan_context(") == 1
    for duplicated_step in (
        "adapter.latest_blend(",
        "calibrator.bucket_probabilities(",
        "build_market_consensus(",
        "evaluator.rank(",
    ):
        assert duplicated_step not in analyze_source
        assert duplicated_step not in portfolio_source


def test_build_scan_context_preserves_event_fallback_and_injected_sizing_model() -> None:
    target = date(2026, 7, 12)
    city = get_city("nyc")
    forecast = ForecastSnapshot(
        target_date=target,
        predicted_high_f=82.0,
        source_spread_override_f=3.0,
        method="fixture",
    )
    market = cli_module.fallback_bins("KXHIGHNY-26JUL12", 82.0)[0]
    event = cli_module.EventSnapshot(
        event_ticker="KXHIGHNY-26JUL12",
        title="NY fixture",
        target_date=target,
        markets=[market],
    )
    adapter = Mock()
    adapter.latest_live_forecast.return_value = forecast
    adapter.load_emos_mu_sigma.return_value = {target: (82.0, 3.0)}
    calibrator = Mock()
    probabilities = {market.ticker: object()}
    calibrator.bucket_probabilities.return_value = probabilities
    store = Mock()
    evaluator = Mock()
    decisions = [object()]
    evaluator.rank.return_value = decisions
    sizing_model = object()
    args = SimpleNamespace(offline_events=None, side="both")

    with (
        patch("sfo_kalshi_quant.cli._enforce_live_forecast_freshness"),
        patch("sfo_kalshi_quant.cli._intraday_for_target", return_value=None),
        patch("sfo_kalshi_quant.cli.build_market_consensus", return_value="consensus"),
        patch("sfo_kalshi_quant.cli._risk_profile_name", return_value="live"),
        patch("sfo_kalshi_quant.cli._sizing_bankroll", return_value=750.0),
        patch("sfo_kalshi_quant.cli._build_sizing_model") as build_sizing,
        patch("sfo_kalshi_quant.cli.TradeEvaluator", return_value=evaluator) as evaluator_type,
    ):
        listed = cli_module.build_scan_context(
            args,
            target,
            adapter,
            calibrator,
            StrategyConfig(emos_distribution_enabled=True),
            store,
            Color.from_no_color(True),
            city=city,
            event_hint=event,
            event_lookup_done=True,
            sizing_model=sizing_model,
            fallback_event_title="fallback",
        )
        fallback = cli_module.build_scan_context(
            args,
            target,
            adapter,
            calibrator,
            StrategyConfig(emos_distribution_enabled=True),
            store,
            Color.from_no_color(True),
            city=city,
            event_lookup_done=True,
            sizing_model=sizing_model,
            fallback_event_title="fallback",
        )

    assert listed.event is event
    assert listed.event_title == "NY fixture"
    assert listed.market_available is True
    assert listed.paper_bankroll == 750.0
    assert listed.decisions is decisions
    assert fallback.event is None
    assert fallback.event_title == "fallback"
    assert fallback.market_available is False
    build_sizing.assert_not_called()
    assert evaluator_type.call_count == 2
    assert all(call.kwargs["sizing_model"] is sizing_model for call in evaluator_type.call_args_list)


# --- PR-52: the scan/decision path must not price an EMOS point against a
# mismatched residual law ----------------------------------------------------


def _sfo_operational_fallback_forecast(target: date) -> ForecastSnapshot:
    """Exactly what ``latest_live_forecast`` returns for SFO under the fallback.

    The legacy SFO blend stopped regenerating, so a fresh live EMOS row stands
    in for it. ``raw`` carries the EMOS Gaussian that was fitted with that point.
    """

    return ForecastSnapshot(
        target_date=target,
        predicted_high_f=71.5,
        station_id="KSFO",
        fetched_at="2026-08-16T05:41:48+00:00",
        method="emos-wmean (live NWP ensemble) [SFO operational fallback]",
        source_spread_override_f=3.2,
        raw={
            "source": "forecast_emos_daily_high",
            "emos": {
                "mu": 71.5,
                "sigma": 2.54,
                "n_models": 8,
                "model_spread_f": 3.2,
                "lead_days": 1,
            },
            "operational_fallback": {
                "reason": "legacy_sfo_blend_stale",
                "legacy_blend_fetched_at": "2026-08-15T09:44:30+00:00",
                "max_age_hours": 12.0,
            },
        },
    )


@contextmanager
def _scan_context_patches():
    """Stub the scan-context collaborators that are not under test here.

    Yields the stack so each test can add its own ``_intraday_for_target``.
    """

    with ExitStack() as stack:
        for name, kwargs in (
            ("_enforce_live_forecast_freshness", {}),
            ("build_market_consensus", {"return_value": "consensus"}),
            ("_risk_profile_name", {"return_value": "live"}),
            ("_sizing_bankroll", {"return_value": 750.0}),
            ("_build_sizing_model", {"return_value": object()}),
            ("TradeEvaluator", {"return_value": Mock()}),
        ):
            stack.enter_context(patch(f"sfo_kalshi_quant.cli.{name}", **kwargs))
        yield stack


def test_live_profile_sfo_fallback_scan_fails_closed_instead_of_mispricing() -> None:
    # REGRESSION (PR-52): the live profile deliberately leaves
    # emos_distribution_enabled off, so before the guard this scan priced the
    # EMOS point through the legacy blend's residual law -- a sigma ~1.8-2.0x too
    # wide, per-bin probability errors averaging 0.076 against a 0.012 min_edge.
    # It must now refuse the target rather than emit mispriced buckets.
    target = date(2026, 8, 16)
    city = get_city("sfo")
    config = config_for_city(strategy_config_for_profile("live"), city)
    assert config.emos_distribution_enabled is False

    adapter = Mock()
    adapter.latest_live_forecast.return_value = _sfo_operational_fallback_forecast(target)
    calibrator = Mock()

    with _scan_context_patches() as stack:
        intraday = stack.enter_context(
            patch("sfo_kalshi_quant.cli._intraday_for_target", return_value=None)
        )
        with pytest.raises(ForecastDataError, match="matching EMOS distribution"):
            cli_module.build_scan_context(
                SimpleNamespace(offline_events=None, side="both"),
                target,
                adapter,
                calibrator,
                config,
                Mock(),
                Color.from_no_color(True),
                city=city,
                event_lookup_done=True,
                sizing_model=object(),
                fallback_event_title="fallback",
            )

    # Fail closed BEFORE any pricing or market work happens.
    calibrator.bucket_probabilities.assert_not_called()
    intraday.assert_not_called()
    adapter.load_emos_mu_sigma.assert_not_called()


def test_tail_basket_one_target_fails_closed_on_the_same_mismatch() -> None:
    # The tail-basket scan builds its own context, so it carries the same guard.
    target = date(2026, 8, 16)
    city = get_city("sfo")
    config = config_for_city(strategy_config_for_profile("live"), city)
    adapter = Mock()
    adapter.latest_live_forecast.return_value = _sfo_operational_fallback_forecast(target)

    with (
        patch.object(scan_module, "_enforce_live_forecast_freshness"),
        patch.object(scan_module, "_intraday_for_target", return_value=None),
    ):
        with pytest.raises(ForecastDataError, match="matching EMOS distribution"):
            scan_module._tail_basket_one_target(
                SimpleNamespace(offline_events=None, side="both"),
                target,
                adapter,
                Mock(),
                config,
                Mock(),
                Color.from_no_color(True),
                city=city,
                event_lookup_done=True,
                sizing_model=object(),
            )


def test_tail_basket_command_has_no_per_target_forecast_catch() -> None:
    # BEHAVIOR CHANGE, stated explicitly: portfolio-scan and analyze contain a
    # fail-closed city/target, tail-basket does not and now exits 2 for live SFO
    # under the fallback. portfolio-scan is the production path; tail-basket is a
    # diagnostic. Pin the asymmetry so it cannot drift unnoticed.
    assert "except ForecastDataError" in inspect.getsource(scan_module.cmd_portfolio_scan)
    assert "except ForecastDataError" in inspect.getsource(scan_module.cmd_analyze)
    assert "except ForecastDataError" not in inspect.getsource(scan_module.cmd_tail_basket)


def test_portfolio_scan_contains_a_fail_closed_city_and_still_succeeds() -> None:
    # SFO failing closed must not take the other fourteen cities down with it,
    # and must not fail the publication cycle.
    args = build_parser().parse_args(
        ["--risk-profile", "live", "portfolio-scan", "--cities", "sfo,nyc"]
    )
    target = date(2026, 8, 16)
    adapter = Mock()
    adapter.load_calibration_outcomes.return_value = [object()] * 30

    with (
        patch(
            "sfo_kalshi_quant.cli._cities_for_args",
            return_value=(get_city("sfo"), get_city("nyc")),
        ),
        patch("sfo_kalshi_quant.cli._resolve_analysis_targets", return_value=([target], {})),
        patch("sfo_kalshi_quant.cli.SfoForecasterAdapter", return_value=adapter),
        patch("sfo_kalshi_quant.cli.ResidualCalibrator", return_value=object()),
        patch("sfo_kalshi_quant.cli.KalshiPublicClient"),
        patch(
            "sfo_kalshi_quant.cli._portfolio_scan_one_target",
            side_effect=[
                ForecastDataError("EMOS point forecast requires its matching EMOS distribution"),
                None,
            ],
        ) as scan_target,
    ):
        code = cmd_portfolio_scan(args)

    assert code == 0
    assert scan_target.call_count == 2


def test_emos_city_scan_prices_the_point_against_its_own_coupled_distribution() -> None:
    # The fourteen EMOS cities keep the flag on, so the guard returns instead of
    # raising and the SAME-ROW pair becomes authoritative for this target. In
    # production the hoisted archive lookup already agrees (495/495 station-days
    # checked), so this is a no-op there; here the lookup is deliberately made to
    # disagree to prove which pair wins.
    target = date(2026, 8, 16)
    city = get_city("nyc")
    config = config_for_city(strategy_config_for_profile("live"), city)
    assert config.emos_distribution_enabled is True

    forecast = _sfo_operational_fallback_forecast(target)
    market = cli_module.fallback_bins("KXHIGHNY-26AUG16", 71.5)[0]
    event = cli_module.EventSnapshot(
        event_ticker="KXHIGHNY-26AUG16",
        title="NY fixture",
        target_date=target,
        markets=[market],
    )
    adapter = Mock()
    adapter.latest_live_forecast.return_value = forecast
    other = date(2026, 8, 17)
    hoisted = {target: (99.0, 9.0), other: (70.0, 2.0)}
    calibrator = Mock()
    calibrator.bucket_probabilities.return_value = {market.ticker: object()}
    evaluator = Mock()
    evaluator.rank.return_value = []

    with _scan_context_patches() as stack:
        stack.enter_context(
            patch("sfo_kalshi_quant.cli.TradeEvaluator", return_value=evaluator)
        )
        stack.enter_context(
            patch("sfo_kalshi_quant.cli._intraday_for_target", return_value=None)
        )
        cli_module.build_scan_context(
            SimpleNamespace(offline_events=None, side="both"),
            target,
            adapter,
            calibrator,
            config,
            Mock(),
            Color.from_no_color(True),
            city=city,
            event_hint=event,
            event_lookup_done=True,
            emos_lookup=hoisted,
            sizing_model=object(),
            fallback_event_title="fallback",
        )

    assert calibrator.bucket_probabilities.call_args.kwargs["emos_mu_sigma"] == (71.5, 2.54)
    assert evaluator.rank.call_args.kwargs["forecast_sigma_f"] == 2.54
    # The per-city hoisted lookup is shared across every target of that city, so
    # the injection must copy rather than mutate it.
    assert hoisted == {target: (99.0, 9.0), other: (70.0, 2.0)}


def test_emos_coupling_guard_runs_before_the_intraday_recentering() -> None:
    # LOAD-BEARING CALL PLACEMENT: apply_intraday_update deliberately moves the
    # point away from mu, so a guard called after it would fail the coupling
    # check and break all fourteen EMOS cities. Pin the ordering.
    target = date(2026, 8, 16)
    city = get_city("nyc")
    config = config_for_city(strategy_config_for_profile("live"), city)

    forecast = _sfo_operational_fallback_forecast(target)
    recentered = replace(forecast, predicted_high_f=76.0)
    market = cli_module.fallback_bins("KXHIGHNY-26AUG16", 76.0)[0]
    adapter = Mock()
    adapter.latest_live_forecast.return_value = forecast
    adapter.apply_intraday_update.return_value = recentered
    adapter.load_emos_mu_sigma.return_value = {target: (71.5, 2.54)}
    calibrator = Mock()
    calibrator.bucket_probabilities.return_value = {market.ticker: object()}

    with _scan_context_patches() as stack:
        stack.enter_context(
            patch(
                "sfo_kalshi_quant.cli._intraday_for_target",
                return_value=SimpleNamespace(observed_high_f=74.0),
            )
        )
        cli_module.build_scan_context(
            SimpleNamespace(offline_events=None, side="both"),
            target,
            adapter,
            calibrator,
            config,
            Mock(),
            Color.from_no_color(True),
            city=city,
            event_lookup_done=True,
            sizing_model=object(),
            fallback_event_title="fallback",
        )

    adapter.apply_intraday_update.assert_called_once()
    call = calibrator.bucket_probabilities.call_args
    assert call.args[1] == 76.0  # the intraday-recentered point still drives pricing
    assert call.kwargs["emos_mu_sigma"] == (71.5, 2.54)
