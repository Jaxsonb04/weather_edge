#!/usr/bin/env python3
"""Mac-only research worker; never changes AWS data, policies or public artifacts."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[2]


def write_json(path, value):
    staged = path.with_suffix(path.suffix + '.tmp')
    staged.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n')
    staged.replace(path)


def run(config):
    state = Path(config['state_dir']).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    with (state / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        clock = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        folder = state / 'runs' / clock
        folder.mkdir(parents=True, mode=0o700)
        receipt = {'schema_version': 1, 'started_at': clock, 'status': 'running',
                   'purpose': 'offline diagnostics; no policy promotion or profit inference',
                   'source_commit': subprocess.check_output(
                       ['git', '-C', str(REPO), 'rev-parse', 'HEAD'], text=True).strip(),
                   'source_dirty': bool(subprocess.check_output(
                       ['git', '-C', str(REPO), 'status', '--porcelain'], text=True).strip())}
        source_paths = subprocess.check_output(
            ['git', '-C', str(REPO), 'ls-files', '-z'], text=True).split('\0')
        source_paths += [str(p.relative_to(REPO)) for p in (REPO / 'scripts/local_compute').glob('*.py')]
        receipt['python_version'] = sys.version
        receipt['source_files_sha256'] = {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()
                                         for name in sorted(set(source_paths)) if name.endswith('.py')}
        write_json(state / 'status.json', receipt)
        try:
            ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                   '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                   '-i', config['ssh_key'], config['ssh_target'], 'python3 -']
            with (folder / 'export.stderr').open('w') as err:
                raw = subprocess.check_output(ssh, input=(REPO / 'scripts/local_compute/export_evidence.py').read_bytes(),
                                              stderr=err, timeout=180)
            exported = json.loads(raw)
            for kind in ('paper', 'weather'):
                write_json(folder / f'{kind}-export.json', exported[kind])
            jobs = [('audit_paper_performance.py', 'paper', []),
                    ('audit_forecast_evidence.py', 'weather', []),
                    ('evaluate_v7_bias_pilot.py', 'weather', ['--repo', str(REPO)])]
            receipt['artifacts'] = {}
            for script, kind, extra in jobs:
                output = folder / (script.removesuffix('.py') + '.json')
                with (folder / (script + '.log')).open('w') as log:
                    subprocess.run([sys.executable, str(REPO / 'scripts' / script),
                                    '--export', str(folder / f'{kind}-export.json'),
                                    '--output', str(output), *extra],
                                   stdout=log, stderr=subprocess.STDOUT, check=True, timeout=1800)
                receipt['artifacts'][output.name] = hashlib.sha256(output.read_bytes()).hexdigest()
            receipt.update(status='complete', finished_at=datetime.now(timezone.utc).isoformat(),
                           export_sha256=hashlib.sha256(raw).hexdigest(), run_directory=str(folder))
            write_json(folder / 'receipt.json', receipt)
            write_json(state / 'latest-success.json', receipt)
        except Exception as error:
            # Details remain private; a failed attempt never replaces last success.
            receipt.update(status='failed', error_type=type(error).__name__,
                           finished_at=datetime.now(timezone.utc).isoformat())
            (folder / 'error.txt').write_text(str(error) + '\n')
            write_json(folder / 'receipt.json', receipt)
            write_json(state / 'status.json', receipt)
            return 1
        write_json(state / 'status.json', receipt)
        return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    sys.exit(run(json.loads(args.config.read_text())))
