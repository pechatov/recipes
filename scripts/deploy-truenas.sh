#!/usr/bin/env bash
set -Eeuo pipefail

: "${TRUENAS_HOST:?Set TRUENAS_HOST}"
: "${RECIPES_APP_ROOT:?Set RECIPES_APP_ROOT}"
: "${RECIPES_BIND_ADDRESS:?Set RECIPES_BIND_ADDRESS}"
: "${RECIPES_PORT:?Set RECIPES_PORT}"
: "${RECIPES_HEALTH_URL:?Set RECIPES_HEALTH_URL}"
: "${RECIPES_PUBLIC_HOSTS:?Set RECIPES_PUBLIC_HOSTS}"
: "${RECIPES_CSRF_TRUSTED_ORIGINS:?Set RECIPES_CSRF_TRUSTED_ORIGINS}"

APP_NAME="recipes"
APP_ROOT="$RECIPES_APP_ROOT"
SOURCE_DIR="${APP_ROOT}/source"
LOCAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATASET_ROOT="${APP_ROOT#/mnt/}"

if [[ ! "$APP_ROOT" =~ ^/mnt/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$ ]] \
  || [[ "${APP_ROOT##*/}" != "recipes" ]] \
  || [[ "$APP_ROOT" == *"/../"* || "$APP_ROOT" == *"/./"* ]] \
  || [[ "$DATASET_ROOT" == "$APP_ROOT" ]]; then
  echo "Refusing to deploy to an unexpected path." >&2
  exit 1
fi
if [[ ! "$RECIPES_HEALTH_URL" =~ ^https?://[A-Za-z0-9.:-]+/ ]] \
  || [[ ! "$RECIPES_PUBLIC_HOSTS" =~ ^[A-Za-z0-9.,:-]+$ ]] \
  || [[ ! "$RECIPES_CSRF_TRUSTED_ORIGINS" =~ ^https?://[A-Za-z0-9.,:/_-]+$ ]]; then
  echo "Invalid deployment network settings." >&2
  exit 1
fi

echo "Preparing ZFS datasets and runtime secrets on ${TRUENAS_HOST}..."
ssh "$TRUENAS_HOST" bash -s -- \
  "$APP_ROOT" "$DATASET_ROOT" "$RECIPES_PUBLIC_HOSTS" \
  "$RECIPES_CSRF_TRUSTED_ORIGINS" <<'REMOTE'
set -Eeuo pipefail
app_root="$1"
dataset_root="$2"
public_hosts="$3"
csrf_origins="$4"

ensure_dataset() {
  local dataset="$1"
  if ! sudo zfs list -H -o name "$dataset" >/dev/null 2>&1; then
    sudo zfs create -p "$dataset"
  fi
}

ensure_dataset "$dataset_root"
ensure_dataset "$dataset_root/data"
ensure_dataset "$dataset_root/postgres"
sudo zfs set atime=off "$dataset_root/data"
sudo zfs set atime=off recordsize=16K "$dataset_root/postgres"

sudo install -d -m 0750 -o "$(id -u)" -g "$(id -g)" "$app_root/source"
sudo chown 1000:1000 "$app_root/data"
sudo chown 70:70 "$app_root/postgres"

if [[ ! -f "$app_root/.env" ]]; then
  secret_key="$(openssl rand -base64 48 | tr -d '\n')"
  db_password="$(openssl rand -base64 36 | tr -d '\n')"
  temp_env="$(mktemp)"
  trap 'rm -f "$temp_env"' EXIT
  {
    printf 'SECRET_KEY=%s\n' "$secret_key"
    printf 'POSTGRES_PASSWORD=%s\n' "$db_password"
    printf 'POSTGRES_DB=recipes\n'
    printf 'POSTGRES_USER=recipes\n'
    printf 'DEBUG=false\n'
    printf 'ALLOWED_HOSTS=%s\n' "$public_hosts"
    printf 'CSRF_TRUSTED_ORIGINS=%s\n' "$csrf_origins"
    printf 'COOKIE_SECURE=true\n'
    printf 'SECURE_SSL_REDIRECT=false\n'
    printf 'SECURE_HSTS_SECONDS=0\n'
  } >"$temp_env"
  sudo install -m 0600 -o root -g root "$temp_env" "$app_root/.env"
fi
REMOTE

echo "Synchronizing application source..."
rsync -az --delete \
  --exclude='.git/' \
  --exclude='.env' \
  --exclude='.venv/' \
  --exclude='data/' \
  --exclude='postgres-data/' \
  --exclude='staticfiles/' \
  "$LOCAL_ROOT/" "$TRUENAS_HOST:$SOURCE_DIR/"

echo "Building the application image..."
ssh "$TRUENAS_HOST" "cd '$SOURCE_DIR' && sudo docker build -t recipes:local ."

rendered_compose="$(mktemp)"
trap 'rm -f "$rendered_compose"' EXIT
RECIPES_IMAGE_REF="recipes:local" \
  python3 "$LOCAL_ROOT/scripts/deploy/render-compose.py" \
  "$LOCAL_ROOT/deploy/compose.truenas.yaml" "$rendered_compose"
scp "$rendered_compose" "$TRUENAS_HOST:$SOURCE_DIR/deploy/compose.truenas.yaml"

echo "Registering the application in TrueNAS..."
ssh "$TRUENAS_HOST" bash -s -- "$APP_NAME" "$SOURCE_DIR/deploy/compose.truenas.yaml" <<'REMOTE'
set -Eeuo pipefail
app_name="$1"
compose_path="$2"

if sudo midclt call app.query | jq -e --arg name "$app_name" '.[] | select(.name == $name)' >/dev/null; then
  payload="$(sudo jq -n --rawfile compose "$compose_path" '{custom_compose_config_string: $compose}')"
  job_id="$(sudo midclt call app.update "$app_name" "$payload")"
else
  payload="$(sudo jq -n --arg name "$app_name" --rawfile compose "$compose_path" '{app_name: $name, custom_app: true, custom_compose_config_string: $compose}')"
  job_id="$(sudo midclt call app.create "$payload")"
fi

while true; do
  job="$(sudo midclt call core.get_jobs "[[\"id\",\"=\",${job_id}]]" '{"get":true}')"
  state="$(jq -r '.state' <<<"$job")"
  case "$state" in
    SUCCESS) break ;;
    FAILED|ABORTED)
      jq '{state, error, exception}' <<<"$job" >&2
      exit 1
      ;;
    *) sleep 2 ;;
  esac
done

sudo midclt call app.get_instance "$app_name" | jq '{name, state, human_version, active_workloads}'
REMOTE

echo "Waiting for the health endpoint..."
for _ in {1..30}; do
  if curl -fsS --max-time 3 "$RECIPES_HEALTH_URL" >/dev/null; then
    echo "Recipes is ready."
    exit 0
  fi
  sleep 2
done

echo "The app was deployed but did not become healthy in time." >&2
exit 1
