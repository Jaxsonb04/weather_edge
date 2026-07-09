from __future__ import annotations

import hashlib
import importlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
FAST_ARTIFACTS = (
    "trading_signal.json",
    "forecast_data.json",
    "weather_story_data.json",
    "cities_data.json",
)


def _publication():
    return importlib.import_module("sfo_kalshi_quant.publication")


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _artifact_root(
    parent: Path,
    *,
    strategy_generated_at: str = "2026-07-09T11:45:00+00:00",
    include_strategy: bool = True,
) -> Path:
    root = parent / "forecaster"
    root.mkdir()
    _write_json(root / "trading_signal.json", {"generated_at": "2026-07-09T11:59:00+00:00"})
    _write_json(root / "cities_data.json", {"generated_at": "2026-07-09T11:58:00+00:00"})
    _write_json(root / "forecast_data.json", {"table": []})
    _write_json(root / "weather_story_data.json", {"temperature_histogram": {}})
    fallback_mtime = datetime(2026, 7, 9, 11, 50, tzinfo=timezone.utc).timestamp()
    os.utime(root / "forecast_data.json", (fallback_mtime, fallback_mtime))
    os.utime(root / "weather_story_data.json", (fallback_mtime, fallback_mtime))
    if include_strategy:
        _write_json(
            root / "strategy_research.json",
            {"generated_at": strategy_generated_at, "available": True},
        )
    return root


def _assert_publication_error(call, message_fragment: str) -> None:
    module = _publication()
    try:
        call()
    except module.PublicationError as exc:
        assert message_fragment in str(exc)
    else:
        raise AssertionError("expected PublicationError")


def test_manifest_hashes_json_and_preserves_artifact_generation_times():
    module = _publication()
    with tempfile.TemporaryDirectory() as tmp:
        root = _artifact_root(Path(tmp))

        manifest = module.build_manifest(root, now=NOW)

        written = json.loads((root / "publication_manifest.json").read_text(encoding="utf-8"))
        expected_hashes = {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in (*FAST_ARTIFACTS, "strategy_research.json")
        }

    assert manifest == written
    assert manifest["schema_version"] == 1
    assert manifest["snapshot_id"]
    assert manifest["published_at"] == "2026-07-09T12:00:00+00:00"
    assert manifest["artifacts"]["trading_signal.json"]["generated_at"] == (
        "2026-07-09T11:59:00+00:00"
    )
    assert manifest["artifacts"]["forecast_data.json"]["generated_at"] == (
        "2026-07-09T11:50:00+00:00"
    )
    assert manifest["artifacts"]["strategy_research.json"]["generated_at"] == (
        "2026-07-09T11:45:00+00:00"
    )
    for name in (*FAST_ARTIFACTS, "strategy_research.json"):
        assert manifest["artifacts"][name]["sha256"] == expected_hashes[name]
        assert manifest["artifacts"][name]["status"] == "ready"


def test_unchanged_research_is_marked_preserved_with_its_old_generated_at():
    module = _publication()
    with tempfile.TemporaryDirectory() as tmp:
        root = _artifact_root(
            Path(tmp),
            strategy_generated_at="2026-07-09T11:30:00+00:00",
        )
        module.build_manifest(root, now=NOW)

        manifest = module.build_manifest(root, now=NOW + timedelta(minutes=5))

    strategy = manifest["artifacts"]["strategy_research.json"]
    assert strategy["status"] == "preserved"
    assert strategy["generated_at"] == "2026-07-09T11:30:00+00:00"


