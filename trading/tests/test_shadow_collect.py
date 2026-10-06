"""Real V7 issuance and resource/provenance boundaries for the local shadow."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

spec = importlib.util.spec_from_file_location('shadow_collect',
    Path(__file__).resolve().parents[2] / 'scripts/local_compute/shadow_collect.py')
shadow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shadow)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, 'ROOT', tmp_path)
    identity = {'source_commit': 'a' * 40, 'source_dirty': False,
                'implementation_sha256': {'fixture.py': 'b' * 64}}
    monkeypatch.setattr(shadow, 'source_identity', lambda: identity)
    captured = datetime.now(timezone.utc) - timedelta(minutes=5)
    truth, nwp = [], []
    for index in range(120):
        target = (date(2024, 1, 1) + timedelta(days=index)).isoformat()
        actual = 62 + index % 11
        truth.append({'station_id': 'KSFO', 'local_date': target,
            'max_temperature_f': actual, 'fetched_at': captured.isoformat(),
            'source': 'nws_cli', 'is_final': 1})
        for lead in (1, 2):
            for model_index, model in enumerate(shadow.nwp_archive.NWP_MODELS):
                nwp.append({'station_id': 'KSFO', 'target_date': target, 'model': model,
                    'lead_days': lead, 'predicted_high_f': actual + model_index * .3 + index % 3 * .2,
                    'fetched_at': captured.isoformat(), 'source': 'openmeteo_previous_runs'})
    seed = {'captured_at': captured.isoformat(), 'nwp_model_forecasts': {'rows': nwp, 'truncated': False},
        'cli_settlements': {'rows': truth, 'truncated': False},
        'forecast_emos_daily_high': {'rows': [], 'truncated': False}}
    seed_path = tmp_path / 'seed.json'
    seed_path.write_text(json.dumps(seed))
    return seed_path, tmp_path / '.local' / 'prospective', shadow.parse_city_slugs('sfo')


class FixtureClient:
    def __init__(self, *, wrong_timezone=False, wrong_units=False, wrong_station=False, preliminary=False):
        self.used, self.urls = 0, []
        self.wrong_timezone, self.wrong_units = wrong_timezone, wrong_units
        self.wrong_station, self.preliminary = wrong_station, preliminary

    def get(self, url):
        self.used += 1
        self.urls.append(url)
        now = datetime.now(timezone.utc)
        today = now.astimezone(shadow.parse_city_slugs('sfo')[0].fixed_standard_timezone()).date()
        if url.startswith('https://api.open-meteo.com/'):
            daily = {'time': [(today + timedelta(days=i)).isoformat() for i in range(3)]}
            units = {}
            for i, model in enumerate(shadow.nwp_archive.NWP_MODELS):
                key = 'temperature_2m_max_' + model
                daily[key] = [68 + i * .3, 69 + i * .3, 70 + i * .3]
                units[key] = '°C' if self.wrong_units else '°F'
            raw = json.dumps({'utc_offset_seconds': 0 if self.wrong_timezone else -28800,
                              'daily': daily, 'daily_units': units}).encode()
        else:
            token = 'CLILAX' if self.wrong_station else 'CLISFO'
            preliminary = 'AS OF 500 PM\n' if self.preliminary else ''
            raw = (f'{token}\n{preliminary}CLIMATE SUMMARY FOR '
                   f'{(today - timedelta(days=1)).strftime("%B %d %Y").upper()}\n'
                   'TEMPERATURE (F)\nMAXIMUM 71 1259 PM\n').encode()
        stamp = now.isoformat(timespec='microseconds')
        return raw, {'url': url, 'request_started_at': stamp, 'retrieved_at': stamp,
                     'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def test_real_serving_preserves_originals_and_explicit_shadow_lineage(workspace):
    seed, state, cities = workspace
    first = shadow.collect(seed, state, cities, client=FixtureClient())
    assert first['new_issued_vintages'] == 3 and first['http_requests'] == 2
    assert first['cities'] == ['sfo'] and first['source_registry_cities'] == 20
    assert first['research_identity'] == 'local-shadow-v7-v1'
    assert not first['live_orders_enabled'] and not first['promotion_eligible']
    exported = json.loads((state / 'weather-export.json').read_text())
    assert len(exported['forecast_emos_live_vintages']['rows']) == 3
    assert {row['lead_days'] for row in exported['forecast_emos_live_vintages']['rows']} == {0, 1, 2}
    for row in exported['forecast_emos_live_vintages']['rows']:
        assert row['provider_initialized_at'] is None and row['retrieved_at'] <= row['recorded_at']
        assert row['training_truth_end'].startswith('2024-')
    assert len(exported['nwp_live_forecast_members']['rows']) == 3 * len(shadow.nwp_archive.NWP_MODELS)
    assert all(row['complete_hour_count'] is None for row in exported['nwp_live_forecast_members']['rows'])
    for row in exported['shadow_forecast_lineage']['rows']:
        fields = {key: row[key] for key in ('snapshot_id', 'research_identity', 'execution_location',
                                           'model_policy_fingerprint_sha256')}
        assert row['lineage_sha256'] == shadow.sha(shadow.canonical(fields).encode())
    evaluator_spec = importlib.util.spec_from_file_location('shadow_evaluator_contract',
        Path(__file__).resolve().parents[2] / 'scripts/evaluate_v7_ml.py')
    evaluator = importlib.util.module_from_spec(evaluator_spec)
    evaluator_spec.loader.exec_module(evaluator)
    audited = evaluator.audit_issued_vintages(exported)
    assert audited['available'] and audited['exported_vintages'] == 3
    assert audited['cases'] == 0 and not audited['promotion_eligible']
    assert audited['excluded']['unsupported_lead_or_unfinished_target'] == 3
    second = shadow.collect(seed, state, cities, client=FixtureClient())
    assert second['new_issued_vintages'] == 3
    with sqlite3.connect(state / 'shadow-weather.db') as conn:
        assert conn.execute('SELECT COUNT(*) FROM forecast_emos_live_vintages').fetchone()[0] == 6
        assert conn.execute('SELECT COUNT(*) FROM shadow_http_evidence').fetchone()[0] == 4
        for table in ('forecast_emos_live_vintages', 'nwp_live_forecast_members',
                      'shadow_http_evidence', 'shadow_forecast_lineage'):
            with pytest.raises(sqlite3.IntegrityError, match='append-only'):
                conn.execute(f'DELETE FROM {table}')
            with pytest.raises(sqlite3.IntegrityError, match='append-only'):
                conn.execute(f'INSERT OR REPLACE INTO {table} SELECT * FROM {table} LIMIT 1')


@pytest.mark.parametrize('option', ['wrong_timezone', 'wrong_units'])
def test_ambiguous_forecast_remains_raw_and_cannot_issue(workspace, option):
    seed, state, cities = workspace
    result = shadow.collect(seed, state, cities, client=FixtureClient(**{option: True}))
    assert result['new_issued_vintages'] == 0
    assert result['status'] == 'no_issued_distribution'
    with sqlite3.connect(state / 'shadow-weather.db') as conn:
        assert conn.execute('SELECT COUNT(*) FROM shadow_http_evidence').fetchone()[0] == 2


@pytest.mark.parametrize('option', ['wrong_station', 'preliminary'])
def test_station_or_preliminary_report_never_becomes_final(workspace, option):
    seed, state, cities = workspace
    result = shadow.collect(seed, state, cities, client=FixtureClient(**{option: True}))
    assert result['new_issued_vintages'] == 3
    with sqlite3.connect(state / 'shadow-weather.db') as conn:
        assert conn.execute("SELECT COUNT(*) FROM cli_settlements WHERE local_date>'2024-12-31' AND is_final=1").fetchone()[0] == 0


def test_seed_never_imports_or_relabels_old_original_vintages(workspace):
    seed, state, cities = workspace
    data = json.loads(seed.read_text())
    data['forecast_emos_live_vintages'] = {'rows': [{'snapshot_id': 'invented old original'}]}
    seed.write_text(json.dumps(data))
    result = shadow.collect(seed, state, cities, client=FixtureClient(wrong_units=True))
    assert result['new_issued_vintages'] == 0
    assert json.loads((state / 'weather-export.json').read_text())['forecast_emos_live_vintages']['rows'] == []


def test_unfinished_final_seed_is_rejected_before_network(workspace):
    seed, state, cities = workspace
    data = json.loads(seed.read_text())
    data['cli_settlements']['rows'][0]['local_date'] = '2099-01-01'
    seed.write_text(json.dumps(data))
    client = FixtureClient()
    with pytest.raises(ValueError, match='unfinished'):
        shadow.collect(seed, state, cities, client=client)
    assert client.used == 0


@pytest.mark.parametrize('field', ['station_id', 'source', 'target_date', 'model', 'lead_days', 'fetched_at'])
def test_seed_defaults_cannot_invent_missing_provenance(workspace, field):
    seed, state, cities = workspace
    data = json.loads(seed.read_text())
    del data['nwp_model_forecasts']['rows'][0][field]
    seed.write_text(json.dumps(data))
    client = FixtureClient()
    with pytest.raises(ValueError, match='explicit.*provenance'):
        shadow.collect(seed, state, cities, client=client)
    assert client.used == 0


def test_seed_mutation_is_rejected_before_network_and_rolls_back(workspace, monkeypatch):
    seed, state, cities = workspace
    original_digest = shadow.hashlib.file_digest
    def changed(stream, algorithm):
        seed.write_text('{}')
        stream.seek(0)
        return original_digest(stream, algorithm)
    monkeypatch.setattr(shadow.hashlib, 'file_digest', changed)
    client = FixtureClient()
    with pytest.raises(ValueError, match='seed changed'):
        shadow.collect(seed, state, cities, client=client)
    assert client.used == 0
    with sqlite3.connect(state / 'shadow-weather.db') as conn:
        assert conn.execute('SELECT COUNT(*) FROM nwp_model_forecasts').fetchone()[0] == 0


def test_later_seed_replacement_does_not_relabel_imported_history(workspace):
    seed, state, cities = workspace
    first = shadow.collect(seed, state, cities, client=FixtureClient())
    seed.write_text('{}')
    second = shadow.collect(seed, state, cities, client=FixtureClient())
    assert second['seed'] == first['seed']


def test_docs_only_git_change_preserves_model_cohort_but_serving_change_rotates_it(workspace, monkeypatch):
    seed, state, cities = workspace
    original = shadow.source_identity()
    first = shadow.collect(seed, state, cities, client=FixtureClient())
    docs_only = dict(original, source_commit='c' * 40, source_dirty=True)
    monkeypatch.setattr(shadow, 'source_identity', lambda: docs_only)
    second = shadow.collect(seed, state, cities, client=FixtureClient())
    assert second['model_policy_fingerprint_sha256'] == first['model_policy_fingerprint_sha256']
    assert second['source_commit'] != first['source_commit']
    serving_change = dict(docs_only, implementation_sha256={'fixture.py': 'd' * 64})
    monkeypatch.setattr(shadow, 'source_identity', lambda: serving_change)
    third = shadow.collect(seed, state, cities, client=FixtureClient())
    assert third['model_policy_fingerprint_sha256'] != second['model_policy_fingerprint_sha256']
    with sqlite3.connect(state / 'shadow-weather.db') as conn:
        lineage = {row[0]: json.loads(row[1]) for row in conn.execute(
            'SELECT snapshot_id,policy_json FROM shadow_forecast_lineage')}
        assert lineage[first['new_snapshot_ids'][0]]['source_commit'] == original['source_commit']
        assert lineage[second['new_snapshot_ids'][0]]['source_commit'] == docs_only['source_commit']
        assert lineage[second['new_snapshot_ids'][0]]['source_dirty'] is True
        assert conn.execute('SELECT COUNT(*) FROM forecast_emos_live_vintages').fetchone()[0] == 9
    evaluator_spec = importlib.util.spec_from_file_location('shadow_docs_commit_contract',
        Path(__file__).resolve().parents[2] / 'scripts/evaluate_v7_ml.py')
    evaluator = importlib.util.module_from_spec(evaluator_spec)
    evaluator_spec.loader.exec_module(evaluator)
    audit = evaluator.audit_issued_vintages(json.loads((state / 'weather-export.json').read_text()))
    assert audit['exported_vintages'] == 9 and not audit['promotion_eligible']


def test_behavior_policy_seed_and_models_remain_bound_to_cohort():
    original = {'source_commit': 'a' * 40, 'source_dirty': False,
        'implementation_sha256': {'serving.py': 'b' * 64}, 'seed_sha256': 'c' * 64,
        'models': ['gfs_seamless'], 'method': 'v7 inverse-variance EMOS'}
    fingerprint = shadow.model_policy_fingerprint(original)
    assert shadow.model_policy_fingerprint(dict(original, source_commit='d' * 40, source_dirty=True)) == fingerprint
    for change in ({'method': 'different serve correction'}, {'models': ['icon_seamless']},
                   {'seed_sha256': 'e' * 64}, {'implementation_sha256': {'serving.py': 'f' * 64}}):
        assert shadow.model_policy_fingerprint(dict(original, **change)) != fingerprint


def test_source_change_does_not_promote_receipt(workspace, monkeypatch):
    seed, state, cities = workspace
    identity = shadow.source_identity()
    states = iter([identity, dict(identity, source_commit='c' * 40)])
    monkeypatch.setattr(shadow, 'source_identity', lambda: next(states))
    with pytest.raises(ValueError, match='source changed'):
        shadow.collect(seed, state, cities, client=FixtureClient())
    assert not (state / 'latest-receipt.json').exists()


def test_rotation_keeps_four_city_eight_request_ceiling_and_persists_cursor(workspace):
    seed, state, _ = workspace
    cities = shadow.CITIES[:4]
    client = FixtureClient()
    first = shadow.collect(seed, state, cities, client=client, rotate_registry=True)
    assert first['cities'] == [city.slug for city in shadow.CITIES[:4]]
    assert client.used == 8
    second = shadow.collect(seed, state, cities, client=FixtureClient(), rotate_registry=True)
    assert second['cities'] == [city.slug for city in shadow.CITIES[4:8]]
    assert json.loads((state / 'rotation.json').read_text())['next_city_index'] == 8
    assert all(url.startswith(('https://api.open-meteo.com/', 'https://forecast.weather.gov/')) for url in client.urls)


def test_shadow_destination_cannot_mutate_canonical_or_symlinked_runtime(workspace):
    seed, state, cities = workspace
    with pytest.raises(ValueError, match='ignored .local'):
        shadow.collect(seed, state.parents[1] / 'forecaster', cities, client=FixtureClient())
    state.mkdir(parents=True)
    outside = state.parents[1] / 'canonical-weather.db'
    outside.write_bytes(b'unchanged')
    (state / 'shadow-weather.db').symlink_to(outside)
    with pytest.raises(ValueError, match='redirect'):
        shadow.collect(seed, state, cities, client=FixtureClient())
    assert outside.read_bytes() == b'unchanged'


@pytest.mark.parametrize('url', ['http://api.open-meteo.com/v1/forecast',
    'https://customer-api.open-meteo.com/v1/forecast', 'https://s3.amazonaws.com/',
    'https://user:password@api.open-meteo.com/v1/forecast', 'https://api.open-meteo.com:8443/'])
def test_network_disallows_paid_private_aws_or_insecure_transport(url):
    with pytest.raises(ValueError):
        shadow.validate_url(url)


def test_redirects_cannot_expand_request_budget():
    with pytest.raises(ValueError, match='redirects'):
        shadow.PublicOnlyRedirects().redirect_request(None, None, 302, None, None,
                                                    'https://api.open-meteo.com/v1/forecast')


def test_failed_requests_are_durably_counted_and_not_retried(tmp_path):
    class Fails:
        def open(self, *args, **kwargs):
            raise OSError('fixture provider unavailable')
    client = shadow.PublicClient(tmp_path, 1, opener=Fails())
    with pytest.raises(OSError):
        client.get('https://api.open-meteo.com/v1/forecast')
    assert json.loads((tmp_path / 'public-http-budget.json').read_text())['requests'] == 1
    with pytest.raises(RuntimeError, match='allowance'):
        client.get('https://api.open-meteo.com/v1/forecast')


def test_cli_requires_resource_budget_wrapper(monkeypatch):
    monkeypatch.delenv('WEATHEREDGE_LOCAL_BUDGET_ACTIVE', raising=False)
    monkeypatch.setattr('sys.argv', ['shadow_collect.py', '--seed-export', 'unused', '--state-dir', 'unused'])
    with pytest.raises(SystemExit) as error:
        shadow.main()
    assert error.value.code == 2
