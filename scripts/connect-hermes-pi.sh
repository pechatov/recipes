#!/usr/bin/env bash
set -Eeuo pipefail

PI_SSH_HOST="${PI_SSH_HOST:-pi}"
TRUENAS_SSH_HOST="${TRUENAS_SSH_HOST:-truenas}"
PI_ADDRESS="${PI_ADDRESS:-192.168.1.147}"
PI_API_PORT="${PI_API_PORT:-8650}"
HERMES_PROFILE="${HERMES_PROFILE:-recipesimport}"
HERMES_MODEL="${HERMES_MODEL:-gpt-5.6-sol}"
TRUENAS_ENV="${TRUENAS_ENV:-/mnt/main-pool/config/recipes/.env}"

ssh "$PI_SSH_HOST" "PI_ADDRESS='$PI_ADDRESS' PI_API_PORT='$PI_API_PORT' HERMES_PROFILE='$HERMES_PROFILE' HERMES_MODEL='$HERMES_MODEL' bash -s" <<'REMOTE'
set -Eeuo pipefail
HERMES_PY="$HOME/.hermes/hermes-agent/venv/bin/python"
PROFILE_HOME="$HOME/.hermes/profiles/$HERMES_PROFILE"

if [[ ! -x "$HERMES_PY" ]]; then
  echo "Hermes virtual environment was not found" >&2
  exit 1
fi

if [[ ! -d "$PROFILE_HOME" ]]; then
  "$HERMES_PY" -m hermes_cli.main profile create "$HERMES_PROFILE" --no-alias --no-skills >/dev/null
fi

if [[ -f "$HOME/.hermes/auth.json" ]]; then
  install -m 0600 "$HOME/.hermes/auth.json" "$PROFILE_HOME/auth.json"
fi

python3 - <<'PY'
import os
import secrets
from pathlib import Path

import yaml

default_home = Path.home() / ".hermes"
home = default_home / "profiles" / os.environ["HERMES_PROFILE"]
env_path = home / ".env"
config_path = home / "config.yaml"
config = yaml.safe_load(config_path.read_text()) or {} if config_path.exists() else {}

values = {}
for line in env_path.read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        name, value = line.split("=", 1)
        values[name] = value

# Some Hermes versions write API_SERVER_* into YAML although the gateway only
# reads them from .env. Migrate those values and remove the duplicate keys.
key = values.get("API_SERVER_KEY") or secrets.token_urlsafe(48)
env_updates = {
    "API_SERVER_ENABLED": "true",
    "API_SERVER_HOST": os.environ["PI_ADDRESS"],
    "API_SERVER_PORT": os.environ["PI_API_PORT"],
    "API_SERVER_MODEL_NAME": "recipes-importer-sol",
    "API_SERVER_KEY": str(key),
}
seen = set()
output = []
for line in env_path.read_text().splitlines():
    name = line.split("=", 1)[0]
    if name in env_updates:
        output.append(f"{name}={env_updates[name]}")
        seen.add(name)
    else:
        output.append(line)
for name, value in env_updates.items():
    if name not in seen:
        output.append(f"{name}={value}")
env_path.write_text("\n".join(output) + "\n")
env_path.chmod(0o600)

default_config = yaml.safe_load((default_home / "config.yaml").read_text()) or {}
default_model = default_config.get("model") or {}
config["model"] = {
    "default": os.environ["HERMES_MODEL"],
    "provider": default_model.get("provider", "openai-codex"),
    "base_url": default_model.get("base_url", "https://chatgpt.com/backend-api/codex"),
}
config.setdefault("agent", {})["reasoning_effort"] = "medium"
# An explicit empty list is different from an omitted value: it disables every
# Hermes tool for API requests while leaving CLI and messaging platforms alone.
config.setdefault("platform_toolsets", {})["api_server"] = []
config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
config_path.chmod(0o600)
PY

printf 'y\ny\n' | "$HERMES_PY" -m hermes_cli.main -p "$HERMES_PROFILE" gateway install --force >/dev/null
"$HERMES_PY" -m hermes_cli.main -p "$HERMES_PROFILE" gateway restart >/dev/null
for _ in $(seq 1 30); do
  if curl -fsS "http://$PI_ADDRESS:$PI_API_PORT/health" >/dev/null 2>&1; then
    exit 0
  fi
  sleep 1
done
echo "Hermes API did not become healthy" >&2
exit 1
REMOTE

# Stream the bearer key directly between the two SSH sessions. It is never
# written to this workstation or printed to stdout.
ssh "$PI_SSH_HOST" "HERMES_PROFILE='$HERMES_PROFILE' python3 -c \"import os; from pathlib import Path; print(next(line.split('=',1)[1].strip() for line in (Path.home()/'.hermes/profiles'/os.environ['HERMES_PROFILE']/'.env').read_text().splitlines() if line.startswith('API_SERVER_KEY=')))\"" \
  | ssh "$TRUENAS_SSH_HOST" "sudo env TRUENAS_ENV='$TRUENAS_ENV' PI_ADDRESS='$PI_ADDRESS' PI_API_PORT='$PI_API_PORT' HERMES_MODEL='$HERMES_MODEL' python3 -c '
import os
import sys
from pathlib import Path

path = Path(os.environ[\"TRUENAS_ENV\"])
key = sys.stdin.read().strip()
if not key or not path.is_file():
    raise SystemExit(\"Missing Hermes key or TrueNAS application .env\")

values = {
    \"RECIPE_AI_BASE_URL\": f\"http://{os.environ['\"'\"'PI_ADDRESS'\"'\"']}:{os.environ['\"'\"'PI_API_PORT'\"'\"']}/v1\",
    \"RECIPE_AI_API_KEY\": key,
    \"RECIPE_AI_MODEL\": \"recipes-importer-sol\",
    \"RECIPE_AI_TIMEOUT_SECONDS\": \"180\",
}
seen = set()
output = []
for line in path.read_text().splitlines():
    name = line.split(\"=\", 1)[0]
    if name in values:
        output.append(f\"{name}={values[name]}\")
        seen.add(name)
    else:
        output.append(line)
for name, value in values.items():
    if name not in seen:
        output.append(f\"{name}={value}\")
tmp = path.with_name(path.name + \".recipes-import.tmp\")
tmp.write_text(\"\\n\".join(output) + \"\\n\")
tmp.chmod(0o600)
tmp.replace(path)
'"

# Remove the previously enabled API from the default profile. Messaging
# platforms continue to use that profile and its existing model unchanged.
ssh "$PI_SSH_HOST" 'python3 - <<'PY'
from pathlib import Path

path = Path.home() / ".hermes" / ".env"
if path.exists():
    lines = [line for line in path.read_text().splitlines() if not line.startswith("API_SERVER_")]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
PY
systemctl --user restart hermes-gateway.service'

echo "Hermes Sol recipe-import profile is configured and its credentials are installed on TrueNAS."
