"""Network-free checks for the optional local-training experiment."""

from __future__ import annotations

import copy
import importlib.util
import math
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("sklearn", reason="offline experiment requires optional training dependencies")
pytest.importorskip("scipy")
from scipy.integrate import quad
from scipy.special import ndtr
from threadpoolctl import threadpool_limits

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "weather_ml_experiment.py"
SPEC = importlib.util.spec_from_file_location("weather_ml_experiment", SCRIPT)
ml = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ml
SPEC.loader.exec_module(ml)


@pytest.fixture(scope="module")
def archive():
    start, end = date(2025, 3, 5), date(2026, 6, 16)
    payload = {"exported_at": "2026-09-05T00:00:00Z", "start": start.isoformat(), "nwp": [], "truth": []}
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        for index, station in enumerate(("KSFO", "KATL")):
            truth = 65 + 9 * math.sin(offset / 59) + 6 * index + math.sin(offset * 0.7)
            payload["truth"].append([station, day.isoformat(), truth])
            for lead in (1, 2):
                for member, model in enumerate(ml.ROSTER):
                    value = truth + index * 0.8 + (member - 3) * 0.5 + math.sin(offset / 5 + member) * lead
                    payload["nwp"].append([station, day.isoformat(), model, lead, value, "openmeteo_previous_runs"])
    return payload


def make_pairs(cases):
    rows = []
    for case in cases:
        if date(2026, 6, 6) <= case.day <= date(2026, 6, 15):
            row = {"station": case.station, "lead": str(case.lead), "target_date": case.day.isoformat(),
                   "truth": str(case.truth)}
            for mu_column, sigma_column in ml.BASELINE_COLUMNS.values():
                row[mu_column] = str(sum(case.members) / len(case.members))
                row[sigma_column] = "2.0"
            rows.append(row)
    return rows


def test_future_truth_and_rows_cannot_change_frozen_point_or_uncertainty(archive):
    cases = ml.load_cases(archive)
    pairs = make_pairs(cases)
    start, end = date(2026, 6, 6), date(2026, 6, 15)
    evaluation = ml.paired_cases(cases, pairs, start, end)
    mutated = copy.deepcopy(archive)
    for row in mutated["truth"]:
        if row[1] > "2026-06-03":
            row[2] += 1000
    mutated["truth"].reverse()
    mutated["nwp"].reverse()
    changed = ml.load_cases(mutated)
    changed_lookup = {case.key: case for case in changed}
    with threadpool_limits(limits=1):
        actual, metadata, calibration = ml.frozen_predictions(cases, evaluation, start)
        other, other_metadata, other_calibration = ml.frozen_predictions(
            changed, [changed_lookup[case.key] for case in evaluation], start)
    assert actual == other
    assert metadata == other_metadata
    assert calibration == other_calibration
    assert metadata["final_fit"]["last_truth_date"] == "2026-06-03"
    for fold in metadata["calibration_folds"]:
        assert date.fromisoformat(fold["last_truth_date"]) <= date.fromisoformat(fold["prediction_start"]) - timedelta(days=3)
        assert fold["prediction_end"] < "2026-06-06"
    assert ml.MODEL_PARAMS["early_stopping"] is False


def test_holdout_start_early_in_month_excludes_unavailable_calibration_truth(archive):
    cases = ml.load_cases(archive)
    start = date(2026, 6, 1)
    evaluation = [case for case in cases if case.day == start]
    with threadpool_limits(limits=1):
        _, metadata, calibration = ml.frozen_predictions(cases, evaluation, start)
    assert metadata["final_fit"]["cutoff"] == "2026-05-29"
    assert max(row["target_date"] for row in calibration) == "2026-05-29"


def test_exact_pair_denominator_rejects_missing_duplicate_and_mismatched_truth(archive):
    cases = ml.load_cases(archive)
    pairs = make_pairs(cases)
    start, end = date(2026, 6, 6), date(2026, 6, 15)
    selected = ml.paired_cases(cases, pairs, start, end)
    assert len(selected) == 40
    with pytest.raises(ValueError, match="missing complete"):
        ml.paired_cases([case for case in cases if case.key != selected[0].key], pairs, start, end)
    with pytest.raises(ValueError, match="duplicate paired"):
        ml.paired_cases(cases, pairs + pairs[:1], start, end)
    bad_truth = [{**pairs[0], "truth": "-500"}, *pairs[1:]]
    with pytest.raises(ValueError, match="truth differs"):
        ml.paired_cases(cases, bad_truth, start, end)
    with pytest.raises(ValueError, match="outside declared"):
        ml.paired_cases(cases, pairs, start + timedelta(days=1), end)


def test_archive_rejects_ambiguous_source_joins_and_excludes_incomplete_roster(archive):
    duplicate = {**archive, "nwp": archive["nwp"] + archive["nwp"][:1]}
    with pytest.raises(ValueError, match="duplicate or nonfinite member"):
        ml.load_cases(duplicate)
    duplicate_truth = {**archive, "truth": archive["truth"] + archive["truth"][:1]}
    with pytest.raises(ValueError, match="duplicate or nonfinite truth"):
        ml.load_cases(duplicate_truth)
    wrong_source = {**archive, "nwp": [[*archive["nwp"][0][:-1], "unexpected"], *archive["nwp"][1:]]}
    with pytest.raises(ValueError, match="unexpected forecast source"):
        ml.load_cases(wrong_source)
    assert len(ml.load_cases({**archive, "nwp": archive["nwp"][1:]})) == len(ml.load_cases(archive)) - 1


@pytest.mark.parametrize(("mu", "sigma", "truth"), [(70, 2, 70), (65, 1.5, 69), (90, 4, 80)])
def test_gaussian_crps_matches_independent_cdf_integral(mu, sigma, truth):
    below = quad(lambda value: ndtr((value - mu) / sigma) ** 2, -np.inf, truth)[0]
    above = quad(lambda value: ndtr((mu - value) / sigma) ** 2, truth, np.inf)[0]
    assert ml.gaussian_crps(mu, sigma, truth) == pytest.approx(below + above, abs=1e-9)


def test_bootstrap_preserves_case_weighting_with_unequal_date_counts():
    rows = [{"target_date": "2026-06-06", "ml_crps": 2.0, "emos_crps": 1.0}]
    rows.extend({"target_date": "2026-06-07", "ml_crps": 0.0, "emos_crps": 1.0} for _ in range(3))
    result = ml.date_block_interval(rows, "emos", "crps", block_days=2, replicates=200)
    assert result["delta"] == -0.5  # An average of date means would incorrectly be zero.
    assert result["date_block_95_ci"] == [-0.5, -0.5]


def test_end_to_end_results_are_reproducible_and_groups_reconcile(archive):
    pairs = make_pairs(ml.load_cases(archive))
    args = (archive, pairs, date(2026, 6, 6), date(2026, 6, 15))
    first = ml.run_experiment(*args, replicates=100)
    second = ml.run_experiment(*args, replicates=100)
    assert first == second
    result, scores, calibration = first
    assert result["primary"]["n"] == len(scores) == 40
    assert sum(group["n"] for group in result["by_lead"]) == 40
    assert sum(group["n"] for group in result["by_station_lead"]) == 40
    assert len(calibration) == sum(fold["prediction_n"] for fold in result["calibration_folds"])
    for arm in ml.ARMS:
        assert result["primary"]["scores"][arm]["mae"] == pytest.approx(
            sum(abs(row["truth"] - row[f"{arm}_mu"]) for row in scores) / len(scores))
        assert 0 <= result["primary"]["scores"][arm]["covered80"] <= 1
