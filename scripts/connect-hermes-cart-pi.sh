#!/usr/bin/env bash
set -Eeuo pipefail

: "${PI_SSH_HOST:?Set PI_SSH_HOST}"
: "${TRUENAS_SSH_HOST:?Set TRUENAS_SSH_HOST}"
: "${PI_ADDRESS:?Set PI_ADDRESS}"
: "${PI_API_PORT:?Set PI_API_PORT}"
: "${TRUENAS_ENV:?Set TRUENAS_ENV}"

HERMES_PROFILE="${HERMES_PROFILE:-recipecart}"
HERMES_MODEL="${HERMES_MODEL:-gpt-5.6-sol}"
LOCAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ssh "$PI_SSH_HOST" 'install -d -m 0700 "$HOME/.local/share/recipes-browser-login" && install -m 0600 /dev/stdin "$HOME/.local/share/recipes-browser-login/server.mjs"' \
  <"$LOCAL_ROOT/scripts/browser-login-server.mjs"

ssh "$PI_SSH_HOST" "PI_ADDRESS='$PI_ADDRESS' PI_API_PORT='$PI_API_PORT' HERMES_PROFILE='$HERMES_PROFILE' HERMES_MODEL='$HERMES_MODEL' bash -s" <<'REMOTE'
set -Eeuo pipefail
HERMES_ROOT="$HOME/.hermes/hermes-agent"
HERMES_PY="$HERMES_ROOT/venv/bin/python"
PROFILE_HOME="$HOME/.hermes/profiles/$HERMES_PROFILE"
CAMOFOX_ROOT="$HOME/.local/share/recipes-camofox"
CAMOFOX_BIN="$CAMOFOX_ROOT/node_modules/.bin/camofox-browser"
LOGIN_ROOT="$HOME/.local/share/recipes-browser-login"
export PATH="$HOME/.hermes/node/bin:$PATH"

if [[ ! -x "$HERMES_PY" || ! -x "$HOME/.hermes/node/bin/node" ]]; then
  echo "Hermes or its Node.js installation was not found" >&2
  exit 1
fi

if [[ ! -d "$PROFILE_HOME" ]]; then
  "$HERMES_PY" -m hermes_cli.main profile create "$HERMES_PROFILE" --no-alias --no-skills >/dev/null
fi
if [[ -f "$HOME/.hermes/auth.json" ]]; then
  install -m 0600 "$HOME/.hermes/auth.json" "$PROFILE_HOME/auth.json"
fi

missing_packages=()
for package in x11vnc novnc python3-websockify net-tools procps; do
  dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'ok installed' || missing_packages+=("$package")
