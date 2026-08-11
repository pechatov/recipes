#!/usr/bin/env bash
set -Eeuo pipefail

TRUENAS_HOST="${TRUENAS_HOST:-truenas}"
APP_NAME="recipes"
APP_ROOT="/mnt/main-pool/config/recipes"
SOURCE_DIR="${APP_ROOT}/source"
LOCAL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$APP_ROOT" != "/mnt/main-pool/config/recipes" || "$SOURCE_DIR" != "/mnt/main-pool/config/recipes/source" ]]; then
  echo "Refusing to deploy to an unexpected path." >&2
  exit 1
fi

echo "Preparing ZFS datasets and runtime secrets on ${TRUENAS_HOST}..."
ssh "$TRUENAS_HOST" bash -s -- "$APP_ROOT" <<'REMOTE'
set -Eeuo pipefail
app_root="$1"

ensure_dataset() {
  local dataset="$1"
  if ! sudo zfs list -H -o name "$dataset" >/dev/null 2>&1; then
    sudo zfs create -p "$dataset"
  fi
}

ensure_dataset main-pool/config/recipes
ensure_dataset main-pool/config/recipes/data
ensure_dataset main-pool/config/recipes/postgres
sudo zfs set atime=off main-pool/config/recipes/data
sudo zfs set atime=off recordsize=16K main-pool/config/recipes/postgres

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
    printf 'ALLOWED_HOSTS=192.168.31.2,truenas,localhost,127.0.0.1\n'
    printf 'CSRF_TRUSTED_ORIGINS=http://192.168.31.2:30111\n'
    printf 'TIME_ZONE=Europe/Moscow\n'
    printf 'COOKIE_SECURE=false\n'
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
  if curl -fsS --max-time 3 http://192.168.31.2:30111/healthz/ >/dev/null; then
    echo "Recipes is ready at http://192.168.31.2:30111/"
    exit 0
  fi
  sleep 2
done

echo "The app was deployed but did not become healthy in time." >&2
exit 1
