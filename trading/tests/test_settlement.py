import io
from unittest.mock import patch

from sfo_kalshi_quant import settlement
from sfo_kalshi_quant.settlement import fetch_recent_clisfo_settlements, parse_clisfo


def test_parse_maximum_temperature_from_clisfo_like_text():
    text = """
    CLIMATE REPORT
    NATIONAL WEATHER SERVICE SAN FRANCISCO BAY AREA

    WEATHER ITEM   OBSERVED TIME
    TEMPERATURE (F)
     MAXIMUM         67
     MINIMUM         52
    """
    report = parse_clisfo(text)
    assert report.max_temperature_f == 67


def test_parse_negative_maximum_temperature():
    report = parse_clisfo("TEMPERATURE (F)\n MAXIMUM         -4\n MINIMUM         -12\n")

    assert report.max_temperature_f == -4


def test_parse_single_digit_maximum_temperature():
    report = parse_clisfo("TEMPERATURE (F)\n MAXIMUM         7\n MINIMUM         1\n")

    assert report.max_temperature_f == 7


def test_parse_report_date_uses_climate_summary_title_not_tomorrow_normals():
    text = """
    CLIMATE REPORT
    NATIONAL WEATHER SERVICE SAN FRANCISCO BAY AREA

    ...THE SAN FRANCISCO AIRPORT CLIMATE SUMMARY FOR JUNE 7 2026...
    VALID TODAY AS OF 0500 PM LOCAL TIME.

    TEMPERATURE (F)
     MAXIMUM         67  12:29 PM

    THE SAN FRANCISCO AIRPORT CLIMATE NORMALS FOR TOMORROW
     MAXIMUM TEMPERATURE (F)   71        98      1973
    SUNRISE AND SUNSET
    JUNE  8 2026..........SUNRISE   5:48 AM PDT
    """
    report = parse_clisfo(text)
    assert report.report_date == settlement.date(2026, 6, 7)
    assert report.max_temperature_f == 67


def test_fetch_recent_clisfo_settlements_reads_versioned_reports():
    payloads = [
        b"""
        CLIMATE REPORT
        ...THE SAN FRANCISCO AIRPORT CLIMATE SUMMARY FOR JUNE 7 2026...
         MAXIMUM         67  12:29 PM
        """,
        b"""
        CLIMATE REPORT
        ...THE SAN FRANCISCO AIRPORT CLIMATE SUMMARY FOR JUNE 6 2026...
         MAXIMUM         64  11:11 AM
        """,
    ]
    urls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return io.BytesIO(self.payload)

        def __exit__(self, *_):
            return False

    def fake_urlopen(request, timeout):
        urls.append(request.full_url)
        return FakeResponse(payloads[len(urls) - 1])

    with patch.object(settlement, "urlopen", fake_urlopen):
        rows = fetch_recent_clisfo_settlements(timeout=1, versions=2)

    assert rows == {
        settlement.date(2026, 6, 7): 67,
        settlement.date(2026, 6, 6): 64,
    }
    assert urls[0] == settlement.CLISFO_URL
    assert urls[1].endswith("&version=2")
