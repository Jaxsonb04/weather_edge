"""M1 market-recalibration trial: pinned math, filters, sizing and replay.

The reference values in ``VLIB_ROWS`` were computed with the verified research
implementation (lead-quant ``vlib.py``: ``bucket``, ``logit``, ``fee_pc``,
``fee_order`` and ``settle(mode="int")``) and the frozen deployment
coefficients, then checked by hand. They pin the production math to the
validated math row by row.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from sfo_kalshi_quant import research_m1 as m1
from sfo_kalshi_quant.account import strategy_fingerprint
from sfo_kalshi_quant.config import strategy_config_for_profile
from sfo_kalshi_quant.research_policy import (
    MOTION_POLICY,
    TARGET_POLICY,
    TARGET_POLICY_V1,
    TARGET_POLICY_V2,
    TARGET_POLICY_V3,
    TARGET_POLICY_V4,
    TARGET_POLICY_V5,
    TARGET_POLICY_V6,
    TARGET_POLICY_V7,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
COEFFICIENT_FILE = REPO_ROOT / "docs" / "research" / "m1_deploy_market_recal_coefs.json"
PREREGISTRATION = REPO_ROOT / "docs" / "research" / "m1_preregistration.md"
LA = ZoneInfo("America/Los_Angeles")
TARGET = date(2026, 10, 21)


def _la(hour: int, minute: int = 0, day: date = date(2026, 10, 20)) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LA).astimezone(UTC)


def _inputs(**overrides) -> m1.M1LegInputs:
    values = dict(
        created_at=_la(12, 15),
        target_date=TARGET,
        market_ticker="KXHIGHLAX-26OCT21-B70.5",
        side="YES",
        civil_tz="America/Los_Angeles",
        market_probability=0.50,
        model_probability=0.55,
        ask=0.48,
        bid=0.46,
        ask_size=40.0,
        bid_size=30.0,
        market_status="active",
        market_close_time="2026-10-22T07:59:00Z",
        forecast_age_hours=2.0,
        source_spread_f=1.5,
        entry_gate_reason=None,
    )
    values.update(overrides)
    return m1.M1LegInputs(**values)


# (side, civil local time, side market_probability, ask, bid, ask size) ->
# vlib bucket, q_yes, p_yes, p_trade, one-contract fee, cost, edge, signal,
# uncapped integer contracts/stake, depth-and-25 capped contracts/stake.
VLIB_ROWS = [
    ("YES", (9, 30), 0.62, 0.60, 0.58, 40.0, "L1a", 0.62, 0.7161269022871964,
     0.7161269022871964, 0.02, 0.62, 0.09612690228719645, True, 16, 9.87, 16, 9.87),
    ("NO", (12, 15), 0.85, 0.80, 0.78, 3.0, "L1b", 0.15000000000000002,
     0.1328971642678682, 0.8671028357321318, 0.02, 0.8200000000000001,
     0.0471028357321317, True, 12, 9.740000000000002, 3, 2.4400000000000004),
    ("YES", (16, 0), 0.999, 0.97, 0.95, 100.0, "L1c", 0.999, 0.9987024876669186,
     0.9987024876669186, 0.01, 0.98, 0.01870248766691862, False, 10,
     9.729999999999999, 10, 9.729999999999999),
    ("NO", (21, 45), 0.70, 0.66, 0.64, 12.0, "L1d", 0.30000000000000004,
     0.30658070897141143, 0.6934192910285886, 0.02, 0.68, 0.01341929102858852,
     False, 14, 9.46, 12, 8.11),
    ("YES", (11, 0), 0.50, 0.48, 0.46, 7.0, "L1b", 0.5, 0.5537910229492018,
     0.5537910229492018, 0.02, 0.5, 0.053791022949201794, True, 20, 9.95, 7,
     3.4899999999999998),
    ("YES", (18, 59), 0.40, 0.37, 0.35, 50.0, "L1c", 0.4, 0.43360205550189007,
     0.43360205550189007, 0.02, 0.39, 0.04360205550189006, True, 25, 9.66, 25, 9.66),
    ("YES", (19, 0), 0.40, 0.37, 0.35, 50.0, "L1d", 0.4, 0.4337522835839401,
     0.4337522835839401, 0.02, 0.39, 0.043752283583940066, True, 25, 9.66, 25, 9.66),
    ("NO", (10, 59), 0.999, 0.99, 0.98, 30.0, "L1a", 0.0010000000000000009,
     0.0015435963071481382, 0.9984564036928518, 0.01, 1.0, -0.0015435963071481762,
     False, 10, 9.91, 10, 9.91),
    ("NO", (20, 0), 0.93, 0.89, 0.86, 9.0, "L1d", 0.06999999999999995,
     0.048347617683129, 0.951652382316871, 0.01, 0.9, 0.05165238231687097, True,
     11, 9.870000000000001, 9, 8.08),
]


def test_coefficients_are_the_committed_deployment_refit() -> None:
    raw = COEFFICIENT_FILE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "bd373ea3e925b767aa6cba8972407e7638841d2e6dd03e29c24c3ead927bce86"
    )
    deploy = json.loads(raw)["refit_on_all_2026-07-19_to_10-28 (for deployment)"]
    expected = tuple(
        (bucket, deploy[f"market-{bucket}"]["a"], deploy[f"market-{bucket}"]["c"])
        for bucket in ("L1a", "L1b", "L1c", "L1d")
    )
    assert m1.M1_COEFFICIENTS == expected
    assert m1.M1_POLICY.coefficients == expected
    assert m1.M1_COEFFICIENT_SET_ID == "m1-deploy-coefs-2026-10-28"
    # The pre-registration names the same file hash and coefficient table.
    text = PREREGISTRATION.read_text(encoding="utf-8")
    assert hashlib.sha256(raw).hexdigest() in text
    for bucket, a, c in expected:
        assert f"| {bucket} |" in text
        assert f"{a:.4f} | {c:.4f}" in text


@pytest.mark.parametrize("row", VLIB_ROWS, ids=lambda row: f"{row[0]}-{row[1]}")
def test_rule_math_matches_the_verified_research_rows(row) -> None:
    (side, (hour, minute), mp, ask, bid, depth, bucket, q_yes, p_yes, p_trade, fee,
     cost, edge, signal, uncapped_n, uncapped_stake, capped_n, capped_stake) = row
    evaluation = m1.evaluate_leg(
        _inputs(
            side=side,
            created_at=_la(hour, minute),
            market_probability=mp,
            model_probability=mp,
            ask=ask,
            bid=bid,
            ask_size=depth,
        )
    )
    assert evaluation.bucket == bucket
    assert evaluation.q_yes == pytest.approx(q_yes, abs=1e-12)
    assert evaluation.p_yes == pytest.approx(p_yes, abs=1e-12)
    assert evaluation.p_trade == pytest.approx(p_trade, abs=1e-12)
    assert evaluation.gate_fee == pytest.approx(fee, abs=1e-12)
    assert evaluation.cost == pytest.approx(cost, abs=1e-12)
    assert evaluation.edge == pytest.approx(edge, abs=1e-12)
    assert evaluation.signal is signal
    assert m1.size_contracts(ask, 1_000_000.0) == min(uncapped_n, 25)
    if uncapped_n <= 25:
        assert m1.entry_stake(ask, uncapped_n) == pytest.approx(uncapped_stake, abs=1e-9)
    assert evaluation.contracts == capped_n
    assert evaluation.stake == pytest.approx(capped_stake, abs=1e-9)


def test_logit_clips_at_half_a_percent() -> None:
    assert m1.logit_clipped(0.0) == pytest.approx(m1.logit_clipped(0.005))
    assert m1.logit_clipped(0.001) == pytest.approx(-5.293304824724492, abs=1e-12)
    assert m1.logit_clipped(1.0) == pytest.approx(5.293304824724492, abs=1e-12)
    assert m1.logit_clipped(0.5) == 0.0


@pytest.mark.parametrize(
    ("local_hour", "bucket"),
    [(0.0, "L1a"), (10.9833, "L1a"), (11.0, "L1b"), (14.99, "L1b"), (15.0, "L1c"),
     (18.9833, "L1c"), (19.0, "L1d"), (23.9833, "L1d")],
)
def test_bucket_boundaries_follow_the_d_minus_one_local_hour(local_hour, bucket) -> None:
    assert m1.bucket_for_local_hour(local_hour) == bucket


def test_bucket_uses_the_station_civil_clock_and_phoenix_has_no_dst() -> None:
    created = datetime(2026, 7, 14, 18, 30, tzinfo=UTC)
    # 11:30 PDT in Los Angeles but 11:30 MST in Phoenix; both L1b in July.
    assert m1.civil_local_hour(created, "America/Los_Angeles") == 11.5
    assert m1.civil_local_hour(created, "America/Phoenix") == 11.5
    winter = datetime(2026, 12, 14, 18, 30, tzinfo=UTC)
    assert m1.civil_local_hour(winter, "America/Los_Angeles") == 10.5
    assert m1.civil_local_hour(winter, "America/Phoenix") == 11.5


def test_listing_hour_is_ten_eastern_on_d_minus_one_across_the_dst_change() -> None:
    # Target 2026-11-02: D-1 is the first EST day, so the listing hour is 15 UTC.
    assert m1.is_listing_hour(date(2026, 11, 2), datetime(2026, 11, 1, 15, 0, 36, tzinfo=UTC))
    assert not m1.is_listing_hour(date(2026, 11, 2), datetime(2026, 11, 1, 14, 30, tzinfo=UTC))
    assert not m1.is_listing_hour(date(2026, 11, 2), datetime(2026, 11, 1, 16, 0, tzinfo=UTC))
    # Under EDT the same rule is 14 UTC, the hour the research coded.
    assert m1.is_listing_hour(date(2026, 10, 15), datetime(2026, 10, 14, 14, 5, tzinfo=UTC))
    assert not m1.is_listing_hour(date(2026, 10, 15), datetime(2026, 10, 14, 15, 5, tzinfo=UTC))
    # Ten o'clock Eastern on the target day itself is not the listing hour.
    assert not m1.is_listing_hour(date(2026, 10, 15), datetime(2026, 10, 15, 14, 5, tzinfo=UTC))


def test_lead_is_measured_on_the_station_civil_calendar() -> None:
    # 00:30 PDT on Oct 20 is still Oct 19 on the fixed-standard clock; the
    # civil day decides the M1 lead.
    created = datetime(2026, 10, 20, 7, 30, tzinfo=UTC)
    assert m1.civil_lead_days(TARGET, created, "America/Los_Angeles") == 1
    assert m1.civil_lead_days(TARGET, created, "America/New_York") == 1
    late = datetime(2026, 10, 21, 4, 30, tzinfo=UTC)
    assert m1.civil_lead_days(TARGET, late, "America/New_York") == 0
    assert m1.civil_lead_days(TARGET, late, "America/Los_Angeles") == 1


def test_sizing_spends_ten_dollars_with_the_order_fee_in_whole_contracts() -> None:
    assert m1.size_contracts(0.60, 100.0) == 16
    assert m1.entry_stake(0.60, 16) == pytest.approx(9.87)
    assert m1.order_fee(0.60, 16) == pytest.approx(0.27)
    # The 25-contract cap and the displayed depth both clamp.
    assert m1.size_contracts(0.37, 100.0) == 25
    assert m1.size_contracts(0.80, 3.9) == 3
    assert m1.size_contracts(0.80, 0.5) == 0
    # The per-order fee is rounded once per order, not per contract.
    assert m1.order_fee(0.85, 4) == pytest.approx(0.04)
    assert 4 * m1.gate_fee(0.85) == pytest.approx(0.04)
    assert m1.order_fee(0.50, 11) == pytest.approx(0.20)
    assert 11 * m1.gate_fee(0.50) == pytest.approx(0.22)
    for ask in (0.05, 0.31, 0.5, 0.77, 0.99):
        contracts = m1.size_contracts(ask, 1_000.0)
        assert contracts * ask + m1.order_fee(ask, contracts) <= 10.0 + 1e-9
        assert (
            contracts == 25
            or (contracts + 1) * ask + m1.order_fee(ask, contracts + 1) > 10.0
        )


def test_spread_filter_compares_integer_cents() -> None:
    # 0.52 - 0.42 is 0.10000000000000003 in floating point: the float
    # comparison the research used dropped exact 10-cent spreads.
    assert (0.52 - 0.42) > 0.10
    assert m1.spread_cents(0.52, 0.42) == 10
    passing = m1.evaluate_leg(_inputs(ask=0.52, bid=0.42, market_probability=0.60))
    assert passing.filter_reason is None
    failing = m1.evaluate_leg(_inputs(ask=0.53, bid=0.42, market_probability=0.60))
    assert failing.decision == m1.DECISION_FILTERED
    assert failing.filter_reason == "spread 11c above 10c"


def test_a_clean_leg_passes_and_records_agreement() -> None:
    evaluation = m1.evaluate_leg(_inputs())
    assert evaluation.passes
    assert evaluation.decision == m1.DECISION_PENDING
    assert evaluation.reason is None
    assert evaluation.agree is True  # p_model 0.55 >= cost 0.50
    assert m1.evaluate_leg(_inputs(model_probability=0.49)).agree is False
    # Agreement is recorded, never gated.
    assert m1.evaluate_leg(_inputs(model_probability=0.49)).passes


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"target_date": date(2026, 10, 22)}, "lead 2 is not day-ahead (lead 1)"),
        ({"target_date": date(2026, 10, 20)}, "lead 0 is not day-ahead (lead 1)"),
        ({"created_at": _la(7, 30)}, "listing hour (D-1 10:00-10:59 America/New_York)"),
        ({"market_status": "closed"}, "market status is closed, not active"),
        ({"market_close_time": "2026-10-20T19:00:00Z"}, "market is past its close time"),
        ({"market_probability": None}, "no same-scan de-vigged market probability"),
        ({"bid": 0.0, "ask": 0.05}, "entry bid below 0.01"),
        ({"ask": 1.0, "bid": 0.95}, "entry ask outside (0, 0.99]"),
        ({"ask": 0.0}, "entry ask outside (0, 0.99]"),
        ({"ask": 0.99, "bid": 0.95, "market_probability": 0.999}, "all-in cost meets or exceeds the $1 payout"),
        ({"ask_size": 0.0}, "displayed ask size below 1"),
    ],
)
def test_every_m1_filter_rejects_with_a_reproducible_reason(overrides, reason) -> None:
    evaluation = m1.evaluate_leg(_inputs(**overrides))
    assert not evaluation.passes
    assert evaluation.decision == m1.DECISION_FILTERED
    assert evaluation.reason == reason


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"bid_size": 0.0}, "bid size below 1"),
        ({"model_probability": 0.80}, "model/market gap 0.300 exceeds 0.25"),
        ({"model_probability": None}, "model/market gap unavailable"),
        ({"source_spread_f": 7.4}, "forecast source spread 7.4F exceeds 7.3F"),
        ({"forecast_age_hours": 30.5}, "forecast 30.5h old exceeds 30h"),
        ({"forecast_age_hours": None}, "forecast age unavailable"),
        (
            {"entry_gate_reason": "paper entry disabled: single-source forecast"},
            "paper entry disabled: single-source forecast",
        ),
    ],
)
def test_existing_risk_limits_still_bind(overrides, reason) -> None:
    evaluation = m1.evaluate_leg(_inputs(**overrides))
    assert evaluation.signal  # the market signal alone would enter
    assert not evaluation.passes
    assert evaluation.decision == m1.DECISION_RISK_BLOCKED
    assert evaluation.reason == reason


def test_a_gap_of_exactly_a_quarter_and_old_but_valid_forecast_pass() -> None:
    assert m1.evaluate_leg(_inputs(model_probability=0.75)).passes
    assert m1.evaluate_leg(_inputs(forecast_age_hours=30.0, source_spread_f=7.3)).passes


def test_model_signal_gates_are_replaced_not_applied() -> None:
    # A model that hates the trade (point edge far below zero) does not block it
    # while the gap stays inside 0.25: the M1 edge gate replaces model gates.
    evaluation = m1.evaluate_leg(_inputs(model_probability=0.30))
    assert evaluation.passes
    assert evaluation.agree is False


def test_signal_threshold_is_two_cents_of_edge() -> None:
    weak = m1.evaluate_leg(
        _inputs(side="NO", created_at=_la(21, 45), market_probability=0.70,
                model_probability=0.70, ask=0.66, bid=0.64, ask_size=12.0)
    )
    assert weak.decision == m1.DECISION_NO_SIGNAL
    assert weak.reason == "edge 0.0134 below 0.02"


def test_policy_fingerprint_is_pinned_and_covers_the_stake_and_loss_stop() -> None:
    assert m1.M1_POLICY.account_id == "paper-research-m1-v1"
    assert m1.M1_POLICY.policy_version == "research-m1-market-recal-v1"
    assert m1.M1_POLICY.reference_equity == 2_000.0
    assert m1.M1_POLICY.real_money_eligible is False
    assert m1.M1_POLICY.policy_fingerprint == "f0258953ff7eb55a1e9318b4"
    payload = m1.M1_POLICY.identity_payload()
    assert payload["stake_dollars"] == 10.0
    assert payload["cumulative_loss_stop"] == -300.0
    assert payload["coefficient_set_id"] == "m1-deploy-coefs-2026-10-28"
    for field, value in (
        ("stake_dollars", 12.0),
        ("cumulative_loss_stop", -400.0),
        ("edge_threshold", 0.025),
        ("coefficient_set_id", "other"),
        ("coefficients", (("L1a", 0.3, 1.28),) + m1.M1_COEFFICIENTS[1:]),
        ("reference_equity", 1_000.0),
        ("sampling", "minute-0-4"),
    ):
        changed = replace(m1.M1_POLICY, **{field: value})
        assert changed.policy_fingerprint != m1.M1_POLICY.policy_fingerprint, field


def test_existing_policy_and_strategy_fingerprints_are_unchanged() -> None:
    assert TARGET_POLICY_V1.policy_fingerprint == "dea759010dc85ca5f4f610e2"
    assert TARGET_POLICY_V2.policy_fingerprint == "6a534a48478301a3068eb10d"
    assert TARGET_POLICY_V3.policy_fingerprint == "edd7001fa6eb6e7ecfd94b48"
    assert TARGET_POLICY_V4.policy_fingerprint == "982654641249be8a63739a5c"
    assert TARGET_POLICY_V5.policy_fingerprint == "cf33c27f3f70129be1e4a9e8"
    assert TARGET_POLICY_V6.policy_fingerprint == "0fd9cc8ebf877a653806fe1a"
    assert TARGET_POLICY_V7.policy_fingerprint == "428de2dc0b33923a909d8eb3"
    assert TARGET_POLICY.policy_fingerprint == "902bcfb7b6417501f3e1df8a"
    assert MOTION_POLICY.policy_fingerprint == "1c50d872ce278b403a6ad80e"
    live = strategy_config_for_profile("live")
    research = strategy_config_for_profile("research")
    assert strategy_fingerprint(live, entry_mode="limit") == "9725984064a55081065c6313"
    assert strategy_fingerprint(live, entry_mode="market") == "3c8a1cf9355693dd9ec130c6"
    assert strategy_fingerprint(research, entry_mode="limit") == "282324065b36575cb6232ebe"


def test_pinned_risk_limits_match_the_research_profile_they_were_copied_from() -> None:
    research = strategy_config_for_profile("research")
    policy = m1.M1_POLICY
    assert research.max_spread == pytest.approx(policy.max_spread_cents / 100.0)
    assert research.min_yes_bid == policy.min_entry_bid
    assert research.min_yes_bid_size == policy.min_bid_size
    assert research.min_ask_size == policy.min_ask_size
    assert research.max_contracts_per_market == policy.max_contracts
    assert research.max_position_risk_pct == policy.max_position_risk_pct
    assert research.max_event_risk_pct == policy.max_event_risk_pct
    assert research.max_target_exposure_pct == policy.max_target_date_risk_pct
    assert research.max_model_market_gap == policy.max_model_market_gap
    assert research.max_source_spread_f == policy.max_source_spread_f
    assert research.max_forecast_age_hours == policy.max_forecast_age_hours
    assert research.taker_fee_rate == policy.taker_fee_rate
    # Copied from V7 on 2026-11-02; V8's 25% pause does not move the frozen trial.
    assert TARGET_POLICY_V7.daily_loss_pause_pct == policy.daily_loss_pause_pct
    summary = m1.policy_summary()
    assert summary["position_cap_dollars"] == 30.0
    assert summary["event_cap_dollars"] == 60.0
    assert summary["target_date_cap_dollars"] == 100.0
    assert summary["daily_loss_pause_dollars"] == 300.0
    assert summary["cumulative_loss_stop_dollars"] == -300.0


def test_m1_module_never_imports_live_execution() -> None:
    script = (
        "import sys; import sfo_kalshi_quant.research_m1; "
        "import sfo_kalshi_quant.research_m1_store; "
        "print('sfo_kalshi_quant.live_execution' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.stdout.strip() == "False"
    for module in ("research_m1.py", "research_m1_store.py"):
        source = (Path(m1.__file__).parent / module).read_text(encoding="utf-8")
        assert "import live_execution" not in source
        assert "from .live_execution" not in source


def _recorded(evaluation_id: int, *, placement: bool = True, decision=None, **overrides):
    inputs = _inputs(**overrides)
    evaluation = m1.evaluate_leg(inputs)
    return m1.RecordedEvaluation(
        evaluation_id=evaluation_id,
        inputs=inputs,
        placement_enabled=placement,
        recorded_decision=decision or (
            m1.DECISION_ENTERED if evaluation.passes else evaluation.decision
        ),
    )


def test_replay_takes_the_first_sampled_crossing_once_per_leg() -> None:
    rows = [
        _recorded(1, created_at=_la(11, 3), market_probability=0.45,
                  decision=m1.DECISION_NO_SIGNAL),
        _recorded(2, created_at=_la(12, 4)),
        _recorded(3, created_at=_la(13, 2), decision=m1.DECISION_ALREADY_ENTERED),
        _recorded(4, created_at=_la(13, 2), side="NO", market_probability=0.5,
                  ask=0.53, bid=0.51, decision=m1.DECISION_NO_SIGNAL),
    ]
    expected, contradictions = m1.replay_expected_entries(rows)
    assert [(entry.evaluation_id, entry.utc_hour) for entry in expected] == [
        (2, utc_hour := m1.utc_hour_key(_la(12, 4)))
    ]
    assert contradictions == []
    assert utc_hour == "2026-10-20T19"
    # A leg entered before the replay window can never be entered again.
    expected, _ = m1.replay_expected_entries(
        rows,
        already_crossed={(TARGET.isoformat(), "KXHIGHLAX-26OCT21-B70.5", "YES")},
    )
    assert expected == []


def test_replay_flags_recorded_entries_the_rule_rejects() -> None:
    bad = _recorded(9, market_probability=0.45, decision=m1.DECISION_ENTERED)
    expected, contradictions = m1.replay_expected_entries([bad])
    assert expected == []
    assert contradictions[0]["issue"] == "recorded entry that the rule rejects"


def test_replay_keeps_scanning_past_account_level_outcomes() -> None:
    rows = [
        _recorded(1, created_at=_la(11, 2), decision=m1.DECISION_ACCOUNT_BLOCKED),
        _recorded(2, created_at=_la(12, 2), decision=m1.DECISION_ADMISSION_ERROR),
        # Committed PENDING with no order behind it: a crash between the two.
        _recorded(3, created_at=_la(13, 2), decision=m1.DECISION_PENDING),
        _recorded(4, created_at=_la(14, 2), decision=m1.DECISION_HALTED),
        _recorded(5, created_at=_la(15, 2)),
    ]
    expected, contradictions = m1.replay_expected_entries(rows)
    assert [entry.evaluation_id for entry in expected] == [5]
    assert contradictions == []


def test_a_first_crossing_in_shadow_mode_closes_the_leg_for_good() -> None:
    rows = [
        _recorded(1, created_at=_la(11, 2), placement=False, decision=m1.DECISION_SHADOW),
        _recorded(2, created_at=_la(12, 2)),  # placement switched on later
    ]
    expected, contradictions = m1.replay_expected_entries(rows)
    assert expected == []
    assert contradictions == [
        {
            "evaluation_id": 2,
            "leg": [TARGET.isoformat(), "KXHIGHLAX-26OCT21-B70.5", "YES"],
            "issue": "recorded entry on an already-crossed leg",
        }
    ]
    # Before the switch the leg had not crossed, so a later passing row would
    # not have been a shadow crossing either: only passing rows close a leg.
    quiet = [
        _recorded(1, created_at=_la(11, 2), placement=False, market_probability=0.45),
        _recorded(2, created_at=_la(12, 2)),
    ]
    expected, _ = m1.replay_expected_entries(quiet)
    assert [entry.evaluation_id for entry in expected] == [2]


def _actual(order_id: int, created_at: datetime, *, price=0.48, bid=0.46,
            leg=(TARGET.isoformat(), "KXHIGHLAX-26OCT21-B70.5", "YES")):
    return m1.ActualEntry(
        order_id=order_id,
        leg=leg,
        created_at=created_at,
        entry_price=price,
        entry_bid=bid,
        civil_tz="America/Los_Angeles",
    )


def test_matching_requires_the_same_leg_utc_hour_and_price_within_a_cent() -> None:
    expected, _ = m1.replay_expected_entries([_recorded(1, created_at=_la(12, 4))])
    ok = m1.match_entries(expected, [_actual(10, _la(12, 4), price=0.49)])
    assert [row["order_id"] for row in ok["matched"]] == [10]
    assert ok["failed"] == [] and ok["breaches"] == [] and ok["missed"] == []

    late = m1.match_entries(expected, [_actual(11, _la(13, 1))])
    assert late["failed"][0]["issue"].startswith("UTC hour 2026-10-20T20 differs")
    off = m1.match_entries(expected, [_actual(12, _la(12, 4), price=0.50)])
    assert "by more than 1c" in off["failed"][0]["issue"]
    stray = m1.match_entries([], [_actual(13, _la(12, 4))])
    assert stray["failed"][0]["issue"] == "no replayed entry for this leg"
    missed = m1.match_entries(expected, [])
    assert missed["missed"][0]["evaluation_id"] == 1


def test_order_breaches_catch_listing_hour_wide_spread_lead_and_reentry() -> None:
    expected, _ = m1.replay_expected_entries([_recorded(1, created_at=_la(12, 4))])
    result = m1.match_entries(
        expected,
        [
            _actual(20, _la(12, 4)),
            _actual(21, _la(12, 30)),
            _actual(22, _la(7, 30), leg=(TARGET.isoformat(), "KXHIGHLAX-26OCT21-B72.5", "YES")),
            _actual(23, _la(12, 4), price=0.60, bid=0.48,
                    leg=(TARGET.isoformat(), "KXHIGHLAX-26OCT21-B74.5", "NO")),
            _actual(24, _la(12, 4), leg=("2026-10-22", "KXHIGHLAX-26OCT22-B70.5", "YES")),
        ],
    )
    breaches = {(row["order_id"], row["breach"]) for row in result["breaches"]}
    assert (21, "re-entry of a leg first entered by order 20") in breaches
    assert (22, "entry in the ET listing hour") in breaches
    assert (23, "entry spread 12c above 10c") in breaches
    assert (24, "entry lead 2 is not 1") in breaches


def test_date_clustered_bootstrap_is_deterministic_and_resamples_dates() -> None:
    entries = [
        m1.SettledEntry("2026-11-01", 2.0, 10.0, True),
        m1.SettledEntry("2026-11-01", -10.0, 10.0, False),
        m1.SettledEntry("2026-11-02", 1.0, 10.0, True),
        m1.SettledEntry("2026-11-03", 3.0, 10.0, False),
    ]
    assert m1.roi(entries) == pytest.approx(-4.0 / 40.0)
    first = m1.date_clustered_bootstrap(entries, resamples=500)
    second = m1.date_clustered_bootstrap(entries, resamples=500)
    assert first == second
    assert first["dates"] == 3
    assert first["lower"] <= first["roi"] <= first["upper"]
    agree = [entry for entry in entries if entry.agree]
    disagree = [entry for entry in entries if not entry.agree]
    diff = m1.date_clustered_difference_bootstrap(agree, disagree, resamples=500)
    assert diff["roi"] == pytest.approx(0.15 - (-0.35))
    assert m1.roi([]) is None


def test_weighted_logistic_fit_recovers_known_parameters() -> None:
    import random

    rng = random.Random(7)
    xs, ys, ws = [], [], []
    for _ in range(4000):
        x = rng.uniform(-3, 3)
        p = 1 / (1 + pow(2.718281828459045, -(0.25 + 1.2 * x)))
        xs.append(x)
        ys.append(1.0 if rng.random() < p else 0.0)
        ws.append(1.0)
    a, c = m1.weighted_logistic_fit(xs, ys, ws)
    assert a == pytest.approx(0.25, abs=0.12)
    assert c == pytest.approx(1.2, abs=0.12)


def test_premise_slope_uses_the_yes_view_and_market_day_weights() -> None:
    rows = []
    for day in range(30):
        target = (date(2026, 11, 1) + timedelta(days=day)).isoformat()
        for market, q, outcome in (("A", 0.9, True), ("B", 0.2, False), ("C", 0.6, True), ("D", 0.4, False)):
            for scan in range(3):
                for side in ("YES", "NO"):
                    rows.append(m1.PremiseRow(target, f"{market}-{target}", f"s{scan}", side, q, outcome, True))
        rows.append(m1.PremiseRow(target, f"E-{target}", "s0", "YES", 0.5, True, False))
    result = m1.premise_slope(rows)
    assert result["target_dates"] == 30
    assert result["market_days"] == 120  # the untradeable E rows are excluded
    assert result["rows"] == 360  # one YES-view row per market and scan
    assert result["slope"] is not None and result["slope"] > 1.05


@pytest.mark.parametrize(
    ("strike", "floor", "cap", "high", "expected"),
    [
        ("between", 70, 71, 70.4, True),
        ("between", 70, 71, 71.5, False),
        ("greater", 75, None, 76, True),
        ("greater", 75, None, 75, False),
        ("less", None, 65, 64, True),
        ("less", None, 65, 65, False),
        ("between", None, 71, 70, None),
    ],
)
def test_outcomes_resolve_through_the_settlement_rule(strike, floor, cap, high, expected) -> None:
    assert m1.resolves_yes(strike, floor, cap, high) is expected