def test_manifest_requires_every_fast_artifact_and_valid_json():
    with tempfile.TemporaryDirectory() as tmp:
        root = _artifact_root(Path(tmp))
        (root / "cities_data.json").unlink()

        _assert_publication_error(
            lambda: _publication().build_manifest(root, now=NOW),
            "required artifact missing: cities_data.json",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = _artifact_root(Path(tmp))
        (root / "trading_signal.json").write_text("{not-json", encoding="utf-8")

        _assert_publication_error(
            lambda: _publication().build_manifest(root, now=NOW),
            "invalid JSON: trading_signal.json",
        )


def test_optional_research_can_be_missing_from_a_fast_manifest_build():
    module = _publication()
    with tempfile.TemporaryDirectory() as tmp:
        root = _artifact_root(Path(tmp), include_strategy=False)

        manifest = module.build_manifest(root, now=NOW)

    assert manifest["artifacts"]["strategy_research.json"] == {
        "generated_at": None,
        "sha256": None,
        "status": "missing",
    }


def test_manifest_validation_checks_hashes_and_can_require_strategy_research():
    module = _publication()
    with tempfile.TemporaryDirectory() as tmp:
        root = _artifact_root(Path(tmp))
        module.build_manifest(root, now=NOW)
        _write_json(root / "cities_data.json", {"generated_at": "2026-07-09T12:01:00+00:00"})

        _assert_publication_error(
            lambda: module.validate_manifest(root, now=NOW),
            "checksum mismatch: cities_data.json",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = _artifact_root(Path(tmp), include_strategy=False)
        module.build_manifest(root, now=NOW)

        module.validate_manifest(root, now=NOW)
        _assert_publication_error(
            lambda: module.validate_manifest(root, now=NOW, require_strategy=True),
            "strategy artifact missing: strategy_research.json",
        )


def test_manifest_rejects_artifacts_outside_the_public_allowlist():
    module = _publication()
    with tempfile.TemporaryDirectory() as tmp:
        root = _artifact_root(Path(tmp))
        manifest = module.build_manifest(root, now=NOW)
        manifest["artifacts"]["unexpected.json"] = {
            "generated_at": "2026-07-09T11:59:00+00:00",
            "sha256": "0" * 64,
            "status": "ready",
        }
        _write_json(root / "publication_manifest.json", manifest)

        _assert_publication_error(
            lambda: module.validate_manifest(root, now=NOW),
            "unsupported artifact in publication manifest: unexpected.json",
        )
        _assert_publication_error(
            lambda: module.published_artifacts(manifest),
            "unsupported artifact in publication manifest: unexpected.json",
        )


def test_manifest_validation_rejects_timestamps_beyond_future_skew_allowance():
    module = _publication()
    future = (NOW + timedelta(minutes=6)).isoformat()
    with tempfile.TemporaryDirectory() as tmp:
        root = _artifact_root(Path(tmp))
        manifest = module.build_manifest(root, now=NOW)
        manifest["published_at"] = future
        _write_json(root / "publication_manifest.json", manifest)

        _assert_publication_error(
            lambda: module.validate_manifest(root, now=NOW),
            "publication_manifest.json.published_at is in the future",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = _artifact_root(Path(tmp))
        manifest = module.build_manifest(root, now=NOW)
        manifest["artifacts"]["cities_data.json"]["generated_at"] = future
        _write_json(root / "publication_manifest.json", manifest)

        _assert_publication_error(
            lambda: module.validate_manifest(root, now=NOW),
            "cities_data.json.generated_at is in the future",
        )


def test_manifest_validation_requires_content_derived_snapshot_id():
    module = _publication()
    with tempfile.TemporaryDirectory() as tmp:
        root = _artifact_root(Path(tmp))
        manifest = module.build_manifest(root, now=NOW)
        manifest["snapshot_id"] = "NOT-LOWERCASE-HEX"
        _write_json(root / "publication_manifest.json", manifest)

        _assert_publication_error(
            lambda: module.validate_manifest(root, now=NOW),
            "publication manifest snapshot_id must be 24 lowercase hex characters",
        )

    with tempfile.TemporaryDirectory() as tmp:
        root = _artifact_root(Path(tmp))
        manifest = module.build_manifest(root, now=NOW)
        manifest["snapshot_id"] = "0" * 24
        _write_json(root / "publication_manifest.json", manifest)

        _assert_publication_error(
            lambda: module.validate_manifest(root, now=NOW),
            "publication manifest snapshot_id does not match artifact hashes",
        )


def test_manifest_validation_enforces_operational_and_strategy_ages():
    module = _publication()
    with tempfile.TemporaryDirectory() as tmp:
        root = _artifact_root(
            Path(tmp),
            strategy_generated_at="2026-07-09T11:35:00+00:00",
        )
        module.build_manifest(root, now=NOW)
        # A fresh manifest must not disguise frozen operational artifacts.
        module.build_manifest(root, now=NOW + timedelta(minutes=11))

        _assert_publication_error(
            lambda: module.validate_manifest(
                root,
                now=NOW + timedelta(minutes=11),
                max_operational_age_minutes=10,
            ),
            "trading_signal.json is stale",
        )
        _assert_publication_error(
            lambda: module.validate_manifest(
                root,
                now=NOW + timedelta(minutes=11),
                max_strategy_age_minutes=20,
            ),
            "strategy_research.json is stale",
        )


def test_manifest_write_is_atomic_when_replace_fails():
    module = _publication()
    with tempfile.TemporaryDirectory() as tmp:
        root = _artifact_root(Path(tmp))
        output = root / "publication_manifest.json"
        output.write_text('{"snapshot": "previous"}\n', encoding="utf-8")

        with patch("sfo_kalshi_quant.publication.os.replace", side_effect=OSError("disk")):
            try:
                module.build_manifest(root, now=NOW)
            except OSError as exc:
                assert str(exc) == "disk"
            else:
                raise AssertionError("replace failure must be surfaced")

        assert json.loads(output.read_text(encoding="utf-8")) == {"snapshot": "previous"}
        assert list(root.glob(".publication_manifest.json.*.tmp")) == []
