from datetime import date
from unittest.mock import patch

import clisfo


def _text(high: int, *, preliminary: bool) -> str:
    marker = "VALID AS OF 500 PM LOCAL TIME\n" if preliminary else ""
    return (
        "CLIMATE SUMMARY FOR JULY 10 2026\n"
        f"{marker}TEMPERATURE (F)\n MAXIMUM {high}\n"
    )


def test_parse_cli_report_detects_explicit_preliminary_marker():
    report = clisfo.parse_cli_report(_text(68, preliminary=True))

    assert report.report_date == date(2026, 7, 10)
    assert report.max_temperature_f == 68
    assert report.is_preliminary is True


def test_recent_cli_reports_prefer_older_final_over_newer_preliminary():
    reports = [
        clisfo.parse_cli_report(_text(68, preliminary=True)),
        clisfo.parse_cli_report(_text(71, preliminary=False)),
    ]
    with patch.object(clisfo, "_fetch_cli_report", side_effect=reports):
        selected = clisfo.fetch_recent_cli_reports("MTR", "SFO", versions=2)

    assert selected[date(2026, 7, 10)].max_temperature_f == 71
    assert selected[date(2026, 7, 10)].is_preliminary is False
    with patch.object(clisfo, "fetch_recent_cli_reports", return_value=selected):
        assert clisfo.fetch_recent_cli_settlements("MTR", "SFO") == {
            date(2026, 7, 10): 71
        }
