from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LANDING_TEMPLATE = ROOT / "forecaster" / "templates" / "landing.html"
DETAILS_TEMPLATE = ROOT / "forecaster" / "templates" / "details.html"
STRATEGY_TEMPLATE = ROOT / "forecaster" / "templates" / "strategy-lab.html"


def test_landing_refreshes_same_google_cache_as_details_page():
    html = LANDING_TEMPLATE.read_text()

    assert "async function refreshGoogleForecastCache()" in html
    assert "google_weather_cache.json?ts=${Date.now()}" in html


def test_landing_fallback_uses_weatheredge_blend_not_google_only_high():
    html = LANDING_TEMPLATE.read_text()

    assert "function fallbackBlendHighFor(iso, googleDaily)" in html
    assert "?? fallbackBlendHighFor(iso, googleDaily);" in html
    assert "?? coerceNumber(googleDaily?.highF)\n        ?? fallbackHighFor(iso)" not in html


def test_details_first_render_uses_cached_published_blend():
    html = DETAILS_TEMPLATE.read_text()

    assert "const todayInitialForecasts = snapshotForecasts(todayIso);" in html
    assert "const tomorrowInitialForecasts = snapshotForecasts(tomorrowIso);" in html
    assert "const tomorrowInitial = buildForecastBlend(\n        tomorrowRow,\n        tomorrowInitialForecasts," in html
    assert "const tomorrowInitial = buildForecastBlend(tomorrowRow);" not in html


def test_strategy_lab_is_linked_from_existing_pages():
    landing = LANDING_TEMPLATE.read_text()
    details = DETAILS_TEMPLATE.read_text()

    assert 'href="strategy-lab.html"' in landing
    assert 'href="strategy-lab.html"' in details


def test_strategy_lab_reads_public_research_artifact():
    html = STRATEGY_TEMPLATE.read_text()

    assert "const STRATEGY_LAB_PROTECTION = __STRATEGY_LAB_PROTECTION__;" in html
    assert "strategy_research.json" in html
    assert "lstm = Active execution calibration" in html
    assert "clean-blend/combined = Challenger research calibration" in html
    assert "Paper trading only" in html


def test_strategy_lab_has_profile_controls_and_manual_refresh():
    html = STRATEGY_TEMPLATE.read_text()

    assert 'id="profileTabs"' in html
    assert "default_profile" in html
    assert "activeProfileKey" in html
    assert "All profiles overview" in html
    assert 'id="strategyLabRefreshButton"' in html
    assert "setInterval" in html
    assert "5 * 60 * 1000" in html


def test_strategy_lab_fetches_artifact_with_cache_buster_on_each_load():
    html = STRATEGY_TEMPLATE.read_text()

    assert "fetch(`${artifact}?ts=${Date.now()}`)" in html


def test_strategy_lab_supports_password_unlock_flow():
    html = STRATEGY_TEMPLATE.read_text()

    assert 'id="strategyLabUnlockForm"' in html
    assert "async function decryptProtectedResearch" in html
    assert "Password did not unlock Strategy Lab" in html
