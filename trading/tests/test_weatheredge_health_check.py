from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


def _load_health_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "weatheredge_health_check.py"
    spec = importlib.util.spec_from_file_location("weatheredge_health_check", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


health = _load_health_module()


def _load_clear_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "clear_local_runtime_state.py"
    spec = importlib.util.spec_from_file_location("clear_local_runtime_state", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


clear_runtime = _load_clear_module()


def _write(root: Path, relative_path: str, content: str = "") -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_minimal_project(root: Path) -> Path:
    _write(root, "README.md", "# WeatherEdge\n")
    _write(root, "CONTEXT.md", "# WeatherEdge Context\n")
    _write(root, ".gitignore", ".env\n")
    _write(root, "docs/architecture.md", "# Architecture\n")
    _write(root, "src/App.tsx", "export default function App() { return null }\n")
    _write(root, "forecaster/forecast_data.json", "{}\n")
    _write(root, "forecaster/weather_story_data.json", "{}\n")
    _write(root, "scripts/run_tests.sh", "#!/usr/bin/env bash\n")
    _write(
        root,
        "trading/sfo_kalshi_quant/config.py",
        "DEFAULT_FORECASTER_ROOT = 'WeatherEdge/forecaster'\n",
    )
    return root


def test_minimal_project_has_no_failures():
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_minimal_project(Path(tmp))
        results = health.run_checks(root)
        failures = [result for result in results if result.status == "FAIL"]
        assert failures == []


def test_env_file_is_a_failure():
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_minimal_project(Path(tmp))
        google_key = "AIza" + ("A" * 35)
        _write(
            root,
            ".env",
            f"GOOGLE_WEATHER_API_KEY={google_key}\n",
        )
        results = health.run_checks(root)
        assert any(
            result.name == "local secret files" and result.status == "FAIL"
            for result in results
        )


def test_high_confidence_secret_pattern_is_a_failure():
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_minimal_project(Path(tmp))
        token = "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyz"
        _write(root, "README.md", f"token {token}\n")
        results = health.run_checks(root)
        assert any(
            result.name == "secret pattern scan" and result.status == "FAIL"
            for result in results
        )


def test_generated_frontend_dependencies_are_excluded_from_secret_scan():
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_minimal_project(Path(tmp))
        access_key = "AKIA" + "1234567890ABCDEF"
        _write(root, "node_modules/vendor.js", f"const fixture = '{access_key}';\n")

        results = health.run_checks(root)

        assert not any(
            result.name == "secret pattern scan" and result.status == "FAIL"
            for result in results
        )


def test_expected_local_state_directories_are_excluded_from_secret_scan():
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_minimal_project(Path(tmp))
        _write(root, ".local/deploy-key.pem", "local operator credential\n")
        _write(root, ".venv-dev/site-packages/certifi/cacert.pem", "certificate bundle\n")

        results = health.run_checks(root)

        assert not any(
            result.name == "local secret files" and result.status == "FAIL"
            for result in results
        )


def test_local_runtime_artifact_is_a_warning():
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_minimal_project(Path(tmp))
        _write(root, "forecaster/google_weather_cache.json", '{"available": true}\n')

        results = health.run_checks(root)
        runtime = next(result for result in results if result.name == "local runtime data")

        assert runtime.status == "WARN"
        assert "AWS-side after sync" in runtime.details


def test_local_runtime_placeholder_is_not_stale():
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_minimal_project(Path(tmp))
        _write(
            root,
            "forecaster/google_weather_cache.json",
            '{"available": false, "local_runtime_placeholder": true}\n',
        )
        _write(
            root,
            "forecaster/strategy_research.json",
            '{"available": false, "local_runtime_placeholder": true}\n',
        )

        results = health.run_checks(root)
        runtime = next(result for result in results if result.name == "local runtime data")

        assert runtime.status == "PASS"
        assert "strategy_research.json" in runtime.details


def test_city_and_manifest_runtime_artifacts_are_ignored_cleared_and_health_checked():
    repo_root = Path(__file__).resolve().parents[2]
    ignored = (repo_root / ".gitignore").read_text(encoding="utf-8")
    documentation = (repo_root / "docs" / "data_and_artifacts.md").read_text(
        encoding="utf-8"
    )
    artifacts = (
        "forecaster/cities_data.json",
        "forecaster/publication_manifest.json",
    )

    for artifact in artifacts:
        assert artifact in ignored
        assert artifact in clear_runtime.RUNTIME_PATHS
        assert artifact in health.LOCAL_RUNTIME_ARTIFACTS
        assert artifact in documentation

    with tempfile.TemporaryDirectory() as tmp:
        root = _make_minimal_project(Path(tmp))
        for artifact in artifacts:
            _write(root, artifact, '{"generated_at": "2026-07-09T12:00:00+00:00"}\n')

        results = health.run_checks(root)
        runtime = next(result for result in results if result.name == "local runtime data")

    assert runtime.status == "WARN"
    assert "cities_data.json" in runtime.details
    assert "publication_manifest.json" in runtime.details


# ---------------------------------------------------------------------------
# Task 8 item 4 (defense in depth): the authoritative gate is
# sfo_kalshi_quant.publication's publish-time scan, but the local health
# check should catch the same class of leak before an operator even runs a
# publish cycle.
# ---------------------------------------------------------------------------


def test_raw_google_content_in_a_public_artifact_is_a_failure():
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_minimal_project(Path(tmp))
        _write(
            root,
            "forecaster/trading_signal.json",
            json.dumps({"leak": {"weatherCondition": {"description": {"text": "Sunny"}}}}),
        )

        results = health.run_checks(root)

        assert any(
            result.name == "raw Google content scan" and result.status == "FAIL"
            for result in results
        )


def test_raw_google_api_key_pattern_in_a_public_artifact_is_a_failure():
    synthetic_google_key = "AI" + "zaSyD-abcdefghijklmnopqrstuvwxyz012345"
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_minimal_project(Path(tmp))
        _write(
            root,
            "forecaster/cities_data.json",
            json.dumps({"leaked_key": synthetic_google_key}),
        )

        results = health.run_checks(root)

        assert any(
            result.name == "raw Google content scan" and result.status == "FAIL"
            for result in results
        )


def test_legacy_google_high_f_field_does_not_fail_the_raw_content_scan():
    """The pre-existing legacy SFO live blend already reports
    `sources.google_high_f` in production trading_signal.json (spec section
    7.5 -- a known, accepted exception, not a new leak).
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_minimal_project(Path(tmp))
        _write(
            root,
            "forecaster/trading_signal.json",
            json.dumps({"targets": [{"forecast": {"sources": {"google_high_f": 71.69}}}]}),
        )

        results = health.run_checks(root)

        assert any(
            result.name == "raw Google content scan" and result.status == "PASS"
            for result in results
        )


def test_raw_content_scan_passes_when_no_public_artifacts_exist():
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_minimal_project(Path(tmp))

        results = health.run_checks(root)

        assert any(
            result.name == "raw Google content scan" and result.status == "PASS"
            for result in results
        )
