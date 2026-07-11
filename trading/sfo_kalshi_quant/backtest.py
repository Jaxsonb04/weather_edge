from __future__ import annotations

import math
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .config import StrategyConfig
from .models import ForecastOutcome, MarketBin
from .probability import ResidualCalibrator
from .standard_bins import standard_sfo_bins


DEFAULT_CALIBRATION_CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "cache" / "calibration"
_CACHE_SCHEMA_VERSION = 1
_CACHE_MAX_ENTRIES = 128


@dataclass(frozen=True)
class CalibrationBucket:
    lower: float
    upper: float
    count: int
    avg_probability: float
    observed_frequency: float
    brier_score: float


@dataclass(frozen=True)
class CalibrationCohort:
    name: str
    count: int
    brier_score: float
    log_loss: float
    top_bin_accuracy: float
    avg_winning_probability: float
    # Climatological-prior Brier on the same cohort days (the baseline a useful
    # forecaster must beat) and the Brier Skill Score 1 - model/clim. A flat
    # absolute-Brier bar is unachievable on the interior 2F bins by ANY calibrated
    # model and is only met by catch-all-dominated cohorts, so the readiness gate
    # judges SKILL (model beats climatology -> skill > 0), not absolute Brier.
    climatology_brier_score: float
    brier_skill: float
    ranked_probability_score: float
    climatology_ranked_probability_score: float
    ranked_probability_skill: float


@dataclass(frozen=True)
class CalibrationBacktestResult:
    n: int
    brier_score: float
    log_loss: float
    top_bin_accuracy: float
    avg_winning_probability: float
    avg_entropy: float
    climatology_brier_score: float
    brier_skill: float
    ranked_probability_score: float
    climatology_ranked_probability_score: float
    ranked_probability_skill: float
    calibration_buckets: tuple[CalibrationBucket, ...]
    cohorts: tuple[CalibrationCohort, ...]
    # Execution diagnostic only; excluded from the cached numeric payload/key.
    cache_hit: bool = False


