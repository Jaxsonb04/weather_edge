#!/usr/bin/env python3
"""Generate the static GitHub Pages dashboard.

HTML/CSS/JS now lives in ``templates/`` so this module only handles data
preparation and token substitution (see docs/dashboard_direction.md).
"""

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dashboard_payload import prepare_dashboard_context


TEMPLATES = Path(__file__).parent / "templates"
STRATEGY_RESEARCH_JSON = Path("strategy_research.json")
PROTECTED_STRATEGY_RESEARCH_JSON = Path("strategy_research.protected.json")
STRATEGY_LAB_PUBLIC_MODE_ENV = "SFO_STRATEGY_LAB_PUBLIC_MODE"
STRATEGY_LAB_PASSWORD_ENV = "SFO_STRATEGY_LAB_PASSWORD"
STRATEGY_LAB_ITERATIONS_ENV = "SFO_STRATEGY_LAB_PBKDF2_ITERATIONS"
DEFAULT_STRATEGY_LAB_ITERATIONS = 210_000
STREAM_LABEL = b"WeatherEdge Strategy Lab stream v1"
MAC_LABEL = b"WeatherEdge Strategy Lab tag v1"


def render(template_name, replacements):
    html = (TEMPLATES / template_name).read_text()
    for key, value in replacements.items():
        html = html.replace(key, value)
    return html


def b64encode(data):
    return base64.b64encode(data).decode("ascii")


def strategy_lab_password():
    if strategy_lab_public_mode():
        return None
    password = os.environ.get(STRATEGY_LAB_PASSWORD_ENV, "")
    return password if password else None


def strategy_lab_public_mode():
    raw = os.environ.get(STRATEGY_LAB_PUBLIC_MODE_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def strategy_lab_iterations():
    raw = os.environ.get(STRATEGY_LAB_ITERATIONS_ENV)
    if raw is None or raw == "":
        return DEFAULT_STRATEGY_LAB_ITERATIONS
    try:
        iterations = int(raw)
    except ValueError as exc:
        raise ValueError(f"{STRATEGY_LAB_ITERATIONS_ENV} must be an integer") from exc
    if iterations < 100_000:
        raise ValueError(f"{STRATEGY_LAB_ITERATIONS_ENV} must be at least 100000")
    return iterations


def hmac_sha256(key, message):
    return hmac.new(key, message, hashlib.sha256).digest()


def derive_strategy_lab_keys(password, salt, iterations):
    material = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
        dklen=64,
    )
    return material[:32], material[32:]


def strategy_lab_keystream(key, nonce, length):
    chunks = []
    counter = 0
    size = 0
    while size < length:
        block = hmac_sha256(key, STREAM_LABEL + nonce + counter.to_bytes(4, "big"))
        chunks.append(block)
        size += len(block)
        counter += 1
    return b"".join(chunks)[:length]


def xor_bytes(left, right):
    return bytes(a ^ b for a, b in zip(left, right))


def strategy_lab_tag_message(iterations, salt, nonce, ciphertext):
    return b"".join(
        (
            b"\x01",
            iterations.to_bytes(4, "big"),
            len(salt).to_bytes(2, "big"),
            salt,
            len(nonce).to_bytes(2, "big"),
            nonce,
            len(ciphertext).to_bytes(4, "big"),
            ciphertext,
        )
    )


def encrypt_strategy_lab_bytes(plaintext, password, iterations):
    salt = os.urandom(16)
    nonce = os.urandom(16)
    stream_key, mac_key = derive_strategy_lab_keys(password, salt, iterations)
    ciphertext = xor_bytes(
        plaintext,
        strategy_lab_keystream(stream_key, nonce, len(plaintext)),
    )
    tag = hmac_sha256(
        mac_key,
        MAC_LABEL + strategy_lab_tag_message(iterations, salt, nonce, ciphertext),
    )
    return {
        "version": 1,
        "kdf": "PBKDF2-HMAC-SHA256",
        "iterations": iterations,
        "cipher": "HMAC-SHA256-CTR-XOR",
        "mac": "HMAC-SHA256",
        "salt": b64encode(salt),
        "nonce": b64encode(nonce),
        "ciphertext": b64encode(ciphertext),
        "tag": b64encode(tag),
    }


def protect_strategy_research(source_path, target_path):
    password = strategy_lab_password()
    if not password:
        if target_path.exists():
            target_path.unlink()
        return {
            "enabled": False,
            "artifact": source_path.name,
            "format": "public-json",
        }

    iterations = strategy_lab_iterations()
    payload = encrypt_strategy_lab_bytes(source_path.read_bytes(), password, iterations)
    target_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    )
    return {
        "enabled": True,
        "artifact": target_path.name,
        "format": "encrypted-json-v1",
    }


