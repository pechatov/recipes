#!/usr/bin/env bash
set -Eeuo pipefail

PI_SSH_HOST="${PI_SSH_HOST:-pi}"
HERMES_PROFILE="${HERMES_PROFILE:-recipecart}"
ACTION="${1:-start}"
USER_ID="${2:-}"
START_URL="https://eda.yandex.ru/retail"

if [[ ! "$USER_ID" =~ ^[1-9][0-9]*$ ]]; then
  echo "Usage: $0 [start|stop] USER_ID" >&2
  echo "USER_ID is shown on the cart page when login is required." >&2
  exit 2
fi

case "$ACTION" in
  start)
    ssh "$PI_SSH_HOST" bash -s -- "$HERMES_PROFILE" "$USER_ID" "$START_URL" <<'REMOTE'
set -Eeuo pipefail
HERMES_PROFILE="$1"
USER_ID="$2"
START_URL="$3"
HERMES_ROOT="$HOME/.hermes/hermes-agent"
HERMES_HOME="$HOME/.hermes/profiles/$HERMES_PROFILE"
TASK_ID="recipes-cart-user-$USER_ID"
identity=$(cd "$HERMES_ROOT" && HERMES_HOME="$HERMES_HOME" "$HERMES_ROOT/venv/bin/python" -c '
import json
import sys
from tools.browser_camofox_state import get_camofox_identity
print(json.dumps(get_camofox_identity(sys.argv[1])))
' "$TASK_ID")
payload=$(python3 -c '
import json
import sys
identity = json.loads(sys.argv[1])
print(json.dumps({"userId": identity["user_id"], "sessionKey": identity["session_key"], "url": sys.argv[2]}))
' "$identity" "$START_URL")
curl -fsS -X POST http://127.0.0.1:9377/tabs \
  -H 'Content-Type: application/json' \
  --data "$payload" >/dev/null
echo "User-specific Camofox login session started. Keep it open while signing in."
REMOTE
    echo "In another terminal run: ssh -N -L 6080:127.0.0.1:6080 $PI_SSH_HOST"
    echo "Then open http://127.0.0.1:6080/vnc.html, sign in and select the delivery address."
    echo "When finished: $0 stop $USER_ID"
    ;;
  stop)
    ssh "$PI_SSH_HOST" bash -s -- "$HERMES_PROFILE" "$USER_ID" <<'REMOTE'
set -Eeuo pipefail
HERMES_PROFILE="$1"
USER_ID="$2"
HERMES_ROOT="$HOME/.hermes/hermes-agent"
HERMES_HOME="$HOME/.hermes/profiles/$HERMES_PROFILE"
TASK_ID="recipes-cart-user-$USER_ID"
user_id=$(cd "$HERMES_ROOT" && HERMES_HOME="$HERMES_HOME" "$HERMES_ROOT/venv/bin/python" -c '
import sys
from tools.browser_camofox_state import get_camofox_identity
print(get_camofox_identity(sys.argv[1])["user_id"])
' "$TASK_ID")
curl -fsS -X DELETE "http://127.0.0.1:9377/sessions/$user_id" >/dev/null 2>&1 || true
echo "Browser state saved and login session stopped."
REMOTE
    ;;
  *)
    echo "Usage: $0 [start|stop] USER_ID" >&2
    exit 2
    ;;
esac
