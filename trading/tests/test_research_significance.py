"""Task 6: Holm-Bonferroni family-wise correction and the one-sided
day-clustered bootstrap p-value it corrects (``research_significance.py``,
split alongside ``research_promotion.py`` for the same file-size-cohesion
reason Task 5 split ``research_bootstrap.py`` out of
``research_evidence.py`` -- see that module's own docstring).

Covers:

- ``one_sided_bootstrap_p_value``: degenerate (fully-determined) cases
  that are hand-verifiable without running the resampling loop at all
  (every cluster identical -> every resample mean is that same constant),
  determinism, and one non-degenerate reference value locked in as a
  golden constant (computed once via this exact seeded algorithm, the
  standard way to test simulation/resampling code).
- ``holm_bonferroni_significant``: empty input, a single hypothesis
  (reduces to the uncorrected boundary at exactly ``alpha``), an
  all-clearly-significant family, the cascading-failure case that is the
  whole point of Task 6 Step 1's "Holm adjustment blocks marginal
  repeated hypotheses", order-independence, and fail-closed validation of
  out-of-range p-values.
"""

from __future__ import annotations

import pytest

from sfo_kalshi_quant.research_bootstrap import DEFAULT_BOOTSTRAP_DRAWS, DEFAULT_BOOTSTRAP_SEED
from sfo_kalshi_quant.research_significance import (
    holm_bonferroni_significant,
    one_sided_bootstrap_p_value,
)

# ---------------------------------------------------------------------------
# one_sided_bootstrap_p_value
# ---------------------------------------------------------------------------


def test_p_value_is_none_for_empty_values() -> None:
    assert one_sided_bootstrap_p_value((), seed=1, draws=100) is None


def test_p_value_is_zero_when_every_cluster_is_identically_positive() -> None:
    # Every resample -- however it's drawn -- draws only from a set of
    # identical positive values, so every resample mean is that same
    # positive constant: 0/draws <= 0 draws, p = 0.0 exactly. No need to
    # actually reason about the resampling to know this in advance.
    values = [3.0] * 10
    assert one_sided_bootstrap_p_value(values, seed=1, draws=500) == 0.0


def test_p_value_is_one_when_every_cluster_is_identically_negative() -> None:
    values = [-3.0] * 10
    assert one_sided_bootstrap_p_value(values, seed=1, draws=500) == 1.0


def test_p_value_is_one_when_every_cluster_is_identically_zero() -> None:
    # <= 0 counts the boundary itself as "not above zero" (H0 not rejected).
    values = [0.0] * 5
    assert one_sided_bootstrap_p_value(values, seed=1, draws=500) == 1.0


def test_p_value_is_deterministic_for_a_fixed_seed() -> None:
    values = [2.0, -1.0, 3.0, -0.5, 1.5, -2.0, 0.5]
    first = one_sided_bootstrap_p_value(values, seed=DEFAULT_BOOTSTRAP_SEED, draws=2000)
    second = one_sided_bootstrap_p_value(values, seed=DEFAULT_BOOTSTRAP_SEED, draws=2000)
    assert first == second


def test_p_value_differs_across_distinct_seeds_for_noisy_input() -> None:
    values = [2.0, -1.0, 3.0, -0.5, 1.5, -2.0, 0.5]
    a = one_sided_bootstrap_p_value(values, seed=1, draws=2000)
    b = one_sided_bootstrap_p_value(values, seed=2, draws=2000)
    assert a != b


def test_p_value_reference_golden_value_for_marginal_mixed_sign_input() -> None:
    # Locked-in reference value for this exact input/seed/draws, computed
    # once via this module's own deterministic algorithm -- the standard
    # way to test a seeded resampling procedure (a hand-computed exact
    # figure would require reasoning about 10000 size-12 resamples, which
    # is not humanly tractable, but the algorithm itself is fully
    # deterministic and spelled out in the docstring above it).
    values = [2.0, 1.5, -1.0, 1.8, -0.8, 2.2, 1.0, -1.5, 1.7, 0.9, -0.6, 1.3]
    p = one_sided_bootstrap_p_value(values, seed=DEFAULT_BOOTSTRAP_SEED, draws=DEFAULT_BOOTSTRAP_DRAWS)
    assert p == pytest.approx(0.0313, abs=0.01)


# ---------------------------------------------------------------------------
# holm_bonferroni_significant
# ---------------------------------------------------------------------------


def test_holm_empty_family_returns_empty_tuple() -> None:
    assert holm_bonferroni_significant(()) == ()


def test_holm_single_hypothesis_at_exactly_alpha_is_significant() -> None:
    # m=1 reduces Holm to the uncorrected single test: threshold == alpha.
    assert holm_bonferroni_significant([0.05], alpha=0.05) == (True,)


def test_holm_single_hypothesis_just_above_alpha_is_not_significant() -> None:
    assert holm_bonferroni_significant([0.0500001], alpha=0.05) == (False,)


def test_holm_single_hypothesis_just_below_alpha_is_significant() -> None:
    assert holm_bonferroni_significant([0.0499999], alpha=0.05) == (True,)


def test_holm_family_of_clearly_significant_p_values_all_pass() -> None:
    assert holm_bonferroni_significant([0.001, 0.002], alpha=0.05) == (True, True)


def test_holm_cascading_failure_blocks_a_family_of_marginal_p_values() -> None:
    # Each of 0.02/0.03/0.04 individually clears an UNCORRECTED single test
    # (all < 0.05), but the smallest already fails its own Holm threshold
    # (0.05/3 ~= 0.0167 < 0.02), so the step-down procedure blocks the
    # entire family -- exactly plan Task 6 Step 1's "Holm adjustment
    # blocks marginal repeated hypotheses".
    assert holm_bonferroni_significant([0.02, 0.03, 0.04], alpha=0.05) == (False, False, False)


def test_holm_preserves_caller_order_not_sorted_order() -> None:
    # Largest p-value listed FIRST; Holm sorts internally but must report
    # back in the caller's original order.
    result = holm_bonferroni_significant([0.04, 0.001], alpha=0.05)
    assert result == (True, True)  # both pass: m=2, thresholds 0.025/0.05


def test_holm_rejects_p_value_below_zero() -> None:
    with pytest.raises(ValueError):
        holm_bonferroni_significant([-0.01, 0.02])


def test_holm_rejects_p_value_above_one() -> None:
    with pytest.raises(ValueError):
        holm_bonferroni_significant([0.5, 1.01])
