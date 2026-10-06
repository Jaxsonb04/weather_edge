"""Public summary accepts only bounded, paired, dated local collection evidence."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/build_hybrid_research_summary.py'
spec = importlib.util.spec_from_file_location('hybrid_summary', SCRIPT)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.fixture(autouse=True)
def fixed_publication_clock(monkeypatch):
    monkeypatch.setattr(builder, '_utc_now', lambda: datetime(2026, 10, 6, 7, tzinfo=timezone.utc))


def report():
    return {'schema_version': 1, 'active': False, 'promotion_eligible': False,
        'evidence_scope': 'exploratory_reconstructed_walk_forward_weather_skill',
        'captured_at': '2026-10-06T05:43:51+00:00',
        'retrospective': {'cases': 6656, 'distinct_calendar_targets': 246,
            'arms': {'existing_bias_only': {'crps_f': 1.2944, 'mean_pinball_f': .4683},
                     'minimum_crps_location_scale': {'crps_f': 1.3090},
                     'gradient_boosted_quantiles': {'mean_pinball_f': .4741}}}}


def receipts():
    collector = {'schema_version': 1, 'status': 'complete',
        'research_identity': 'local-shadow-v7-v1', 'execution_location': 'local_mac',
        'started_at': '2026-10-06T06:45:00.100000+00:00',
        'finished_at': '2026-10-06T06:45:04.000000+00:00',
        'new_issued_vintages': 12, 'http_requests': 8,
        'maximum_http_requests': 8, 'maximum_runtime_seconds': 300,
        'live_orders_enabled': False, 'promotion_eligible': False,
        'private_path': '/private/SECRET-NOT-FOR-PUBLIC',
        'policy_json': {'access_config': 'SECRET-NOT-FOR-PUBLIC'}}
    worker = {'schema_version': 2, 'status': 'complete',
        'evidence_mode': 'retained_aws_paper_and_local_prospective_weather',
        'started_at': '20261006T064500000000Z',
        'finished_at': '2026-10-06T06:45:20.000000+00:00',
        'live_orders_enabled': False, 'run_directory': '/private/SECRET-NOT-FOR-PUBLIC',
        'source_files_sha256': {'/private/SECRET-NOT-FOR-PUBLIC': 'x'},
        'resources': {'elapsed_seconds': 20.013, 'observed_cpu_seconds': 8.6,
            'maximum_group_rss_bytes': 384647168,
            'limits': {'wall_seconds': 1200, 'cpu_seconds': 600, 'cpu_fraction': .5,
                'rss_bytes': 2 * 1024**3, 'maximum_load_per_cpu': .25,
                'minimum_free_memory_percent': 25, 'minimum_free_disk_bytes': 20 * 1024**3}}}
    return worker, collector


def encoded(worker, collector):
    collector_raw = json.dumps(collector, indent=2).encode()
    worker = deepcopy(worker)
    worker['local_shadow_receipt_sha256'] = hashlib.sha256(collector_raw).hexdigest()
    return json.dumps(worker, indent=2).encode(), collector_raw


def test_optional_collection_is_allowlisted_and_never_changes_historical_qualification():
    worker_raw, collector_raw = encoded(*receipts())
    result = builder.summary(report(), 'a' * 64, worker_receipt_raw=worker_raw,
                             collector_receipt_raw=collector_raw)
    collection = result['local_collection']
    assert set(collection) == {'finished_at', 'new_issued_forecasts', 'http_requests',
        'elapsed_seconds', 'observed_cpu_seconds', 'maximum_group_rss_bytes',
        'worker_receipt_sha256', 'collector_receipt_sha256'}
    assert collection['new_issued_forecasts'] == 12 and collection['http_requests'] == 8
    assert collection['worker_receipt_sha256'] == hashlib.sha256(worker_raw).hexdigest()
    assert collection['collector_receipt_sha256'] == hashlib.sha256(collector_raw).hexdigest()
    assert 'SECRET-NOT-FOR-PUBLIC' not in json.dumps(result)
    assert result['qualified_original_vintage_cases'] == 0
    assert result['evaluation_kind'] == 'retrospective_diagnostic'
    assert not result['active'] and not result['promotion_eligible']
    assert result['cases'] == 6656 and result['distinct_target_dates'] == 246
    assert 'local_collection' not in builder.summary(report(), 'a' * 64)


def test_pair_is_required_and_exact_collector_bytes_must_match():
    worker_raw, collector_raw = encoded(*receipts())
    for key in ('worker_receipt_raw', 'collector_receipt_raw'):
        with pytest.raises(ValueError, match='together'):
            builder.summary(report(), 'a' * 64, **{key: worker_raw})
    with pytest.raises(ValueError, match='digest'):
        builder.local_collection(worker_raw, collector_raw + b'\n')


@pytest.mark.parametrize('target,key,value', [
    ('worker', 'status', 'failed'), ('worker', 'status', 'running'),
    ('worker', 'live_orders_enabled', True), ('worker', 'promotion_eligible', True),
    ('worker', 'evidence_mode', 'retained_offline_export'),
    ('collector', 'status', 'no_issued_distribution'),
    ('collector', 'research_identity', 'aws-v7'), ('collector', 'execution_location', 'aws'),
    ('collector', 'live_orders_enabled', True), ('collector', 'promotion_eligible', True),
    ('collector', 'schema_version', True),
    ('collector', 'new_issued_vintages', -1), ('collector', 'new_issued_vintages', True),
    ('collector', 'new_issued_vintages', 13), ('collector', 'http_requests', 9),
    ('collector', 'http_requests', 0),
    ('collector', 'http_requests', True), ('collector', 'http_requests', float('nan')),
    ('collector', 'maximum_http_requests', 9), ('collector', 'maximum_runtime_seconds', 301),
    ('collector', 'finished_at', '2026-10-06T06:45:21+00:00'),
    ('collector', 'started_at', '2026-10-06T06:44:59+00:00'),
    ('collector', 'finished_at', '2026-10-06T06:45:04'),
    ('worker', 'started_at', '20991006T064500000000Z'),
    ('worker', 'finished_at', '2099-10-06T06:45:20+00:00'),
    ('collector', 'started_at', '2099-10-06T06:45:00+00:00'),
    ('collector', 'finished_at', '2099-10-06T06:45:04+00:00'),
])
def test_failed_unsafe_unbound_or_undated_receipts_are_rejected(target, key, value):
    worker, collector = receipts()
    (worker if target == 'worker' else collector)[key] = value
    with pytest.raises(ValueError):
        builder.local_collection(*encoded(worker, collector))


@pytest.mark.parametrize('key,value', [
    ('wall_seconds', 1201), ('cpu_seconds', 601), ('cpu_fraction', .51),
    ('rss_bytes', 2 * 1024**3 + 1), ('maximum_load_per_cpu', .3),
    ('minimum_free_memory_percent', 24), ('minimum_free_disk_bytes', 1),
    ('cpu_fraction', float('nan')), ('wall_seconds', float('inf')), ('rss_bytes', True),
])
def test_weakened_or_nonfinite_guard_contract_is_rejected(key, value):
    worker, collector = receipts()
    worker['resources']['limits'][key] = value
    with pytest.raises(ValueError):
        builder.local_collection(*encoded(worker, collector))


@pytest.mark.parametrize('key,value', [
    ('elapsed_seconds', 1201), ('elapsed_seconds', float('nan')),
    ('observed_cpu_seconds', 601), ('observed_cpu_seconds', -1),
    ('maximum_group_rss_bytes', 2 * 1024**3 + 1), ('maximum_group_rss_bytes', 2.5),
])
def test_observed_usage_must_be_finite_nonnegative_and_inside_guards(key, value):
    worker, collector = receipts()
    worker['resources'][key] = value
    with pytest.raises(ValueError):
        builder.local_collection(*encoded(worker, collector))


def test_cli_failed_pair_preserves_existing_public_artifact(tmp_path):
    worker, collector = receipts()
    worker['status'] = 'failed'
    worker_raw, collector_raw = encoded(worker, collector)
    inputs = {'report': json.dumps(report()).encode(), 'worker-receipt': worker_raw,
              'collector-receipt': collector_raw}
    paths = {}
    for name, raw in inputs.items():
        paths[name] = tmp_path / (name + '.json')
        paths[name].write_bytes(raw)
    output = tmp_path / 'public.json'
    output.write_bytes(b'{"previous":"verified"}\n')
    completed = subprocess.run([sys.executable, str(SCRIPT), '--report', str(paths['report']),
        '--worker-receipt', str(paths['worker-receipt']), '--collector-receipt', str(paths['collector-receipt']),
        '--output', str(output)], capture_output=True)
    assert completed.returncode != 0
    assert output.read_bytes() == b'{"previous":"verified"}\n'
    assert not output.with_suffix('.json.tmp').exists()


@pytest.mark.parametrize('captured', [
    {'private_path': '/private/SECRET-NOT-FOR-PUBLIC'}, ['SECRET-NOT-FOR-PUBLIC'],
    123, None, 'invalid', '2026-10-06T06:45:00', '2099-10-06T06:45:00+00:00',
])
def test_report_capture_cannot_copy_private_objects_or_unverified_clocks(captured):
    source = report()
    source['captured_at'] = captured
    with pytest.raises(ValueError):
        builder.summary(source, 'a' * 64)


@pytest.mark.parametrize('schema', [True, 1.0, '1'])
def test_report_schema_must_be_exact_integer_one(schema):
    source = report()
    source['schema_version'] = schema
    with pytest.raises(ValueError):
        builder.summary(source, 'a' * 64)


def test_report_timezone_is_normalized_without_changing_evidence_instant():
    source = report()
    source['captured_at'] = '2026-10-05T22:43:51-07:00'
    assert builder.summary(source, 'a' * 64)['captured_at'] == '2026-10-06T05:43:51+00:00'


def test_collector_timezone_is_normalized_without_copying_original_text():
    worker, collector = receipts()
    collector['started_at'] = '2026-10-05T23:45:00.100000-07:00'
    collector['finished_at'] = '2026-10-05T23:45:04.000000-07:00'
    result = builder.local_collection(*encoded(worker, collector))
    assert result['finished_at'] == '2026-10-06T06:45:04+00:00'


@pytest.mark.parametrize('digest', [
    {'private_path': '/private/SECRET-NOT-FOR-PUBLIC'}, ['SECRET-NOT-FOR-PUBLIC'],
    None, 1, 'a' * 63, 'a' * 65, 'A' * 64, 'g' * 64,
])
def test_report_digest_cannot_copy_private_objects_or_invalid_hashes(digest):
    with pytest.raises(ValueError, match='digest'):
        builder.summary(report(), digest)


def test_distinct_dates_cannot_exceed_case_count():
    source = report()
    source['retrospective']['cases'] = 245
    with pytest.raises(ValueError, match='exceed case count'):
        builder.summary(source, 'a' * 64)


def test_valid_form_future_receipt_pair_cannot_be_published():
    worker, collector = receipts()
    worker['started_at'] = '20991006T064500000000Z'
    worker['finished_at'] = '2099-10-06T06:45:20+00:00'
    collector['started_at'] = '2099-10-06T06:45:00+00:00'
    collector['finished_at'] = '2099-10-06T06:45:04+00:00'
    with pytest.raises(ValueError, match='future'):
        builder.local_collection(*encoded(worker, collector))


def test_cli_private_capture_payload_preserves_prior_artifact(tmp_path):
    source = report()
    source['captured_at'] = {'credentials': 'SECRET-NOT-FOR-PUBLIC'}
    input_path = tmp_path / 'report.json'
    input_path.write_text(json.dumps(source))
    output = tmp_path / 'public.json'
    previous = b'{"previous":"verified"}\n'
    output.write_bytes(previous)
    result = subprocess.run([sys.executable, str(SCRIPT), '--report', str(input_path),
                             '--output', str(output)], capture_output=True)
    assert result.returncode != 0 and output.read_bytes() == previous
    assert not output.with_suffix('.json.tmp').exists()
