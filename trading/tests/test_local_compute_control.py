"""Owner pause survives restarts and stops owned work without provider calls."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest


def load(name):
    path = Path(__file__).resolve().parents[2] / 'scripts/local_compute' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def controlled(tmp_path):
    module = load('control')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'state_dir': str(tmp_path / 'state'),
                                 'limits': {'wall_seconds': 300, 'cpu_seconds': 120}}))
    config.chmod(0o600)
    service = {'loaded': True, 'disabled': False, 'calls': []}

    def launch(*args):
        service['calls'].append(args)
        code = 0
        output = ''
        if args[0] == 'print-disabled':
            output = f'"{module.LABEL}" => {str(service["disabled"]).lower()}'
        elif args[0] == 'print':
            code = 0 if service['loaded'] else 113
        elif args[0] == 'disable':
            service['disabled'] = True
        elif args[0] == 'enable':
            service['disabled'] = False
        elif args[0] == 'bootstrap':
            service['loaded'] = True
        elif args[0] == 'bootout':
            service['loaded'] = False
        return subprocess.CompletedProcess(args, code, output, '')

    with patch.object(module.Path, 'home', return_value=tmp_path), patch.object(module, 'launchctl', side_effect=launch):
        control = module.Control(config)
        control.plist.parent.mkdir(parents=True)
        control.plist.write_bytes(plistlib.dumps(module.launch_document(config, control.state, sys.executable)))
        yield module, control, service


def test_off_is_persistent_idempotent_and_on_does_not_start_work(controlled):
    module, control, service = controlled
    assert control.status()['enabled']
    for _ in range(2):
        result = control.off()
        assert result['paused'] and not result['running'] and not result['loaded']
        assert control.marker.exists() and service['disabled']
    # Reconstructing the controller cannot silently reset the saved preference.
    assert module.Control(control.config).status()['paused']
    result = control.on()
    assert result['enabled'] and not result['running']
    assert not control.marker.exists() and not service['disabled']
    assert all(call[0] != 'kickstart' for call in service['calls'])
    service['calls'].clear()
    control.on()
    assert all(call[0] not in ('enable', 'bootstrap', 'bootout') for call in service['calls'])


def test_off_marks_pause_before_waiting_and_does_not_unload_a_busy_supervisor(controlled):
    _, control, service = controlled
    with (control.state / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match='still stopping'):
            control.off(wait_seconds=0)
    assert control.marker.exists() and service['disabled']
    assert not any(call[0] == 'bootout' for call in service['calls'])
    assert not control.off()['running']


def test_on_rejects_changed_schedule_without_enabling_it(controlled):
    _, control, service = controlled
    control.off()
    document = plistlib.loads(control.plist.read_bytes())
    document['RunAtLoad'] = True
    control.plist.write_bytes(plistlib.dumps(document))
    service['calls'].clear()
    with pytest.raises(ValueError, match='installed schedule differs'):
        control.on()
    assert control.marker.exists()
    assert not service['calls']


def test_failed_resume_keeps_owner_pause(controlled):
    module, control, service = controlled
    control.off()
    original = module.launchctl

    def fail_bootstrap(*args):
        if args[0] == 'bootstrap':
            return subprocess.CompletedProcess(args, 113, '', '')
        return original(*args)

    with patch.object(module, 'launchctl', side_effect=fail_bootstrap), pytest.raises(RuntimeError, match='could not load'):
        control.on()
    assert control.marker.exists() and service['disabled'] and not service['loaded']


def test_failed_child_cleanup_is_never_claimed_as_stopped_or_resumed(controlled):
    _, control, service = controlled
    (control.state / 'status.json').write_text(json.dumps({'status': 'failed', 'error_type': 'GroupCleanupError'}))
    with pytest.raises(RuntimeError, match='child cleanup failed'):
        control.off()
    assert control.marker.exists() and service['disabled'] and not service['loaded']
    assert control.status()['cleanup_unverified']
    with pytest.raises(RuntimeError, match='Child cleanup is unverified'):
        control.on()


def test_schedule_installer_preserves_owner_pause(controlled):
    module, control, service = controlled
    control.off()
    installer = load('install_launch_agent')
    with patch.object(installer.Path, 'home', return_value=control.plist.parents[2]), \
         patch.object(installer.sys, 'argv', ['install', '--config', str(control.config)]), \
         patch.object(installer.subprocess, 'run') as calls:
        installer.main()
    actions = [call.args[0][1] for call in calls.call_args_list]
    assert actions == ['bootout', 'disable']
    assert not plistlib.loads(control.plist.read_bytes())['RunAtLoad']


def test_paused_schedule_defers_before_host_or_child_work(tmp_path):
    worker = load('worker')
    (tmp_path / 'background-paused').touch()
    config = {'state_dir': str(tmp_path), 'limits': {'wall_seconds': 300, 'cpu_seconds': 120}}
    with patch.object(worker, 'host_snapshot') as native, patch.object(worker, 'offline_inputs') as inputs:
        assert worker.run(config, scheduled=True) == 0
    native.assert_not_called()
    inputs.assert_not_called()
    receipt = json.loads((tmp_path / 'status.json').read_text())
    assert receipt['status'] == 'deferred' and 'turned off by owner' in receipt['reason']


def test_cancellation_marker_stops_actual_owned_child(tmp_path):
    guard = load('guards')
    marker = tmp_path / 'background-paused'
    pid_file = tmp_path / 'child-pid'
    failures = []

    def pause_when_started():
        deadline = time.monotonic() + 4
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        if not pid_file.exists():
            failures.append('child never started')
        marker.touch()

    thread = threading.Thread(target=pause_when_started)
    thread.start()
    try:
        with patch.object(guard, 'host_snapshot', return_value={}), pytest.raises(guard.Deferred, match='turned off by owner'):
            guard.Budget(tmp_path, guard.policy({'limits': {'wall_seconds': 5, 'cpu_seconds': 2}}),
                         cancellation_file=marker).run(
                [sys.executable, '-c', 'import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(30)', str(pid_file)],
                stdout=subprocess.DEVNULL)
        thread.join(timeout=5)
        assert not thread.is_alive() and not failures
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
    finally:
        thread.join(timeout=5)


def test_unknown_launchctl_errors_are_not_reported_as_off():
    module = load('control')
    with patch.object(module.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')):
        with pytest.raises(RuntimeError, match='macOS could not'):
            module.launchctl('print', 'fixture')


def test_unloaded_bootout_is_idempotent_but_other_errors_are_rejected():
    module = load('control')
    absent = subprocess.CompletedProcess([], 3, '', 'No such process')
    with patch.object(module.subprocess, 'run', return_value=absent):
        assert module.launchctl('bootout', 'fixture').returncode == 3
        with pytest.raises(RuntimeError):
            module.launchctl('disable', 'fixture')


def test_unplug_during_job_cleans_up_actual_child(tmp_path):
    guard = load('guards')
    original = guard.subprocess.Popen
    children = []

    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    # Advance the supervisor clock to the next periodic native power check;
    # retain a real separately-sessioned child to verify cancellation cleanup.
    with patch.object(guard, 'host_snapshot', side_effect=[{}, guard.Deferred('AC power is required')]), \
         patch.object(guard.subprocess, 'Popen', side_effect=spawn), \
         patch.object(guard, 'process_group_sample', return_value=[]), \
         patch.object(guard.time, 'monotonic', side_effect=[0, 0, 0, 11]), \
         pytest.raises(guard.Deferred, match='AC power is required'):
        guard.Budget(tmp_path, guard.policy({})).run(
            [sys.executable, '-c', 'import time;time.sleep(30)'], stdout=subprocess.DEVNULL)
    assert len(children) == 1 and children[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(children[0].pid, 0)
