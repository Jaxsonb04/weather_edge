"""Task 7: research-evaluate / research-propose-target commands.

Two commands, matching the plan's own authority split (Task 7 Step 3):

- ``research-evaluate``: loads (or, with ``--declare``, first declares) a
  challenger, runs the full chronological walk-forward/candidate/replay/
  promotion pipeline (``research_operate.run_research_evaluation``) over
  the paper database's own history, durably persists newly-seen fold
  evidence, and prints/writes a report. "Read-only" (plan Task 7 Step 1)
  means this command carries NO authority to change what is or is not
  eligible for target-paper promotion, and NEVER writes a target-paper
  candidate proposal artifact -- it may (and, to "operate the evidence
  loop", must) durably record immutable, append-only evidence rows.
- ``research-propose-target``: paper-only proposal authority. Re-evaluates
  an ALREADY-declared challenger read-only (``persist=False`` -- it never
  records new evidence itself; that is exclusively ``research-evaluate``'s
  job), and if -- and only if -- the resulting verdict is
  ``eligible_for_target_paper``, writes a versioned JSON "target-paper
  candidate proposal" artifact to ``--output``. This writes ONLY a paper
  artifact for a human/operator to review; it can never edit
  ``LIVE_PROFILE_OVERRIDES``, a live fingerprint, ``LIVE_ORDERS_ENABLED``,
  a dry-run flag, or any AWS real-order unit (this module never imports
  ``config``/``live_execution`` at all).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from ..colors import Color
from ..db import PaperStore
from ..forecast import SfoForecasterAdapter
from ..forecast_scorecards import build_research_evaluation_report
from ..research_operate import (
    DeclarationConflictError,
    declare_challenger,
    historical_rows_from_paper_store,
    load_declared_challenger,
    run_research_evaluation,
)


def _load_settlements(args: argparse.Namespace):
    adapter = SfoForecasterAdapter(args.forecaster_root)
    return adapter.load_cli_settlement_truth()


def _load_or_declare(store: PaperStore, args: argparse.Namespace):
    """E1/E2: when ``--declare`` is passed, ALWAYS route through
    ``declare_challenger`` -- never only on a first run -- so a repeat
    invocation with DIFFERENT tolerance/scope flags is caught and rejected
    loudly (``DeclarationConflictError``) instead of silently reusing
    whatever was declared the first time. Without ``--declare``, this is a
    pure reload: ``load_declared_challenger`` reconstructs the
    ``ChallengerDeclaration`` ONLY from the stored row, and a caller who
    never declared this identity gets a loud ``LookupError`` rather than an
    implicit declaration from whatever flags happen to be at their
    defaults."""

    if args.declare:
        experiment_id = args.experiment_id or (
            f"{args.hypothesis_family}:{args.candidate_key}:{args.candidate_version}"
        )
        return declare_challenger(
            store,
            experiment_id=experiment_id,
            hypothesis_family=args.hypothesis_family,
            candidate_key=args.candidate_key,
            candidate_version=args.candidate_version,
            evidence_role=args.evidence_role,
            predicted_edge_scope=args.predicted_edge_scope,
            max_drawdown_tolerance_pct=args.max_drawdown_tolerance_pct,
            crps_regression_tolerance=args.crps_regression_tolerance,
            brier_regression_tolerance=args.brier_regression_tolerance,
            calibration_gap_regression_tolerance=args.calibration_gap_regression_tolerance,
        )
    return load_declared_challenger(
        store,
        hypothesis_family=args.hypothesis_family,
        candidate_key=args.candidate_key,
        candidate_version=args.candidate_version,
    )


def _print_report(report: dict, *, color: Color) -> None:
    identity = report["experiment_identity"]
    gate = report["promotion_gate"]
    kpi = report["daily_target_kpi"]
    coverage = report["fold_coverage"]
    replay = report["replay_completeness"]
    google_join = report["google_evidence_join"]

    print(
        color.cyan(
            color.bold(
                f"research-evaluate — {identity['hypothesis_family']}/"
                f"{identity['candidate_key']}/{identity['candidate_version']}"
            )
        )
    )
    print(f"experiment_id: {identity['experiment_id']}")
    print(f"evidence_role: {identity['evidence_role']}")
    print(f"predicted_edge_scope: {identity['predicted_edge_scope']}")
    print("")
    print(
        f"fold_coverage: folds={coverage['folds']} "
        f"unavailable={coverage['unavailable_folds']} "
        f"skipped_rows={coverage['skipped_historical_rows']}"
    )
    print(
        f"replay_completeness: paired_cases={replay['paired_case_count']} "
        f"coverage_exclusions={replay['coverage_exclusion_count']}"
    )
    print(
        f"google_evidence_join: matched_rows={google_join['matched_row_count']} "
        f"vintage_mismatches={google_join['vintage_mismatch_count']}"
    )
    print("")
    print(color.gray(kpi["label"]))
    print(
        f"$50/day target: observed_days={kpi['observed_days']} "
        f"hit_rate={kpi['observed_hit_rate']!r} "
        f"mean_daily_pnl={kpi['observed_mean_daily_pnl']!r} "
        f"shortfall_vs_target={kpi['observed_shortfall_vs_target']!r}"
    )
    print("")
    verdict = "ELIGIBLE" if gate["eligible_for_target_paper"] else "BLOCKED"
    print(color.bold(f"promotion_gate: {verdict} ({gate['effect_classification']})"))
    if gate["block_reasons"]:
        print("block_reasons:")
        for reason in gate["block_reasons"]:
            print(f"  - {reason}")
    print(f"live_activation_allowed: {gate['live_activation_allowed']}")
    print(
        f"independent_confirmatory_days={gate['independent_confirmatory_days']} "
        f"distinct_calendar_target_days={gate['distinct_calendar_target_days']} "
        f"holm_adjusted_significant={gate['holm_adjusted_significant']}"
    )


def cmd_research_evaluate(args: argparse.Namespace) -> int:
    store = PaperStore(args.db_path)
    settlements = _load_settlements(args)

    try:
        declaration = _load_or_declare(store, args)
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except DeclarationConflictError as exc:
        print(f"declaration conflict: {exc}", file=sys.stderr)
        return 2

    with store.connect() as conn:
        rows = historical_rows_from_paper_store(conn, settlements=settlements)

    run = run_research_evaluation(
        store,
        declaration=declaration,
        historical_rows=rows,
        embargo_days=args.embargo_days,
        persist=True,
    )
    report = build_research_evaluation_report(run)

    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    _print_report(report, color=Color.from_no_color(args.no_color))
    return 0


def cmd_research_propose_target(args: argparse.Namespace) -> int:
    store = PaperStore(args.db_path)
    settlements = _load_settlements(args)

    try:
        declaration = load_declared_challenger(
            store,
            hypothesis_family=args.hypothesis_family,
            candidate_key=args.candidate_key,
            candidate_version=args.candidate_version,
        )
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with store.connect() as conn:
        rows = historical_rows_from_paper_store(conn, settlements=settlements)

    run = run_research_evaluation(
        store,
        declaration=declaration,
        historical_rows=rows,
        embargo_days=args.embargo_days,
        # paper-only proposal authority never itself records new evidence
        # -- research-evaluate is exclusively responsible for "operating"
        # the evidence loop.
        persist=False,
    )
    report = build_research_evaluation_report(run)
    color = Color.from_no_color(args.no_color)
    _print_report(report, color=color)

    if not run.decision.eligible_for_target_paper:
        print("", file=sys.stderr)
        print("NOT eligible for target-paper promotion; no proposal written.", file=sys.stderr)
        return 1

    proposal = {
        "kind": "research_target_candidate_proposal",
        "proposed_at": datetime.now(UTC).isoformat(),
        **report,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(proposal, indent=2, default=str), encoding="utf-8")
    print("")
    print(f"target-paper candidate proposal written: {output_path}")
    print(
        color.gray(
            "this proposal grants NO live or automatic activation authority -- "
            "it is a paper-only artifact for operator review."
        )
    )
    return 0


def register_research_evaluation_commands(sub, *, command_module) -> None:
    from ..research_promotion import (
        CONFIRMATORY_EVIDENCE_ROLE,
        EXPLORATORY_EVIDENCE_ROLE,
        PREDICTED_EDGE_SCOPE_NO_SIDE_OR_MAKER,
        PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER,
    )
    from ..research_walkforward import DEFAULT_EMBARGO_DAYS

    def _add_identity_and_declaration_args(parser) -> None:
        parser.add_argument("--hypothesis-family", required=True)
        parser.add_argument("--candidate-key", required=True)
        parser.add_argument("--candidate-version", required=True)
        parser.add_argument(
            "--embargo-days", type=int, default=DEFAULT_EMBARGO_DAYS,
            help="Station-day embargo width at fold boundaries.",
        )

    evaluate = sub.add_parser(
        "research-evaluate",
        help=(
            "Read-only chronological research evaluation: no authority to change "
            "target-paper promotion eligibility, but durably records immutable "
            "fold evidence as it operates."
        ),
    )
    _add_identity_and_declaration_args(evaluate)
    evaluate.add_argument(
        "--output", type=Path, default=None,
        help="Optional JSON report output path.",
    )
    evaluate.add_argument(
        "--declare", action="store_true",
        help="Declare this challenger identity if it has never been declared before.",
    )
    evaluate.add_argument("--experiment-id", default=None)
    evaluate.add_argument(
        "--evidence-role", choices=(EXPLORATORY_EVIDENCE_ROLE, CONFIRMATORY_EVIDENCE_ROLE),
        default=CONFIRMATORY_EVIDENCE_ROLE,
    )
    evaluate.add_argument(
        "--predicted-edge-scope",
        choices=(PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER, PREDICTED_EDGE_SCOPE_NO_SIDE_OR_MAKER),
        default=PREDICTED_EDGE_SCOPE_YES_SIDE_TAKER,
    )
    evaluate.add_argument("--max-drawdown-tolerance-pct", type=float, default=0.10)
    evaluate.add_argument("--crps-regression-tolerance", type=float, default=0.5)
    evaluate.add_argument("--brier-regression-tolerance", type=float, default=0.5)
    evaluate.add_argument("--calibration-gap-regression-tolerance", type=float, default=0.3)
    evaluate.set_defaults(func=command_module.cmd_research_evaluate)

    propose = sub.add_parser(
        "research-propose-target",
        help=(
            "Paper-only target-paper candidate proposal authority. Writes a "
            "proposal artifact only when the re-evaluated verdict is eligible; "
            "never touches live configuration."
        ),
    )
    _add_identity_and_declaration_args(propose)
    propose.add_argument(
        "--output", type=Path, required=True,
        help="Required: where to write the target-paper candidate proposal JSON.",
    )
    propose.set_defaults(func=command_module.cmd_research_propose_target)
