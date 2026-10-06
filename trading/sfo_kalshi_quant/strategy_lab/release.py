"""Versioned research findings, separate from current account performance."""
from __future__ import annotations

from ..research_policy import TARGET_POLICY


def release_payload() -> dict:
    return {
        "version": "v7",
        "policy_version": TARGET_POLICY.policy_version,
        "evidence_snapshot_at": "2026-10-06T02:56:55+00:00",
        "research_objectives": {"daily_return_on_initial": 0.05, "daily_dollars_by_october_31": 40.0},
        "baseline_report_url": "https://github.com/Jaxsonb04/weather_edge/blob/main/docs/research/2026-10-05-v7-audit.md",
        "comparison_note": "v7 starts a separate paper research ledger. v6 losses, fills, goals and unsettled positions retain their original account and policy. No v6 result is counted as v7 performance.",
        "findings": [
            {
                "code": "release-gap", "title": "Tested changes did not reach production",
                "evidence": "confirmed October 5 production snapshot", "state": "baseline release gap; cutover verified separately",
                "impact": "The October 5 baseline still ran the September 4 backend; later sizing, partial-loss memory, execution and market coverage fixes were absent.",
            },
            {
                "code": "low-capture", "title": "Requested size is not filled size",
                "evidence": "measured v6 history", "state": "capacity remains a research constraint",
                "impact": "v6 filled 5,312.96 of 108,965 requested contracts (4.88%). Larger requests reserve capital without proving more liquidity or profit.",
            },
            {
                "code": "weak-net-return", "title": "Losses erased most research gains",
                "evidence": "measured v6 history", "state": "prospective profitability unproven",
                "impact": "v6 netted $17.93 on $3,927.86 resolved entry capital. Atlanta contributed -$123.98. A 72% trading win rate did not establish useful net returns.",
            },
            {
                "code": "partial-loss-memory", "title": "Partial exits lost earlier loss memory",
                "evidence": "confirmed September incident", "state": "v7 includes tested correction",
                "impact": "The preserved September journal review documented six incorrectly vetoed stop attempts after a partial exit. Dollar-loss guards must retain earlier realized slices across monitor restarts; saved dollars are not established.",
            },
            {
                "code": "forecast-input-integrity", "title": "Incomplete and mixed forecast inputs",
                "evidence": "reproduced source defects", "state": "v7 rejects incomplete input and separates sources",
                "impact": "A partial hourly response could masquerade as a whole-day high; duplicate source rows could overwrite one another. Neither defect is proven to explain the measured losses.",
            },
            {
                "code": "exit-evidence", "title": "Paper exits overstated quote evidence",
                "evidence": "reproduced source defects", "state": "v7 shares depth and preserves quote time",
                "impact": "Legacy duplicate lots could reuse one bid's liquidity, and execution-time timestamps concealed request age. V7 conserves each account's exit depth and retains the conservative request-start clock. Historical dollar impact remains unproven.",
            },
            {
                "code": "maker-account-interference", "title": "Separate experiments consumed each other's maker tape",
                "evidence": "reproduced allocation defect", "state": "v7 isolates economic accounts under exec-v5",
                "impact": "Live and Research orders pooled finite tape despite separate ledgers. V7 gives each account an independent paper scenario while conserving volume across its own orders and restarts. Old results retain their original execution generation.",
            },
            {
                "code": "calibration-outcome-leakage", "title": "Diagnostic uncertainty depended on the observed answer",
                "evidence": "reproduced calibration defect", "state": "v7 uses pre-truth forecast cohorts",
                "impact": "Two diagnostics selected sigma from the realized temperature cohort. V7 selects it from the prediction, reports own-sigma postprocessor Brier separately, and labels retrospective diagnostics as in-sample rather than promotion evidence.",
            },
            {
                "code": "history-durability", "title": "Archives omitted important execution and research evidence",
                "evidence": "confirmed source and regression", "state": "v7 exports evidence and displays all eras",
                "impact": "Maker allocations, frozen goals and several research/settlement records were missing from nightly exports. V7 includes them and depth partitions, keeps all archived profiles visible, and starts immutable served forecast vintages without inventing historical inputs.",
            },
            {
                "code": "evidence-gaps", "title": "Old analysis and incomplete replay",
                "evidence": "confirmed baseline and source audit", "state": "further evidence required",
                "impact": "October 5 historical analysis still dated September 4. Reconstructed forecasts are not original decision-time runs. Full NO/maker replay and prospective calibration remain necessary before scaling.",
            },
            {
                "code": "forecast-truth-timing", "title": "Backtests learned truth unavailable at serving",
                "evidence": "causal mutation regressions", "state": "v7 applies lead-aware truth cutoff and serving policy",
                "impact": "Some EMOS, analog and cohort diagnostics used future truth relative to their serve date. V7 applies the lead cutoff and matches the challenger to production's 45-day bias-only correction. Retrospective weather skill remains separate from issued trading probabilities.",
            },
            {
                "code": "dataset-approval", "title": "Accuracy scores were treated as live approval",
                "evidence": "reproduced source and repeated-vintage defects", "state": "v7 requires explicit after-cost approval and observed availability",
                "impact": "Dataset guidance could cross station or availability boundaries, and repeated vintages inflated MOS sample counts. V7 keeps station-days distinct, requires explicit approved keys and after-cost approval, and blocks automatic research promotion.",
            },
            {
                "code": "forecast-report-coupling", "title": "Reports could pair probabilities with the wrong distribution",
                "evidence": "causal report regression", "state": "v7 couples mean and sigma before intraday adjustment",
                "impact": "A stale independent EMOS lookup could override the served forecast's distribution. V7 validates and uses the snapshot's own paired mean and sigma before applying intraday information.",
            },
            {
                "code": "signed-settlement", "title": "Legacy settlement labels lost negative signs",
                "evidence": "reproduced signed and decimal boundary cases", "state": "v7 preserves signs and decimal ranges",
                "impact": "A legacy label such as -5 or below could be interpreted as +5. V7 parses signed thresholds and explicit range separators. Old outcomes are not automatically rewritten; any restatement requires verified contract truth.",
            },
        ],
    }
