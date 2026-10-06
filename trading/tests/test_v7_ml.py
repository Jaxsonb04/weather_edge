"""Leakage, lineage, proper-score and resource boundaries for offline challengers."""
from copy import deepcopy
from datetime import date, timedelta
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import time

import pytest

spec = importlib.util.spec_from_file_location(
    "v7_ml", Path(__file__).resolve().parents[2] / "scripts/evaluate_v7_ml.py")
ml = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ml)


@pytest.fixture
def compact_configuration(monkeypatch):
    config = dict(ml.CONFIG, minimum_training_dates_per_station_lead=3,
        minimum_baseline_history_dates=3, baseline_window_days=3,
        training_window_days=10, forward_block_days=5,
        location_grid_f=[-1, 0, 1], scale_grid=[.75, 1, 1.25],
        bootstrap_replicates=50, boosting_trees_per_quantile=3,
        boosting_minimum_leaf_rows=2)
    monkeypatch.setattr(ml, "CONFIG", config)


def evidence(days=32, stations=("KSFO",), leads=(1, 2)):
    start = date(2026, 1, 1)
    truth, forecasts = [], []
    for index in range(days):
        target = (start + timedelta(days=index)).isoformat()
        for station in stations:
            actual = 60 + index % 7
            truth.append({"station_id": station, "local_date": target,
                "is_final": 1, "max_temperature_f": actual,
                "fetched_at": "2026-03-01T00:00:00+00:00"})
            for lead in leads:
                forecasts.append({"station_id": station, "target_date": target,
                    "lead_days": lead, "predicted_high_f": actual + 1.2,
                    "sigma_f": 3, "method": "emos_wmean", "source": "rolling_origin_v2",
                    "actual_high_f": -9999, "model_spread_f": None,
                    "n_models": 8, "fetched_at": "2026-03-01T00:00:00+00:00"})
    return {"captured_at": "2026-03-02T00:00:00+00:00",
        "nwp_model_forecasts": {"rows": [], "truncated": False},
        "forecast_emos_daily_high": {"rows": forecasts, "truncated": False},
        "cli_settlements": {"rows": truth, "truncated": False}}


def test_temporal_training_excludes_serve_date_and_other_leads():
    rows = [{"target_date": f"2026-01-{day:02d}", "lead": lead} for day in range(1, 15) for lead in (1, 2)]
    selected = ml.training_for_fold(rows, date(2026, 1, 10), 2)
    assert all(row["target_date"] < "2026-01-10" and row["lead"] == 2 for row in selected)
    assert len(selected) == 9


def test_reconstructed_truth_is_never_prospective_or_profit(compact_configuration):
    result = ml.evaluate(evidence(), source_hash="fixture")
    assert result["retrospective"]["cases"] > 0
    assert result["strict_prequential"]["cases"] == 0
    assert not result["active"] and not result["promotion_eligible"]
    assert result["capital_gate"]["live_allocation_usd"] == 0
    assert result["capital_gate"]["initial_bankroll_recommendation_usd"] is None
    for fold in result["folds"]:
        assert fold["last_training_target"] < fold["fit_before_serve_date"]
        assert fold["training_truth_rows_available_by_fold_clock"] == 0
    for row in result["paired_predictions_and_lineage"]:
        assert row["truth"] != -9999
        assert row["candidate_sigma"] >= 1.5
        assert not row["strict_original_vintage_eligible"]


def test_storage_clock_cannot_certify_reconstructed_issuance(compact_configuration):
    data = evidence()
    for row in data["forecast_emos_daily_high"]["rows"]:
        row["fetched_at"] = "2025-12-01T00:00:00+00:00"
    result = ml.evaluate(data, source_hash="fixture")
    assert result["strict_prequential"]["necessary_storage_clock_passes"] > 0
    assert result["strict_prequential"]["cases"] == 0


def test_future_truth_does_not_change_earlier_prediction(compact_configuration):
    original = evidence()
    first = ml.evaluate(original, source_hash="fixture")
    before = first["paired_predictions_and_lineage"][0]
    revised = deepcopy(original)
    for row in revised["cli_settlements"]["rows"]:
        if row["local_date"] >= before["target_date"]:
            row["max_temperature_f"] = 200
    second = ml.evaluate(revised, source_hash="fixture")
    after = next(row for row in second["paired_predictions_and_lineage"] if
        (row["station"], row["lead"], row["target_date"]) == (before["station"], before["lead"], before["target_date"]))
    assert after["baseline_mu"] == before["baseline_mu"]
    assert after["candidate_mu"] == before["candidate_mu"]
    assert after["candidate_sigma"] == before["candidate_sigma"]
    assert after["fold_id"] == before["fold_id"]