done
if ((${#missing_packages[@]})); then
  sudo env DEBIAN_FRONTEND=noninteractive apt-get update
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing_packages[@]}"
fi

if [[ ! -x "$CAMOFOX_BIN" ]]; then
  npm install --prefix "$CAMOFOX_ROOT" --omit=dev --no-audit --no-fund @askjo/camofox-browser@1.13.1
fi
npm install --prefix "$LOGIN_ROOT" --omit=dev --no-audit --no-fund http-proxy@1.18.1
install -d -m 0700 "$PROFILE_HOME/camofox-profiles"

# The upstream no-proxy defaults identify every context as en-US in Los
# Angeles. That contradicts the Russian residential IP used here and is a
# strong anti-bot signal for local grocery sites. Keep the override as a small
# plugin so npm installation remains reproducible and the package itself stays
# unmodified.
CAMOFOX_ROOT="$CAMOFOX_ROOT" python3 - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["CAMOFOX_ROOT"]) / "node_modules" / "@askjo" / "camofox-browser"
plugin = root / "plugins" / "recipes-locale"
plugin.mkdir(parents=True, exist_ok=True)
(plugin / "index.js").write_text("""export async function register(app, ctx) {
  ctx.events.on('session:creating', ({ contextOptions }) => {
    contextOptions.locale = process.env.CAMOFOX_LOCALE || 'ru-RU';
    contextOptions.timezoneId = process.env.CAMOFOX_TIMEZONE || 'Europe/Moscow';
    delete contextOptions.geolocation;
    contextOptions.permissions = (contextOptions.permissions || [])
      .filter((permission) => permission !== 'geolocation');
  });
  ctx.log('info', 'recipes locale plugin enabled', {
    locale: process.env.CAMOFOX_LOCALE || 'ru-RU',
    timezone: process.env.CAMOFOX_TIMEZONE || 'Europe/Moscow',
  });
}
""")
(plugin / "plugin.json").write_text(json.dumps({
    "name": "Recipes locale",
    "description": "Matches browser locale and timezone to the household region",
}, indent=2) + "\n")

config_path = root / "camofox.config.json"
config = json.loads(config_path.read_text())
plugins = config.setdefault("plugins", {})
if isinstance(plugins, list):
    if "recipes-locale" not in plugins:
        plugins.append("recipes-locale")
else:
    plugins["recipes-locale"] = {"enabled": True}
config_path.write_text(json.dumps(config, indent=2) + "\n")

# Camofox 1.13.1's periodic probe opens a default Playwright context. The
# bundled Camoufox build rejects Playwright's implicit `isMobile: false`
# viewport field, causing a healthy authenticated browser to restart every few
# minutes. Production sessions already use viewport=null; make the probe do
# the same.
server_path = root / "server.js"
server = server_path.read_text()
old_probe = "testContext = await browser.newContext();"
new_probe = "testContext = await browser.newContext({ viewport: null });"
if old_probe in server:
    server_path.write_text(server.replace(old_probe, new_probe, 1))
elif new_probe not in server:
    raise SystemExit("Unsupported Camofox health-probe implementation")
PY

# Hermes normally scopes a managed Camofox profile only to the Hermes profile.
# This cart endpoint serves several recipe-site users, so derive the browser
# userId from the authenticated X-Hermes-Session-Key instead. Parallel shard
# sessions keep distinct gateway conversations but deliberately share the base
# user's browser profile, login cookies and server-side cart. The fallback to
# task_id keeps CLI and non-gateway callers working.
HERMES_ROOT="$HERMES_ROOT" python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["HERMES_ROOT"]) / "tools" / "browser_camofox_state.py"
source = path.read_text()
import_line = "from gateway.session_context import get_session_env\n"
if import_line not in source:
    marker = "from hermes_constants import get_hermes_home\n"
    if marker not in source:
        raise SystemExit("Unsupported Hermes Camofox state imports")
    source = source.replace(marker, marker + import_line, 1)

old_scope = '    logical_scope = task_id or "default"\n'
previous_scope = (
    '    logical_scope = (\n'
    '        get_session_env("HERMES_SESSION_KEY", "").strip()\n'
    '        or task_id\n'
    '        or "default"\n'
    '    )\n'
)
new_scope = (
    '    session_scope = (\n'
    '        get_session_env("HERMES_SESSION_KEY", "").strip()\n'
    '        or task_id\n'
    '        or "default"\n'
    '    )\n'
    '    base_scope, separator, shard = session_scope.rpartition("-shard-")\n'
    '    logical_scope = (\n'
    '        base_scope\n'
    '        if separator and shard in {"1", "2", "3", "4", "5"}\n'
    '        else session_scope\n'
    '    )\n'
)
if old_scope in source:
    source = source.replace(old_scope, new_scope, 1)
elif previous_scope in source:
    source = source.replace(previous_scope, new_scope, 1)
elif new_scope not in source:
    raise SystemExit("Unsupported Hermes Camofox logical scope implementation")

old_user = '        f"camofox-user:{scope_root}",\n'
new_user = '        f"camofox-user:{scope_root}:{logical_scope}",\n'
if old_user in source:
    source = source.replace(old_user, new_user, 1)
elif new_user not in source:
    raise SystemExit("Unsupported Hermes Camofox user identity implementation")

source = source.replace(
    "The user identity is profile-scoped (same Hermes profile = same userId).",
    "The user identity is scoped to the gateway session key within this profile.",
)
path.write_text(source)
PY

python3 - <<'PY'
import os
import secrets
from pathlib import Path

import yaml

home = Path.home() / ".hermes" / "profiles" / os.environ["HERMES_PROFILE"]
env_path = home / ".env"
config_path = home / "config.yaml"
config = yaml.safe_load(config_path.read_text()) or {} if config_path.exists() else {}
current = env_path.read_text().splitlines() if env_path.exists() else []
values = {}
for line in current:
    if "=" in line and not line.lstrip().startswith("#"):
        name, value = line.split("=", 1)
        values[name] = value

