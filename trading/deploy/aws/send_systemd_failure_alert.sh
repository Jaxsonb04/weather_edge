#!/usr/bin/env bash
set -euo pipefail

FAILED_UNIT="${1:-unknown-unit}"
ALERT_UNIT="${2:-unknown-alert-unit}"
ALERT_URL="${SFO_FRESHNESS_ALERT_URL:-}"

if [[ -z "$ALERT_URL" ]]; then
  echo "warning: SFO_FRESHNESS_ALERT_URL is unset; systemd failure alert was not sent" >&2
  exit 0
fi

HOST_NAME="${SFO_ALERT_HOSTNAME:-$(hostname 2>/dev/null || printf unknown-host)}"
TIMESTAMP="${SFO_ALERT_TIMESTAMP:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
export FAILED_UNIT ALERT_UNIT ALERT_URL HOST_NAME TIMESTAMP

# Feed both the webhook URL and JSON body over stdin so endpoint credentials do
# not appear in process arguments or journal output.
if ! python3 - <<'PY' | curl --fail --silent --show-error --max-time 15 \
    --request POST --header "Content-Type: application/json" --config - >/dev/null
import json
import os

payload = json.dumps(
    {
        "message": f"{os.environ['FAILED_UNIT']}/{os.environ['ALERT_UNIT']} failed",
        "failed_unit": os.environ["FAILED_UNIT"],
        "alert_unit": os.environ["ALERT_UNIT"],
        "host": os.environ["HOST_NAME"],
        "timestamp": os.environ["TIMESTAMP"],
    },
    separators=(",", ":"),
)
print("url = " + json.dumps(os.environ["ALERT_URL"]))
print("data = " + json.dumps(payload))
PY
then
  echo "alert POST failed (check SFO_FRESHNESS_ALERT_URL)" >&2
  exit 1
fi

echo "systemd failure alert posted"
