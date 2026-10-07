#!/usr/bin/env python3
"""Bounded Mac research; no AWS writes, money, automatic promotion or paid service."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
from guards import Budget, Deferred, host_snapshot, policy
from install_launch_agent import scheduled_policy

REPO = Path(__file__).resolve().parents[2]


def write_json(path, value):
    staged = path.with_suffix(path.suffix + '.tmp')
    staged.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n')
    os.chmod(staged, 0o600)
    staged.replace(path)


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def reserve_egress(state, config, now):
    """Reserve the full ceiling before SSH; failed transfers still count.

    The allowance must be independently verified for the current billing month,
    after other AWS usage and a safety margin. This program cannot establish it.
    """
    month = now.strftime('%Y-%m')
    allowed = config.get('network_policy') or {}
    maximum = allowed.get('max_export_bytes', 256 * 1024**2)
    free = allowed.get('verified_free_bytes', 0)
    monthly = allowed.get('monthly_export_budget_bytes', 512 * 1024**2)
    values = (maximum, free, monthly)
    if allowed.get('verified_month') != month or any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in values):
        raise Deferred('fresh exports require a verified current-month free egress allowance')
    if maximum < 1024**2 or maximum > 256 * 1024**2 or monthly > 512 * 1024**2:
        raise ValueError('reviewed export ceiling exceeded')
    counter = state / 'egress-budget.json'
    saved = json.loads(counter.read_text()) if counter.exists() else {}
    used = saved.get('reserved_bytes', 0) if saved.get('month') == month else 0
    if isinstance(used, bool) or not isinstance(used, int) or used < 0:
        raise ValueError('invalid existing egress reservation; fresh transfer refused')
    if used + maximum > min(free, monthly):
        raise Deferred('free egress reservation exhausted')
    write_json(counter, {'month': month, 'reserved_bytes': used + maximum,
                         'policy': 'failed attempts retain worst-case reservation'})
    return maximum


def offline_inputs(state, config):
    folder = config.get('offline_export_dir')
    if folder is None:
        last = state / 'latest-success.json'
        if not last.exists():
            raise Deferred('no local evidence export; fresh network collection is disabled')
        saved = json.loads(last.read_text())
        folder = saved.get('input_directory') or saved.get('run_directory')
    if not folder:
        raise Deferred('local evidence directory is unavailable')
    folder = Path(folder).expanduser().resolve()
    inputs = {kind: folder / f'{kind}-export.json' for kind in ('paper', 'weather')}
    if not all(path.is_file() for path in inputs.values()):
        raise Deferred('local evidence export is incomplete')
    return inputs


def collect_export(folder, state, config, budget):
    maximum = reserve_egress(state, config, datetime.now(timezone.utc))
    # The remote exporter aborts before printing oversized JSON. Half the
    # reservation covers payload; the other half covers protocol overhead.
    exporter = (REPO / 'scripts/local_compute/export_evidence.py').read_text()
    exporter = exporter.replace('MAX_OUTPUT_BYTES = 128 * 1024**2', f'MAX_OUTPUT_BYTES = {maximum // 2}')
    script = folder / 'export-source.py'
    script.write_text(exporter)
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
           '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
           '-i', config['ssh_key'], config['ssh_target'], 'python3 -']
    combined = folder / 'combined-export.json'
    with script.open('rb') as source, combined.open('wb') as out, (folder / 'export.stderr').open('w') as err:
        budget.run(ssh, stdin=source, stdout=out, stderr=err, timeout=180)
    if combined.stat().st_size > maximum // 2:
        raise ValueError('fresh export exceeded reserved payload ceiling')
    exported = json.loads(combined.read_bytes())
    for kind in ('paper', 'weather'):
        write_json(folder / f'{kind}-export.json', exported[kind])
    return {kind: folder / f'{kind}-export.json' for kind in ('paper', 'weather')}


def source_identity():
    paths = subprocess.check_output(['git', '-C', str(REPO), 'ls-files', '-z'], text=True).split('\0')
    paths += [str(p.relative_to(REPO)) for p in (REPO / 'scripts/local_compute').glob('*.py')]
    paths += [str(p.relative_to(REPO)) for p in (REPO / 'scripts').glob('*.py')]
    return {
        'source_commit': subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], text=True).strip(),
        'source_dirty': bool(subprocess.check_output(['git', '-C', str(REPO), 'status', '--porcelain'], text=True).strip()),
        'source_files_sha256': {name: file_hash(REPO / name) for name in sorted(set(paths)) if name.endswith('.py')},
    }


def raw_only_shadow_receipt(shadow, source, attempted_at, log_path):
    """Accept only this attempt's source-bound, unissued collection evidence."""
    path = shadow / 'latest-receipt.json'
    if path.stat().st_size > 1024**2 or log_path.stat().st_size > 16 * 1024:
        raise ValueError('raw-only completion evidence exceeds its bounded receipt/log size')
    raw = path.read_bytes()
    value = json.loads(raw)
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value.get('schema_version') != 1 or value.get('status') != 'no_issued_distribution'
            or value.get('research_identity') != 'local-shadow-v7-v1'
            or value.get('execution_location') != 'local_mac'
            or value.get('live_orders_enabled') is not False or value.get('promotion_eligible') is not False
            or type(value.get('new_issued_vintages')) is not int or value.get('new_issued_vintages') != 0
            or type(value.get('new_snapshot_ids')) is not list or value.get('new_snapshot_ids')
            or type(value.get('http_requests')) is not int or not 0 <= value.get('http_requests') <= 8
            or value.get('source_commit') != source['source_commit']
            or type(value.get('source_dirty')) is not bool or value.get('source_dirty') != source['source_dirty']):
        raise ValueError('raw-only collector exception lacks the exact research receipt contract')
    clocks = []
    for key in ('started_at', 'finished_at'):
        token = value.get(key)
        if not isinstance(token, str):
            raise ValueError('raw-only collection requires fresh explicit clocks')
        stamp = datetime.fromisoformat(token.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('raw-only collection clocks require timezones')
        clocks.append(stamp.astimezone(timezone.utc))
    if not attempted_at <= clocks[0] <= clocks[1] <= datetime.now(timezone.utc):
        raise ValueError('raw-only receipt is stale, reversed or outside this collection attempt')
    if value.get('weather_export_sha256') != file_hash(shadow / 'weather-export.json'):
        raise ValueError('raw-only receipt does not bind the current weather export')
    # collect() writes its receipt before its final rotation write. main() emits
    # this single exact summary only after collect returns successfully, so a
    # post-receipt crash cannot masquerade as raw-only completion.
    lines = log_path.read_text().splitlines()
    keys = ('status', 'research_identity', 'new_issued_vintages', 'http_requests',
            'live_orders_enabled', 'promotion_eligible')
    if len(lines) != 1:
        raise ValueError('raw-only child lacks an unambiguous final completion summary')
    printed = json.loads(lines[0])
    if (not isinstance(printed, dict) or set(printed) != set(keys)
            or any(type(printed[key]) is not type(value[key]) or printed[key] != value[key] for key in keys)):
        raise ValueError('raw-only child completion summary does not match its receipt')
    return value, hashlib.sha256(raw).hexdigest()


def run(config, *, checks_only=False, scheduled=False):
    # Revalidate each installed invocation. Editing private configuration after
    # installation cannot weaken the more frequent background run's ceilings.
    limits = scheduled_policy(config) if scheduled else policy(config)
    state = Path(config['state_dir']).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state, 0o700)
    with (state / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        clock = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        receipt = {'schema_version': 2, 'started_at': clock, 'status': 'running',
                   'purpose': 'offline diagnostics; no policy promotion or profit inference',
                   'python_version': sys.version, 'live_orders_enabled': False}
        folder = None
        budget = Budget(state, limits)
        try:
            receipt['host_at_start'] = host_snapshot(state, limits)
            if checks_only:
                print(json.dumps({'status': 'admitted', 'host': receipt['host_at_start'], 'limits': limits}))
                return 0
            if hasattr(os, 'nice'):
                os.nice(10)
            original_source = source_identity()
            receipt.update(original_source)
            if config.get('fresh_export') is True:
                folder = state / 'runs' / clock
                folder.mkdir(parents=True, mode=0o700)
                inputs = collect_export(folder, state, config, budget)
                receipt['evidence_mode'] = 'fresh_read_only_export'
            else:
                inputs = offline_inputs(state, config)
                receipt['evidence_mode'] = 'retained_offline_export'
            receipt['input_directory'] = str(inputs['paper'].parent)
            receipt['retained_input_sha256'] = {kind: file_hash(path) for kind, path in inputs.items()}
            if config.get('local_prospective_collection') is True:
                cities = config.get('shadow_cities', ['sfo'])
                if (not isinstance(cities, list) or not 1 <= len(cities) <= 4 or
                        any(not isinstance(city, str) or not city.isalpha() for city in cities)):
                    raise ValueError('local shadow collection requires one to four city slugs')
                if folder is None:
                    folder = state / 'runs' / clock
                    folder.mkdir(parents=True, mode=0o700)
                shadow = state / 'prospective'
                command = [sys.executable, str(REPO / 'scripts/local_compute/shadow_collect.py'),
                           '--seed-export', str(inputs['weather']), '--state-dir', str(shadow),
                           '--cities', ','.join(cities), '--maximum-cities', '4',
                           '--maximum-http-requests', '8'] + \
                          (['--rotate-registry'] if config.get('shadow_rotate_registry') is True else [])
                attempted_at = datetime.now(timezone.utc)
                shadow_value = None
                with (folder / 'shadow-collection.log').open('w') as log:
                    try:
                        budget.run(command, stdout=log, stderr=subprocess.STDOUT)
                    except subprocess.CalledProcessError as error:
                        if error.returncode != 1 or error.cmd != command:
                            raise
                        # The unchanged collector uses exit 1 for a completed
                        # raw-only batch. Crashes, stale output and other child
                        # failures cannot borrow that exception.
                        log.flush()
                        shadow_value, shadow_hash = raw_only_shadow_receipt(
                            shadow, original_source, attempted_at, folder / 'shadow-collection.log')
                shadow_receipt = shadow / 'latest-receipt.json'
                if shadow_value is None:
                    raw = shadow_receipt.read_bytes()
                    shadow_value, shadow_hash = json.loads(raw), hashlib.sha256(raw).hexdigest()
                receipt['local_shadow_receipt_sha256'] = shadow_hash
                receipt['local_shadow_status'] = shadow_value.get('status')
                receipt['local_shadow_new_issued_vintages'] = shadow_value.get('new_issued_vintages')
                receipt['local_shadow_scope'] = 'separate Mac weather evidence; no AWS ledger or trading change'
                inputs['weather'] = shadow / 'weather-export.json'
                receipt['evidence_mode'] = 'retained_aws_paper_and_local_prospective_weather'
            receipt['input_sha256'] = {kind: file_hash(path) for kind, path in inputs.items()}
            previous = json.loads((state / 'latest-success.json').read_text()) if (state / 'latest-success.json').exists() else {}
            receipt['new_evidence'] = receipt['input_sha256'] != previous.get('input_sha256')
            if not receipt['new_evidence'] and receipt['source_files_sha256'] == previous.get('source_files_sha256'):
                receipt.update(status='skipped', reason='same retained evidence and Python source already completed')
                write_json(state / 'status.json', receipt)
                return 0
            if folder is None:
                folder = state / 'runs' / clock
                folder.mkdir(parents=True, mode=0o700)
            write_json(state / 'status.json', receipt)
            jobs = [('audit_paper_performance.py', 'paper', []),
                    ('audit_forecast_evidence.py', 'weather', []),
                    ('evaluate_v7_bias_pilot.py', 'weather', ['--repo', str(REPO)]),
                    ('evaluate_v7_ml.py', 'weather',
                     ['--lineage-output', str(folder / 'evaluate_v7_ml.lineage.jsonl.gz')] +
                     (['--with-boosting'] if config.get('ml_boosting') is True else []))]
            receipt['artifacts'] = {}
            for script, kind, extra in jobs:
                output = folder / (script.removesuffix('.py') + '.json')
                with (folder / (script + '.log')).open('w') as log:
                    budget.run([sys.executable, str(REPO / 'scripts' / script),
                                '--export', str(inputs[kind]), '--output', str(output), *extra],
                               stdout=log, stderr=subprocess.STDOUT)
                receipt['artifacts'][output.name] = file_hash(output)
            lineage = folder / 'evaluate_v7_ml.lineage.jsonl.gz'
            receipt['artifacts'][lineage.name] = file_hash(lineage)
            if {kind: file_hash(path) for kind, path in inputs.items()} != receipt['input_sha256']:
                raise ValueError('evidence inputs changed during analysis; no completed receipt can be promoted')
            if source_identity() != original_source:
                raise ValueError('Python source or Git identity changed during analysis; retry from stable source')
            receipt.update(status='complete', finished_at=datetime.now(timezone.utc).isoformat(),
                           run_directory=str(folder), resources=budget.receipt())
            write_json(folder / 'receipt.json', receipt)
            write_json(state / 'latest-success.json', receipt)
        except Exception as error:
            status = 'deferred' if isinstance(error, Deferred) else 'failed'
            receipt.update(status=status, error_type=type(error).__name__,
                           finished_at=datetime.now(timezone.utc).isoformat(), resources=budget.receipt())
            if isinstance(error, Deferred):
                receipt['reason'] = str(error)
            if folder is not None:
                failure = folder / 'error.txt'
                failure.write_text(traceback.format_exc())
                os.chmod(failure, 0o600)
                write_json(folder / 'receipt.json', receipt)
            write_json(state / 'status.json', receipt)
            return 0 if status == 'deferred' else 1
        write_json(state / 'status.json', receipt)
        return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--checks-only', action='store_true')
    parser.add_argument('--scheduled', action='store_true',
                        help='require the reviewed two-hour background resource budgets')
    args = parser.parse_args()
    if args.config.stat().st_mode & 0o077:
        parser.error('private configuration must have mode 0600')
    sys.exit(run(json.loads(args.config.read_text()), checks_only=args.checks_only,
                 scheduled=args.scheduled))
