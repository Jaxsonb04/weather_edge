"""Fail-closed power/resource/egress guards and preserved offline research evidence."""
import importlib.util
import fcntl
import io
import errno
import json
import os
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[2] / 'scripts/local_compute' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExportTests(unittest.TestCase):
    def fixtures(self, folder):
        paper, weather = [str(Path(folder) / x) for x in ('paper.db', 'weather.db')]
        for path, tables in [(paper, ['paper_accounts', 'paper_orders']),
                             (weather, ['nwp_model_forecasts', 'cli_settlements', 'forecast_emos_daily_high'])]:
            with sqlite3.connect(path) as c:
                for name in tables:
                    c.execute(f'CREATE TABLE {name} (target_date TEXT, local_date TEXT, value INTEGER)')
                    c.execute(f"INSERT INTO {name} VALUES ('2026-10-05', '2026-10-05', 1)")
            c.close()
        return paper, weather

    def test_complete_snapshot_no_source_writes_and_missing_prospective_tables(self):
        exporter = load('export_evidence')
        with tempfile.TemporaryDirectory() as folder:
            paper, weather = self.fixtures(folder)
            before = [Path(path).read_bytes() for path in (paper, weather)]
            result = exporter.collect(paper, weather)
            self.assertEqual(len(result['paper']['paper']['paper_orders']['rows']), 1)
            self.assertFalse(result['weather']['cli_settlements']['truncated'])
            self.assertFalse(result['weather']['forecast_emos_live_vintages']['available'])
            self.assertEqual(before, [Path(path).read_bytes() for path in (paper, weather)])
            with patch.object(exporter, 'LIMIT', 0):
                with self.assertRaisesRegex(ValueError, 'bounded export limit'):
                    exporter.collect(paper, weather)

    def test_actual_v7_member_schema_keeps_original_null_initialization_and_clock(self):
        exporter = load('export_evidence')
        with tempfile.TemporaryDirectory() as folder:
            paper, weather = self.fixtures(folder)
            with sqlite3.connect(weather) as c:
                c.execute('CREATE TABLE forecast_emos_live_vintages (snapshot_id TEXT PRIMARY KEY, target_date TEXT, station_id TEXT, fetched_at TEXT)')
                c.execute('CREATE TABLE nwp_live_forecast_members (snapshot_id TEXT, model TEXT, predicted_high_f REAL, used_in_fit INTEGER, complete_hour_count INTEGER, aggregation_basis TEXT)')
                c.execute("INSERT INTO forecast_emos_live_vintages VALUES ('s1','2026-10-06','KSFO','2026-10-05T15:00:00Z')")
                c.execute("INSERT INTO nwp_live_forecast_members VALUES ('s1','gfs',70,1,NULL,NULL)")
            c.close()
            before = Path(weather).read_bytes()
            report = exporter.collect(paper, weather)['weather']
            self.assertTrue(report['nwp_live_forecast_members']['available'])
            row = report['nwp_live_forecast_members']['rows'][0]
            self.assertIsNone(row['complete_hour_count'])
            self.assertIsNone(row['aggregation_basis'])
            self.assertNotIn('target_date', row)
            self.assertEqual(report['forecast_emos_live_vintages']['rows'][0]['fetched_at'], '2026-10-05T15:00:00Z')
            self.assertEqual(Path(weather).read_bytes(), before)


