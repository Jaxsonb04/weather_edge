from __future__ import annotations

import os
import importlib.util
import json
import hashlib
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

AWS_DIR = Path(__file__).resolve().parents[1] / "deploy" / "aws"
SPEC = importlib.util.spec_from_file_location("web_app_release", AWS_DIR / "web_app_release.py")
release_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_module)


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def test_failed_partial_web_transfer_preserves_complete_active_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "trading" / "deploy" / "aws"
    scripts.mkdir(parents=True)
    shutil.copyfile(AWS_DIR / "deploy_web_app.sh", scripts / "deploy_web_app.sh")
    helper = AWS_DIR / "web_app_release.py"
    if helper.exists():
        shutil.copyfile(helper, scripts / helper.name)
    base = tmp_path / "remote"
    active = base / "webdist"
    active.mkdir(parents=True)
    (active / "index.html").write_text("old index")
    (active / "old.js").write_text("old complete asset")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "bun",
        "#!/bin/sh\nmkdir -p dist\nprintf 'new index' > dist/index.html\nprintf 'new asset' > dist/new.js\n",
    )
    _executable(
        fake_bin / "ssh",
        f"#!{sys.executable}\nimport subprocess, sys\nraise SystemExit(subprocess.call(['bash', '-c', sys.argv[-1]], stdin=sys.stdin))\n",
    )
    _executable(
        fake_bin / "rsync",
        f"""#!{sys.executable}
import pathlib, shutil, sys
if sys.argv[1:] == ['--protect-args', '--version']: raise SystemExit(0)
source = pathlib.Path(sys.argv[-2])
target = pathlib.Path(sys.argv[-1].split(':', 1)[1])
target.mkdir(parents=True, exist_ok=True)
for item in target.iterdir():
    if item.is_dir(): shutil.rmtree(item)
    else: item.unlink()
shutil.copyfile(source / 'index.html', target / 'index.html')
raise SystemExit(23)
""",
    )
    key = tmp_path / "key"
    key.touch()
    target_env = tmp_path / "target.env"
    target_env.write_text(f"EC2_IP=example\nEC2_KEY='{key}'\nREMOTE_BASE='{base}'\n")

    result = subprocess.run(
        ["bash", str(scripts / "deploy_web_app.sh"), str(target_env)],
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 23
    assert {path.name: path.read_text() for path in active.iterdir()} == {
        "index.html": "old index", "old.js": "old complete asset"
    }
    assert "Done" not in result.stdout


def _portable_exchange(left: Path, right: Path) -> None:
    """Transaction harness only; the real Linux zero-gap exchange is tested below."""
    temporary = left.with_name(left.name + ".exchange-test")
    left.rename(temporary)
    right.rename(left)
    temporary.rename(right)


@pytest.fixture
def release(tmp_path, monkeypatch):
    base = tmp_path / "base"
    stage = base / ".web-deploy" / "release1"
    active = base / "webdist"
    candidate = stage / "candidate"
    for root, name in ((active, "old"), (candidate, "new")):
        (root / "assets").mkdir(parents=True)
        (root / "index.html").write_text(f"{name} index")
        (root / "assets" / f"{name}.js").write_text(f"{name} asset")
    (stage / "candidate_hashes.json").write_text(json.dumps(release_module.tree_hashes(candidate)))
    forecaster = base / "forecaster"
    forecaster.mkdir()
    for name in release_module.DYNAMIC_FILES:
        (forecaster / name).write_text(json.dumps({"name": name, "current": True}))
    manifest = {"artifacts": {
        name: {"status": "ready", "sha256": hashlib.sha256((forecaster / name).read_bytes()).hexdigest()}
        for name in release_module.DYNAMIC_FILES - {"publication_manifest.json"}
    }}
    (forecaster / "publication_manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setenv("SFO_PUBLISH_PAGES", "1")
    monkeypatch.setenv("SFO_WEBDIST_DIR", str(active))
    monkeypatch.setenv("SFO_FORECASTER_ROOT", str(forecaster))
    monkeypatch.setenv("SFO_TRADING_ROOT", str(base / "trading"))
    monkeypatch.setenv("SFO_BASE_DIR", str(base))
    monkeypatch.setenv("SFO_PAGES_LOCK", str(base / ".locks/pages-publish.lock"))
    monkeypatch.setenv("SFO_WEB_PUBLIC_BASE_URL", "https://example.test/weather/")
    monkeypatch.setenv("SFO_FORECASTER_GIT_REMOTE", "https://github.com/example/weather.git")
    monkeypatch.setenv("SFO_WEB_DEPLOY_LOCK_WAIT_SECONDS", "2")
    monkeypatch.setenv("SFO_WEB_MAINTENANCE_MARKER", str(tmp_path / "maintenance"))
    monkeypatch.setenv("SFO_WEB_DEADMAN_STATE", str(tmp_path / "deadman"))
    monkeypatch.setattr(release_module, "atomic_exchange", _portable_exchange)
    monkeypatch.setattr(release_module.signal, "signal", lambda *args: None)
    return release_module.WebRelease(base, stage)


def test_changed_or_incomplete_transfer_fails_before_install(release, monkeypatch):
    before = release_module.tree_hashes(release.active)
    (release.candidate / "assets/new.js").write_text("corrupted transfer")
    monkeypatch.setattr(release, "publish", lambda: pytest.fail("publisher ran"))

    with pytest.raises(release_module.ReleaseError, match="transfer hashes"):
        release.run()

    assert release_module.tree_hashes(release.active) == before


def test_unsupported_atomic_exchange_fails_without_changing_active_app(release, monkeypatch):
    before = release_module.tree_hashes(release.active)
    def unsupported(*args):
        raise release_module.ReleaseError("Linux renameat2 is required")
    monkeypatch.setattr(release_module, "atomic_exchange", unsupported)

    with pytest.raises(release_module.ReleaseError, match="renameat2"):
        release.run()

    assert release_module.tree_hashes(release.active) == before


@pytest.mark.parametrize("failure", ["push", "public_hash", "deferred_zero_exit"])
def test_publication_failure_restores_and_republishes_complete_previous_tree(release, monkeypatch, failure):
    before = release_module.tree_hashes(release.active)
    initial_json = release.runtime_hashes()
    publications = []
    verifications = []
    def publish():
        publications.append(release_module.tree_hashes(release.active))
        if failure == "push" and len(publications) == 1:
            raise release_module.ReleaseError("push rejected")
    def verify(expected):
        verifications.append(expected)
        if len(publications) == 1:
            raise release_module.ReleaseError(failure)
        assert expected == before
        return "rollback-commit"
    monkeypatch.setattr(release, "publish", publish)
    monkeypatch.setattr(release, "verify", verify)

    with pytest.raises(release_module.ReleaseError):
        release.run()

    assert publications == [release.expected, before]
    assert release_module.tree_hashes(release.active) == before
    assert release_module.tree_hashes(release.candidate) == release.expected
    assert release.runtime_hashes() == initial_json
    receipt = json.loads((release.stage / "deployment_result.json").read_text())
    assert receipt["status"] == "rolled_back_public_verified"
    assert receipt["rollback_restored"] is True
    assert receipt["rollback_public_commit"] == "rollback-commit"


def test_failed_rollback_publication_preserves_old_tree_and_records_attention(release, monkeypatch):
    before = release_module.tree_hashes(release.active)
    def failed_publish():
        raise release_module.ReleaseError("publisher failed")
    monkeypatch.setattr(release, "publish", failed_publish)

    with pytest.raises(release_module.ReleaseError, match="publisher failed"):
        release.run()

    assert release_module.tree_hashes(release.active) == before
    receipt = json.loads((release.stage / "deployment_result.json").read_text())
    assert receipt["rollback_restored"] is True
    assert receipt["status"] == "rollback_needs_attention"


def test_interruption_during_public_verification_bypasses_retry_and_rolls_back(release, monkeypatch):
    before = release_module.tree_hashes(release.active)
    calls = []
    def interrupted_read(url, deadline):
        calls.append(url)
        raise release_module.ReleaseInterrupted("operator interrupted verification")
    monkeypatch.setattr(release_module, "read_url", interrupted_read)
    publications = []
    monkeypatch.setattr(release, "publish", lambda: publications.append(release_module.tree_hashes(release.active)))
    def verify(expected):
        if expected == release.expected:
            # Exercise the actual verification retry loop, not a mocked failure
            # above it. An interrupt must escape the 420-second retry budget.
            release.verify_public("a" * 40, {})
        assert expected == before
        return "rollback-commit"
    monkeypatch.setattr(release, "verify", verify)
    started = time.monotonic()

    with pytest.raises(release_module.ReleaseInterrupted, match="interrupted"):
        release.run()

    assert time.monotonic() - started < 1.0
    assert len(calls) == 1
    assert publications == [release.expected, before]
    assert release_module.tree_hashes(release.active) == before
    assert release.result["status"] == "rolled_back_public_verified"


def test_active_backend_deployment_blocks_web_swap(release, monkeypatch):
    before = release_module.tree_hashes(release.active)
    marker = Path(os.environ["SFO_WEB_MAINTENANCE_MARKER"])
    marker.touch()

    with pytest.raises(release_module.ReleaseError, match="maintenance"):
        release.run()

    assert release_module.tree_hashes(release.active) == before


def test_direct_publisher_reads_runtime_environment_and_preserves_data(release, monkeypatch):
    monkeypatch.setenv("SFO_PAGES_MAX_GATE_DEFERRALS", "3")
    monkeypatch.setenv("EXPECTED_GATE_POLICY", "3")
    publisher = release.trading / "deploy/aws/publish_forecaster_pages.sh"
    publisher.parent.mkdir(parents=True)
    publisher.write_text(
        '#!/bin/bash\nset -eu\n'
        '[ "$SFO_PUBLISH_PAGES" = 1 ]\n'
        '[ "${SFO_PAGES_MAX_GATE_DEFERRALS:-}" = "${EXPECTED_GATE_POLICY:-}" ]\n'
        '[ -f "$SFO_WEBDIST_DIR/index.html" ]\n'
        'echo "publisher invoked"\n'
    )
    before = release.runtime_hashes()

    release.publish()

    assert release.runtime_hashes() == before
    assert (release.stage / "publication.log").read_text().strip() == "publisher invoked"


def test_manifest_preparation_removes_local_runtime_json_without_touching_static_files(tmp_path):
    app = tmp_path / "dist"
    app.mkdir()
    (app / "index.html").write_text("static app")
    for name in release_module.DYNAMIC_FILES:
        (app / name).write_text("local placeholder must not be published")

    result = subprocess.run(
        [sys.executable, str(AWS_DIR / "web_app_release.py"), "--manifest", str(app), "--strip-runtime-json"],
        capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout)) == {"index.html"}
    assert {path.name for path in app.iterdir()} == {"index.html"}


def test_unstripped_runtime_json_candidate_is_rejected(release):
    (release.candidate / "strategy_research.json").write_text("local placeholder")
    release.expected = release_module.tree_hashes(release.candidate)

    with pytest.raises(release_module.ReleaseError, match="must not contain runtime JSON"):
        release.run()

    assert (release.active / "index.html").read_text() == "old index"


@pytest.mark.parametrize("base_name", ["SFO_BASE_DIR", "BASE_DIR", "trading_parent"])
def test_pages_lock_default_matches_installed_publisher(release, monkeypatch, base_name):
    monkeypatch.delenv("SFO_PAGES_LOCK")
    monkeypatch.delenv("SFO_BASE_DIR", raising=False)
    monkeypatch.delenv("BASE_DIR", raising=False)
    if base_name != "trading_parent":
        monkeypatch.setenv(base_name, str(release.base))
    configured = release_module.WebRelease(release.base, release.stage)

    assert configured.pages_lock == release.base / ".locks/pages-publish.lock"
    configured.check_host()


def test_mismatched_installed_publisher_base_fails_closed(release, monkeypatch):
    monkeypatch.setenv("SFO_BASE_DIR", "/another/publisher/base")
    configured = release_module.WebRelease(release.base, release.stage)

    with pytest.raises(release_module.ReleaseError, match="disagree"):
        configured.check_host()


@pytest.mark.parametrize("wrong_app", [False, True])
def test_branch_snapshot_checks_installed_app_and_preserves_published_json_hashes(
    release, monkeypatch, wrong_app
):
    commit = "a" * 40
    files = {
        path.relative_to(release.candidate).as_posix(): path.read_bytes()
        for path in release.candidate.rglob("*") if path.is_file()
    }
    for name in release_module.DYNAMIC_FILES:
        files[name] = (release.forecaster / name).read_bytes()
    if wrong_app:
        files["index.html"] = b"stale index"
    def git(command, **kwargs):
        args = command[3:]
        if args[0] in {"init", "fetch"}:
            return b""
        if args[0] == "rev-parse":
            return commit.encode()
        if args[0] == "ls-tree":
            return "\n".join(files).encode()
        if args[0] == "show":
            return files[args[1].split(":", 1)[1]]
        pytest.fail(f"unexpected git call: {args}")
    monkeypatch.setattr(release_module.subprocess, "check_output", git)

    if wrong_app:
        with pytest.raises(release_module.ReleaseError, match="installed app hashes"):
            release.branch_snapshot(release.expected)
    else:
        actual_commit, hashes = release.branch_snapshot(release.expected)
        assert actual_commit == commit
        assert hashes == {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}


@pytest.mark.parametrize("problem", ["strategy_withheld", "corrupt_json", "missing_manifest_entry"])
def test_branch_snapshot_rejects_runtime_json_not_validated_by_manifest(release, monkeypatch, problem):
    files = {
        path.relative_to(release.candidate).as_posix(): path.read_bytes()
        for path in release.candidate.rglob("*") if path.is_file()
    }
    for name in release_module.DYNAMIC_FILES:
        files[name] = (release.forecaster / name).read_bytes()
    if problem == "strategy_withheld":
        del files["strategy_research.json"]
    elif problem == "corrupt_json":
        files["strategy_research.json"] = b"wrong unvalidated research data"
    else:
        manifest = json.loads(files["publication_manifest.json"])
        del manifest["artifacts"]["cities_data.json"]
        files["publication_manifest.json"] = json.dumps(manifest).encode()
    ssh_commands = []
    monkeypatch.delenv("SFO_PAGES_DEPLOY_KEY", raising=False)
    def git(command, **kwargs):
        ssh_commands.append(kwargs["env"]["GIT_SSH_COMMAND"])
        args = command[3:]
        if args[0] in {"init", "fetch"}: return b""
        if args[0] == "rev-parse": return b"a" * 40
        if args[0] == "ls-tree": return "\n".join(files).encode()
        if args[0] == "show": return files[args[1].split(":", 1)[1]]
        pytest.fail(f"unexpected git call: {args}")
    monkeypatch.setattr(release_module.subprocess, "check_output", git)

    with pytest.raises(release_module.ReleaseError, match="runtime JSON"):
        release.branch_snapshot(release.expected)

    assert all(str(Path.home() / ".ssh/sfo_weather_pages_deploy") in command for command in ssh_commands)


@pytest.mark.parametrize("configuration", ["valid", "different_env", "multiple_env", "direct_override"])
def test_remote_wrapper_uses_native_environmentfile_without_shell_expansion(tmp_path, configuration):
    script = (AWS_DIR / "deploy_web_app.sh").read_text().split("<<'REMOTE'\n", 1)[1].split("\nREMOTE\n", 1)[0]
    base = tmp_path / "base"
    stage = base / ".web-deploy/release"
    stage.mkdir(parents=True)
    workdir = base / "forecaster"
    workdir.mkdir()
    sentinel = tmp_path / "must-not-exist"
    env_file = tmp_path / "publisher.env"
    env_file.write_text(f"LITERAL=$(touch '{sentinel}')\nOTHER=`touch '{sentinel}'`\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "native-loader.json"
    loaded_files = str(env_file) + " (ignore_errors=no)"
    if configuration == "different_env":
        loaded_files = "/other/publisher.env (ignore_errors=no)"
    elif configuration == "multiple_env":
        loaded_files += " /other/publisher.env (ignore_errors=no)"
    _executable(fake_bin / "chown", "#!/bin/sh\nexit 0\n")
    _executable(fake_bin / "getent", "#!/bin/sh\nprintf 'app:x:1:1:app:/home/app:/bin/bash\\n'\n")
    _executable(
        fake_bin / "systemctl",
        f"""#!{sys.executable}
import sys
values = {{'EnvironmentFiles': {loaded_files!r},
          'Environment': {'EXTRA=override' if configuration == 'direct_override' else ''!r},
          'User': 'app', 'WorkingDirectory': {str(workdir)!r}}}
print(values.get(sys.argv[3], ''))
""",
    )
    _executable(
        fake_bin / "systemd-run",
        f"#!{sys.executable}\nimport json, sys\nopen({str(calls)!r}, 'w').write(json.dumps(sys.argv[1:]))\n",
    )

    result = subprocess.run(
        ["bash", "-s", "--", str(base), str(stage), str(env_file)], input=script,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}, capture_output=True, text=True,
    )

    assert not sentinel.exists()
    if configuration != "valid":
        assert result.returncode != 0
        assert not calls.exists()
        return
    assert result.returncode == 0, result.stderr
    native_args = json.loads(calls.read_text())
    assert {"--wait", "--pipe", "--collect", "--uid=app", "--setenv=HOME=/home/app"}.issubset(native_args)
    assert f"--property=EnvironmentFile={env_file}" in native_args
    assert f"--working-directory={workdir}" in native_args


def test_publisher_copy_lock_keeps_old_tree_whole_until_reader_finishes(release, monkeypatch):
    before = release_module.tree_hashes(release.active)
    ready = threading.Event()
    original_probe = release.capability_probe
    def probe():
        original_probe()
        ready.set()
    monkeypatch.setattr(release, "capability_probe", probe)
    monkeypatch.setattr(release, "publish", lambda: None)
    monkeypatch.setattr(release, "verify", lambda expected: "new-commit")
    errors = []
    def run():
        try:
            release.run()
        except BaseException as exc:
            errors.append(exc)
    with release_module.file_lock(release.pages_lock, 1):
        worker = threading.Thread(target=run)
        worker.start()
        assert ready.wait(1)
        time.sleep(0.1)
        assert release_module.tree_hashes(release.active) == before
    worker.join(2)
    assert not worker.is_alive()
    assert errors == []
    assert release_module.tree_hashes(release.active) == release.expected


def test_two_web_installs_are_serialized_through_public_verification(release, monkeypatch):
    stage2 = release.base / ".web-deploy/release2"
    shutil.copytree(release.candidate, stage2 / "candidate")
    (stage2 / "candidate/index.html").write_text("second app")
    (stage2 / "candidate_hashes.json").write_text(json.dumps(release_module.tree_hashes(stage2 / "candidate")))
    second = release_module.WebRelease(release.base, stage2)
    entered, finish = threading.Event(), threading.Event()
    def first_publish():
        entered.set()
        assert finish.wait(2)
    monkeypatch.setattr(release, "publish", first_publish)
    monkeypatch.setattr(release, "verify", lambda expected: "first-commit")
    monkeypatch.setattr(second, "publish", lambda: None)
    monkeypatch.setattr(second, "verify", lambda expected: "second-commit")
    errors = []
    def run(item):
        try:
            item.run()
        except BaseException as exc:
            errors.append(exc)
    first_thread = threading.Thread(target=run, args=(release,))
    second_thread = threading.Thread(target=run, args=(second,))
    first_thread.start()
    assert entered.wait(1)
    second_thread.start()
    time.sleep(0.1)
    assert release_module.tree_hashes(release.active) == release.expected
    finish.set()
    first_thread.join(2)
    second_thread.join(2)
    assert errors == []
    assert not first_thread.is_alive() and not second_thread.is_alive()
    assert release_module.tree_hashes(second.active) == second.expected


@pytest.mark.skipif(sys.platform != "linux", reason="production atomic exchange requires Linux")
def test_linux_atomic_exchange_has_no_missing_active_directory(tmp_path, monkeypatch):
    left, right = tmp_path / "active", tmp_path / "candidate"
    left.mkdir(); right.mkdir()
    (left / "index.html").write_text("old")
    (right / "index.html").write_text("new")
    done = threading.Event()
    errors = []
    def read():
        while not done.is_set():
            try:
                assert (left / "index.html").read_text() in {"old", "new"}
            except BaseException as exc:
                errors.append(exc)
    reader = threading.Thread(target=read)
    reader.start()
    try:
        for _ in range(1000):
            release_module.atomic_exchange(left, right)
    finally:
        done.set()
        reader.join(2)
    assert errors == []


def test_exact_commit_pages_success_and_all_public_hashes_are_required(release, monkeypatch):
    commit = "a" * 40
    files = {"index.html": b"new index", "assets/new.js": b"new asset", "cities_data.json": b"current data"}
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    calls = []
    def fetch(url, deadline):
        calls.append(url)
        if "/deployments?" in url:
            return json.dumps([{"sha": commit, "environment": "github-pages", "statuses_url": "https://api.github.com/statuses"}]).encode()
        if url == "https://api.github.com/statuses":
            return b'[{"state":"success"}]'
        path = url.split("/weather/", 1)[1].split("?", 1)[0]
        return files[path]
    monkeypatch.setattr(release_module, "read_url", fetch)

    release.verify_public(commit, hashes)

    assert any("sha=" + commit in url for url in calls)
    assert sum("web_release=" in url for url in calls) == len(files)


@pytest.mark.parametrize("problem", ["wrong_commit", "failed_pages", "wrong_index", "missing_asset", "wrong_json"])
def test_public_verification_rejects_old_or_incomplete_publication(release, monkeypatch, problem):
    release.verify_timeout, release.poll = 0.01, 0.001
    commit = "a" * 40
    files = {"index.html": b"new index", "assets/new.js": b"new asset", "cities_data.json": b"current data"}
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    def fetch(url, deadline):
        if "/deployments?" in url:
            return json.dumps([{"sha": "old" if problem == "wrong_commit" else commit,
                                "environment": "github-pages", "statuses_url": "https://api.github.com/statuses"}]).encode()
        if url == "https://api.github.com/statuses":
            return json.dumps([{"state": "failure" if problem == "failed_pages" else "success"}]).encode()
        path = url.split("/weather/", 1)[1].split("?", 1)[0]
        if path == "assets/new.js" and problem == "missing_asset":
            raise OSError("new asset unavailable")
        if (path == "index.html" and problem == "wrong_index") or (path == "cities_data.json" and problem == "wrong_json"):
            return b"old public data"
        return files[path]
    monkeypatch.setattr(release_module, "read_url", fetch)

    with pytest.raises(release_module.ReleaseError, match="public verification failed"):
        release.verify_public(commit, hashes)