@pytest.mark.parametrize("change", ["duplicate_forecast", "duplicate_truth", "future_forecast", "future_truth", "truncated", "naive_clock"])
def test_ambiguous_or_incomplete_input_fails_closed(change):
    data = evidence(days=1)
    if change == "duplicate_forecast":
        data["forecast_emos_daily_high"]["rows"].append(deepcopy(data["forecast_emos_daily_high"]["rows"][0]))
    elif change == "duplicate_truth":
        data["cli_settlements"]["rows"].append(deepcopy(data["cli_settlements"]["rows"][0]))
    elif change == "future_forecast":
        data["forecast_emos_daily_high"]["rows"][0]["fetched_at"] = "2026-03-03T00:00:00+00:00"
    elif change == "future_truth":
        data["cli_settlements"]["rows"][0]["fetched_at"] = "2026-03-03T00:00:00+00:00"
    elif change == "truncated":
        data["nwp_model_forecasts"]["truncated"] = True
    else:
        data["captured_at"] = "2026-03-02T00:00:00"
    with pytest.raises(ValueError):
        ml.evaluate(data, source_hash="fixture")


def test_other_station_and_source_never_contaminate_baseline(compact_configuration):
    base = evidence()
    augmented = deepcopy(base)
    for row in evidence(stations=("KNYC",))["forecast_emos_daily_high"]["rows"]:
        row["predicted_high_f"] += 50
        augmented["forecast_emos_daily_high"]["rows"].append(row)
    augmented["cli_settlements"]["rows"].extend(evidence(stations=("KNYC",))["cli_settlements"]["rows"])
    for row in deepcopy(base["forecast_emos_daily_high"]["rows"]):
        row.update(source="rolling_origin", predicted_high_f=300)
        augmented["forecast_emos_daily_high"]["rows"].append(row)
    before = ml.evaluate(base, source_hash="fixture")["paired_predictions_and_lineage"]
    after = [row for row in ml.evaluate(augmented, source_hash="fixture")["paired_predictions_and_lineage"] if row["station"] == "KSFO"]
    assert [(r["target_date"], r["lead"], r["candidate_mu"], r["candidate_sigma"]) for r in before] == [
        (r["target_date"], r["lead"], r["candidate_mu"], r["candidate_sigma"]) for r in after]


def test_no_training_and_expired_budget_are_explicit(compact_configuration):
    result = ml.evaluate(evidence(days=4), source_hash="fixture")
    assert result["retrospective"]["cases"] == 0
    with pytest.raises(TimeoutError):
        ml.evaluate(evidence(), source_hash="fixture", deadline=time.monotonic() - 1)


def test_quantile_scores_do_not_invent_distribution_or_crps():
    result = ml.quantile_scores([50, 60, 70], 75)
    assert result["interval_score_90_f"] == 120
    assert result["central_90pct_coverage"] == 0
    assert "crps_f" not in result
    assert ml.pinball(60, 62, .5) == 1


def test_small_real_boosting_arm_is_forward_fitted(compact_configuration):
    pytest.importorskip("sklearn")
    result = ml.evaluate(evidence(), source_hash="fixture", with_boosting=True)
    assert result["retrospective"]["cases"] > 0
    assert "gradient_boosted_quantiles" in result["retrospective"]["arms"]
    assert "crps_f" not in result["retrospective"]["arms"]["gradient_boosted_quantiles"]
    for row in result["paired_predictions_and_lineage"]:
        assert row["boosting_quantiles_f"] == sorted(row["boosting_quantiles_f"])
    assert result["sklearn_version"]


def test_bootstrap_never_bridges_missing_calendar_days():
    daily = {f"2026-01-{day:02d}": {"candidate": {"mae_f": 0}, "existing_bias_only": {"mae_f": 1}}
             for day in range(1, 30, 2)}
    assert ml.calendar_block_interval(daily, "mae_f", "candidate") is None


def test_bootstrap_cannot_drop_isolated_adverse_dates():
    daily = {f"2026-01-{day:02d}": {"candidate": {"mae_f": 0}, "existing_bias_only": {"mae_f": 1}}
             for day in range(1, 8)}
    daily.update({f"2026-01-{day:02d}": {"candidate": {"mae_f": 3}, "existing_bias_only": {"mae_f": 1}}
                  for day in range(9, 22, 2)})
    assert sum(row["candidate"]["mae_f"] - row["existing_bias_only"]["mae_f"] for row in daily.values()) / len(daily) == .5
    assert ml.calendar_block_interval(daily, "mae_f", "candidate") is None