class GuardTests(unittest.TestCase):
    def test_pacing_permission_race_reaps_exited_child_and_cleans_group(self):
        guard = load('guards')
        process = Mock(pid=999, returncode=0)
        polls = iter([None])
        process.poll.side_effect = lambda: next(polls, 0)
        missing = ProcessLookupError(errno.ESRCH, 'No such process')
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(guard, 'host_snapshot', return_value={}), \
             patch.object(guard.subprocess, 'Popen', return_value=process), \
             patch.object(guard, 'process_group_sample', return_value=[(999, 1, .8)]), \
             patch.object(guard.os, 'killpg', side_effect=[PermissionError(errno.EPERM, 'Operation not permitted'), missing, missing]) as kill:
            guard.Budget(folder, guard.policy({'limits': {'cpu_seconds': 2}})).run(['unused'], stdout=subprocess.DEVNULL)
        self.assertEqual([call.args[1] for call in kill.call_args_list], [signal.SIGSTOP, signal.SIGKILL, signal.SIGKILL])
        process.wait.assert_called_once_with(timeout=5)

    def test_cleanup_permission_race_reaps_child_then_kills_remaining_descendants(self):
        guard = load('guards')
        process = Mock(pid=999, returncode=0)
        process.poll.return_value = 0
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(guard, 'host_snapshot', return_value={}), \
             patch.object(guard.subprocess, 'Popen', return_value=process), \
             patch.object(guard.os, 'killpg', side_effect=[PermissionError(errno.EPERM, 'Operation not permitted'), None]) as kill:
            guard.Budget(folder, guard.policy({})).run(['unused'], stdout=subprocess.DEVNULL)
        self.assertEqual([call.args[1] for call in kill.call_args_list], [signal.SIGKILL, signal.SIGKILL])
        self.assertGreaterEqual(process.poll.call_count, 2)
        process.wait.assert_called_once_with(timeout=5)

    def test_permission_error_for_live_child_is_not_suppressed_and_cleanup_preserves_primary(self):
        guard = load('guards')
        for cleanup_fails in (False, True):
            with self.subTest(cleanup_fails=cleanup_fails), tempfile.TemporaryDirectory() as folder:
                process = Mock(pid=999, returncode=None)
                process.poll.return_value = None
                primary = PermissionError(errno.EPERM, 'pacing denied for live child')
                cleanup = PermissionError(errno.EPERM, 'cleanup denied for live child')
                with patch.object(guard, 'host_snapshot', return_value={}), \
                     patch.object(guard.subprocess, 'Popen', return_value=process), \
                     patch.object(guard, 'process_group_sample', return_value=[(999, 1, .8)]), \
                     patch.object(guard.os, 'killpg', side_effect=[primary, cleanup if cleanup_fails else None]), \
                     self.assertRaises(RuntimeError if cleanup_fails else PermissionError) as raised:
                    guard.Budget(folder, guard.policy({'limits': {'cpu_seconds': 2}})).run(['unused'], stdout=subprocess.DEVNULL)
                process.wait.assert_called_once_with(timeout=5)
                if cleanup_fails:
                    self.assertIs(raised.exception.__cause__, primary)
                    self.assertIs(raised.exception.cleanup_error, cleanup)
                else:
                    self.assertIs(raised.exception, primary)

    def test_cleanup_failure_is_fatal_after_collector_exit_or_resource_defer(self):
        guard = load('guards')
        for mode in ('collector_exit', 'resource_defer'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                process = Mock(pid=999, returncode=1 if mode == 'collector_exit' else None)
                process.poll.return_value = 1 if mode == 'collector_exit' else None
                denied = PermissionError(errno.EPERM, 'cleanup denied')
                with patch.object(guard, 'host_snapshot', return_value={}), \
                     patch.object(guard.subprocess, 'Popen', return_value=process), \
                     patch.object(guard, 'process_group_sample', return_value=[(999, 3*1024**3, 0)]), \
                     patch.object(guard.os, 'killpg', side_effect=denied), \
                     self.assertRaises(RuntimeError) as raised:
                    guard.Budget(folder, guard.policy({})).run(['collector'], stdout=subprocess.DEVNULL)
                self.assertNotIsInstance(raised.exception, guard.Deferred)
                self.assertNotIsInstance(raised.exception, subprocess.CalledProcessError)
                self.assertIsInstance(raised.exception.__cause__,
                    subprocess.CalledProcessError if mode == 'collector_exit' else guard.Deferred)
                self.assertIs(raised.exception.cleanup_error, denied)
                process.wait.assert_called_once_with(timeout=5)

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin exited-unreaped group returns EPERM')
    def test_native_exited_unreaped_child_signal_race_completes_under_budget(self):
        guard = load('guards')
        process_class = guard.subprocess.Popen
        child = None
        def spawn(*args, **kwargs):
            nonlocal child
            child = process_class(*args, **kwargs)
            return child
        def race(group):
            time.sleep(.1)
            return [(group, 1, .8)]
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(guard, 'host_snapshot', return_value={}), \
             patch.object(guard.subprocess, 'Popen', side_effect=spawn), \
             patch.object(guard, 'process_group_sample', side_effect=race):
            try:
                guard.Budget(folder, guard.policy({'limits': {'wall_seconds': 3, 'cpu_seconds': 2}})).run(
                    [sys.executable, '-c', 'import time;time.sleep(.04)'], stdout=subprocess.DEVNULL)
                self.assertEqual(child.returncode, 0)
            finally:
                if child is not None and child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)

    def test_actual_supervisor_termination_cleans_up_separate_child_session(self):
        guard_dir = Path(__file__).resolve().parents[2] / 'scripts/local_compute'
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number), tempfile.TemporaryDirectory() as folder:
                state = Path(folder)
                script = state / 'supervisor.py'
                script.write_text('''import os,sys\nfrom pathlib import Path\nimport subprocess\nsys.path.insert(0,sys.argv[1])\nimport guards\nstate=Path(sys.argv[2])\nguards.host_snapshot=lambda *args:{}\noriginal=guards.subprocess.Popen\nclass RecordChild(original):\n def __init__(self,*args,**kwargs):\n  super().__init__(*args,**kwargs)\n  if kwargs.get("start_new_session"):\n   (state/"child-pid").write_text(str(self.pid))\nguards.subprocess.Popen=RecordChild\ntry:\n guards.Budget(state,guards.policy({"limits":{"wall_seconds":8,"cpu_seconds":2}})).run([sys.executable,"-c","import time;time.sleep(30)"],stdout=subprocess.DEVNULL)\nexcept guards.Deferred:\n (state/"cancelled").write_text("cleaned")\n''')
                supervisor = subprocess.Popen([sys.executable, str(script), str(guard_dir), folder],
                                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                child = None
                try:
                    deadline = time.monotonic() + 3
                    while not (state / 'child-pid').exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue((state / 'child-pid').exists())
                    child = int((state / 'child-pid').read_text())
                    supervisor.send_signal(number)
                    _, stderr = supervisor.communicate(timeout=5)
                    self.assertEqual(supervisor.returncode, 0, stderr.decode())
                    self.assertEqual((state / 'cancelled').read_text(), 'cleaned')
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child, 0)
                finally:
                    if supervisor.poll() is None:
                        supervisor.kill()
                        supervisor.communicate(timeout=5)
                    if child is not None:
                        try:
                            os.kill(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_nan_infinity_bool_or_relaxed_limits_are_rejected(self):
        guard = load('guards')
        for field in guard.DEFAULT_LIMITS:
            for value in (float('nan'), float('inf'), True):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        guard.policy({'limits': {field: value}})
        with self.assertRaises(ValueError):
            guard.policy({'limits': {'cpu_fraction': 1}})
        self.assertEqual(guard.policy({'limits': {'cpu_fraction': .2}})['cpu_fraction'], .2)

    def test_thermal_warning_or_speed_restriction_stops_work(self):
        guard = load('guards')
        self.assertTrue(guard.thermal_ok('Note: No thermal warning level has been recorded'))
        self.assertTrue(guard.thermal_ok('Thermal_Level = 0\nCPU_Speed_Limit = 100'))
        self.assertFalse(guard.thermal_ok('Thermal_Level = 1\nCPU_Speed_Limit = 100'))
        self.assertFalse(guard.thermal_ok('Thermal_Level = 0\nCPU_Speed_Limit = 90'))
        self.assertFalse(guard.thermal_ok('unknown sensor output'))

    def test_native_power_memory_or_load_guard_fails_closed(self):
        guard = load('guards')
        with tempfile.TemporaryDirectory() as folder:
            defaults = {'batt': "Now drawing from 'AC Power'", 'therm': 'Note: No thermal warning level has been recorded',
                        'custom': 'Battery Power:\n lowpowermode 0\nAC Power:\n powermode 0',
                        'pressure': 'System-wide memory free percentage: 79%', 'cpus': '16'}
            def response(cmd):
                if cmd[0].endswith('memory_pressure'):
                    return defaults['pressure']
                if cmd[0].endswith('sysctl'):
                    return defaults['cpus']
                return defaults[cmd[-1]]
            with patch.object(guard.sys, 'platform', 'darwin'), patch.object(guard, '_output', side_effect=response), patch.object(guard.os, 'getloadavg', return_value=(1, 1, 1)):
                self.assertTrue(guard.host_snapshot(folder, guard.policy({}))['ac_power'])
                for key, value in [('batt', "Now drawing from 'Battery Power'"),
                                   ('custom', 'AC Power:\n powermode 1'),
                                   ('pressure', 'System-wide memory free percentage: 10%'),
                                   ('therm', 'unavailable')]:
                    with self.subTest(key=key), patch.dict(defaults, {key: value}):
                        with self.assertRaises(guard.Deferred):
                            guard.host_snapshot(folder, guard.policy({}))
                with patch.object(guard.os, 'getloadavg', return_value=(5, 5, 5)):
                    with self.assertRaises(guard.Deferred):
                        guard.host_snapshot(folder, guard.policy({}))

    def test_process_group_memory_failure_terminates_child_and_thread_env_is_one(self):
        guard = load('guards')
        process = Mock(pid=999, returncode=None)
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as folder, patch.object(guard, 'host_snapshot', return_value={}), patch.object(guard.subprocess, 'Popen', return_value=process) as popen, patch.object(guard, 'process_group_sample', return_value=[(999, 3 * 1024**3, .1)]), patch.object(guard.os, 'killpg') as kill:
            budget = guard.Budget(folder, guard.policy({}))
            with self.assertRaisesRegex(guard.Deferred, 'memory budget'):
                budget.run(['python', 'unused'], stdout=None)
            kill.assert_called_with(999, guard.signal.SIGKILL)
            process.wait.assert_called_once()
            for key in guard.THREAD_ENV:
                self.assertEqual(popen.call_args.kwargs['env'][key], '1')

    def test_cpu_clock_parses_mac_and_long_runtime(self):
        guard = load('guards')
        self.assertAlmostEqual(guard.cpu_clock('01:02.30'), 62.3)
        self.assertEqual(guard.cpu_clock('01:02:03'), 3723)
        self.assertEqual(guard.cpu_clock('1-01:02:03'), 90123)


class WorkerTests(unittest.TestCase):
    def gap_fixture(self, worker, state, *, mutate=None, collector_exit=1, audit_failure=False,
                    stdout_kind='valid', cleanup_failure=False, wrong_command=False):
        for kind in ('paper', 'weather'):
            (state / f'{kind}-export.json').write_text('{}')
        old = {'old': 'verified', 'input_directory': str(state)}
        (state / 'latest-success.json').write_text(json.dumps(old))
        identity = {'source_commit': 'a' * 40, 'source_dirty': False, 'source_files_sha256': {}}
        calls = []
        def completed(command, **kwargs):
            calls.append(Path(command[1]).name)
            if command[1].endswith('shadow_collect.py'):
                shadow = Path(command[command.index('--state-dir') + 1]);shadow.mkdir(exist_ok=True)
                weather = shadow / 'weather-export.json';weather.write_text('{"raw_only":true}')
                receipt = {'schema_version': 1, 'status': 'no_issued_distribution',
                    'research_identity': 'local-shadow-v7-v1', 'execution_location': 'local_mac',
                    'source_commit': identity['source_commit'], 'source_dirty': False,
                    'started_at': datetime.now(timezone.utc).isoformat(),
                    'finished_at': datetime.now(timezone.utc).isoformat(),
                    'new_issued_vintages': 0, 'new_snapshot_ids': [],
                    'http_requests': 8,
                    'live_orders_enabled': False, 'promotion_eligible': False,
                    'weather_export_sha256': worker.file_hash(weather)}
                if mutate: mutate(receipt, weather)
                (shadow / 'latest-receipt.json').write_text(json.dumps(receipt))
                final = {key: receipt[key] for key in ('status', 'research_identity',
                    'new_issued_vintages', 'http_requests', 'live_orders_enabled', 'promotion_eligible')}
                if stdout_kind == 'mismatch': final['http_requests'] = 7
                if stdout_kind == 'valid' or stdout_kind == 'mismatch':
                    kwargs['stdout'].write(json.dumps(final) + '\n')
                if stdout_kind == 'post_receipt_crash':
                    kwargs['stdout'].write('Traceback (most recent call last):\nPermissionError: rotation write failed\n')
                kwargs['stdout'].flush()
                if cleanup_failure:
                    guard = load('guards')
                    primary = subprocess.CalledProcessError(collector_exit, command)
                    raise guard.GroupCleanupError(PermissionError(errno.EPERM, 'cleanup denied'), primary) from primary
                raise subprocess.CalledProcessError(collector_exit, ['/bin/ps'] if wrong_command else command)
            if audit_failure:
                raise subprocess.CalledProcessError(1, command)
            Path(command[command.index('--output') + 1]).write_text('{}')
            if '--lineage-output' in command:
                Path(command[command.index('--lineage-output') + 1]).write_bytes(b'lineage')
        config = {'state_dir': str(state), 'offline_export_dir': str(state),
                  'local_prospective_collection': True, 'shadow_cities': ['sfo']}
        return config, identity, completed, calls, old

    def test_fresh_valid_raw_only_batch_continues_all_four_audits_and_keeps_gap_explicit(self):
        worker = load('worker')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            config, identity, completed, calls, _ = self.gap_fixture(worker, state)
            with patch.object(worker, 'host_snapshot', return_value={}), patch.object(worker.os, 'nice'), \
                 patch.object(worker, 'source_identity', return_value=identity), \
                 patch.object(worker.Budget, 'run', side_effect=completed):
                self.assertEqual(worker.run(config), 0)
            self.assertEqual(len(calls), 5)
            receipt = json.loads((state / 'latest-success.json').read_text())
            self.assertEqual(receipt['status'], 'complete')
            self.assertEqual(receipt['local_shadow_status'], 'no_issued_distribution')
            self.assertEqual(receipt['local_shadow_new_issued_vintages'], 0)
            self.assertEqual(len(receipt['artifacts']), 5)
            self.assertFalse(receipt['live_orders_enabled'])

    def test_raw_only_exception_requires_fresh_bound_exact_contract(self):
        worker = load('worker')
        changes = [(key, value) for key, value in (
            ('schema_version', True), ('status', 'complete'), ('research_identity', 'aws'),
            ('execution_location', 'aws'), ('source_commit', 'b' * 40), ('source_dirty', True),
            ('source_dirty', 0), ('new_issued_vintages', 1), ('new_issued_vintages', False),
            ('new_snapshot_ids', ['unverified']), ('new_snapshot_ids', None),
            ('live_orders_enabled', True), ('promotion_eligible', True),
            ('started_at', '2020-01-01T00:00:00+00:00'),
            ('finished_at', '2099-01-01T00:00:00+00:00'),
            ('finished_at', '2020-01-01T00:00:00'), ('weather_export_sha256', '0' * 64))]
        for key, value in changes:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as folder:
                state = Path(folder)
                config, identity, completed, calls, old = self.gap_fixture(worker, state,
                    mutate=lambda receipt, weather: receipt.update({key: value}))
                with patch.object(worker, 'host_snapshot', return_value={}), patch.object(worker.os, 'nice'), \
                     patch.object(worker, 'source_identity', return_value=identity), \
                     patch.object(worker.Budget, 'run', side_effect=completed):
                    self.assertEqual(worker.run(config), 1)
                self.assertEqual(calls, ['shadow_collect.py'])
                self.assertEqual(json.loads((state / 'latest-success.json').read_text()), old)
        for collector_exit, audit_failure in ((2, False), (1, True)):
            with self.subTest(collector_exit=collector_exit, audit_failure=audit_failure), tempfile.TemporaryDirectory() as folder:
                state = Path(folder)
                config, identity, completed, calls, old = self.gap_fixture(worker, state,
                    collector_exit=collector_exit, audit_failure=audit_failure)
                with patch.object(worker, 'host_snapshot', return_value={}), patch.object(worker.os, 'nice'), \
                     patch.object(worker, 'source_identity', return_value=identity), \
                     patch.object(worker.Budget, 'run', side_effect=completed):
                    self.assertEqual(worker.run(config), 1)
                self.assertEqual(json.loads((state / 'latest-success.json').read_text()), old)
                self.assertEqual(len(calls), 2 if audit_failure else 1)

    def test_raw_only_receipt_cannot_borrow_a_crashed_collector_or_failed_cleanup(self):
        worker = load('worker')
        variants = [{'stdout_kind': 'missing'}, {'stdout_kind': 'post_receipt_crash'},
                    {'stdout_kind': 'mismatch'}, {'cleanup_failure': True}, {'wrong_command': True}]
        for variant in variants:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as folder:
                state = Path(folder)
                config, identity, completed, calls, old = self.gap_fixture(worker, state, **variant)
                with patch.object(worker, 'host_snapshot', return_value={}), patch.object(worker.os, 'nice'), \
                     patch.object(worker, 'source_identity', return_value=identity), \
                     patch.object(worker.Budget, 'run', side_effect=completed):
                    self.assertEqual(worker.run(config), 1)
                self.assertEqual(calls, ['shadow_collect.py'])
                self.assertEqual(json.loads((state / 'latest-success.json').read_text()), old)
                failure = json.loads((state / 'status.json').read_text())
                self.assertEqual(failure['status'], 'failed')
                error = next((state / 'runs').glob('*/error.txt'))
                self.assertIn('Traceback', error.read_text())
                self.assertEqual(error.stat().st_mode & 0o777, 0o600)

    def test_installed_mode_rejects_later_weakened_config_before_state_or_work(self):
        worker = load('worker')
        for limits in ({'wall_seconds': 1200, 'cpu_seconds': 600},
                       {'wall_seconds': 301, 'cpu_seconds': 120},
                       {'wall_seconds': 300, 'cpu_seconds': 121}, {}):
            for existing in (False, True):
                with self.subTest(limits=limits, existing_state=existing), tempfile.TemporaryDirectory() as folder:
                    state = Path(folder) / 'state'
                    if existing:
                        state.mkdir(mode=0o755)
                        (state / 'latest-success.json').write_bytes(b'{"previous":"verified"}\n')
                    before = {path.name: (path.stat().st_mode, path.stat().st_mtime_ns,
                              path.read_bytes() if path.is_file() else None)
                              for path in (state, *state.iterdir())} if existing else {}
                    config = {'state_dir': str(state), 'fresh_export': True,
                              'local_prospective_collection': True, 'limits': limits}
                    with patch.object(worker, 'host_snapshot') as admission, \
                         patch.object(worker, 'Budget') as budget, \
                         patch.object(worker, 'collect_export') as network, \
                         patch.object(worker, 'offline_inputs') as inputs, \
                         patch.object(worker, 'source_identity') as source, \
                         patch.object(worker.subprocess, 'run') as subprocess_run, \
                         patch.object(worker.subprocess, 'Popen') as popen, \
                         patch.object(worker.subprocess, 'check_output') as check_output, \
                         self.assertRaisesRegex(ValueError, 'two-hour schedule'):
                        worker.run(config, scheduled=True)
                    for action in (admission, budget, network, inputs, source,
                                   subprocess_run, popen, check_output):
                        action.assert_not_called()
                    after = {path.name: (path.stat().st_mode, path.stat().st_mtime_ns,
                             path.read_bytes() if path.is_file() else None)
                             for path in (state, *state.iterdir())} if state.exists() else {}
                    self.assertEqual(after, before)

    def test_scheduled_check_uses_tighter_limits_while_manual_check_retains_defaults(self):
        worker = load('worker')
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(worker, 'host_snapshot', return_value={}) as admission, \
             patch.object(worker.sys, 'stdout', io.StringIO()):
            config = {'state_dir': folder, 'limits': {'wall_seconds': 300, 'cpu_seconds': 120}}
            self.assertEqual(worker.run(config, checks_only=True, scheduled=True), 0)
            self.assertEqual(admission.call_args.args[1]['wall_seconds'], 300)
            self.assertEqual(admission.call_args.args[1]['cpu_seconds'], 120)
            self.assertEqual(worker.run({'state_dir': folder}, checks_only=True), 0)
            self.assertEqual(admission.call_args.args[1]['wall_seconds'], 1200)
            self.assertEqual(admission.call_args.args[1]['cpu_seconds'], 600)

    def test_missing_egress_attestation_never_starts_ssh_and_preserves_success(self):
        worker = load('worker')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            (state / 'latest-success.json').write_text('{"old":"verified"}')
            config = {'state_dir': folder, 'ssh_key': 'unused', 'ssh_target': 'unused', 'fresh_export': True}
            with patch.object(worker, 'host_snapshot', return_value={}), patch.object(worker.os, 'nice'), patch.object(worker.Budget, 'run') as run:
                self.assertEqual(worker.run(config), 0)
            run.assert_not_called()
            self.assertEqual(json.loads((state / 'latest-success.json').read_text()), {'old': 'verified'})
            self.assertEqual(json.loads((state / 'status.json').read_text())['status'], 'deferred')

    def test_egress_reserves_failures_and_rejects_expired_month(self):
        worker = load('worker')
        now = datetime(2026, 10, 5, tzinfo=timezone.utc)
        unit = 1024**2
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            config = {'network_policy': {'verified_month': '2026-10', 'verified_free_bytes': 4 * unit,
                                        'max_export_bytes': unit, 'monthly_export_budget_bytes': 2 * unit}}
            self.assertEqual(worker.reserve_egress(state, config, now), unit)
            self.assertEqual(worker.reserve_egress(state, config, now), unit)
            with self.assertRaisesRegex(worker.Deferred, 'exhausted'):
                worker.reserve_egress(state, config, now)
            with self.assertRaisesRegex(worker.Deferred, 'current-month'):
                worker.reserve_egress(state, config, now.replace(month=11))
            for used in (-1, True, '0', float('nan')):
                (state / 'egress-budget.json').write_text(json.dumps({'month': '2026-10', 'reserved_bytes': used}))
                with self.subTest(used=used), self.assertRaisesRegex(ValueError, 'existing egress'):
                    worker.reserve_egress(state, config, now)

    def test_duplicate_offline_evidence_skips_all_jobs_without_new_run_directory(self):
        worker = load('worker')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            for name in ('paper', 'weather'):
                (state / f'{name}-export.json').write_text('{}')
            source = {'source_commit': 'abc', 'source_dirty': False, 'source_files_sha256': {'file.py': 'one'}}
            hashes = {kind: worker.file_hash(state / f'{kind}-export.json') for kind in ('paper', 'weather')}
            (state / 'latest-success.json').write_text(json.dumps({'input_directory': folder, 'input_sha256': hashes, 'source_files_sha256': source['source_files_sha256']}))
            with patch.object(worker, 'host_snapshot', return_value={}), patch.object(worker, 'source_identity', return_value=source), patch.object(worker.os, 'nice'), patch.object(worker.Budget, 'run') as run:
                self.assertEqual(worker.run({'state_dir': folder}), 0)
            run.assert_not_called()
            self.assertFalse((state / 'runs').exists())
            self.assertEqual(json.loads((state / 'status.json').read_text())['status'], 'skipped')
            self.assertFalse(json.loads((state / 'status.json').read_text())['new_evidence'])

    def test_disconnect_after_reservation_preserves_success(self):
        worker = load('worker')
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            (state / 'latest-success.json').write_text('{"old":"verified"}')
            config = {'state_dir': folder, 'ssh_key': 'unused', 'ssh_target': 'unused', 'fresh_export': True,
                      'network_policy': {'verified_month': now.strftime('%Y-%m'), 'verified_free_bytes': 1024**2, 'max_export_bytes': 1024**2}}
            with patch.object(worker, 'host_snapshot', return_value={}), patch.object(worker.os, 'nice'), patch.object(worker.Budget, 'run', side_effect=TimeoutError('disconnected')):
                self.assertEqual(worker.run(config), 1)
            self.assertEqual(json.loads((state / 'latest-success.json').read_text()), {'old': 'verified'})
            self.assertEqual(json.loads((state / 'status.json').read_text())['status'], 'failed')
            self.assertEqual(json.loads((state / 'egress-budget.json').read_text())['reserved_bytes'], 1024**2)

    def test_source_mutation_rejects_completed_results_and_preserves_last_success(self):
        worker = load('worker')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            for kind in ('paper', 'weather'):
                (state / f'{kind}-export.json').write_text('{}')
            old = {'old': 'verified', 'input_directory': folder}
            (state / 'latest-success.json').write_text(json.dumps(old))
            before = {'source_commit': 'abc', 'source_dirty': True, 'source_files_sha256': {'model.py': 'original'}}
            after = {**before, 'source_files_sha256': {'model.py': 'changed'}}
            def completed(command, **kwargs):
                Path(command[command.index('--output') + 1]).write_text('{}')
                if '--lineage-output' in command:
                    Path(command[command.index('--lineage-output') + 1]).write_bytes(b'lineage')
            with patch.object(worker, 'host_snapshot', return_value={}), patch.object(worker.os, 'nice'), patch.object(worker, 'source_identity', side_effect=[before, after]), patch.object(worker.Budget, 'run', side_effect=completed):
                self.assertEqual(worker.run({'state_dir': folder}), 1)
            self.assertEqual(json.loads((state / 'latest-success.json').read_text()), old)
            self.assertEqual(json.loads((state / 'status.json').read_text())['status'], 'failed')

    def test_input_mutation_rejects_results_bound_to_previous_evidence(self):
        worker = load('worker')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            for kind in ('paper', 'weather'):
                (state / f'{kind}-export.json').write_text('{}')
            old = {'old': 'verified', 'input_directory': folder}
            (state / 'latest-success.json').write_text(json.dumps(old))
            identity = {'source_commit': 'abc', 'source_dirty': True, 'source_files_sha256': {'model.py': 'original'}}
            def completed(command, **kwargs):
                Path(command[command.index('--output') + 1]).write_text('{}')
                if '--lineage-output' in command:
                    Path(command[command.index('--lineage-output') + 1]).write_bytes(b'lineage')
                    (state / 'weather-export.json').write_text('{"changed":true}')
            with patch.object(worker, 'host_snapshot', return_value={}), patch.object(worker.os, 'nice'), patch.object(worker, 'source_identity', return_value=identity), patch.object(worker.Budget, 'run', side_effect=completed):
                self.assertEqual(worker.run({'state_dir': folder}), 1)
            self.assertEqual(json.loads((state / 'latest-success.json').read_text()), old)
            self.assertEqual(json.loads((state / 'status.json').read_text())['status'], 'failed')

    def test_shadow_rotation_remains_bounded_and_uses_separate_weather_evidence(self):
        worker = load('worker')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            for kind in ('paper', 'weather'):
                (state / f'{kind}-export.json').write_text('{}')
            identity = {'source_commit': 'abc', 'source_dirty': True, 'source_files_sha256': {}}
            calls = []
            def completed(command, **kwargs):
                calls.append(command)
                if command[1].endswith('shadow_collect.py'):
                    shadow = Path(command[command.index('--state-dir') + 1])
                    shadow.mkdir()
                    (shadow / 'latest-receipt.json').write_text('{"research_identity":"separate"}')
                    (shadow / 'weather-export.json').write_text('{"prospective":true}')
                else:
                    Path(command[command.index('--output') + 1]).write_text('{}')
                    if '--lineage-output' in command:
                        Path(command[command.index('--lineage-output') + 1]).write_bytes(b'lineage')
            config = {'state_dir': folder, 'offline_export_dir': folder,
                      'local_prospective_collection': True, 'shadow_cities': ['sfo'], 'shadow_rotate_registry': True}
            with patch.object(worker, 'host_snapshot', return_value={}), patch.object(worker.os, 'nice'), patch.object(worker, 'source_identity', return_value=identity), patch.object(worker.Budget, 'run', side_effect=completed):
                self.assertEqual(worker.run(config), 0)
            self.assertIn('--rotate-registry', calls[0])
            self.assertEqual(calls[0][calls[0].index('--maximum-http-requests') + 1], '8')
            self.assertEqual(calls[2][calls[2].index('--export') + 1], str(state.resolve() / 'prospective/weather-export.json'))
            receipt = json.loads((state / 'latest-success.json').read_text())
            self.assertNotEqual(receipt['retained_input_sha256']['weather'], receipt['input_sha256']['weather'])
            self.assertEqual(receipt['evidence_mode'], 'retained_aws_paper_and_local_prospective_weather')

    def test_full_analysis_and_scheduled_worker_share_one_lock(self):
        worker = load('worker')
        full = load('full_analysis')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            manifest = state / 'manifest.json'
            manifest.write_text(json.dumps({'state_dir': folder}))
            with (state / 'worker.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(worker, 'host_snapshot') as host:
                    self.assertEqual(worker.run({'state_dir': folder}), 0)
                    host.assert_not_called()
                with patch.object(full.sys, 'argv', ['full_analysis', '--manifest', str(manifest), '--output', str(state / 'output')]):
                    with self.assertRaisesRegex(SystemExit, 'another local research job'):
                        full.main()


class FullAnalysisTests(unittest.TestCase):
    def test_hidden_execute_cli_refuses_missing_resource_controller_marker(self):
        wrapper = load('full_analysis')
        with patch.dict(wrapper.os.environ, {}, clear=True), patch.object(wrapper.sys, 'argv', ['full_analysis', '--manifest', '/unused/manifest.json', '--output', '/unused/output', '--execute']):
            with self.assertRaisesRegex(SystemExit, 'must be launched through its resource budget'):
                wrapper.main()

    def fixture_manifest(self, folder):
        wrapper = load('full_analysis')
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'trading'))
        from sfo_kalshi_quant.db import PaperStore
        from sfo_kalshi_quant.strategy_lab.build import _analysis_config_fingerprint
        base = Path(folder)
        paper = base / 'paper.db'
        PaperStore(paper)
        with sqlite3.connect(paper) as connection:
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        connection.close()
        weather = base / 'weather.db'
        connection = sqlite3.connect(weather)
        connection.execute('CREATE TABLE no_weather_evidence (value INTEGER)')
        connection.close()
        records = {}
        for kind, path in (('paper', paper), ('weather', weather)):
            digest = wrapper.file_hash(path)
            receipt = base / f'{kind}.receipt.json'
            receipt.write_text(json.dumps({'integrity_check': 'ok', 'foreign_key_check': 'ok', 'bytes': path.stat().st_size, 'sha256': digest}))
            records[kind] = {'path': str(path), 'receipt': str(receipt), 'sha256': digest}
        identity = wrapper.source_identity()
        build = base / 'build_info.json'
        build.write_text(json.dumps({'source_sha': identity['source_commit'], 'source_dirty': identity['source_dirty']}))
        signal = base / 'trading_signal.json'
        signal.write_text('{}')
        manifest = base / 'manifest.json'
        manifest.write_text(json.dumps({'target_source_sha': identity['source_commit'],
            'allow_dirty_diagnostics': True, 'state_dir': folder,
            'runtime_config': {'PAPER_BANKROLL': 1000, 'PAPER_ENTRY_MODE': 'market', 'PAPER_RISK_PROFILE': 'live'},
            'expected_config_fingerprint': _analysis_config_fingerprint(calibration_min_train=180),
            'paper_snapshot': records['paper'], 'weather_snapshot': records['weather'],
            'forecaster_files': {p.name: {'path': str(p), 'sha256': wrapper.file_hash(p)} for p in (build, signal)}}))
        return wrapper, manifest

    @unittest.skipUnless(sys.platform == 'darwin', 'APFS COW helper requires macOS')
    def test_actual_full_builder_fixture_stages_matching_artifacts_and_denies_network(self):
        with tempfile.TemporaryDirectory() as folder:
            wrapper, manifest = self.fixture_manifest(folder)
            original = Path(folder) / 'paper.db'
            digest = wrapper.file_hash(original)
            output = Path(folder) / 'analysis'
            with patch.dict(wrapper.os.environ, {'PAPER_ENTRY_MODE': 'market'}), patch.object(wrapper.sys, 'addaudithook') as hooks:
                wrapper.execute(manifest, output)
            hook = hooks.call_args.args[0]
            with self.assertRaisesRegex(RuntimeError, 'forbids network'):
                hook('socket.connect', ())
            with self.assertRaisesRegex(RuntimeError, 'forbids network'):
                hook('socket.getaddrinfo', ())
            receipt = json.loads((output / 'analysis-receipt.json').read_text())
            self.assertFalse(receipt['promotion_eligible'])
            self.assertFalse(receipt['live_orders_enabled'])
            cache = json.loads((output / 'forecaster/strategy_analysis_cache.json').read_text())
            evidence = json.loads((output / 'forecaster/strategy_research_evidence.private.json').read_text())
            self.assertEqual(cache['source_sha'], evidence['source_sha'])
            self.assertEqual(cache['config_fingerprint'], evidence['config_fingerprint'])
            self.assertEqual(receipt['artifacts']['strategy_analysis_cache.json'], wrapper.file_hash(output / 'forecaster/strategy_analysis_cache.json'))
            self.assertEqual(wrapper.file_hash(original), digest)
            self.assertFalse((output / 'paper-working.db').exists())

    def test_clone_content_changed_during_preparation_rejects_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            wrapper, manifest = self.fixture_manifest(folder)
            def corrupt(source, destination):
                destination.write_bytes(b'corrupt clone')
            output = Path(folder) / 'analysis'
            with patch.object(wrapper, 'clone_snapshot', side_effect=corrupt):
                with self.assertRaisesRegex(ValueError, 'working clone differs'):
                    wrapper.execute(manifest, output)
            self.assertFalse((output / 'analysis-receipt.json').exists())

    def test_staged_json_changed_during_copy_rejects_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            wrapper, manifest = self.fixture_manifest(folder)
            copy = wrapper.shutil.copyfile
            def changed(source, destination):
                copy(source, destination)
                if destination.name == 'build_info.json':
                    destination.write_text('{}')
            output = Path(folder) / 'analysis'
            with patch.object(wrapper, 'clone_snapshot', side_effect=copy), patch.object(wrapper.shutil, 'copyfile', side_effect=changed):
                with self.assertRaisesRegex(ValueError, 'staged forecaster input differs'):
                    wrapper.execute(manifest, output)
            self.assertFalse((output / 'analysis-receipt.json').exists())

    def test_manifest_mutation_rejects_full_builder_completed_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            wrapper, manifest = self.fixture_manifest(folder)
            from sfo_kalshi_quant.strategy_lab import build
            write = build.write_strategy_research
            def mutate(path, value):
                write(path, value)
                manifest.write_bytes(manifest.read_bytes() + b'\n')
            output = Path(folder) / 'analysis'
            with patch.dict(wrapper.os.environ, {'PAPER_ENTRY_MODE': 'market'}), patch.object(wrapper.sys, 'addaudithook'), patch.object(wrapper, 'clone_snapshot', side_effect=wrapper.shutil.copyfile), patch.object(build, 'write_strategy_research', side_effect=mutate):
                with self.assertRaisesRegex(ValueError, 'manifest changed'):
                    wrapper.execute(manifest, output)
            self.assertFalse((output / 'analysis-receipt.json').exists())

    def test_snapshot_validation_rejects_tamper_and_live_sidecar(self):
        wrapper = load('full_analysis')
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'snapshot.db'
            with sqlite3.connect(db) as connection:
                connection.execute('CREATE TABLE proof (value INTEGER)')
            connection.close()
            digest = wrapper.file_hash(db)
            receipt = Path(folder) / 'receipt.json'
            receipt.write_text(json.dumps({'integrity_check': 'ok', 'foreign_key_check': 'ok', 'bytes': db.stat().st_size, 'sha256': digest}))
            record = {'path': str(db), 'receipt': str(receipt), 'sha256': digest}
            self.assertEqual(wrapper.verified_snapshot(record), db.resolve())
            db.with_name('snapshot.db-wal').write_bytes(b'')
            self.assertEqual(wrapper.verified_snapshot(record), db.resolve())
            db.with_name('snapshot.db-wal').write_text('unverified transaction')
            with self.assertRaisesRegex(ValueError, 'sidecar'):
                wrapper.verified_snapshot(record)
            db.with_name('snapshot.db-wal').unlink()
            db.write_bytes(b'X' + db.read_bytes()[1:])
            with self.assertRaisesRegex(ValueError, 'content differs'):
                wrapper.verified_snapshot(record)

    def test_launch_schedule_has_no_install_time_run_and_is_background(self):
        installer = load('install_launch_agent')
        state = Path('/private/local')
        plist = installer.launch_document(state / 'config.json', state, '/python')
        self.assertFalse(plist['RunAtLoad'])
        self.assertEqual(plist['StartInterval'], 7200)
        self.assertTrue(plist['LowPriorityIO'])
        self.assertEqual(plist['ProcessType'], 'Background')
        self.assertEqual(plist['Nice'], 10)
        self.assertEqual(plist['ProgramArguments'][:2], ['/usr/bin/caffeinate', '-i'])
        self.assertEqual(plist['ProgramArguments'][-1], '--scheduled')

    def test_frequent_schedule_rejects_weak_budget_before_any_install_change(self):
        installer = load('install_launch_agent')
        for limits in (None, {'wall_seconds': 301, 'cpu_seconds': 120},
                       {'wall_seconds': 300, 'cpu_seconds': 121},
                       {'wall_seconds': 300}, {'cpu_seconds': 120},
                       {'wall_seconds': float('nan'), 'cpu_seconds': 120},
                       {'wall_seconds': 300, 'cpu_seconds': 120, 'cpu_fraction': .6}):
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as folder:
                home = Path(folder)
                state = home / 'uncreated-state'
                config = home / 'config.json'
                value = {'state_dir': str(state)}
                if limits is not None:
                    value['limits'] = limits
                config.write_text(json.dumps(value))
                config.chmod(0o600)
                plist = home / 'Library/LaunchAgents/com.weatheredge.v7.local-research.plist'
                plist.parent.mkdir(parents=True)
                original = b'previous independently verified schedule'
                plist.write_bytes(original)
                original_time = plist.stat().st_mtime_ns
                with patch.object(installer.Path, 'home', return_value=home), \
                     patch.object(installer.sys, 'argv', ['install', '--config', str(config)]), \
                     patch.object(installer.subprocess, 'run') as launchctl, \
                     patch.object(installer.sys, 'stderr', io.StringIO()), \
                     self.assertRaises(SystemExit) as rejected:
                    installer.main()
                self.assertEqual(rejected.exception.code, 2)
                launchctl.assert_not_called()
                self.assertFalse(state.exists())
                self.assertEqual(plist.read_bytes(), original)
                self.assertEqual(plist.stat().st_mtime_ns, original_time)

    def test_frequent_schedule_accepts_tighter_config_and_keeps_manual_default_budget(self):
        installer = load('install_launch_agent')
        limits = installer.scheduled_policy({'limits': {'wall_seconds': 300, 'cpu_seconds': 120}})
        self.assertEqual(limits['wall_seconds'], 300)
        self.assertEqual(limits['cpu_seconds'], 120)
        self.assertEqual(limits['cpu_fraction'], .5)
        self.assertEqual(limits['rss_bytes'], 2 * 1024**3)
        self.assertEqual(installer.policy({})['wall_seconds'], 1200)
        self.assertEqual(installer.policy({})['cpu_seconds'], 600)


if __name__ == '__main__':
    unittest.main()
