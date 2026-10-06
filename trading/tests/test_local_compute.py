"""Failure and preservation checks for the local-only research runner."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[2] / 'scripts/local_compute' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExportTests(unittest.TestCase):
    def test_complete_snapshot_and_no_source_writes(self):
        exporter = load('export_evidence')
        with tempfile.TemporaryDirectory() as folder:
            paper, weather = [str(Path(folder) / x) for x in ('paper.db', 'weather.db')]
            for path, tables in [(paper, ['paper_accounts', 'paper_orders']),
                                 (weather, ['nwp_model_forecasts', 'cli_settlements', 'forecast_emos_daily_high'])]:
                with sqlite3.connect(path) as c:
                    for name in tables:
                        c.execute(f'CREATE TABLE {name} (target_date TEXT, local_date TEXT, value INTEGER)')
                        c.execute(f"INSERT INTO {name} VALUES ('2026-10-05', '2026-10-05', 1)")
            before = [Path(path).read_bytes() for path in (paper, weather)]
            result = exporter.collect(paper, weather)
            self.assertEqual(len(result['paper']['paper']['paper_orders']['rows']), 1)
            self.assertFalse(result['weather']['cli_settlements']['truncated'])
            self.assertEqual(before, [Path(path).read_bytes() for path in (paper, weather)])
            with patch.object(exporter, 'LIMIT', 0):
                with self.assertRaisesRegex(ValueError, 'bounded export limit'):
                    exporter.collect(paper, weather)


class WorkerTests(unittest.TestCase):
    def test_disconnect_preserves_last_success(self):
        worker = load('worker')
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            (state / 'latest-success.json').write_text('{"old":"verified"}')
            config = {'state_dir': folder, 'ssh_key': 'unused', 'ssh_target': 'unused'}
            original = worker.subprocess.check_output
            def command(args, **kwargs):
                if args[0] == 'ssh':
                    raise TimeoutError('disconnected')
                return original(args, **kwargs)
            with patch.object(worker.subprocess, 'check_output', side_effect=command):
                self.assertEqual(worker.run(config), 1)
            self.assertEqual(json.loads((state / 'latest-success.json').read_text()), {'old': 'verified'})
            self.assertEqual(json.loads((state / 'status.json').read_text())['status'], 'failed')
            self.assertEqual(len(list((state / 'runs').glob('*/receipt.json'))), 1)


if __name__ == '__main__':
    unittest.main()
