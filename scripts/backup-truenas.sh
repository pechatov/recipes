#!/usr/bin/env bash
set -Eeuo pipefail

: "${TRUENAS_HOST:?Set TRUENAS_HOST}"
: "${RECIPES_APP_ROOT:?Set RECIPES_APP_ROOT}"

if [[ ! "$RECIPES_APP_ROOT" =~ ^/mnt/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$ ]] \
  || [[ "${RECIPES_APP_ROOT##*/}" != "recipes" ]] \
  || [[ "$RECIPES_APP_ROOT" == *"/../"* || "$RECIPES_APP_ROOT" == *"/./"* ]]; then
  echo "Refusing to back up an unexpected path." >&2
  exit 1
fi

ssh "$TRUENAS_HOST" bash -s -- "$RECIPES_APP_ROOT" <<'REMOTE'
set -Eeuo pipefail

app_root="$1"
dataset_root="${app_root#/mnt/}"
dataset="$dataset_root/backups"
backup_root="$app_root/backups"
stamp="$(date +%Y%m%d-%H%M%S)"
dump_path="$backup_root/recipes-$stamp.dump"
snapshot="manual-$stamp"

if ! sudo zfs list -H -o name "$dataset" >/dev/null 2>&1; then
  sudo zfs create -p "$dataset"
  sudo chmod 0700 "$backup_root"
fi

if ! sudo docker ps --format '{{.Names}}' | grep -qx 'ix-recipes-db-1'; then
  echo "Recipes database container is not running." >&2
  exit 1
fi

sudo sh -c "docker exec ix-recipes-db-1 pg_dump -U recipes -Fc recipes > '$dump_path'"
sudo chmod 0600 "$dump_path"
sudo zfs snapshot -r "$dataset_root@$snapshot"

echo "Database dump: $dump_path"
echo "Recursive ZFS snapshot created."
REMOTE
