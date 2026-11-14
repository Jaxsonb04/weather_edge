from sfo_kalshi_quant.strategy_lab.calibration import _decisions_from_trading_signal


def test_trading_signal_rows_score_the_model_high_not_the_market_headline():
    payload = {
        "targets": [
            {
                "target_date": "2026-11-14",
                "forecast": {
                    "predicted_high_f": 64.1,
                    "model_high_f": 62.3,
                    "forecast_source": "market",
                },
                "decisions": [{"ticker": "KXHIGHTSFO-26NOV14-B62.5", "approved": False}],
            }
        ]
    }
    rows = _decisions_from_trading_signal(payload)
    assert [row["forecast_predicted_high_f"] for row in rows] == [62.3]