def issued_evidence():
    from live_forecast_evidence import LiveModelForecast, record_live_forecast
    from cities import get_city
    data = evidence(days=1, leads=(1,))
    # Actual preserved outputs, generated by the production append-only writer.
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        for recorded, prediction in (("2025-12-31T18:00:00+00:00", 62.0),
                                     ("2025-12-31T19:00:00+00:00", 60.0)):
            inputs = LiveModelForecast({"gfs_seamless": 61.0, "ecmwf_ifs025": 62.0, "icon_seamless": 63.0},
                                      retrieved_at="2025-12-31T17:55:00+00:00")
            record_live_forecast(conn, city=get_city("sfo"), target_date=date(2026, 1, 1),
                recorded_at=recorded, inputs=inputs, used_models=set(inputs),
                mu=prediction, sigma=2.0, fit_lead_days=1, legacy_lead_days=1,
                method="emos_wmean", training=[("2025-12-29", dict(inputs), 60)],
                fit_params={"a": 0}, serve_policy={"apply_bias": True})
        data["forecast_emos_live_vintages"] = {"available": True, "truncated": False,
            "rows": [dict(r) for r in conn.execute("SELECT * FROM forecast_emos_live_vintages")]}
        data["nwp_live_forecast_members"] = {"available": True, "truncated": False,
            "rows": [dict(r) for r in conn.execute("SELECT * FROM nwp_live_forecast_members")]}
    return data


def rehash_vintage(data, index=0):
    row = data["forecast_emos_live_vintages"]["rows"][index]
    original = row["snapshot_id"]
    members = [member for member in data["nwp_live_forecast_members"]["rows"] if member["snapshot_id"] == original]
    new = ml.digest({**{name: row[name] for name in ml.VINTAGE_FIELDS},
        "members": sorted([{name: member[name] for name in ml.MEMBER_FIELDS} for member in members], key=lambda r: r["model"])})
    row["snapshot_id"] = new
    for member in members:
        member["snapshot_id"] = new


def test_actual_issued_scoring_keeps_null_quality_and_deduplicates_without_outcomes():
    data = issued_evidence()
    result = ml.audit_issued_vintages(data)
    assert result["available"] is True and result["cases"] == 1
    assert result["exported_vintages"] == 2
    # Earlier 62F is worse than later perfect 60F, but earliest is the fixed rule.
    assert result["scores"]["mae_f"] == 2
    assert result["known_provider_initialization_cases"] == 0
    assert result["known_all_used_member_hour_counts_cases"] == 0
    assert not result["new_challengers_evaluated_prospectively"]
    assert not result["training_truth_availability_receipts_established"]
    assert not result["promotion_eligible"]
    row = result["issued_predictions_and_lineage"][0]
    assert row["provider_initialized_at"] is None
    assert set(row["used_member_hour_counts"].values()) == {None}


def test_actual_issued_subfloor_sigma_is_preserved_and_scored_exactly():
    data = issued_evidence()
    for index, row in enumerate(data["forecast_emos_live_vintages"]["rows"]):
        row["sigma_f"] = .75
        row["predicted_high_f"] = 60.0
        rehash_vintage(data, index)
    result = ml.audit_issued_vintages(data)
    assert result["issued_predictions_and_lineage"][0]["sigma"] == .75
    import math
    assert result["scores"]["crps_f"] == pytest.approx(.75 * (math.sqrt(2) - 1) / math.sqrt(math.pi))
    assert result["scores"]["interval_width_f"] == pytest.approx(2 * ml.Z90 * .75)
    assert "exact preserved" in result["scoring_convention"]


def test_unavailable_and_empty_issued_tables_are_distinct():
    assert ml.audit_issued_vintages(evidence())["available"] is False
    data = evidence()
    for name in ("forecast_emos_live_vintages", "nwp_live_forecast_members"):
        data[name] = {"available": True, "rows": [], "truncated": False}
    result = ml.audit_issued_vintages(data)
    assert result["available"] is True and result["cases"] == 0
    data["forecast_emos_live_vintages"]["available"] = False
    assert ml.audit_issued_vintages(data)["available"] is False