updates = {
    "API_SERVER_ENABLED": "true",
    "API_SERVER_HOST": os.environ["PI_ADDRESS"],
    "API_SERVER_PORT": os.environ["PI_API_PORT"],
    "API_SERVER_MODEL_NAME": "recipes-cart-sol",
    "API_SERVER_KEY": values.get("API_SERVER_KEY") or secrets.token_urlsafe(48),
    "CAMOFOX_URL": "http://127.0.0.1:9377",
    "CAMOFOX_PROFILE_DIR": str(home / "camofox-profiles"),
    "CAMOFOX_CRASH_REPORT_ENABLED": "false",
    "CAMOFOX_LOCALE": "ru-RU",
    "CAMOFOX_TIMEZONE": "Europe/Moscow",
    "BROWSER_LOGIN_CONTROL_KEY": values.get("BROWSER_LOGIN_CONTROL_KEY") or secrets.token_urlsafe(48),
}
obsolete = {
    "AGENT_BROWSER_PROFILE",
    "AGENT_BROWSER_EXECUTABLE_PATH",
    "AGENT_BROWSER_HEADED",
    "AGENT_BROWSER_ARGS",
    "AGENT_BROWSER_CONTENT_BOUNDARIES",
    "AGENT_BROWSER_MAX_OUTPUT",
    "AGENT_BROWSER_STREAM_PORT",
    "AGENT_BROWSER_ALLOWED_DOMAINS",
    "CHROME_DEVEL_SANDBOX",
    "DISPLAY",
    "CAMOFOX_USER_ID",
    "CAMOFOX_SESSION_KEY",
    "CAMOFOX_ADOPT_EXISTING_TAB",
}
seen = set()
output = []
for line in current:
    name = line.split("=", 1)[0]
    if name in updates:
        output.append(f"{name}={updates[name]}")
        seen.add(name)
    elif name in obsolete:
        continue
    else:
        output.append(line)
for name, value in updates.items():
    if name not in seen:
        output.append(f"{name}={value}")
env_path.write_text("\n".join(output) + "\n")
env_path.chmod(0o600)

default_config = yaml.safe_load((Path.home() / ".hermes" / "config.yaml").read_text()) or {}
default_model = default_config.get("model") or {}
config["model"] = {
    "default": os.environ["HERMES_MODEL"],
    "provider": default_model.get("provider", "openai-codex"),
    "base_url": default_model.get("base_url", "https://chatgpt.com/backend-api/codex"),
}
config.setdefault("agent", {})["reasoning_effort"] = "medium"
config.setdefault("browser", {})["provider"] = "camofox"
config["browser"]["command_timeout"] = 90
config["browser"]["camofox"] = {
    "managed_persistence": True,
    "adopt_existing_tab": True,
}
# The cart endpoint gets browser automation only: no shell, files, memory,
# messaging, delegation, or smart-home tools.
config.setdefault("platform_toolsets", {})["api_server"] = ["browser"]
config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
config_path.chmod(0o600)
PY

install -d -m 0700 "$HOME/.config/systemd/user"
HERMES_PROFILE="$HERMES_PROFILE" python3 - <<'PY'
import os
from pathlib import Path

home = Path.home()
profile = os.environ["HERMES_PROFILE"]
path = home / ".config/systemd/user/recipes-camofox.service"
path.write_text(f"""[Unit]
Description=Private Camofox browser for the recipes cart
After=network-online.target

[Service]
Environment=PATH={home}/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin
Environment=CAMOFOX_BIND_HOST=127.0.0.1
Environment=CAMOFOX_PORT=9377
Environment=ENABLE_VNC=1
Environment=VNC_BIND=127.0.0.1
Environment=VNC_PORT=5900
Environment=NOVNC_PORT=6080
Environment=VNC_RESOLUTION=1440x1000x24
Environment=SESSION_TIMEOUT_MS=3600000
Environment=TAB_INACTIVITY_MS=3600000
EnvironmentFile={home}/.hermes/profiles/{profile}/.env
ExecStart={home}/.local/share/recipes-camofox/node_modules/.bin/camofox-browser
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
""")
path.chmod(0o600)
PY

HERMES_PROFILE="$HERMES_PROFILE" python3 - <<'PY'
import os
from pathlib import Path

home = Path.home()
profile = os.environ["HERMES_PROFILE"]
path = home / ".config/systemd/user/recipes-browser-login.service"
path.write_text(f"""[Unit]
Description=One-time browser login gateway for recipes
Requires=recipes-camofox.service
After=recipes-camofox.service network-online.target

[Service]
Environment=PATH={home}/.hermes/node/bin:/usr/local/bin:/usr/bin:/bin
Environment=BROWSER_LOGIN_BIND_HOST={os.environ['PI_ADDRESS']}
Environment=BROWSER_LOGIN_PORT=9380
Environment=BROWSER_LOGIN_STATE_PATH={home}/.local/share/recipes-browser-login/active-session.json
Environment=HERMES_ROOT={home}/.hermes/hermes-agent
Environment=HERMES_HOME={home}/.hermes/profiles/{profile}
EnvironmentFile={home}/.hermes/profiles/{profile}/.env
ExecStart={home}/.hermes/node/bin/node {home}/.local/share/recipes-browser-login/server.mjs
ExecStartPost=/usr/bin/curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused http://{os.environ['PI_ADDRESS']}:9380/healthz
ExecStartPost=/usr/bin/systemctl --user --no-block start hermes-gateway-{profile}.service
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
""")
path.chmod(0o600)
PY

