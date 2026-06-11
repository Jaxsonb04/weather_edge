from __future__ import annotations

import base64
import hmac
import json
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "forecaster"))

import build_dashboard  # noqa: E402


def _decrypt_payload(payload: dict[str, object], password: str) -> bytes:
    iterations = int(payload["iterations"])
    salt = base64.b64decode(str(payload["salt"]))
    nonce = base64.b64decode(str(payload["nonce"]))
    ciphertext = base64.b64decode(str(payload["ciphertext"]))
    tag = base64.b64decode(str(payload["tag"]))
    stream_key, mac_key = build_dashboard.derive_strategy_lab_keys(
        password,
        salt,
        iterations,
    )
    expected = build_dashboard.hmac_sha256(
        mac_key,
        build_dashboard.MAC_LABEL
        + build_dashboard.strategy_lab_tag_message(iterations, salt, nonce, ciphertext),
    )
    assert hmac.compare_digest(tag, expected)
    return build_dashboard.xor_bytes(
        ciphertext,
        build_dashboard.strategy_lab_keystream(stream_key, nonce, len(ciphertext)),
    )


def _with_env(**updates):
    old_values = {key: os.environ.get(key) for key in updates}
    for key, value in updates.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return old_values


def _restore_env(old_values):
    for key, value in old_values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_strategy_lab_password_writes_protected_research_artifact():
    old_values = _with_env(
        **{
            build_dashboard.STRATEGY_LAB_PUBLIC_MODE_ENV: "0",
            build_dashboard.STRATEGY_LAB_PASSWORD_ENV: "unit-test-password",
            build_dashboard.STRATEGY_LAB_ITERATIONS_ENV: "100000",
        }
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "strategy_research.json"
            target = root / "strategy_research.protected.json"
            source.write_text('{"available":true,"paper_trading":{"open_positions":[1]}}\n')

            config = build_dashboard.protect_strategy_research(source, target)
            payload = json.loads(target.read_text())

            assert config == {
                "enabled": True,
                "artifact": "strategy_research.protected.json",
                "format": "encrypted-json-v1",
            }
            assert payload["cipher"] == "HMAC-SHA256-CTR-XOR"
            assert "open_positions" not in target.read_text()
            assert _decrypt_payload(payload, "unit-test-password") == source.read_bytes()
    finally:
        _restore_env(old_values)


def test_strategy_lab_public_mode_removes_stale_protected_artifact():
    old_values = _with_env(**{build_dashboard.STRATEGY_LAB_PASSWORD_ENV: None})
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "strategy_research.json"
            target = root / "strategy_research.protected.json"
            source.write_text('{"available":false}\n')
            target.write_text("stale")

            config = build_dashboard.protect_strategy_research(source, target)

            assert config == {
                "enabled": False,
                "artifact": "strategy_research.json",
                "format": "public-json",
            }
            assert not target.exists()
    finally:
        _restore_env(old_values)


def test_strategy_lab_temporary_public_mode_overrides_password():
    old_values = _with_env(
        **{
            build_dashboard.STRATEGY_LAB_PUBLIC_MODE_ENV: "1",
            build_dashboard.STRATEGY_LAB_PASSWORD_ENV: "unit-test-password",
        }
    )
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "strategy_research.json"
            target = root / "strategy_research.protected.json"
            source.write_text('{"available":true}\n')
            target.write_text("stale")

            config = build_dashboard.protect_strategy_research(source, target)

            assert config == {
                "enabled": False,
                "artifact": "strategy_research.json",
                "format": "public-json",
            }
            assert not target.exists()
    finally:
        _restore_env(old_values)
