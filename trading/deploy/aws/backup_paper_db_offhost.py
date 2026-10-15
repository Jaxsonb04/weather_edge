#!/usr/bin/env python3
"""Opt-in compressed S3 backup with a guarded independent Mac restore.

The ordinary same-volume backup remains the default. This path never creates
a second raw journal on EC2. It requires fresh private cost/egress evidence,
quiesced writers, an empty WAL, an encrypted durable S3 round trip, complete
SQLite recovery checks, and unchanged source lineage before source transfer.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile

GIB = 1024**3
MAX_COMPRESSED_BYTES = 8 * GIB
PART_BYTES = 4 * GIB
CHUNK = 1024**2
REPO = Path(__file__).resolve().parents[3] if __file__ != "<stdin>" else None


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    temporary.replace(path)


def source_identity(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("backup source must be a regular non-symlink database")
    for suffix, label in (("-wal", "WAL"), ("-journal", "rollback journal")):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError(f"source {label} is not empty")
    value = path.stat()
    return {"device": value.st_dev, "inode": value.st_ino, "bytes": value.st_size,
            "mtime_ns": value.st_mtime_ns, "ctime_ns": value.st_ctime_ns}


def assert_identity(path, expected):
    if source_identity(path) != expected:
        raise ValueError("authoritative database lineage changed after backup")


def compress_snapshot(source, archive, maximum):
    """Physical copy is admissible only after checkpoint and writer quiescence."""
    identity = source_identity(source)
    digest = hashlib.sha256()
    raw_bytes = 0
    with Path(source).open("rb") as incoming, Path(archive).open("xb") as outgoing:
        os.chmod(archive, 0o600)
        with gzip.GzipFile(fileobj=outgoing, mode="wb", compresslevel=1, mtime=0) as compressor:
            while chunk := incoming.read(CHUNK):
                digest.update(chunk)
                raw_bytes += len(chunk)
                compressor.write(chunk)
                if outgoing.tell() > maximum:
                    raise ValueError("compressed backup exceeds hard byte ceiling")
        if outgoing.tell() > maximum:
            raise ValueError("compressed backup exceeds hard byte ceiling")
    assert_identity(source, identity)
    if raw_bytes != identity["bytes"]:
        raise ValueError("source byte count changed during backup")
    return {"source_identity": identity, "raw_sha256": digest.hexdigest(),
            "raw_bytes": raw_bytes, "compressed_bytes": Path(archive).stat().st_size,
            "compressed_sha256": file_hash(archive)}


class CompressedParts:
    """Write one gzip directly to bounded parts without a second gzip copy."""
    def __init__(self, folder, maximum):
        self.folder, self.maximum = Path(folder), maximum
        self.total = 0
        self.digest = hashlib.sha256()
        self.parts, self.paths = [], []
        self.stream = None
        self.part_bytes = 0
        self.part_digest = None

    def finish_part(self):
        if self.stream is not None:
            self.stream.close()
            self.parts.append({"part_number": len(self.parts) + 1,
                               "compressed_bytes": self.part_bytes,
                               "compressed_sha256": self.part_digest.hexdigest()})
            self.stream = None

    def write(self, data):
        if self.total + len(data) > self.maximum:
            raise ValueError("compressed backup exceeds hard byte ceiling")
        self.digest.update(data)
        self.total += len(data)
        remaining = memoryview(data)
        while remaining:
            if self.stream is None:
                path = self.folder / f"snapshot.part-{len(self.paths) + 1:04d}"
                self.stream = path.open("xb")
                os.chmod(path, 0o600)
                self.paths.append(path)
                self.part_bytes, self.part_digest = 0, hashlib.sha256()
            chunk = remaining[:PART_BYTES - self.part_bytes]
            self.stream.write(chunk)
            self.part_digest.update(chunk)
            self.part_bytes += len(chunk)
            remaining = remaining[len(chunk):]
            if self.part_bytes == PART_BYTES:
                self.finish_part()
        return len(data)

    def tell(self):
        return self.total


def compress_snapshot_parts(source, folder, maximum):
    identity = source_identity(source)
    raw_digest, raw_bytes = hashlib.sha256(), 0
    writer = CompressedParts(folder, maximum)
    try:
        with Path(source).open("rb") as incoming:
            with gzip.GzipFile(fileobj=writer, mode="wb", compresslevel=1, mtime=0) as compressor:
                while chunk := incoming.read(CHUNK):
                    raw_digest.update(chunk)
                    raw_bytes += len(chunk)
                    compressor.write(chunk)
    finally:
        writer.finish_part()
    assert_identity(source, identity)
    if raw_bytes != identity["bytes"]:
        raise ValueError("source byte count changed during backup")
    return ({"source_identity": identity, "raw_sha256": raw_digest.hexdigest(),
             "raw_bytes": raw_bytes, "compressed_bytes": writer.total,
             "compressed_sha256": writer.digest.hexdigest(),
             "format": "single-put-parts-v1", "parts": writer.parts}, writer.paths)


def conditional_single_put(aws, bucket, key, path):
    # Single PUT plus one SDK attempt: an ambiguous response cannot cause an
    # automatic second version or leave a multipart upload outside lifecycle.
    subprocess.run([aws, "s3api", "put-object", "--bucket", bucket, "--key", key,
                    "--body", str(path), "--server-side-encryption", "AES256",
                    "--if-none-match", "*"], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=600,
                   env={**os.environ, "AWS_MAX_ATTEMPTS": "1"})
    return encrypted_object(aws, bucket, key, Path(path).stat().st_size)


def verified_parts(metadata, bucket, prefix):
    parts = metadata.get("parts")
    base = metadata.get("object_uri", "")
    if (metadata.get("format") != "single-put-parts-v1"
            or not base.startswith(f"s3://{bucket}/{prefix}/paper_trading-")
            or not base.endswith(".sqlite3.gz") or not isinstance(parts, list)
            or not 1 <= len(parts) <= 2):
        raise ValueError("bounded single-PUT part manifest is invalid")
    total = 0
    for number, part in enumerate(parts, 1):
        if (not isinstance(part, dict) or part.get("part_number") != number
                or part.get("object_uri") != base + f".part-{number:04d}"
                or type(part.get("compressed_bytes")) is not int
                or not 0 < part["compressed_bytes"] <= PART_BYTES
                or not isinstance(part.get("compressed_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", part["compressed_sha256"])
                or not isinstance(part.get("version_id"), str)
                or part["version_id"] in ("", "null") or part.get("sse") != "AES256"):
            raise ValueError("ordered encrypted single-PUT part proof is invalid")
        total += part["compressed_bytes"]
    if total != metadata.get("compressed_bytes") or total > MAX_COMPRESSED_BYTES:
        raise ValueError("part sizes differ from the bounded whole gzip")
    return parts


def stream_pinned_part(aws, bucket, key, part):
    # get-object JSON is discarded separately from its binary body. Linux's
    # inherited descriptor path lets the AWS CLI stream its body into our pipe.
    encrypted_object(aws, bucket, key, part["compressed_bytes"], part["version_id"])
    read_fd, write_fd = os.pipe()
    try:
        process = subprocess.Popen([aws, "s3api", "get-object", "--bucket", bucket,
                                    "--key", key, "--version-id", part["version_id"],
                                    f"/proc/self/fd/{write_fd}"], pass_fds=(write_fd,),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env={**os.environ, "AWS_MAX_ATTEMPTS": "1"})
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    total, digest = 0, hashlib.sha256()
    try:
        with os.fdopen(read_fd, "rb") as incoming:
            while chunk := incoming.read(CHUNK):
                total += len(chunk)
                if total > part["compressed_bytes"]:
                    raise ValueError("single-PUT part download exceeds pinned size")
                digest.update(chunk)
                sys.stdout.buffer.write(chunk)
        if process.wait(timeout=30):
            raise ValueError("pinned part download failed")
        if total != part["compressed_bytes"] or digest.hexdigest() != part["compressed_sha256"]:
            raise ValueError("pinned part checksum or size differs from manifest")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def verify_archive(archive, restored, expected):
    if (Path(archive).stat().st_size != expected["compressed_bytes"]
            or file_hash(archive) != expected["compressed_sha256"]):
        raise ValueError("independently downloaded compressed checksum mismatch")
    digest = hashlib.sha256()
    total = 0
    with gzip.open(archive, "rb") as incoming, Path(restored).open("xb") as outgoing:
        os.chmod(restored, 0o600)
        while chunk := incoming.read(CHUNK):
            total += len(chunk)
            if total > expected["raw_bytes"]:
                raise ValueError("restored database exceeds recorded source size")
            digest.update(chunk)
            outgoing.write(chunk)
    if total != expected["raw_bytes"] or digest.hexdigest() != expected["raw_sha256"]:
        raise ValueError("independently restored database checksum mismatch")
    # immutable avoids creating sidecars or changing the restored evidence.
    with sqlite3.connect(Path(restored).resolve().as_uri() + "?mode=ro&immutable=1", uri=True) as conn:
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("independently restored database failed integrity_check")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("independently restored database failed foreign_key_check")
    return {"integrity_check": "ok", "foreign_key_check": "ok",
            "raw_sha256": digest.hexdigest(), "raw_bytes": total}


def verified_backup_target(value):
    """Bind the operator's existing bucket-control evidence to this destination.

    The instance role cannot read lifecycle/versioning configuration. These
    controls therefore require a fresh signed-in operator audit in the already
    hash-bound private evidence file; object version/encryption stay live checks.
    """
    bucket = value.get("verified_backup_bucket")
    prefix = value.get("verified_backup_prefix")
    region = value.get("verified_backup_region")
    if (not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
            or not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", prefix)
            or prefix != prefix.strip("/") or any(part in ("", ".", "..") for part in prefix.split("/"))
            or not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+){2,3}", region)
            or type(value.get("verified_snapshot_retention_days")) is not int
            or value["verified_snapshot_retention_days"] != 35
            or value.get("verified_bucket_versioning") != "Enabled"):
        raise ValueError("exact backup destination and existing 35-day retention/versioning evidence are required")
    try:
        observed = datetime.fromisoformat(value["controls_verified_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("backup control evidence requires a recent UTC observation") from None
    now = datetime.now(timezone.utc)
    if observed.tzinfo is None or observed.utcoffset() != timedelta(0) or not timedelta(0) <= now - observed <= timedelta(hours=24):
        raise ValueError("backup control evidence requires a UTC observation within the last 24 hours")
    return {key: value[key] for key in (
        "verified_backup_bucket", "verified_backup_prefix", "verified_backup_region",
        "verified_snapshot_retention_days", "verified_bucket_versioning", "controls_verified_at")}


def matching_private_evidence(path, expected):
    return (isinstance(path, str) and Path(path).is_file()
            and Path(path).stat().st_size <= 1024**2 and file_hash(path) == expected)


def validate_monthly_budget(value, attempt_cap, days):
    """Require fresh whole-account estimates for every retained-storage month."""
    cap = value.get("authorized_monthly_aws_budget_cents")
    baselines = value.get("verified_monthly_aws_baseline_upper_bounds_cents")
    now = datetime.now(timezone.utc)
    try:
        observed = datetime.fromisoformat(value["monthly_costs_verified_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("fresh whole-account monthly AWS cost evidence is required") from None
    if (observed.tzinfo is None or observed.utcoffset() != timedelta(0)
            or not timedelta(0) <= now - observed <= timedelta(hours=24)
            or type(cap) is not int or not 0 < cap <= 4000
            or not isinstance(baselines, dict)):
        raise ValueError("fresh whole-account AWS estimates within the 40-dollar monthly cap are required")
    months = {(now + timedelta(days=offset)).strftime("%Y-%m") for offset in range(days + 1)}
    for month in months:
        baseline = baselines.get(month)
        if type(baseline) is not int or baseline < 0 or baseline + attempt_cap > cap:
            raise ValueError("existing AWS costs plus the entire recovery allowance exceed a retained-storage month's budget")


def validate_spend_policy(value, maximum):
    """Validate truthful operator evidence; this cannot cap the AWS invoice.

    The paid branch authorizes one attempt only. Its all-in upper estimate must
    include every retained version, request/retry cost, taxes and failure cleanup
    in the operator's hash-bound evidence, rather than relabeling paid use free.
    """
    mode = value.get("spend_mode", "zero-increment")
    if mode == "zero-increment":
        if value.get("no_incremental_spend_verified") is not True:
            raise ValueError("current-month no-increase-spend evidence is required")
        return None
    if mode != "authorized-one-attempt" or value.get("no_incremental_spend_verified") is not False:
        raise ValueError("explicit one-attempt paid mode requires a false no-increment claim")
    cap = value.get("authorized_incremental_spend_cents")
    estimate = value.get("verified_all_in_cost_upper_bound_cents")
    days = value.get("verified_cost_horizon_days")
    rate = value.get("verified_storage_rate_microusd_per_gb_month")
    if (type(cap) is not int or not 0 < cap <= 100
            or type(estimate) is not int or not 0 < estimate <= cap
            or type(value.get("authorized_backup_attempts")) is not int
            or value["authorized_backup_attempts"] != 1
            or type(value.get("verified_noncurrent_retention_days")) is not int
            or value["verified_noncurrent_retention_days"] != 7
            or type(days) is not int or not 45 <= days <= 90
            or type(rate) is not int or rate <= 0):
        raise ValueError("one paid attempt requires a verified all-in estimate within a maximum 100-cent authorization")
    # Conservative decimal GB and a bounded manifest allowance. This is merely
    # a storage lower bound; the operator estimate must also cover all other
    # charges, failure states and actual lifecycle controls in private evidence.
    numerator = (maximum + 1024**2) * rate * days
    denominator = 10**9 * 30 * 10000
    storage_cents = (numerator + denominator - 1) // denominator
    if estimate <= storage_cents:
        raise ValueError("all-in paid estimate must cover storage plus other charge margin")
    validate_monthly_budget(value, cap, days)
    authorization = value.get("spend_authorization_id")
    if (not isinstance(authorization, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,99}", authorization)
            or not matching_private_evidence(value.get("spend_authorization_evidence_file"),
                                             value.get("spend_authorization_evidence_sha256"))):
        raise ValueError("actual owner-answer evidence and a stable authorization identity are required")
    return {"authorization_id": authorization,
            "authorization_evidence_sha256": value["spend_authorization_evidence_sha256"],
            "authorized_cap_cents": cap, "estimated_upper_bound_cents": estimate,
            "reserved_cost_cents": cap, "reserved_attempts": 1}


def validate_policy(value, month):
    maximum = value.get("max_compressed_bytes")
    free = value.get("verified_free_egress_bytes")
    monthly = value.get("monthly_egress_budget_bytes")
    if (value.get("verified_month") != month
            or any(type(number) is not int or number <= 0 for number in (maximum, free, monthly))
            or maximum > MAX_COMPRESSED_BYTES or maximum < 1024**2
            or 2 * maximum > min(free, monthly)):
        raise ValueError("current-month verified free-egress evidence is required")
    if not matching_private_evidence(value.get("cost_evidence_file"), value.get("cost_evidence_sha256")):
        raise ValueError("private current cost-evidence hash is missing or changed")
    verified_backup_target(value)
    validate_spend_policy(value, maximum)
    return maximum


def reserve_egress(path, policy, month, maximum, *, deploy_id=None):
    # Reserve before any upload. Failed attempts retain both egress and the
    # entire authorized cost cap; one answer cannot silently fund a retry.
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(saved, dict):
            raise ValueError("existing backup reservation is invalid")
        used = saved.get("reserved_bytes", 0) if saved.get("month") == month else 0
        paid = saved.get("paid_authorizations", {})
        if type(used) is not int or used < 0 or not isinstance(paid, dict):
            raise ValueError("existing backup reservation is invalid")
        if used + 2 * maximum > min(policy["verified_free_egress_bytes"], policy["monthly_egress_budget_bytes"]):
            raise ValueError("verified free egress reservation exhausted")
        reservation = validate_spend_policy(policy, maximum)
        if reservation is not None:
            if (not isinstance(deploy_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,100}", deploy_id)):
                raise ValueError("paid attempt requires its deployment identity")
            authorization = reservation["authorization_id"]
            if (authorization in paid
                    or any(not isinstance(entry, dict)
                           or entry.get("authorization_evidence_sha256") == reservation["authorization_evidence_sha256"]
                           for entry in paid.values())):
                raise ValueError("owner-authorized paid backup attempt already reserved; explicit new authorization required")
            paid[authorization] = {**reservation, "deploy_id": deploy_id,
                                   "cost_evidence_sha256": policy["cost_evidence_sha256"],
                                   "reserved_at": datetime.now(timezone.utc).isoformat()}
        write_json(path, {"month": month, "reserved_bytes": used + 2 * maximum,
                          "failed_attempts_keep_reservation": True,
                          "paid_authorizations": paid})


def require_encrypted_local_restore(state):
    result = subprocess.check_output(["/usr/bin/fdesetup", "status"], text=True, timeout=10,
                                     stderr=subprocess.DEVNULL).strip()
    if result != "FileVault is On." or Path(state).stat().st_dev != Path.home().stat().st_dev:
        raise ValueError("independent raw restore requires the FileVault-protected home volume")


@contextmanager
def shared_compute_lock(config_path):
    """Serialize with the installed worker using its configured state directory."""
    config_path = Path(config_path).expanduser().resolve()
    raw = config_path.read_bytes()
    if len(raw) > 1024**2:
        raise ValueError("local compute configuration exceeds bounded receipt size")
    config = json.loads(raw)
    if not isinstance(config, dict) or not isinstance(config.get("state_dir"), str) or not config["state_dir"].strip():
        raise ValueError("installed local compute configuration requires its state_dir")
    digest = hashlib.sha256(raw).hexdigest()
    state = Path(config["state_dir"]).expanduser().resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "worker.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another local research job is active; offhost backup deferred") from None
        if file_hash(config_path) != digest:
            raise ValueError("local compute configuration changed before lock admission")
        yield digest
        if file_hash(config_path) != digest:
            raise ValueError("local compute configuration changed while the worker lock was held")


def encrypted_object(aws, bucket, key, expected_bytes=None, expected_version=None):
    value = json.loads(subprocess.check_output([aws, "s3api", "head-object", "--bucket", bucket,
                                               "--key", key], text=True, timeout=30,
                                              stderr=subprocess.DEVNULL))
    if (value.get("ServerSideEncryption") != "AES256"
            or value.get("VersionId") in (None, "", "null")
            or type(value.get("ContentLength")) is not int
            or (expected_bytes is not None and value["ContentLength"] != expected_bytes)
            or (expected_version is not None and value["VersionId"] != expected_version)):
        raise ValueError("durable backup encryption, version or byte-count proof failed")
    return value


def remote_config():
    path = Path("/etc/weatheredge.env")
    values = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value.rstrip("\r")
    bucket = values.get("SFO_ARCHIVE_S3_BUCKET", "")
    prefix = values.get("SFO_DATABASE_BACKUP_S3_PREFIX", "database-snapshots").strip("/")
    aws = values.get("SFO_ARCHIVE_AWS_CLI", "aws")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) or not re.fullmatch(r"[A-Za-z0-9._/-]+", prefix):
        raise ValueError("configured durable S3 backup target is invalid")
    if shutil.which(aws) is None:
        raise ValueError("configured AWS CLI is unavailable")
    return aws, bucket, prefix


def verified_remote_config(payload):
    target = verified_backup_target(payload.get("backup_target") or {})
    aws, bucket, prefix = remote_config()
    if (bucket != target["verified_backup_bucket"] or prefix != target["verified_backup_prefix"]):
        raise ValueError("configured backup destination differs from verified operator evidence")
    location = json.loads(subprocess.check_output(
        [aws, "s3api", "get-bucket-location", "--bucket", bucket],
        text=True, timeout=30, stderr=subprocess.DEVNULL))
    if "LocationConstraint" not in location:
        raise ValueError("backup bucket region could not be verified")
    region = location["LocationConstraint"]
    region = "us-east-1" if region is None else ("eu-west-1" if region == "EU" else region)
    if region != target["verified_backup_region"]:
        raise ValueError("backup bucket region differs from verified operator evidence")
    return aws, bucket, prefix


def require_quiescence(payload):
    if not Path("/run/weatheredge-deploy-maintenance").is_file():
        raise ValueError("deployment maintenance marker is required")
    for unit in payload["units"]:
        if not re.fullmatch(r"(?:sfo|weatheredge)-[a-z0-9-]+\.(?:timer|service)", unit):
            raise ValueError("unexpected quiescence unit")
        state = subprocess.check_output(["systemctl", "show", "--property=ActiveState", "--value", unit], text=True, timeout=10).strip()
        if state not in ("inactive", "failed"):
            raise ValueError("a WeatherEdge writer or timer is active or unverifiable")


def remote_main(action, payload):
    """Executed from stdin with the candidate helper, never installed early."""
    import resource
    import signal
    resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
    os.nice(10)
    def cancelled(number, frame):
        raise RuntimeError("remote backup cancelled; source database remains untouched")
    for number in (signal.SIGALRM, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, cancelled)
    signal.alarm(1200)
    source = Path(payload["database"])
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", str(source)) or ".." in source.parts:
        raise ValueError("database path is not conservative")
    if payload.get("single_put_parts"):
        os.environ["AWS_MAX_ATTEMPTS"] = "1"
    aws, bucket, prefix = verified_remote_config(payload)
    if action == "preflight":
        if payload.get("single_put_parts"):
            capabilities = json.loads(subprocess.check_output(
                [aws, "s3api", "put-object", "--generate-cli-skeleton", "input"],
                text=True, timeout=30, stderr=subprocess.DEVNULL))
            if "IfNoneMatch" not in capabilities or "ServerSideEncryption" not in capabilities:
                raise ValueError("installed AWS CLI lacks conditional single-PUT support")
        subprocess.run([aws, "sts", "get-caller-identity"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if source.is_symlink() or not source.is_file():
            raise ValueError("backup source must be a regular non-symlink database")
        # Preflight precedes quiescence, so a healthy active writer may have a
        # WAL here. Backup itself checkpoints and proves it empty under pause.
        database_bytes = source.stat().st_size
        free = shutil.disk_usage(source.parent).free
        if free < payload["maximum"] + GIB:
            raise ValueError("compressed snapshot ceiling plus operating headroom does not fit")
        print(json.dumps({"database_bytes": database_bytes, "available_bytes": free}))
    elif action == "backup":
        require_quiescence(payload)
        with sqlite3.connect(source, timeout=0, isolation_level=None) as conn:
            busy, log, checkpointed = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy or log not in (0, -1) or checkpointed not in (0, -1):
                raise ValueError("authoritative WAL checkpoint did not complete")
            conn.execute("BEGIN")
            conn.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            with tempfile.TemporaryDirectory(prefix=".compressed-deploy-", dir=source.parent) as folder:
                os.chmod(folder, 0o700)
                archive = Path(folder) / "snapshot.sqlite3.gz"
                if payload.get("single_put_parts"):
                    metadata, part_paths = compress_snapshot_parts(source, folder, payload["maximum"])
                else:
                    metadata = compress_snapshot(source, archive, payload["maximum"])
                token = payload["deploy_id"]
                if not re.fullmatch(r"[A-Za-z0-9-]{8,100}", token):
                    raise ValueError("deploy identity is invalid")
                key = f"{prefix}/paper_trading-{token}.sqlite3.gz"
                metadata.update({"object_uri": f"s3://{bucket}/{key}", "database": str(source),
                                 "deploy_id": token, "candidate_sha": payload["candidate_sha"],
                                 "created_at": datetime.now(timezone.utc).isoformat()})
                if payload.get("single_put_parts"):
                    for part, path in zip(metadata["parts"], part_paths, strict=True):
                        if verified_remote_config(payload) != (aws, bucket, prefix):
                            raise ValueError("configured backup destination changed before part upload")
                        part_key = key + f".part-{part['part_number']:04d}"
                        proof = conditional_single_put(aws, bucket, part_key, path)
                        part.update({"object_uri": f"s3://{bucket}/{part_key}",
                                     "version_id": proof["VersionId"], "sse": proof["ServerSideEncryption"]})
                    verified_parts(metadata, bucket, prefix)
                else:
                    if verified_remote_config(payload) != (aws, bucket, prefix):
                        raise ValueError("configured backup destination changed before upload")
                    subprocess.run([aws, "s3", "cp", str(archive), metadata["object_uri"],
                                    "--sse", "AES256", "--only-show-errors"], check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
                    proof = encrypted_object(aws, bucket, key, metadata["compressed_bytes"])
                    metadata.update({"archive_version_id": proof["VersionId"], "sse": proof["ServerSideEncryption"]})
                manifest = Path(folder) / "snapshot.manifest.json"
                write_json(manifest, metadata)
                if verified_remote_config(payload) != (aws, bucket, prefix):
                    raise ValueError("configured backup destination changed before manifest upload")
                if payload.get("single_put_parts"):
                    conditional_single_put(aws, bucket, key + ".manifest.json", manifest)
                else:
                    subprocess.run([aws, "s3", "cp", str(manifest), metadata["object_uri"] + ".manifest.json",
                                    "--sse", "AES256", "--only-show-errors"], check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
                    encrypted_object(aws, bucket, key + ".manifest.json", manifest.stat().st_size)
                require_quiescence(payload)
                assert_identity(source, metadata["source_identity"])
                print(json.dumps(metadata))
    elif action in ("download", "manifest"):
        metadata = payload["metadata"]
        expected_prefix = f"s3://{bucket}/{prefix}/paper_trading-"
        if not metadata["object_uri"].startswith(expected_prefix) or not metadata["object_uri"].endswith(".sqlite3.gz"):
            raise ValueError("backup object is outside configured durable prefix")
        if action == "download" and payload.get("single_put_parts"):
            parts = verified_parts(metadata, bucket, prefix)
            if metadata["compressed_bytes"] > payload["maximum"]:
                raise ValueError("whole gzip exceeds reserved compressed ceiling")
            for part in parts:
                stream_pinned_part(aws, bucket, part["object_uri"].split("/", 3)[3], part)
            return
        suffix = ".manifest.json" if action == "manifest" else ""
        key = metadata["object_uri"].split("/", 3)[3] + suffix
        proof = encrypted_object(aws, bucket, key,
                                 metadata["compressed_bytes"] if action == "download" else None,
                                 metadata["archive_version_id"] if action == "download" else None)
        if action == "manifest" and proof["ContentLength"] > 1024**2:
            raise ValueError("durable backup manifest is oversized")
        # Bound bytes while receiving, including a changed/oversized S3 object.
        limit = 1024**2 if action == "manifest" else min(payload["maximum"], metadata["compressed_bytes"])
        process = subprocess.Popen([aws, "s3", "cp", metadata["object_uri"] + suffix, "-", "--only-show-errors"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        total = 0
        try:
            while chunk := process.stdout.read(CHUNK):
                total += len(chunk)
                if total > limit:
                    raise ValueError("download exceeds recorded backup byte ceiling")
                sys.stdout.buffer.write(chunk)
            if process.wait(timeout=30):
                raise ValueError("durable backup object download failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    elif action == "assert-lineage":
        require_quiescence(payload)
        assert_identity(source, payload["metadata"]["source_identity"])
        print(json.dumps({"unchanged": True}))
    else:
        raise ValueError("unsupported remote backup action")


def ssh_command(args, action, payload):
    remote = "sudo -n python3 - --remote-action " + shlex.quote(action) + " --payload " + shlex.quote(json.dumps(payload, separators=(",", ":")))
    return ["ssh", "-i", args.key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=6", args.target, remote]


def call_remote(args, action, payload, budget, folder, *, binary=False):
    output = folder / (action + (".gz" if binary else ".json"))
    with Path(__file__).open("rb") as source, output.open("xb") as destination, (folder / (action + ".stderr")).open("wb") as errors:
        os.chmod(output, 0o600)
        try:
            budget.run(ssh_command(args, action, payload), stdin=source, stdout=destination, stderr=errors)
        except BaseException:
            errors.flush()
            shutil.copyfile(errors.name, folder.parent / ("failure-" + args.deploy_id + "-" + action + ".stderr"))
            os.chmod(folder.parent / ("failure-" + args.deploy_id + "-" + action + ".stderr"), 0o600)
            raise
    if binary:
        if output.stat().st_size > payload["maximum"]:
            raise ValueError("download exceeds reserved compressed byte ceiling")
        return output
    if output.stat().st_size > 1024**2:
        raise ValueError("backup metadata exceeds bounded receipt size")
    return json.loads(output.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", choices=("preflight", "backup", "assert-lineage"))
    parser.add_argument("--remote-action")
    parser.add_argument("--payload")
    parser.add_argument("--verify-local")
    parser.add_argument("--policy")
    parser.add_argument("--receipt")
    parser.add_argument("--database")
    parser.add_argument("--target")
    parser.add_argument("--key")
    parser.add_argument("--deploy-id")
    parser.add_argument("--candidate-sha")
    parser.add_argument("--local-compute-config")
    args = parser.parse_args()
    if args.remote_action:
        remote_main(args.remote_action, json.loads(args.payload))
        return
    if args.verify_local:
        if os.environ.get("WEATHEREDGE_LOCAL_BUDGET_ACTIVE") != "1":
            raise ValueError("independent local restore must be launched through its resource budget")
        value = json.loads(Path(args.verify_local).read_text())
        checks = verify_archive(value["archive"], value["restored"], value["metadata"])
        write_json(value["result"], checks)
        return
    if any(not getattr(args, key) for key in ("mode", "policy", "receipt", "database", "target", "key", "deploy_id", "candidate_sha", "local_compute_config")):
        parser.error("explicit policy, receipt, database, SSH, deploy identity, candidate SHA and installed local compute config are required")
    if sys.version_info < (3, 11):
        raise ValueError("offhost backup requires local Python 3.11 or newer")
    if not re.fullmatch(r"[0-9a-f]{40}", args.candidate_sha):
        raise ValueError("full candidate source SHA is required")
    with shared_compute_lock(args.local_compute_config) as compute_config_sha:
        local_main(args, compute_config_sha)


def local_main(args, compute_config_sha):
    sys.path.insert(0, str(REPO / "scripts/local_compute"))
    from guards import Budget, host_snapshot, policy as resource_policy
    state = Path(args.receipt).resolve().parent
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    value = json.loads(Path(args.policy).read_text())
    policy_sha = file_hash(args.policy)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    maximum = validate_policy(value, month)
    limits = resource_policy({})
    host_snapshot(state, limits)
    require_encrypted_local_restore(state)
    budget = Budget(state, limits)
    canonical = (Path(__file__).parent / "disable_systemd_timers.sh").read_text()
    units = sorted(set(re.findall(r'"((?:sfo|weatheredge)-[a-z0-9-]+\.timer) ((?:sfo|weatheredge)-[a-z0-9-]+\.service)"', canonical)))
    all_units = [unit for pair in units for unit in pair] + ["weatheredge-strategy-analysis-cache.service"]
    if len(units) != 14:
        raise ValueError("canonical quiescence allowlist requires review")
    payload = {"database": args.database, "maximum": maximum, "deploy_id": args.deploy_id,
               "candidate_sha": args.candidate_sha, "units": all_units,
               "backup_target": verified_backup_target(value),
               "single_put_parts": validate_spend_policy(value, maximum) is not None}
    with tempfile.TemporaryDirectory(prefix=".offhost-check-", dir=state) as temporary:
        folder = Path(temporary)
        if args.mode == "assert-lineage":
            receipt = json.loads(Path(args.receipt).read_text())
            if (receipt.get("verified") is not True or receipt.get("deploy_id") != args.deploy_id
                    or receipt.get("candidate_sha") != args.candidate_sha
                    or receipt.get("policy_sha256") != policy_sha
                    or receipt.get("local_compute_config_sha256") != compute_config_sha):
                raise ValueError("verified offhost receipt does not bind this deployment")
            payload["metadata"] = receipt["metadata"]
            call_remote(args, "assert-lineage", payload, budget, folder)
            print("offhost backup lineage remains frozen")
            return
        inspection = call_remote(args, "preflight", payload, budget, folder)
        if shutil.disk_usage(state).free < inspection["database_bytes"] + maximum + 20 * GIB:
            raise ValueError("independent Mac restore lacks database, archive and operating headroom")
        if args.mode == "preflight":
            print("WEATHEREDGE_DATABASE_PRESENT=1")
            print("compressed offhost backup preflight passed")
            return
        reserve_egress(REPO / ".local/offhost-backup/egress-budget.json", value, month, maximum, deploy_id=args.deploy_id)
        metadata = call_remote(args, "backup", payload, budget, folder)
        payload["metadata"] = metadata
        downloaded_manifest = call_remote(args, "manifest", payload, budget, folder)
        if downloaded_manifest != metadata:
            raise ValueError("independently downloaded S3 manifest differs from source receipt")
        archive = call_remote(args, "download", payload, budget, folder, binary=True)
        specification = folder / "verify-spec.json"
        result = folder / "verified.json"
        write_json(specification, {"archive": str(archive), "restored": str(folder / "restored.sqlite3"),
                                   "metadata": metadata, "result": str(result)})
        with (folder / "verify.stdout").open("wb") as output, (folder / "verify.stderr").open("wb") as error:
            try:
                budget.run([sys.executable, str(Path(__file__).resolve()), "--verify-local", str(specification)], stdout=output, stderr=error)
            except BaseException:
                error.flush()
                diagnostic = state / ("failure-" + args.deploy_id + "-restore.stderr")
                shutil.copyfile(error.name, diagnostic)
                os.chmod(diagnostic, 0o600)
                raise
        checks = json.loads(result.read_text())
        call_remote(args, "assert-lineage", payload, budget, folder)
        if file_hash(args.policy) != policy_sha:
            raise ValueError("private cost policy changed during recovery verification")
        validate_policy(value, month)
        if file_hash(Path(args.local_compute_config).expanduser().resolve()) != compute_config_sha:
            raise ValueError("local compute configuration changed during recovery verification")
        write_json(args.receipt, {"verified": True, "deploy_id": args.deploy_id,
                                  "candidate_sha": args.candidate_sha, "metadata": metadata,
                                  "checks": checks, "resources": budget.receipt(),
                                  "policy_sha256": policy_sha,
                                  "local_compute_config_sha256": compute_config_sha,
                                  "verified_at": datetime.now(timezone.utc).isoformat()})
        print("WEATHEREDGE_OFFHOST_BACKUP_VERIFIED=1")
        print("encrypted S3 backup independently restored; checksum, integrity and foreign keys verified")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # AWS/SSH arguments and outputs may contain private operator identifiers.
        # Full diagnostic evidence belongs to the private caller log, not stdout.
        if "--remote-action" in sys.argv:
            import traceback
            traceback.print_exc()  # Captured by the host in owner-only diagnostics.
        else:
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
            print("offhost backup refused: " + reason, file=sys.stderr)
        sys.exit(1)