@pytest.mark.parametrize("change", ["content_hash", "duplicate_member", "lead", "window", "future_retrieval", "future_capture", "orphan_member", "truncated"])
def test_issued_provenance_ambiguity_fails_closed(change):
    data = issued_evidence()
    row = data["forecast_emos_live_vintages"]["rows"][0]
    if change == "content_hash": row["predicted_high_f"] = 59.0
    elif change == "duplicate_member": data["nwp_live_forecast_members"]["rows"].append(deepcopy(data["nwp_live_forecast_members"]["rows"][0]))
    elif change == "orphan_member":
        new = deepcopy(data["nwp_live_forecast_members"]["rows"][0]); new["snapshot_id"] = "a" * 64
        data["nwp_live_forecast_members"]["rows"].append(new)
    elif change == "truncated": data["nwp_live_forecast_members"]["truncated"] = True
    else:
        if change == "lead": row["lead_days"] = 2
        elif change == "window": row["standard_utc_offset_hours"] = -7
        elif change == "future_retrieval": row["retrieved_at"] = "2026-01-01T00:00:00+00:00"
        else: row["recorded_at"] = "2026-03-03T00:00:00+00:00"
        rehash_vintage(data)
    with pytest.raises(ValueError): ml.audit_issued_vintages(data)


def test_issued_training_on_serve_day_cannot_enter_scores():
    data = issued_evidence()
    for index, row in enumerate(data["forecast_emos_live_vintages"]["rows"]):
        row["training_truth_end"] = "2025-12-31"
        rehash_vintage(data, index)
    result = ml.audit_issued_vintages(data)
    assert result["cases"] == 0
    assert result["excluded"]["training_target_not_strictly_prior_to_issue_day"] == 2


def prospective_fill_days(pnl=1):
    return [{"date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
             "net_pnl_usd": pnl, "realized_loss_usd": max(-pnl, 0), "peak_open_cost_usd": 10, "prospective": True,
             "net_fills_verified": True, "fees_and_slippage_included": True,
             "economic_account_key": "synthetic-single-account",
             "strategy_policy_fingerprint_sha256": "a" * 64} for index in range(30)]


def verified_capital_study(rows, **kwargs):
    return ml.capital_frontier(rows, identity_verified=True, as_of="2026-02-01T00:00:00+00:00", calendar_timezone="UTC", **kwargs)


def test_capital_frontier_never_substitutes_weather_or_unverified_fills():
    assert ml.capital_frontier([], identity_verified=True)["initial_bankroll_recommendation_usd"] is None
    rows = prospective_fill_days()
    assert ml.capital_frontier(rows)["capital_study_candidate_usd"] is None
    rows[0]["fees_and_slippage_included"] = False
    assert verified_capital_study(rows)["status"] == "unverified_net_fills_exposure_or_costs"


def test_nonpositive_net_edge_and_missing_calendar_days_fail_capital_gate(monkeypatch):
    monkeypatch.setitem(ml.CAPITAL_LIMITS, "bootstrap_replicates", 30)
    negative = verified_capital_study(prospective_fill_days(-1))
    assert negative["status"] == "net_filled_daily_pnl_lower_bound_not_positive"
    assert negative["capital_study_candidate_usd"] is None
    missing = prospective_fill_days() + [dict(prospective_fill_days()[0], date="2026-02-05")]
    assert verified_capital_study(missing)["status"] == "missing_calendar_days_include_zero_and_outage_days"


def test_positive_fixed_fills_produce_research_frontier_without_live_allocation(monkeypatch):
    monkeypatch.setitem(ml.CAPITAL_LIMITS, "bootstrap_replicates", 30)
    result = verified_capital_study(prospective_fill_days())
    assert result["status"] == "experimental_fixed_fill_capital_frontier"
    assert result["capital_study_candidate_usd"] == 200
    assert result["initial_bankroll_recommendation_usd"] is None
    assert result["live_allocation_usd"] == 0
    assert all(row["feasible_under_declared_assumptions"] for row in result["frontier"])


def test_capital_budget_and_duplicate_days_are_rejected():
    rows = prospective_fill_days()
    rows.append(deepcopy(rows[-1]))
    with pytest.raises(ValueError): verified_capital_study(rows)
    with pytest.raises(TimeoutError):
        verified_capital_study(prospective_fill_days(),
            check_budget=lambda: (_ for _ in ()).throw(TimeoutError("budget")))


def test_gross_realized_losses_cannot_hide_in_net_positive_capital_days(monkeypatch):
    monkeypatch.setitem(ml.CAPITAL_LIMITS, "bootstrap_replicates", 30)
    rows = prospective_fill_days()
    for row in rows:
        row["realized_loss_usd"] = 10
    result = verified_capital_study(rows)
    assert result["capital_study_candidate_usd"] >= 1000
    del rows[0]["realized_loss_usd"]
    assert verified_capital_study(rows)["status"] == "unverified_net_fills_exposure_or_costs"