def run_walk_forward_calibration_backtest(
    outcomes: list[ForecastOutcome],
    *,
    config: StrategyConfig | None = None,
    min_train: int = 180,
    markets: list[MarketBin] | None = None,
    emos_lookup: dict | None = None,
    cache_dir: Path | None = DEFAULT_CALIBRATION_CACHE_DIR,
) -> CalibrationBacktestResult:
    """Walk-forward probability backtest using historical forecast outcomes.

    This tests the model's probability calibration independent of Kalshi prices.
    Market-PnL backtests require archived entry-time quotes, which the collector
    stores going forward.

    ``emos_lookup`` (target_date -> (mu, sigma)) feeds the trained EMOS Gaussian
    into the calibrator; combined with ``config.emos_distribution_enabled`` it
    scores the EMOS-distribution path head-to-head against the residual-calibrated
    path on the same days. The EMOS (mu, sigma) values are already out-of-sample
    (rolling-origin), so the comparison stays leakage-safe.
    """

    cfg = config or StrategyConfig()
    ladder = markets or standard_sfo_bins("KXHIGHTSFO-BACKTEST")
    cache_path = _calibration_cache_path(
        outcomes,
        config=cfg,
        min_train=min_train,
        markets=ladder,
        emos_lookup=emos_lookup,
        cache_dir=cache_dir,
    )
    if cache_path is not None:
        cached = _read_calibration_cache(cache_path)
        if cached is not None:
            return replace(cached, cache_hit=True)
    scored = []
    calibration_samples: list[tuple[float, float]] = []
    for idx in range(min_train, len(outcomes)):
        train = outcomes[:idx]
        test = outcomes[idx]
        calibrator = ResidualCalibrator(train, cfg)
        emos_mu_sigma = emos_lookup.get(test.local_date) if emos_lookup else None
        probs = calibrator.bucket_probabilities(
            ladder, test.predicted_high_f, emos_mu_sigma=emos_mu_sigma
        )
        # Climatological prior: each bin's marginal YES-frequency over the
        # training window, independent of the forecast. It is the no-skill
        # baseline a useful forecaster must beat; per-bin Brier vs this prior
        # gives a Brier Skill Score that does not penalize irreducible multi-bin
        # spread the way a flat absolute-Brier threshold does.
        clim_prior = _climatological_prior(ladder, train)
        brier = 0.0
        clim_brier = 0.0
        probabilities: list[float] = []
        outcomes_yes: list[float] = []
        climatology_probabilities: list[float] = []
        winning_probability = 0.0
        top_ticker = max(probs.values(), key=lambda row: row.probability).ticker
        winning_ticker = None
        entropy = 0.0
        for market in ladder:
            probability = probs[market.ticker].probability
            # Half-up to the integer settlement value, matching the NWS/Kalshi
            # convention used by the forecast adapters (never banker's rounding).
            outcome = 1.0 if market.resolves_yes(math.floor(test.actual_high_f + 0.5)) else 0.0
            calibration_samples.append((probability, outcome))
            brier += (probability - outcome) ** 2
            clim_brier += (clim_prior[market.ticker] - outcome) ** 2
            probabilities.append(probability)
            outcomes_yes.append(outcome)
            climatology_probabilities.append(clim_prior[market.ticker])
            entropy -= probability * math.log(max(probability, 1e-12))
            if outcome:
                winning_probability = probability
                winning_ticker = market.ticker
        scored.append(
            {
                "brier": brier,
                "clim_brier": clim_brier,
                "rps": _ranked_probability_score(probabilities, outcomes_yes),
                "clim_rps": _ranked_probability_score(climatology_probabilities, outcomes_yes),
                "log_loss": -math.log(max(winning_probability, 1e-12)),
                "top_hit": 1.0 if top_ticker == winning_ticker else 0.0,
                "winning_probability": winning_probability,
                "entropy": entropy,
                "actual_high_f": test.actual_high_f,
            }
        )
    if not scored:
        raise ValueError("Not enough outcomes for backtest")
    n = len(scored)
    model_brier = sum(row["brier"] for row in scored) / n
    clim_brier_overall = sum(row["clim_brier"] for row in scored) / n
    model_rps = sum(row["rps"] for row in scored) / n
    clim_rps = sum(row["clim_rps"] for row in scored) / n
    result = CalibrationBacktestResult(
        n=n,
        brier_score=model_brier,
        log_loss=sum(row["log_loss"] for row in scored) / n,
        top_bin_accuracy=sum(row["top_hit"] for row in scored) / n,
        avg_winning_probability=sum(row["winning_probability"] for row in scored) / n,
        avg_entropy=sum(row["entropy"] for row in scored) / n,
        climatology_brier_score=clim_brier_overall,
        brier_skill=_skill_score(model_brier, clim_brier_overall),
        ranked_probability_score=model_rps,
        climatology_ranked_probability_score=clim_rps,
        ranked_probability_skill=_skill_score(model_rps, clim_rps),
        calibration_buckets=_calibration_buckets(calibration_samples),
        cohorts=_calibration_cohorts(scored),
    )
    if cache_path is not None:
        _write_calibration_cache(cache_path, result)
    return result