printf 'y\ny\n' | "$HERMES_PY" -m hermes_cli.main -p "$HERMES_PROFILE" gateway install --force >/dev/null
install -d -m 0700 "$HOME/.config/systemd/user/hermes-gateway-$HERMES_PROFILE.service.d"
HERMES_PROFILE="$HERMES_PROFILE" python3 - <<'PY'
import os
from pathlib import Path

path = (
    Path.home()
    / ".config/systemd/user"
    / f"hermes-gateway-{os.environ['HERMES_PROFILE']}.service.d"
    / "recipes-browser.conf"
)
path.write_text(f"""[Unit]
Requires=recipes-camofox.service recipes-browser-login.service
BindsTo=recipes-browser-login.service
After=recipes-camofox.service recipes-browser-login.service

[Service]
ExecStartPre=/usr/bin/curl --fail --silent --show-error --retry 30 --retry-delay 1 --retry-connrefused http://{os.environ['PI_ADDRESS']}:9380/healthz
""")
path.chmod(0o600)
PY
systemctl --user daemon-reload
systemctl --user disable --now recipes-xvfb.service >/dev/null 2>&1 || true
systemctl --user enable recipes-camofox.service >/dev/null
systemctl --user enable recipes-browser-login.service >/dev/null
systemctl --user restart recipes-camofox.service
systemctl --user restart recipes-browser-login.service
systemctl --user restart "hermes-gateway-$HERMES_PROFILE.service"

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:9377/health" >/dev/null 2>&1 \
    && curl -fsS "http://$PI_ADDRESS:$PI_API_PORT/health" >/dev/null 2>&1 \
    && curl -fsS "http://$PI_ADDRESS:9380/healthz" >/dev/null 2>&1; then
    exit 0
  fi
  sleep 1
done
echo "Hermes cart API did not become healthy" >&2
exit 1
REMOTE

# Pass both bearer keys directly from Pi to the root-owned TrueNAS env file.
ssh "$PI_SSH_HOST" "HERMES_PROFILE='$HERMES_PROFILE' python3 -c \"import json, os; from pathlib import Path; values=dict(line.split('=',1) for line in (Path.home()/'.hermes/profiles'/os.environ['HERMES_PROFILE']/'.env').read_text().splitlines() if '=' in line and not line.startswith('#')); print(json.dumps({'cart': values['API_SERVER_KEY'], 'browser': values['BROWSER_LOGIN_CONTROL_KEY']}))\"" \
  | ssh "$TRUENAS_SSH_HOST" "sudo env TRUENAS_ENV='$TRUENAS_ENV' CART_BASE_URL='http://$PI_ADDRESS:$PI_API_PORT/v1' python3 -c '
import json
import os
import sys
from pathlib import Path

path = Path(os.environ[\"TRUENAS_ENV\"])
keys = json.loads(sys.stdin.read())
if not keys.get(\"cart\") or not keys.get(\"browser\") or not path.is_file():
    raise SystemExit(\"Missing Hermes key or TrueNAS application .env\")
values = {
    \"CART_AI_BASE_URL\": os.environ[\"CART_BASE_URL\"],
    \"CART_AI_API_KEY\": keys[\"cart\"],
    \"CART_AI_MODEL\": \"recipes-cart-sol\",
    \"CART_AI_TIMEOUT_SECONDS\": \"900\",
    \"CART_BROWSER_CONTROL_URL\": \"http://$PI_ADDRESS:9380\",
    \"CART_BROWSER_CONTROL_KEY\": keys[\"browser\"],
    \"CART_BROWSER_LOGIN_MINUTES\": \"15\",
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
tmp = path.with_name(path.name + \".recipes-cart.tmp\")
tmp.write_text(\"\\n\".join(output) + \"\\n\")
tmp.chmod(0o600)
tmp.replace(path)
'"

echo "Hermes cart profile is ready."
echo "Browser login is available through the recipes site after the HTTPS proxy is updated."
