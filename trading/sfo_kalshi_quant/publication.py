"""Build and validate coherent public-artifact publication manifests."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = 1
MAX_FUTURE_SKEW_MINUTES = 5.0
MANIFEST_NAME = "publication_manifest.json"
REQUIRED_FAST_ARTIFACTS = (
    "trading_signal.json",
    "forecast_data.json",
    "weather_story_data.json",
    "cities_data.json",
)
STRATEGY_ARTIFACT = "strategy_research.json"
ALLOWED_ARTIFACTS = frozenset((*REQUIRED_FAST_ARTIFACTS, STRATEGY_ARTIFACT))
OPERATIONAL_FRESHNESS_ARTIFACTS = (
    "trading_signal.json",
    "cities_data.json",
)
_TIMESTAMP_KEYS = (
    "generated_at",
    "generated_at_utc",
    "updated_at",
    "fetched_at",
)


class PublicationError(ValueError):
    """Raised when an artifact snapshot cannot be safely published."""


def _utc_now(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise PublicationError("publication time must be timezone-aware")
    return moment.astimezone(timezone.utc)


def _parse_timestamp(value: object, *, label: str) -> datetime:
    if value in (None, ""):
        raise PublicationError(f"missing timestamp: {label}")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicationError(f"invalid timestamp for {label}: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _load_json_object(path: Path, *, display_name: str | None = None) -> dict:
    name = display_name or path.name
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublicationError(f"invalid JSON: {name}") from exc
    if not isinstance(payload, dict):
        raise PublicationError(f"invalid JSON object: {name}")
    return payload


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _previous_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _artifact_generated_at(
    path: Path,
    payload: dict,
    *,
    previous_entry: dict | None,
    sha256: str,
) -> str:
    for key in _TIMESTAMP_KEYS:
        if key not in payload or payload[key] in (None, ""):
            continue
        return _format_timestamp(
            _parse_timestamp(payload[key], label=f"{path.name}.{key}")
        )

    if (
        isinstance(previous_entry, dict)
        and previous_entry.get("sha256") == sha256
        and previous_entry.get("generated_at")
    ):
        return _format_timestamp(
            _parse_timestamp(
                previous_entry["generated_at"],
                label=f"previous {path.name}.generated_at",
            )
        )
    return _format_timestamp(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _snapshot_id_for_artifacts(artifacts: dict[str, dict]) -> str:
    identity = json.dumps(
        {name: entry.get("sha256") for name, entry in artifacts.items()},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()[:24]


def build_manifest(
    artifact_root: Path,
    *,
    output: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Validate artifact JSON, hash it, and atomically publish one manifest."""

    root = Path(artifact_root)
    manifest_path = Path(output) if output is not None else root / MANIFEST_NAME
    published = _utc_now(now)
    previous = _previous_manifest(manifest_path)
    previous_artifacts = previous.get("artifacts", {})
    if not isinstance(previous_artifacts, dict):
        previous_artifacts = {}

    artifacts: dict[str, dict] = {}
    for name in REQUIRED_FAST_ARTIFACTS:
        path = root / name
        if not path.is_file():
            raise PublicationError(f"required artifact missing: {name}")
        payload = _load_json_object(path, display_name=name)
        digest = _sha256(path)
        artifacts[name] = {
            "generated_at": _artifact_generated_at(
                path,
                payload,
                previous_entry=previous_artifacts.get(name),
                sha256=digest,
            ),
            "sha256": digest,
            "status": "ready",
        }

    strategy_path = root / STRATEGY_ARTIFACT
    if strategy_path.is_file():
        strategy_payload = _load_json_object(
            strategy_path,
            display_name=STRATEGY_ARTIFACT,
        )
        strategy_hash = _sha256(strategy_path)
        prior_strategy = previous_artifacts.get(STRATEGY_ARTIFACT)
        preserved = (
            isinstance(prior_strategy, dict)
            and prior_strategy.get("sha256") == strategy_hash
        )
        artifacts[STRATEGY_ARTIFACT] = {
            "generated_at": _artifact_generated_at(
                strategy_path,
                strategy_payload,
                previous_entry=prior_strategy,
                sha256=strategy_hash,
            ),
            "sha256": strategy_hash,
            "status": "preserved" if preserved else "ready",
        }
    else:
        artifacts[STRATEGY_ARTIFACT] = {
            "generated_at": None,
            "sha256": None,
            "status": "missing",
        }

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": _snapshot_id_for_artifacts(artifacts),
        "published_at": _format_timestamp(published),
        "artifacts": artifacts,
    }
    # Audit PR-01: carry the deployed source provenance (stamped by
    # sync_to_box.sh) into the public manifest so freshness can prove WHICH
    # source revision generated these artifacts. Tolerant of absence: hosts
    # synced before the provenance stamp simply omit the block.
    build_info_path = root / "build_info.json"
    if build_info_path.is_file():
        try:
            build_info = _load_json_object(
                build_info_path, display_name="build_info.json"
            )
        except PublicationError:
            build_info = None
        if isinstance(build_info, dict) and build_info:
            manifest["provenance"] = build_info
    _atomic_write_json(manifest_path, manifest)
    return manifest