def _calibration_cache_path(
    outcomes: list[ForecastOutcome],
    *,
    config: StrategyConfig,
    min_train: int,
    markets: list[MarketBin],
    emos_lookup: dict | None,
    cache_dir: Path | None,
) -> Path | None:
    if cache_dir is None:
        return None
    outcome_rows = [
        {
            "date": row.local_date.isoformat(),
            "predicted_high_f": row.predicted_high_f,
            "actual_high_f": row.actual_high_f,
            "model_name": row.model_name,
            "station_id": row.station_id,
        }
        for row in outcomes
    ]
    # Fingerprint the complete normalized MarketBin, including its raw API row.
    # An allowlist here will inevitably drift when the calibrator starts reading
    # another price/depth/status field and can then return a stale numeric result.
    market_rows = [asdict(row) for row in markets]
    emos_rows = sorted(
        (type(key).__name__, str(key), list(value) if isinstance(value, (list, tuple)) else value)
        for key, value in (emos_lookup or {}).items()
    )
    material = {
        "schema": _CACHE_SCHEMA_VERSION,
        "algorithm": "walk-forward-calibration-v2-full-market-input",
        "stations": sorted({row.station_id for row in outcomes}),
        "sources": sorted({row.model_name for row in outcomes}),
        "min_train": min_train,
        "config": asdict(config),
        "markets": market_rows,
        "outcomes": outcome_rows,
        "emos_lookup": emos_rows,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return Path(cache_dir) / f"{digest}.json"


def _read_calibration_cache(path: Path) -> CalibrationBacktestResult | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != _CACHE_SCHEMA_VERSION:
            return None
        result = payload["result"]
        return CalibrationBacktestResult(
            n=int(result["n"]),
            brier_score=float(result["brier_score"]),
            log_loss=float(result["log_loss"]),
            top_bin_accuracy=float(result["top_bin_accuracy"]),
            avg_winning_probability=float(result["avg_winning_probability"]),
            avg_entropy=float(result["avg_entropy"]),
            climatology_brier_score=float(result["climatology_brier_score"]),
            brier_skill=float(result["brier_skill"]),
            ranked_probability_score=float(result["ranked_probability_score"]),
            climatology_ranked_probability_score=float(
                result["climatology_ranked_probability_score"]
            ),
            ranked_probability_skill=float(result["ranked_probability_skill"]),
            calibration_buckets=tuple(
                CalibrationBucket(**row) for row in result["calibration_buckets"]
            ),
            cohorts=tuple(CalibrationCohort(**row) for row in result["cohorts"]),
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _write_calibration_cache(path: Path, result: CalibrationBacktestResult) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "result": asdict(replace(result, cache_hit=False)),
        }
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        _prune_calibration_cache(path.parent)
    except (OSError, TypeError, ValueError):
        # Regenerable optimization: permissions, disk pressure, or a concurrent
        # cleanup must never make calibration/reporting unavailable.
        return


def _prune_calibration_cache(cache_dir: Path) -> None:
    lock_handle = None
    try:
        # Every writer prunes after its atomic replace. Serializing pruners
        # avoids cross-process glob/stat/unlink races; repeated passes also
        # tolerate cache files disappearing during external cleanup.
        try:
            import fcntl

            candidate = (cache_dir / ".prune.lock").open("a+")
            try:
                fcntl.flock(candidate.fileno(), fcntl.LOCK_EX)
            except OSError:
                candidate.close()
            else:
                lock_handle = candidate
        except (ImportError, OSError):
            lock_handle = None
        for _ in range(3):
            entries: list[tuple[float, Path]] = []
            for item in cache_dir.glob("*.json"):
                try:
                    entries.append((item.stat().st_mtime, item))
                except OSError:
                    continue
            entries.sort(key=lambda row: row[0], reverse=True)
            if len(entries) <= _CACHE_MAX_ENTRIES:
                break
            for _, stale in entries[_CACHE_MAX_ENTRIES:]:
                try:
                    stale.unlink()
                except OSError:
                    pass
    except OSError:
        pass
    finally:
        if lock_handle is not None:
            try:
                import fcntl

                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
            except OSError:
                pass


def _climatological_prior(
    ladder: list[MarketBin], train: list[ForecastOutcome]
) -> dict[str, float]:
    """Each bin's marginal YES-frequency over the training window (no-skill prior)."""

    n = len(train)
    if n == 0:
        return {market.ticker: 0.0 for market in ladder}
    settled = [math.floor(o.actual_high_f + 0.5) for o in train]
    return {
        market.ticker: sum(1 for high in settled if market.resolves_yes(high)) / n
        for market in ladder
    }


def _skill_score(model_score: float, reference_score: float) -> float:
    """Proper-score skill: 1 - model/reference. Positive beats the reference."""

    if reference_score <= 0.0:
        return 0.0
    return 1.0 - (model_score / reference_score)


def _brier_skill(model_brier: float, clim_brier: float) -> float:
    """Brier Skill Score: 1 - model/clim. > 0 means the model beats climatology."""

    return _skill_score(model_brier, clim_brier)


def _ranked_probability_score(probabilities: list[float], outcomes: list[float]) -> float:
    """Ranked probability score over ordered, mutually exclusive temperature bins."""

    if not probabilities or len(probabilities) != len(outcomes):
        return 0.0
    cumulative_probability = 0.0
    cumulative_outcome = 0.0
    total = 0.0
    for probability, outcome in zip(probabilities, outcomes):
        cumulative_probability += probability
        cumulative_outcome += outcome
        total += (cumulative_probability - cumulative_outcome) ** 2
    denominator = max(1, len(probabilities) - 1)
    return total / denominator


def _calibration_buckets(samples: list[tuple[float, float]]) -> tuple[CalibrationBucket, ...]:
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(10)]
    for probability, outcome in samples:
        bucket_idx = min(9, max(0, int(probability * 10.0)))
        buckets[bucket_idx].append((probability, outcome))

    rows = []
    for idx, bucket in enumerate(buckets):
        lower = idx / 10.0
        upper = (idx + 1) / 10.0
        if not bucket:
            rows.append(CalibrationBucket(lower, upper, 0, 0.0, 0.0, 0.0))
            continue
        count = len(bucket)
        avg_probability = sum(probability for probability, _ in bucket) / count
        observed_frequency = sum(outcome for _, outcome in bucket) / count
        brier_score = sum((probability - outcome) ** 2 for probability, outcome in bucket) / count
        rows.append(
            CalibrationBucket(
                lower=lower,
                upper=upper,
                count=count,
                avg_probability=avg_probability,
                observed_frequency=observed_frequency,
                brier_score=brier_score,
            )
        )
    return tuple(rows)