def write_strategy_research_placeholder(path):
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}
        is_generic_local_placeholder = (
            isinstance(existing, dict)
            and existing.get("local_runtime_placeholder") is True
            and existing.get("artifact") == "forecaster/strategy_research.json"
            and "schema_version" not in existing
        )
        if not is_generic_local_placeholder:
            return
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "schema_version": 1,
        "available": False,
        "default_profile": "balanced",
        "local_runtime_placeholder": True,
        "mode": "paper_research_only",
        "live_orders_enabled": False,
        "source": "AWS runtime only",
        "source_of_truth": "AWS Lightsail after sync and refresh",
        "generated_at": now,
        "reason": (
            "Live WeatherEdge strategy diagnostics are generated on AWS after "
            "sync and refresh. This local placeholder prevents stale MacBook "
            "paper-trading data from confusing dashboard design smoke tests."
        ),
        "status": {
            "active_calibration_source": "lstm",
            "active_calibration_label": "lstm = Active execution calibration",
            "challenger_calibration_source": "clean-blend/combined",
            "challenger_calibration_label": (
                "clean-blend/combined = Challenger research calibration"
            ),
            "aws_execution_calibration_locked": True,
            "paper_only": True,
            "paper_trading_status": "AWS runtime data pending",
            "entry_scanner_status": "Entry scanner pending AWS runtime data.",
            "entry_scanner_reason": None,
            "last_updated": now,
            "latest_target_date": None,
            "latest_signal_targets": [],
            "raw_signal_count": 0,
            "pre_resolution_signal_count": 0,
            "deduped_signal_count": 0,
            "post_resolution_excluded_count": 0,
            "alerts": [
                {
                    "level": "info",
                    "code": "local-placeholder",
                    "title": "AWS runtime pending",
                    "detail": "Local Strategy Lab placeholder is active; live alerts are generated on AWS.",
                    "action": "Sync, refresh, and publish from Lightsail to see production diagnostics.",
                }
            ],
            "alert_level": "info",
            "sample_warning": "Local placeholder only; AWS publishes live research data.",
            "bankroll": None,
            "daily_budget": None,
            "open_risk": 0.0,
        },
        "daily_summary": {
            "available": False,
            "reason": "AWS runtime data pending",
            "days": [],
            "totals": {},
            "gate_behavior": {"approved": 0, "rejected": 0, "top_rejections": []},
            "biggest_winners": [],
            "biggest_losers": [],
            "learnings": [],
            "recommended_changes": [],
        },
        "calibration_comparison": {
            "active": {"available": False, "source": "lstm", "role": "Active execution calibration"},
            "challenger": {
                "available": False,
                "source": "clean-blend/combined",
                "role": "Challenger research calibration",
            },
            "comparison": {
                "winner": "not_enough_clean_data",
                "label": "AWS research data pending",
                "recommendation": "Keep AWS execution pinned to lstm.",
            },
        },
        "signal_quality": {"available": False, "latest_candidates": [], "charts": {}},
        "backtest_summary": {
            "available": False,
            "counts": {
                "raw_signals": 0,
                "pre_resolution_signals": 0,
                "deduped_signals": 0,
                "excluded_post_resolution_signals": 0,
                "settled_signals": 0,
                "approved_signals": 0,
                "approved_raw_signals": 0,
                "approved_pre_resolution_signals": 0,
            },
            "metrics_available": False,
            "metrics": {},
            "quality_buckets": [],
        },
        "paper_trading": {
            "available": False,
            "monitor": {
                "take_profit_pct": 35.0,
                "stop_loss_pct": 35.0,
            },
            "summary": {
                "open_positions": 0,
                "published_open_positions": 0,
                "hidden_open_positions": 0,
                "closed_positions": 0,
                "realized_pnl": 0.0,
                "unrealized_pnl": None,
                "marked_open_positions": 0,
                "open_risk": 0.0,
                "open_value": None,
                "win_count": 0,
                "loss_count": 0,
            },
            "open_positions": [],
            "closed_positions": [],
            "recent_monitor_actions": [],
            "profiles": [],
        },
        "profiles": [],
        "research_notes": [
            {"term": "Backtest", "note": "Replay historical rows to score probabilities."},
            {"term": "Pre-resolution", "note": "A signal recorded before market close or observed-high lock."},
            {"term": "Dedupe", "note": "Repeated 15-minute scans count once per target, market, and side."},
            {"term": "Paper trading", "note": "Simulated research positions. No live money is placed."},
        ],
        "disclaimer": "Paper-trading research only. AWS execution remains pinned to lstm.",
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def main():
    context = prepare_dashboard_context()
    replacements = dict(context["replacements"])
    write_strategy_research_placeholder(STRATEGY_RESEARCH_JSON)
    strategy_lab_protection = protect_strategy_research(
        STRATEGY_RESEARCH_JSON,
        PROTECTED_STRATEGY_RESEARCH_JSON,
    )
    replacements.update(
        {
            "__STRATEGY_LAB_PROTECTION__": json.dumps(strategy_lab_protection),
            "__STRATEGY_LAB_BODY_CLASS__": (
                "lab-protected lab-locked"
                if strategy_lab_protection["enabled"]
                else "lab-public"
            ),
            "__STRATEGY_LAB_GATE_ATTRS__": (
                ""
                if strategy_lab_protection["enabled"]
                else "hidden"
            ),
            "__STRATEGY_LAB_CONTENT_ATTRS__": (
                "hidden inert"
                if strategy_lab_protection["enabled"]
                else ""
            ),
        }
    )

    Path("index.html").write_text(render("landing.html", replacements))
    Path("details.html").write_text(render("details.html", replacements))
    Path("strategy-lab.html").write_text(render("strategy-lab.html", replacements))
    if strategy_lab_protection["enabled"]:
        print("wrote index.html, details.html, strategy-lab.html, and strategy_research.protected.json")
    else:
        print("wrote index.html, details.html, and strategy-lab.html")


if __name__ == "__main__":
    main()
