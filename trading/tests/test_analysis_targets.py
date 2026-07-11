from types import SimpleNamespace

from sfo_kalshi_quant.cli import _resolve_analysis_targets
from sfo_kalshi_quant.colors import Color
from sfo_kalshi_quant.kalshi import KalshiUnavailable


def test_rolling_paper_targets_skip_gracefully_when_event_listing_is_unavailable(capsys):
    class UnavailableClient:
        def list_event_snapshots(self, **_kwargs):
            raise KalshiUnavailable("event listing unavailable")

    args = SimpleNamespace(target_date="rolling", offline_events=None, place_paper=True)

    targets, events = _resolve_analysis_targets(args, Color(enabled=False), UnavailableClient())

    assert targets == []
    assert events == {}
    assert "skipping paper scan" in capsys.readouterr().err
