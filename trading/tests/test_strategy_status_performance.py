from datetime import UTC, datetime

import pytest

from sfo_kalshi_quant.strategy_lab.build import _bind_accounting_to_profiles
from sfo_kalshi_quant.strategy_lab.status_alerts import _strategy_alerts


def _accounting():
    return {
        "active_ledgers": {
            "live_stability": {
                "initial_equity": 1000.0,
                "realized_equity": 1053.75,
                "days": [{"opening_equity": 1055.57, "closing_equity": 1053.75}],
            },
            "research_roi": {
                "initial_equity": 1000.0,
                "realized_equity": 1029.87,
                "days": [{"opening_equity": 1120.82, "closing_equity": 1029.87}],
            },
        }
    }


def _alerts(**kwargs):
    return _strategy_alerts(
        paper={"available": True, "summary": {}},
        entry_block_reason=None,
        now=datetime(2026, 9, 12, 22, 20, tzinfo=UTC),
        **kwargs,
    )


def test_current_drawdown_is_visible_even_while_research_account_is_profitable():
    alerts = _alerts(accounting=_accounting())
    assert [a["code"] for a in alerts] == ["research_roi-drawdown"]
    assert alerts[0]["level"] == "warning"
    assert "Research ROI" in alerts[0]["title"]
    assert "$90.95" in alerts[0]["detail"]
    assert "8.11%" in alerts[0]["detail"]
    assert "reported account window" in alerts[0]["detail"]


def test_stale_analysis_cannot_publish_healthy_status():
    alerts = _alerts(analysis_generated_at="2026-09-05T04:36:08+00:00")
    assert [a["code"] for a in alerts] == ["analysis-stale"]
    assert "historical analysis is stale" in alerts[0]["detail"]


@pytest.mark.parametrize("timestamp", ["", "unreadable", "2026-09-13T22:20:00+00:00"])
def test_invalid_or_future_analysis_cannot_publish_healthy_status(timestamp):
    assert "analysis-stale" in {a["code"] for a in _alerts(analysis_generated_at=timestamp)}


def test_account_validation_failure_cannot_publish_healthy_status():
    alerts = _alerts(accounting={"available": False, "reason": "ledger mismatch"})
    assert [a["code"] for a in alerts] == ["accounting-unavailable"]
    assert alerts[0]["level"] == "critical"


def test_only_active_individual_accounts_contribute_drawdown_alerts():
    accounting = _accounting()
    accounting["active_ledgers"]["research_roi"]["realized_equity"] = 1120.82
    accounting["combined"] = {"realized_equity": 1, "initial_equity": 2000}
    accounting["archived_accounts"] = [{"initial_equity": 1000, "realized_equity": 1}]
    assert [a["code"] for a in _alerts(accounting=accounting)] == ["strategy-lab-healthy"]


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf"), True])
def test_missing_equity_does_not_become_a_fabricated_total_loss(missing):
    accounting = _accounting()
    accounting["active_ledgers"]["research_roi"]["realized_equity"] = missing
    assert "research_roi-drawdown" not in {a["code"] for a in _alerts(accounting=accounting)}


def test_drawdown_alert_is_bound_only_to_the_affected_profile():
    rows = [
        {"risk_profile": name, "status": {"alerts": [{"code": "strategy-lab-healthy", "level": "ok"}]} }
        for name in ("live", "research-target")
    ]
    profiles = _bind_accounting_to_profiles(rows, _accounting())
    by_name = {p["risk_profile"]: p for p in profiles}
    assert by_name["live"]["status"]["alerts"][0]["code"] == "strategy-lab-healthy"
    research = by_name["research-target"]["status"]
    assert research["alert_level"] == "warning"
    assert [a["code"] for a in research["alerts"]] == ["research_roi-drawdown"]
