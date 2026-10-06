#!/usr/bin/env python3
"""Run the actual full Strategy builder on verified, isolated local snapshots.

No uploader, SSH, public publisher, collector or order command is invoked.
The manifest and detailed receipt are private operator state.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from guards import Budget, host_snapshot, policy
from worker import file_hash, source_identity, write_json

REPO = Path(__file__).resolve().parents[2]
ALLOWED_JSON = {'build_info.json', 'trading_signal.json', 'forecast_data.json',
                'dataset_research.json', 'ab_test_results.json', 'prediction_replay.json'}
ALLOWED_CONFIG = {'PAPER_BANKROLL', 'PAPER_ENTRY_MODE', 'PAPER_RISK_PROFILE'}


def clone_snapshot(source, destination):
    """Require a local APFS copy-on-write clone; never fall back to a full copy."""
    if destination.exists():
        raise ValueError('working snapshot destination already exists')
    subprocess.run(['/bin/cp', '-c', str(source), str(destination)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    os.chmod(destination, 0o600)
    if os.path.samefile(source, destination):
        raise ValueError('working snapshot must have a separate file identity')


def verified_snapshot(record):
    path = Path(record['path']).expanduser().resolve()
    receipt = json.loads(Path(record['receipt']).expanduser().read_text())
    if receipt.get('integrity_check') != 'ok' or receipt.get('foreign_key_check') != 'ok':
        raise ValueError('snapshot lacks a successful integrity and foreign-key receipt')
    if receipt.get('bytes') != path.stat().st_size or receipt.get('sha256') != record['sha256']:
        raise ValueError('snapshot receipt does not match the manifest')
    if any(Path(str(path) + suffix).exists() and Path(str(path) + suffix).stat().st_size > 0
           for suffix in ('-wal', '-journal')):
        raise ValueError('snapshot has a live transaction sidecar')
    if file_hash(path) != record['sha256']:
        raise ValueError('snapshot content differs from its verified receipt')
    return path


def validate_manifest(manifest):
    source = subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = bool(subprocess.check_output(['git', '-C', str(REPO), 'status', '--porcelain'], text=True).strip())
    if source != manifest.get('target_source_sha'):
        raise ValueError('local source does not match the planned analysis source')
    if dirty and manifest.get('allow_dirty_diagnostics') is not True:
        raise ValueError('clean source required for a promotable analysis')
    values = manifest.get('runtime_config', {})
    if not isinstance(values, dict) or set(values) - ALLOWED_CONFIG:
        raise ValueError('only the documented non-secret strategy settings are allowed')
    if values.get('PAPER_ENTRY_MODE') not in ('market', 'limit'):
        raise ValueError('exact deployed entry mode is required')
    if values.get('PAPER_RISK_PROFILE', 'live') != 'live':
        raise ValueError('full production cache is bound to the live readiness profile')
    bankroll = values.get('PAPER_BANKROLL')
    if isinstance(bankroll, bool) or not isinstance(bankroll, (int, float)) or not 0 < bankroll < 1e9:
        raise ValueError('exact paper configuration bankroll is required')
    files = manifest.get('forecaster_files', {})
    if set(files) - ALLOWED_JSON or not {'build_info.json', 'trading_signal.json'} <= set(files):
        raise ValueError('forecaster input allowlist or required provenance is invalid')
    for record in files.values():
        if file_hash(Path(record['path']).expanduser()) != record['sha256']:
            raise ValueError('forecaster input checksum mismatch')
    build = json.loads(Path(files['build_info.json']['path']).expanduser().read_text())
    if build.get('source_sha') != source or build.get('source_dirty') != dirty:
        raise ValueError('analysis build_info must describe actual local source')
    return source, dirty, values


def execute(manifest_path, output):
    manifest_raw = manifest_path.read_bytes()
    manifest_hash = hashlib.sha256(manifest_raw).hexdigest()
    manifest = json.loads(manifest_raw)
    before = source_identity()
    source, dirty, settings = validate_manifest(manifest)
    paper = verified_snapshot(manifest['paper_snapshot'])
    weather = verified_snapshot(manifest['weather_snapshot'])
    if output.exists():
        raise ValueError('choose a new analysis output directory')
    output.mkdir(parents=True, mode=0o700)
    stage = output / 'forecaster'
    stage.mkdir(mode=0o700)
    clone_snapshot(paper, output / 'paper-working.db')
    clone_snapshot(weather, stage / 'weather.db')
    for clone, record in ((output / 'paper-working.db', manifest['paper_snapshot']),
                          (stage / 'weather.db', manifest['weather_snapshot'])):
        if file_hash(clone) != record['sha256']:
            raise ValueError('working clone differs from the verified immutable input')
    for name, record in manifest['forecaster_files'].items():
        shutil.copyfile(Path(record['path']).expanduser(), stage / name)
        if file_hash(stage / name) != record['sha256']:
            raise ValueError('staged forecaster input differs from the approved manifest')
    # The builder is entirely local. Fail even if a future diagnostic helper
    # accidentally attempts a network request. No cloud credential is needed.
    def deny_network(event, args):
        if event in ('socket.connect', 'socket.connect_ex', 'socket.sendto', 'socket.getaddrinfo'):
            raise RuntimeError('offline full analysis forbids network access')
    sys.addaudithook(deny_network)
    for key in list(os.environ):
        if key.startswith('PAPER_') or key.startswith('SFO_'):
            del os.environ[key]
    os.environ.update({key: str(value) for key, value in settings.items()})
    os.environ['SFO_STRATEGY_FAST_PUBLICATION'] = '0'
    os.environ['SFO_STRATEGY_BUILD_STAGING'] = '1'
    sys.path.insert(0, str(REPO / 'trading'))
    from sfo_kalshi_quant.strategy_lab.build import (
        _analysis_config_fingerprint, build_strategy_research, write_strategy_research)
    minimum = manifest.get('calibration_min_train', 180)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum <= 0:
        raise ValueError('calibration training threshold must be a positive integer')
    fingerprint = _analysis_config_fingerprint(calibration_min_train=minimum)
    if fingerprint != manifest.get('expected_config_fingerprint'):
        raise ValueError('local and approved strategy configuration fingerprints differ')
    payload = build_strategy_research(forecaster_root=stage, db_path=output / 'paper-working.db',
                                      calibration_min_train=minimum)
    write_strategy_research(stage / 'strategy_research.json', payload)
    artifacts = {}
    for name in ('strategy_analysis_cache.json', 'strategy_research_evidence.private.json', 'strategy_research.json'):
        path = stage / name
        if not path.is_file():
            raise ValueError('full analysis did not produce the required artifact')
        artifacts[name] = file_hash(path)
    cache = json.loads((stage / 'strategy_analysis_cache.json').read_text())
    evidence = json.loads((stage / 'strategy_research_evidence.private.json').read_text())
    expected_source = source + (':dirty' if dirty else '')
    if cache.get('source_sha') != expected_source or cache.get('config_fingerprint') != fingerprint:
        raise ValueError('analysis output source/configuration mismatch')
    if evidence.get('source_sha') != expected_source or evidence.get('config_fingerprint') != fingerprint:
        raise ValueError('private replay evidence source/configuration mismatch')
    if evidence.get('analysis_generated_at') != cache.get('analysis_generated_at'):
        raise ValueError('cache and private replay evidence disagree on analysis time')
    if source_identity() != before:
        raise ValueError('source changed during full analysis; no completed receipt can be promoted')
    if file_hash(manifest_path) != manifest_hash:
        raise ValueError('input manifest changed during full analysis')
    write_json(output / 'analysis-receipt.json', {
        'schema_version': 1, 'status': 'complete', 'source_sha': expected_source,
        'config_fingerprint': fingerprint, 'manifest_sha256': manifest_hash,
        'input_sha256': {'paper': manifest['paper_snapshot']['sha256'],
                         'weather': manifest['weather_snapshot']['sha256'],
                         **{name: row['sha256'] for name, row in manifest['forecaster_files'].items()}},
        'analysis_generated_at': cache.get('analysis_generated_at'), 'artifacts': artifacts,
        'source_files_sha256': before['source_files_sha256'],
        'promotion_eligible': False, 'clean_source': not dirty,
        'cache_validated': True, 'live_orders_enabled': False,
        'scope': 'offline diagnostic cache; publication requires deployed source/config/input gates',
    })
    # Disposable COW working clones can grow after schema/index changes. Remove
    # exactly our staged clones after a successful receipt, preserving originals.
    for path in (output / 'paper-working.db', stage / 'weather.db'):
        for suffix in ('', '-wal', '-shm', '-journal'):
            Path(str(path) + suffix).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--execute', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.execute:
        if os.environ.get('WEATHEREDGE_LOCAL_BUDGET_ACTIVE') != '1':
            raise SystemExit('full analysis execution must be launched through its resource budget')
        execute(args.manifest.resolve(), args.output.resolve())
        return
    manifest = json.loads(args.manifest.read_text())
    limits = policy(manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = Path(manifest['state_dir']).expanduser().resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('another local research job is active; full analysis deferred')
        host_snapshot(state, limits)
        budget = Budget(state, limits)
        log = args.output.with_suffix('.log')
        with log.open('w') as stream:
            budget.run([sys.executable, str(Path(__file__).resolve()), '--manifest', str(args.manifest.resolve()),
                        '--output', str(args.output.resolve()), '--execute'], stdout=stream, stderr=subprocess.STDOUT)
        receipt = args.output / 'analysis-receipt.json'
        value = json.loads(receipt.read_text())
        value['resources'] = budget.receipt()
        write_json(receipt, value)
    print('Full Strategy analysis completed locally; no publication or policy changed.')


if __name__ == '__main__':
    main()
