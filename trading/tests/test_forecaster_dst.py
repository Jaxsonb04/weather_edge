"""The forecaster must file target dates on the same fixed-PST settlement clock
as the trader, especially in the DST 00:00-01:00 window where civil and
settlement dates disagree."""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# The forecaster modules use project-relative imports; expose them on the path.
FORECASTER_DIR = Path(__file__).resolve().parents[2] / "forecaster"
if str(FORECASTER_DIR) not in sys.path:
    sys.path.insert(0, str(FORECASTER_DIR))

import google_weather_cache as gwc  # noqa: E402


def test_target_date_uses_settlement_clock_in_dst_window():
    # 00:30 PDT on 2026-07-01 is still 2026-06-30 on the fixed-PST settlement
    # clock the trader uses. Civil math would have filed tomorrow as 2026-07-02;
    # the settlement clock files it as 2026-07-01 so the trader finds the blend.
    now = datetime(2026, 7, 1, 0, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert gwc.settlement_today_iso(now) == "2026-06-30"
    assert gwc.target_date(now) == "2026-07-01"


def test_target_date_matches_civil_outside_dst_overlap():
    # Mid-afternoon, civil and settlement dates agree, so tomorrow is the next
    # civil day either way.
    now = datetime(2026, 7, 1, 14, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert gwc.settlement_today_iso(now) == "2026-07-01"
    assert gwc.target_date(now) == "2026-07-02"
