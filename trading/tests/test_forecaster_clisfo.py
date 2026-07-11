"""The forecaster must score against the CLISFO Daily Climate Report MAXIMUM --
the same settlement truth the trader resolves on."""

import io
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

FORECASTER_DIR = Path(__file__).resolve().parents[2] / "forecaster"
if str(FORECASTER_DIR) not in sys.path:
    sys.path.insert(0, str(FORECASTER_DIR))

import clisfo  # noqa: E402


def test_parse_clisfo_reads_observed_max_not_tomorrow_normals():
    text = """
    ...THE SAN FRANCISCO AIRPORT CLIMATE SUMMARY FOR JUNE 7 2026...
    TEMPERATURE (F)
     MAXIMUM         67  12:29 PM
     MINIMUM         52

    THE SAN FRANCISCO AIRPORT CLIMATE NORMALS FOR TOMORROW
     MAXIMUM TEMPERATURE (F)   71
    """
    report_date, max_temperature = clisfo.parse_clisfo(text)
    assert report_date == date(2026, 6, 7)
    assert max_temperature == 67


def test_max_temperature_is_anchored_to_the_temperature_section():
    # A stray "MAXIMUM" earlier in the product (e.g. a wind row) must not be
    # mistaken for the daily high.
    text = """
    PRECIPITATION MAXIMUM 99 IN SOME UNRELATED ROW
    TEMPERATURE (F)
     MAXIMUM         63  01:14 PM
    """
    _, max_temperature = clisfo.parse_clisfo(text)
    assert max_temperature == 63


def test_headerless_fallback_parses_negative_maximum_temperature():
    _, max_temperature = clisfo.parse_clisfo("CLIMATE REPORT\n MAXIMUM -4\n MINIMUM -12\n")

    assert max_temperature == -4


def test_headerless_fallback_parses_single_digit_maximum_temperature():
    _, max_temperature = clisfo.parse_clisfo("CLIMATE REPORT\n MAX TEMP 7\n MINIMUM 1\n")

    assert max_temperature == 7


def test_fetch_recent_clisfo_settlements_reads_versioned_reports():
    payloads = [
        b"...CLIMATE SUMMARY FOR JUNE 7 2026...\nTEMPERATURE (F)\n MAXIMUM 67 12:29 PM\n",
        b"...CLIMATE SUMMARY FOR JUNE 6 2026...\nTEMPERATURE (F)\n MAXIMUM 64 11:11 AM\n",
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

    with patch.object(clisfo, "urlopen", fake_urlopen):
        rows = clisfo.fetch_recent_clisfo_settlements(timeout=1, versions=2)

    assert rows == {date(2026, 6, 7): 67, date(2026, 6, 6): 64}
    assert urls[0] == clisfo.CLISFO_URL
    assert urls[1].endswith("&version=2")