def _calibration_cohorts(scored: list[dict[str, float]]) -> tuple[CalibrationCohort, ...]:
    definitions = (
        ("cold_below_60f", lambda row: row["actual_high_f"] < 60.0),
        ("normal_60_69f", lambda row: 60.0 <= row["actual_high_f"] < 70.0),
        ("warm_70_79f", lambda row: 70.0 <= row["actual_high_f"] < 80.0),
        ("hot_80f_plus", lambda row: row["actual_high_f"] >= 80.0),
    )
    cohorts = []
    for name, predicate in definitions:
        rows = [row for row in scored if predicate(row)]
        if not rows:
            continue
        count = len(rows)
        cohort_brier = sum(row["brier"] for row in rows) / count
        cohort_clim_brier = sum(row["clim_brier"] for row in rows) / count
        cohort_rps = sum(row["rps"] for row in rows) / count
        cohort_clim_rps = sum(row["clim_rps"] for row in rows) / count
        cohorts.append(
            CalibrationCohort(
                name=name,
                count=count,
                brier_score=cohort_brier,
                log_loss=sum(row["log_loss"] for row in rows) / count,
                top_bin_accuracy=sum(row["top_hit"] for row in rows) / count,
                avg_winning_probability=sum(row["winning_probability"] for row in rows) / count,
                climatology_brier_score=cohort_clim_brier,
                brier_skill=_brier_skill(cohort_brier, cohort_clim_brier),
                ranked_probability_score=cohort_rps,
                climatology_ranked_probability_score=cohort_clim_rps,
                ranked_probability_skill=_skill_score(cohort_rps, cohort_clim_rps),
            )
        )
    return tuple(cohorts)
