#!/usr/bin/env python3
"""Validate and atomically publish a staged SPA without running data builders.

The operator uploads this helper beside, never inside, the candidate app. It
runs as the installed publisher's user with its EnvironmentFile already loaded.
Release evidence and both trees stay in the private staging directory.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path


DYNAMIC_FILES = {
    "trading_signal.json", "forecast_data.json", "weather_story_data.json",
    "cities_data.json", "strategy_research.json", "publication_manifest.json",
}


class ReleaseError(RuntimeError):
    pass


class ReleaseInterrupted(RuntimeError):
    """An operator/process interruption must bypass public-verification retries."""


def tree_hashes(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise ReleaseError("app tree must be a real directory")
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ReleaseError("app tree contains a symlink or special file")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if "index.html" not in result:
        raise ReleaseError("app tree has no index.html")
    return result


def atomic_exchange(left: Path, right: Path) -> None:
    """Linux-only exchange: the active path never disappears, even briefly."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise ReleaseError("Linux renameat2 is required; active app was not changed")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(left), -100, os.fsencode(right), 2) != 0:
        code = ctypes.get_errno()
        raise ReleaseError(f"atomic directory exchange failed: {os.strerror(code)}")


@contextlib.contextmanager
def file_lock(path: Path, wait: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise ReleaseError("timed out waiting for deployment/publication lock") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def read_url(url: str, deadline: float) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ReleaseError("public verification deadline expired")
    request = urllib.request.Request(url, headers={
        "User-Agent": "WeatherEdge-static-release", "Cache-Control": "no-cache",
        "Accept": "application/vnd.github+json" if "api.github.com/" in url else "*/*",
    })
    with urllib.request.urlopen(request, timeout=min(10.0, remaining)) as response:
        return response.read()


def repository_slug(remote: str) -> str:
    match = re.fullmatch(r"(?:git@github\.com:|https://github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?", remote)
    if not match:
        raise ReleaseError("publication remote must identify a GitHub repository")
    return match.group(1)


class WebRelease:
    def __init__(self, base: Path, stage: Path):
        self.base, self.stage = base, stage
        self.active, self.candidate = base / "webdist", stage / "candidate"
        self.trading = Path(os.environ.get("SFO_TRADING_ROOT") or "/opt/weatheredge/trading")
        self.publisher_base = Path(os.environ.get("SFO_BASE_DIR") or os.environ.get("BASE_DIR") or self.trading.parent)
        self.pages_lock = Path(os.environ.get("SFO_PAGES_LOCK") or str(self.publisher_base / ".locks/pages-publish.lock"))
        self.deploy_lock = base / ".locks/web-app-deploy.lock"
        self.lock_wait = float(os.environ.get("SFO_WEB_DEPLOY_LOCK_WAIT_SECONDS", "900"))
        self.verify_timeout = float(os.environ.get("SFO_WEB_PUBLIC_VERIFY_SECONDS", "420"))
        self.poll = float(os.environ.get("SFO_WEB_PUBLIC_POLL_SECONDS", "15"))
        self.forecaster = Path(os.environ.get("SFO_FORECASTER_ROOT") or "/opt/weatheredge/forecaster")
        self.remote = os.environ.get("SFO_FORECASTER_GIT_REMOTE", "git@github.com:Jaxsonb04/weather_edge.git")
        self.branch = os.environ.get("SFO_PAGES_BRANCH", "gh-pages")
        manifest_url = os.environ.get("SFO_PUBLICATION_MANIFEST_URL", os.environ.get("SFO_PUBLIC_MANIFEST_URL", ""))
        self.public_url = os.environ.get("SFO_WEB_PUBLIC_BASE_URL", manifest_url.rsplit("/", 1)[0] + "/" if manifest_url else "")
        self.result: dict = {"status": "not_installed", "rollback_restored": False}
        self.expected = json.loads((stage / "candidate_hashes.json").read_text())

    def check_host(self) -> None:
        if os.environ.get("SFO_PUBLISH_PAGES") != "1":
            raise ReleaseError("Pages publication must be enabled for a verified web release")
        configured = Path(os.environ.get("SFO_WEBDIST_DIR") or "/opt/weatheredge/webdist")
        if configured != self.active or self.publisher_base != self.base or self.stage.parent != self.base / ".web-deploy":
            raise ReleaseError("release paths disagree with the installed publisher")
        if not self.public_url.startswith(("https://", "http://")):
            raise ReleaseError("a public website base URL is required")
        repository_slug(self.remote)
        if self.lock_wait <= 0 or self.verify_timeout <= 0 or self.poll <= 0:
            raise ReleaseError("deployment waits must be positive")
        if Path(os.environ.get("SFO_WEB_MAINTENANCE_MARKER", "/run/weatheredge-deploy-maintenance")).exists():
            raise ReleaseError("backend deployment maintenance is active")
        if Path(os.environ.get("SFO_WEB_DEADMAN_STATE", "/var/lib/weatheredge/deploy-deadman/state")).exists():
            raise ReleaseError("backend deployment recovery state is active")
        tree_hashes(self.active)
        if tree_hashes(self.candidate) != self.expected:
            raise ReleaseError("candidate transfer hashes or file set differ from the local build")
        if DYNAMIC_FILES & self.expected.keys():
            raise ReleaseError("candidate must not contain runtime JSON artifacts")
        if self.active.stat().st_dev != self.candidate.stat().st_dev:
            raise ReleaseError("candidate and active app must share one filesystem")

    def runtime_hashes(self) -> dict[str, str]:
        result = {}
        for name in sorted(DYNAMIC_FILES - {"publication_manifest.json"}):
            try:
                result[name] = hashlib.sha256((self.forecaster / name).read_bytes()).hexdigest()
            except FileNotFoundError:
                continue
        return result

    def capability_probe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="exchange-probe-", dir=self.stage) as temp:
            left, right = Path(temp) / "left", Path(temp) / "right"
            left.mkdir()
            right.mkdir()
            (left / "marker").write_text("left")
            (right / "marker").write_text("right")
            atomic_exchange(left, right)
            if (left / "marker").read_text() != "right" or (right / "marker").read_text() != "left":
                raise ReleaseError("atomic directory exchange capability probe failed")

    def publish(self) -> None:
        env = dict(os.environ)
        # A direct publisher reads and validates current artifacts; it invokes no
        # forecast, strategy, trading-signal, database, or calibration builder.
        with (self.stage / "publication.log").open("a") as log:
            process = subprocess.Popen(
                ["/bin/bash", str(self.trading / "deploy/aws/publish_forecaster_pages.sh")],
                env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            try:
                status = process.wait(timeout=float(os.environ.get("SFO_WEB_PUBLISH_TIMEOUT_SECONDS", "900")))
            except BaseException:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
        if status:
            raise ReleaseError(f"direct publisher failed with status {status}")

    def branch_snapshot(self, expected: dict[str, str]) -> tuple[str, dict[str, str]]:
        with tempfile.TemporaryDirectory(prefix="branch-check-", dir=self.stage) as temp:
            env = dict(os.environ)
            key = env.get("SFO_PAGES_DEPLOY_KEY") or str(Path.home() / ".ssh/sfo_weather_pages_deploy")
            env["GIT_SSH_COMMAND"] = f"ssh -i {shlex.quote(key)} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
            def git(*args):
                return subprocess.check_output(["git", "-C", temp, *args], env=env, stderr=subprocess.DEVNULL, timeout=60)
            git("init", "-q")
            git("fetch", "--depth=1", self.remote, self.branch)
            commit = git("rev-parse", "FETCH_HEAD").decode().strip()
            files = git("ls-tree", "-r", "--name-only", "FETCH_HEAD").decode().splitlines()
            contents = {name: git("show", f"FETCH_HEAD:{name}") for name in files}
            hashes = {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}
            static = {name: value for name, value in expected.items() if name not in DYNAMIC_FILES}
            if any(hashes.get(name) != value for name, value in static.items()):
                raise ReleaseError("published branch does not contain the installed app hashes")
            if not DYNAMIC_FILES.issubset(hashes):
                raise ReleaseError("published branch omits required runtime JSONs")
            manifest = json.loads(contents["publication_manifest.json"])
            if not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), dict):
                raise ReleaseError("published manifest has no validated runtime JSON artifact map")
            artifacts = manifest.get("artifacts", {})
            for name in DYNAMIC_FILES - {"publication_manifest.json"}:
                entry = artifacts.get(name, {})
                if not isinstance(entry, dict) or entry.get("status") not in {"ready", "preserved"} or entry.get("sha256") != hashes[name]:
                    raise ReleaseError(f"published runtime JSON disagrees with its validated manifest: {name}")
            return commit, {name: hashes[name] for name in static.keys() | (DYNAMIC_FILES & hashes.keys())}

    def verify_public(self, commit: str, hashes: dict[str, str]) -> None:
        deadline = time.monotonic() + self.verify_timeout
        slug = repository_slug(self.remote)
        query = urllib.parse.urlencode({"sha": commit, "environment": "github-pages", "per_page": 10})
        last_error = "no successful Pages deployment"
        while time.monotonic() < deadline:
            try:
                deployments = json.loads(read_url(f"https://api.github.com/repos/{slug}/deployments?{query}", deadline))
                ready = False
                for deployment in deployments:
                    if deployment.get("sha") != commit or deployment.get("environment") != "github-pages":
                        continue
                    statuses = json.loads(read_url(deployment["statuses_url"], deadline))
                    if statuses and statuses[0].get("state") == "success":
                        ready = True
                        break
                if not ready:
                    raise ReleaseError("exact branch commit has no successful Pages deployment")
                for name, digest in sorted(hashes.items()):
                    path = urllib.parse.quote(name, safe="/")
                    url = urllib.parse.urljoin(self.public_url, path) + f"?web_release={commit}"
                    if hashlib.sha256(read_url(url, deadline)).hexdigest() != digest:
                        raise ReleaseError(f"public file hash does not match: {name}")
                return
            except (OSError, ValueError, KeyError, ReleaseError) as exc:
                last_error = str(exc)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(self.poll, remaining))
        raise ReleaseError(f"public verification failed: {last_error}")

    def verify(self, expected: dict[str, str]) -> str:
        commit, hashes = self.branch_snapshot(expected)
        self.verify_public(commit, hashes)
        return commit

    def save_result(self) -> None:
        target = self.stage / "deployment_result.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.result, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(target)

    def run(self) -> None:
        with file_lock(self.deploy_lock, self.lock_wait):
            self.check_host()
            self.capability_probe()
            previous = tree_hashes(self.active)
            self.result["runtime_json_before"] = self.runtime_hashes()
            installed = False
            try:
                with file_lock(self.pages_lock, self.lock_wait):
                    self.check_host()
                    signals = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}
                    old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
                    try:
                        atomic_exchange(self.active, self.candidate)
                        installed = True
                    finally:
                        signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
                    self.result["status"] = "installed_awaiting_publication"
                    self.save_result()
                self.publish()
                with file_lock(self.pages_lock, self.lock_wait):
                    self.result["published_commit"] = self.verify(self.expected)
                self.result["status"] = "public_verified"
            except BaseException as exc:
                self.result["error"] = str(exc)
                if installed:
                    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                        signal.signal(sig, signal.SIG_IGN)
                    try:
                        with file_lock(self.pages_lock, self.lock_wait):
                            atomic_exchange(self.active, self.candidate)
                            self.result["rollback_restored"] = True
                        self.publish()
                        with file_lock(self.pages_lock, self.lock_wait):
                            self.result["rollback_public_commit"] = self.verify(previous)
                        self.result["status"] = "rolled_back_public_verified"
                    except BaseException as rollback_error:
                        self.result["status"] = "rollback_needs_attention"
                        self.result["rollback_error"] = str(rollback_error)
                raise
            finally:
                self.result["runtime_json_after"] = self.runtime_hashes()
                self.save_result()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--strip-runtime-json", action="store_true")
    parser.add_argument("--base", type=Path)
    parser.add_argument("--stage", type=Path)
    args = parser.parse_args()
    if args.manifest:
        if args.strip_runtime_json:
            for name in DYNAMIC_FILES:
                (args.manifest / name).unlink(missing_ok=True)
        print(json.dumps(tree_hashes(args.manifest), sort_keys=True))
        return 0
    if not args.base or not args.stage:
        parser.error("--base and --stage are required for deployment")
    def interrupted(signum, frame):
        raise ReleaseInterrupted(f"web release interrupted by signal {signum}")
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    try:
        release = WebRelease(args.base, args.stage)
        release.run()
    except (OSError, ValueError, ReleaseError, ReleaseInterrupted, subprocess.SubprocessError) as exc:
        print(f"web release failed: {exc}", file=sys.stderr)
        return 1
    print(f"Public app and runtime JSON hashes verified at Pages commit {release.result['published_commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