def _artifact_entry(manifest: dict, name: str) -> dict:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise PublicationError("publication manifest has no artifacts object")
    entry = artifacts.get(name)
    if not isinstance(entry, dict):
        raise PublicationError(f"manifest artifact entry missing: {name}")
    return entry


def _validate_artifact_allowlist(manifest: dict) -> None:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise PublicationError("publication manifest has no artifacts object")
    unexpected = sorted(set(artifacts) - ALLOWED_ARTIFACTS)
    if unexpected:
        raise PublicationError(
            "unsupported artifact in publication manifest: " + ", ".join(unexpected)
        )


def _validate_artifact(root: Path, manifest: dict, name: str) -> None:
    entry = _artifact_entry(manifest, name)
    if entry.get("status") == "missing":
        raise PublicationError(f"configured artifact missing: {name}")
    path = root / name
    if not path.is_file():
        raise PublicationError(f"configured artifact missing: {name}")
    _load_json_object(path, display_name=name)
    expected = entry.get("sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise PublicationError(f"invalid manifest checksum: {name}")
    if _sha256(path) != expected:
        raise PublicationError(f"checksum mismatch: {name}")
    _parse_timestamp(entry.get("generated_at"), label=f"{name}.generated_at")
    if entry.get("status") not in {"ready", "preserved"}:
        raise PublicationError(f"invalid artifact status: {name}")


def _validate_age(
    generated_at: object,
    *,
    now: datetime,
    max_minutes: float,
    label: str,
) -> None:
    generated = _parse_timestamp(generated_at, label=f"{label}.generated_at")
    age_minutes = (now - generated).total_seconds() / 60.0
    if age_minutes > max_minutes:
        raise PublicationError(
            f"{label} is stale: {age_minutes:.1f}m old "
            f"(maximum {max_minutes:g}m)"
        )


def _validate_not_future(
    timestamp: datetime,
    *,
    now: datetime,
    label: str,
) -> None:
    future_minutes = (timestamp - now).total_seconds() / 60.0
    if future_minutes > MAX_FUTURE_SKEW_MINUTES:
        raise PublicationError(
            f"{label} is in the future by {future_minutes:.1f}m "
            f"(maximum clock skew {MAX_FUTURE_SKEW_MINUTES:g}m)"
        )


def validate_manifest_metadata(
    manifest: dict,
    *,
    now: datetime | None = None,
    require_strategy: bool = False,
    max_operational_age_minutes: float | None = None,
    max_strategy_age_minutes: float | None = None,
) -> dict:
    """Validate manifest structure and freshness without local artifact files."""

    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise PublicationError("unsupported publication manifest schema_version")
    _validate_artifact_allowlist(manifest)
    snapshot_id = manifest.get("snapshot_id")
    if (
        not isinstance(snapshot_id, str)
        or len(snapshot_id) != 24
        or any(char not in "0123456789abcdef" for char in snapshot_id)
    ):
        raise PublicationError(
            "publication manifest snapshot_id must be 24 lowercase hex characters"
        )
    published_at = _parse_timestamp(
        manifest.get("published_at"),
        label="publication_manifest.json.published_at",
    )

    artifact_generated_at: dict[str, datetime] = {}
    for name in REQUIRED_FAST_ARTIFACTS:
        entry = _artifact_entry(manifest, name)
        if entry.get("status") not in {"ready", "preserved"}:
            raise PublicationError(f"configured artifact missing: {name}")
        checksum = entry.get("sha256")
        if not isinstance(checksum, str) or len(checksum) != 64:
            raise PublicationError(f"invalid manifest checksum: {name}")
        artifact_generated_at[name] = _parse_timestamp(
            entry.get("generated_at"),
            label=f"{name}.generated_at",
        )

    strategy_entry = _artifact_entry(manifest, STRATEGY_ARTIFACT)
    strategy_present = strategy_entry.get("status") != "missing"
    if require_strategy and not strategy_present:
        raise PublicationError(f"strategy artifact missing: {STRATEGY_ARTIFACT}")
    if strategy_present:
        if strategy_entry.get("status") not in {"ready", "preserved"}:
            raise PublicationError(f"invalid artifact status: {STRATEGY_ARTIFACT}")
        checksum = strategy_entry.get("sha256")
        if not isinstance(checksum, str) or len(checksum) != 64:
            raise PublicationError(f"invalid manifest checksum: {STRATEGY_ARTIFACT}")
        artifact_generated_at[STRATEGY_ARTIFACT] = _parse_timestamp(
            strategy_entry.get("generated_at"),
            label=f"{STRATEGY_ARTIFACT}.generated_at",
        )

    expected_snapshot_id = _snapshot_id_for_artifacts(manifest["artifacts"])
    if snapshot_id != expected_snapshot_id:
        raise PublicationError(
            "publication manifest snapshot_id does not match artifact hashes"
        )

    checked_at = _utc_now(now)
    _validate_not_future(
        published_at,
        now=checked_at,
        label="publication_manifest.json.published_at",
    )
    for name, generated_at in artifact_generated_at.items():
        _validate_not_future(
            generated_at,
            now=checked_at,
            label=f"{name}.generated_at",
        )
    if max_operational_age_minutes is not None:
        age_minutes = (checked_at - published_at).total_seconds() / 60.0
        if age_minutes > max_operational_age_minutes:
            raise PublicationError(
                f"publication manifest is stale: {age_minutes:.1f}m old "
                f"(maximum {max_operational_age_minutes:g}m)"
            )
        for name in OPERATIONAL_FRESHNESS_ARTIFACTS:
            _validate_age(
                _artifact_entry(manifest, name).get("generated_at"),
                now=checked_at,
                max_minutes=max_operational_age_minutes,
                label=name,
            )
    if max_strategy_age_minutes is not None:
        if not strategy_present:
            raise PublicationError(f"strategy artifact missing: {STRATEGY_ARTIFACT}")
        _validate_age(
            strategy_entry.get("generated_at"),
            now=checked_at,
            max_minutes=max_strategy_age_minutes,
            label=STRATEGY_ARTIFACT,
        )
    return manifest


def validate_manifest(
    artifact_root: Path,
    *,
    manifest_path: Path | None = None,
    now: datetime | None = None,
    require_strategy: bool = False,
    max_operational_age_minutes: float | None = None,
    max_strategy_age_minutes: float | None = None,
) -> dict:
    """Validate manifest structure, JSON inputs, checksums, and optional ages."""

    root = Path(artifact_root)
    path = Path(manifest_path) if manifest_path is not None else root / MANIFEST_NAME
    if not path.is_file():
        raise PublicationError(f"publication manifest missing: {path}")
    manifest = _load_json_object(path, display_name=MANIFEST_NAME)
    validate_manifest_metadata(
        manifest,
        now=now,
        require_strategy=require_strategy,
        max_operational_age_minutes=max_operational_age_minutes,
        max_strategy_age_minutes=max_strategy_age_minutes,
    )

    for name in REQUIRED_FAST_ARTIFACTS:
        _validate_artifact(root, manifest, name)

    strategy_entry = _artifact_entry(manifest, STRATEGY_ARTIFACT)
    strategy_present = strategy_entry.get("status") != "missing"
    if strategy_present:
        _validate_artifact(root, manifest, STRATEGY_ARTIFACT)
    elif (root / STRATEGY_ARTIFACT).exists():
        raise PublicationError(
            f"unmanifested strategy artifact present: {STRATEGY_ARTIFACT}"
        )

    return manifest


def published_artifacts(manifest: dict) -> tuple[str, ...]:
    """Return the data files represented by a validated manifest."""

    _validate_artifact_allowlist(manifest)
    names = list(REQUIRED_FAST_ARTIFACTS)
    if _artifact_entry(manifest, STRATEGY_ARTIFACT).get("status") != "missing":
        names.append(STRATEGY_ARTIFACT)
    return tuple(names)


def _parse_cli_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    return _parse_timestamp(value, label="--now")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build an atomic publication manifest")
    build.add_argument("--artifact-root", type=Path, required=True)
    build.add_argument("--output", type=Path)
    build.add_argument("--now", help=argparse.SUPPRESS)

    validate = subparsers.add_parser("validate", help="validate a publication snapshot")
    validate.add_argument("--artifact-root", type=Path, required=True)
    validate.add_argument("--manifest", type=Path)
    validate.add_argument("--require-strategy", action="store_true")
    validate.add_argument("--max-operational-age-minutes", type=float)
    validate.add_argument("--max-strategy-age-minutes", type=float)
    validate.add_argument("--print-artifacts", action="store_true")
    validate.add_argument("--now", help=argparse.SUPPRESS)

    metadata = subparsers.add_parser(
        "validate-metadata",
        help="validate manifest structure and freshness without local artifacts",
    )
    metadata.add_argument("--manifest", type=Path, required=True)
    metadata.add_argument("--require-strategy", action="store_true")
    metadata.add_argument("--max-operational-age-minutes", type=float)
    metadata.add_argument("--max-strategy-age-minutes", type=float)
    metadata.add_argument("--now", help=argparse.SUPPRESS)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "build":
            manifest = build_manifest(
                args.artifact_root,
                output=args.output,
                now=_parse_cli_time(args.now),
            )
            print(
                f"wrote {args.output or args.artifact_root / MANIFEST_NAME}: "
                f"snapshot {manifest['snapshot_id']}"
            )
            return 0

        if args.command == "validate-metadata":
            manifest = _load_json_object(args.manifest, display_name=MANIFEST_NAME)
            validate_manifest_metadata(
                manifest,
                now=_parse_cli_time(args.now),
                require_strategy=args.require_strategy,
                max_operational_age_minutes=args.max_operational_age_minutes,
                max_strategy_age_minutes=args.max_strategy_age_minutes,
            )
            print(f"valid publication manifest metadata {manifest['snapshot_id']}")
            return 0

        manifest = validate_manifest(
            args.artifact_root,
            manifest_path=args.manifest,
            now=_parse_cli_time(args.now),
            require_strategy=args.require_strategy,
            max_operational_age_minutes=args.max_operational_age_minutes,
            max_strategy_age_minutes=args.max_strategy_age_minutes,
        )
        if args.print_artifacts:
            print("\n".join((*published_artifacts(manifest), MANIFEST_NAME)))
        else:
            print(f"valid publication snapshot {manifest['snapshot_id']}")
        return 0
    except (OSError, PublicationError) as exc:
        print(f"publication error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
