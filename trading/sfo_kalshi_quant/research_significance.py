"""Task 6: generic, project-agnostic Holm-Bonferroni family-wise error
correction and a one-sided day-clustered bootstrap p-value.

Plan deviation (documented, mirrors the Task 5 precedent): the plan names
``trading/sfo_kalshi_quant/research_promotion.py`` as the one file to
create for Task 6. Per Task 5's own decision (``research_bootstrap.py``
split out of ``research_evidence.py`` for the same reason), this task
splits the two purely-statistical primitives Task 6 Step 3's final bullet
needs -- "Holm-adjusted significance within the predeclared hypothesis
family" -- into their own sibling module, so ``research_promotion.py``
stays focused on declarations, fold-inventory reconciliation, and the gate
itself, and neither file grows past the project's 800-line cap.

Both functions here are pure, generic statistics with zero coupling to
this project's dataclasses (``PairedCaseRecord``, ``FoldPairedAggregate``,
etc.) -- callers extract plain ``float`` sequences first. Determinism
matches ``research_bootstrap.py``'s own convention exactly: the one source
of randomness is a ``random.Random`` instance seeded with an explicit,
caller-supplied integer, never global ``random`` state, so two calls with
the same input always produce byte-identical output.
"""

from __future__ import annotations

import statistics
from random import Random
from typing import Sequence


def one_sided_bootstrap_p_value(
    values: Sequence[float],
    *,
    seed: int,
    draws: int,
) -> float | None:
    """One-sided bootstrap p-value for H0: the population mean of
    ``values`` is <= 0, against H1: it is > 0.

    Resamples ``len(values)`` draws WITH replacement from ``values``,
    ``draws`` times, and reports the fraction of resample means that are
    <= 0 -- the classic bootstrap-percentile p-value for a one-sided
    "is the effect positive" test. ``values`` is expected to already be
    one independent observation per cluster (e.g. one
    ``FoldPairedAggregate.roi_delta`` per (station_id, target_date) fold
    -- never a per-case/per-ticker row), matching
    ``research_bootstrap.day_clustered_bootstrap``'s own clustering
    contract; this function does not itself cluster anything.

    Returns ``None`` for an empty ``values`` (no evidence to test at all)
    rather than fabricating a p-value from zero data.
    """

    if not values:
        return None
    rng = Random(seed)
    size = len(values)
    at_or_below_zero = 0
    for _ in range(draws):
        draw_mean = statistics.fmean(rng.choice(values) for _ in range(size))
        if draw_mean <= 0.0:
            at_or_below_zero += 1
    return at_or_below_zero / draws


def holm_bonferroni_significant(
    p_values: Sequence[float],
    *,
    alpha: float = 0.05,
) -> tuple[bool, ...]:
    """Classic Holm (1979) step-down family-wise error correction.

    Returns, in the SAME order as ``p_values``, whether each hypothesis is
    rejected (significant) at family-wise ``alpha``. Sorts ascending
    internally, compares the ``i``-th smallest p-value (0-indexed) against
    ``alpha / (m - i)``, and STOPS rejecting at the first comparison that
    fails: every hypothesis ranked at or after that point is also reported
    non-significant, even one whose own raw p-value would individually
    clear ``alpha`` -- this is exactly what makes a family of marginal,
    repeated hypothesis attempts harder to promote than any one of them
    would be in isolation (Task 6 Step 1: "Holm adjustment blocks marginal
    repeated hypotheses").

    An empty ``p_values`` returns an empty tuple. Every ``p_values`` entry
    must be finite and in ``[0, 1]``; out-of-range input fails closed with
    ``ValueError`` rather than silently producing a meaningless ranking.
    """

    for p in p_values:
        if not (0.0 <= p <= 1.0):
            raise ValueError(f"p_values entries must be within [0, 1], got {p!r}")

    m = len(p_values)
    if m == 0:
        return ()

    order = sorted(range(m), key=lambda i: p_values[i])
    rejected = [False] * m
    still_rejecting = True
    for rank, idx in enumerate(order):
        if not still_rejecting:
            break
        threshold = alpha / (m - rank)
        if p_values[idx] <= threshold:
            rejected[idx] = True
        else:
            still_rejecting = False
    return tuple(rejected)
