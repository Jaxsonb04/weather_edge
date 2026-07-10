from sfo_kalshi_quant.fees import (
    FEE_SCHEDULE_VERSION,
    fee_multipliers,
    contracts_for_budget,
    expected_profit_per_yes_contract,
    kelly_fraction_spent,
    quadratic_fee_average_per_contract,
    quadratic_fee_total,
    quadratic_fee_per_contract,
)


def test_july_2026_fee_schedule_handles_general_and_contract_overrides():
    assert FEE_SCHEDULE_VERSION == "2026-07-07"
    assert fee_multipliers("KXHIGHNY-26JUL09-B85") == (0.0, 1.0)
    assert fee_multipliers("KXCPI-26JUL") == (1.0, 1.0)
    assert fee_multipliers("KXBTCY-26DEC") == (0.0, 0.0)
    assert quadratic_fee_total(
        0.5, 100, maker=True, series_ticker="KXHIGHNY"
    ) == 0.0
    assert quadratic_fee_total(
        0.5, 100, maker=False, series_ticker="KXHIGHNY"
    ) == 1.75
from sfo_kalshi_quant.standard_bins import standard_sfo_bins


def test_standard_bins_cover_integer_settlements_once():
    bins = standard_sfo_bins()
    for value in range(55, 90):
        hits = [market for market in bins if market.resolves_yes(value)]
        assert len(hits) == 1, value


def test_continuous_intervals_follow_integer_bins():
    bins = {market.yes_sub_title: market for market in standard_sfo_bins()}
    assert bins["65° or below"].continuous_interval()[1] == 65.5
    assert bins["66° to 67°"].continuous_interval() == (65.5, 67.5)
    assert bins["74° or above"].continuous_interval()[0] == 73.5


def test_fee_and_kelly_are_conservative():
    fee = quadratic_fee_per_contract(0.5)
    assert fee == 0.02
    assert expected_profit_per_yes_contract(0.60, 0.50, fee) > 0
    assert kelly_fraction_spent(0.60, 0.52) > 0
    assert kelly_fraction_spent(0.50, 0.52) == 0


def test_order_total_fee_budget_uses_rounded_total_not_one_contract_fee_times_size():
    contracts = 200.0
    average_fee = quadratic_fee_average_per_contract(0.04, contracts)
    assert round(average_fee, 4) == 0.0027
    assert average_fee < quadratic_fee_per_contract(0.04)

    budget_contracts = contracts_for_budget(0.04, 10.0)
    spend = budget_contracts * 0.04 + quadratic_fee_total(0.04, budget_contracts)
    assert spend <= 10.0 + 1e-9
    assert budget_contracts > contracts