def test_capital_study_requires_exact_true_identity_and_explicit_completion_clock():
    rows = prospective_fill_days()
    assert ml.capital_frontier(rows, identity_verified="true")["status"] == "insufficient_independent_verified_prospective_fill_days"
    assert ml.capital_frontier(rows, identity_verified=True)["status"] == "missing_explicit_capture_clock_or_calendar_timezone"
    assert ml.capital_frontier(rows, identity_verified=True, as_of="2026-01-15T12:00:00+00:00", calendar_timezone="UTC")["status"] == "unfinished_or_future_calendar_day"
    with pytest.raises(ValueError):
        ml.capital_frontier(rows, identity_verified=True, as_of="2026-02-01T00:00:00", calendar_timezone="UTC")


@pytest.mark.parametrize("change", ["mixed_account", "mixed_policy", "missing_account", "malformed_policy"])
def test_capital_study_never_combines_accounts_or_policies(change):
    rows = prospective_fill_days()
    if change == "mixed_account": rows[-1]["economic_account_key"] = "synthetic-separate-account"
    elif change == "mixed_policy": rows[-1]["strategy_policy_fingerprint_sha256"] = "b" * 64
    elif change == "missing_account": del rows[-1]["economic_account_key"]
    else: rows[-1]["strategy_policy_fingerprint_sha256"] = "not-a-policy-hash"
    result = verified_capital_study(rows)
    assert result["capital_study_candidate_usd"] is None
    assert result["live_allocation_usd"] == 0
    assert result["status"] in ("mixed_economic_accounts_or_strategy_policy_identities", "missing_or_invalid_economic_account_and_policy_lineage")


def test_local_shadow_metadata_is_hash_verified_and_never_labeled_deployed_aws():
    data = issued_evidence()
    metadata = []
    for row in data["forecast_emos_live_vintages"]["rows"]:
        fields = {"snapshot_id": row["snapshot_id"], "research_identity": "local-shadow-v7-v1",
            "execution_location": "local_mac", "model_policy_fingerprint_sha256": "a" * 64}
        metadata.append({**fields, "lineage_sha256": ml.digest(fields)})
    data["shadow_forecast_lineage"] = {"available": True, "truncated": False, "rows": metadata}
    result = ml.audit_issued_vintages(data)
    assert result["cohort_case_counts"] == {"local_shadow": 1}
    assert result["issued_predictions_and_lineage"][0]["research_identity"] == "local-shadow-v7-v1"
    data["shadow_forecast_lineage"]["rows"][0]["research_identity"] = "installed-aws-v7"
    with pytest.raises(ValueError): ml.audit_issued_vintages(data)


def test_separate_producer_identities_never_overwrite_each_other():
    data = issued_evidence()
    fields = {"snapshot_id": data["forecast_emos_live_vintages"]["rows"][1]["snapshot_id"],
        "research_identity": "local-shadow-v7-v1", "execution_location": "local_mac",
        "model_policy_fingerprint_sha256": "a" * 64}
    data["shadow_forecast_lineage"] = {"available": True, "truncated": False,
        "rows": [{**fields, "lineage_sha256": ml.digest(fields)}]}
    result = ml.audit_issued_vintages(data)
    assert result["cases"] == 2
    assert result["distinct_calendar_targets"] == 1
    assert result["cohort_case_counts"] == {"local_shadow": 1, "producer_identity_unestablished": 1}
    assert len(result["by_producer_identity"]) == 2
    by_cohort = {row["cohort"]: row for row in result["by_producer_identity"]}
    assert by_cohort["local_shadow"]["scores"]["mae_f"] == 0
    assert by_cohort["producer_identity_unestablished"]["scores"]["mae_f"] == 2


def test_all_arms_share_exact_denominator_and_deterministic_lineage(compact_configuration, tmp_path):
    result = ml.evaluate(evidence(), source_hash="fixture")
    output, lineage = tmp_path / "result.json", tmp_path / "lineage.gz"
    ml.write_outputs(result, output, lineage)
    first_bytes = lineage.read_bytes()
    document = json.loads(output.read_text())
    rows = [json.loads(line) for line in gzip.decompress(first_bytes).splitlines()]
    assert document["lineage_prediction_count"] == len(rows) == document["retrospective"]["cases"]
    assert document["lineage_sha256"] == hashlib.sha256(first_bytes).hexdigest()
    assert all(set(row["scores"]) == set(document["retrospective"]["arms"]) for row in rows)
    ml.write_outputs(result, output, lineage)
    assert lineage.read_bytes() == first_bytes
