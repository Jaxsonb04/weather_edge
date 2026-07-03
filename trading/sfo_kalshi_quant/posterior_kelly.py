"""Posterior-mean Kelly sizing from the realized paper-trade record (Phase 2b).

Replaces the hand-tuned ``fractional_kelly`` constant with a per-cohort *trust*
multiplier learned from the journal, so size grows only as a real edge is
demonstrated and shrinks on cohorts the engine keeps losing. This is the
decision-theoretic basis for fractional Kelly under estimation error
(Baker & McHale 2013; Chu, Wu & Swartz 2018): the Bayes-optimal stake is Kelly
evaluated at the *posterior mean* win-rate, not the raw model probability.

The construction, and why it does what Phase 0 asked for:

* Each cohort's journal record is ``(n, wins, mean_claimed_prob, mean_cost)`` --
  settled trades, side wins, the model's mean claimed win-probability, and the
  mean breakeven cost (a YES contract at cost ``c`` breaks even at win-rate ``c``).
* The Beta prior is centered on the *no-edge* point (win-rate = cost), so the
  posterior win-rate is ``(wins + kappa * cost) / (n + kappa)``. With no record
  the posterior sits at breakeven -> zero realized edge -> zero trust: a cohort
  must EARN its size. As wins accumulate and confirm the claimed edge, trust
  climbs toward 1 and size climbs toward the base fraction.
* ``trust = clamp(realized_edge / claimed_edge, 0, 1)`` is the fraction of the
  model's claimed edge the shrunk record actually supports. A ``floor`` keeps a
  minimal stake so a data-collecting profile keeps filling the journal (set
  floor=0 for a strict real-money profile that should stand down until proven).

Small-sample robustness: a cohort with fewer than ``min_cohort_n`` settled
trades falls back to the pooled (all-cohort) record, so a brand-new cohort is
sized off the engine's overall demonstrated calibration rather than noise.

Pure and DB-free; the journal loader and the risk-engine wiring live elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass

_DEFAULT_PRIOR_STRENGTH = 20.0
_DEFAULT_FLOOR = 0.2
_DEFAULT_MIN_COHORT_N = 8


@dataclass(frozen=True)
class CohortRecord:
    """Settled-trade summary for one cohort (or the pooled 'overall' record)."""

    n: int
    wins: float
    mean_claimed_prob: float
    mean_cost: float

    @classmethod
    def empty(cls) -> "CohortRecord":
        return cls(n=0, wins=0.0, mean_claimed_prob=0.0, mean_cost=0.0)


def posterior_win_rate(record: CohortRecord, *, prior_strength: float) -> float:
    """Beta-posterior mean win-rate with the prior centered on breakeven cost."""

    denom = record.n + prior_strength
    if denom <= 0.0:
        return record.mean_cost
    return (record.wins + prior_strength * record.mean_cost) / denom


def calibration_trust(record: CohortRecord, *, prior_strength: float = _DEFAULT_PRIOR_STRENGTH) -> float:
    """Fraction of the model's claimed edge the shrunk record supports, in [0, 1]."""

    claimed_edge = record.mean_claimed_prob - record.mean_cost
    if claimed_edge <= 0.0:
        return 0.0
    realized_edge = posterior_win_rate(record, prior_strength=prior_strength) - record.mean_cost
    return max(0.0, min(1.0, realized_edge / claimed_edge))


def resolve_record(
    cohort: str | None,
    cohort_records: dict[str, CohortRecord],
    overall: CohortRecord,
    *,
    min_cohort_n: int = _DEFAULT_MIN_COHORT_N,
) -> CohortRecord:
    """Use the cohort's own record when it has enough trades, else the pooled one."""

    record = cohort_records.get(cohort) if cohort is not None else None
    if record is not None and record.n >= min_cohort_n:
        return record
    return overall


@dataclass(frozen=True)
class PosteriorKellyModel:
    """Answers ``size_multiplier(cohort)`` from the journal's per-cohort records."""

    cohort_records: dict[str, CohortRecord]
    overall: CohortRecord
    prior_strength: float = _DEFAULT_PRIOR_STRENGTH
    floor: float = _DEFAULT_FLOOR
    min_cohort_n: int = _DEFAULT_MIN_COHORT_N

    def size_multiplier(self, cohort: str | None) -> float:
        """Multiplier on the base fractional-Kelly for a cohort, in [floor, 1]."""

        record = resolve_record(
            cohort, self.cohort_records, self.overall, min_cohort_n=self.min_cohort_n
        )
        trust = calibration_trust(record, prior_strength=self.prior_strength)
        return self.floor + (1.0 - self.floor) * trust


def _accumulate(rows: list[tuple[str, float, float, int, float]]) -> tuple[
    dict[str, CohortRecord], CohortRecord
]:
    """Build per-cohort + pooled CohortRecords from settled-order tuples.

    Each row is ``(side, claimed_prob, cost, resolved_yes, settlement_high_f)``.
    Cohort is the SETTLED regime (available on every settled order); serving keys
    on the forecast regime, an approximation that is moot while cohorts are thin
    (they fall back to the pooled record) and self-corrects as the journal grows.
    A NO side wins when the bin resolved NO (``resolved_yes == 0``).
    """

    from .config import temperature_cohort

    sums: dict[str, list[float]] = {}  # cohort -> [n, wins, sum_claimed, sum_cost]
    overall = [0.0, 0.0, 0.0, 0.0]
    for side, claimed, cost, resolved_yes, high in rows:
        won = (resolved_yes == 1) if side.upper() == "YES" else (resolved_yes == 0)
        cohort = temperature_cohort(high)
        bucket = sums.setdefault(cohort, [0.0, 0.0, 0.0, 0.0])
        for acc in (bucket, overall):
            acc[0] += 1.0
            acc[1] += 1.0 if won else 0.0
            acc[2] += claimed
            acc[3] += cost

    def to_record(acc: list[float]) -> CohortRecord:
        n = int(acc[0])
        if n == 0:
            return CohortRecord.empty()
        return CohortRecord(
            n=n, wins=acc[1], mean_claimed_prob=acc[2] / n, mean_cost=acc[3] / n
        )

    cohort_records = {cohort: to_record(acc) for cohort, acc in sums.items()}
    return cohort_records, to_record(overall)


def load_posterior_kelly_model(
    conn,
    *,
    prior_strength: float = _DEFAULT_PRIOR_STRENGTH,
    floor: float = _DEFAULT_FLOOR,
    min_cohort_n: int = _DEFAULT_MIN_COHORT_N,
) -> PosteriorKellyModel:
    """Build the sizing model from settled paper orders in ``paper_trading.db``."""

    rows = conn.execute(
        "SELECT side, probability, cost_per_contract, resolved_yes, settlement_high_f "
        "FROM paper_orders "
        "WHERE settled_at IS NOT NULL AND resolved_yes IS NOT NULL "
        "AND settlement_high_f IS NOT NULL AND cost_per_contract IS NOT NULL"
    ).fetchall()
    cohort_records, overall = _accumulate(
        [(r[0], float(r[1]), float(r[2]), int(r[3]), float(r[4])) for r in rows]
    )
    return PosteriorKellyModel(
        cohort_records=cohort_records,
        overall=overall,
        prior_strength=prior_strength,
        floor=floor,
        min_cohort_n=min_cohort_n,
    )
