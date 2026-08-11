#!/usr/bin/env bash
set -Eeuo pipefail

TRUENAS_HOST="${TRUENAS_HOST:-truenas}"

ssh "$TRUENAS_HOST" bash -s <<'REMOTE'
set -Eeuo pipefail

dataset="main-pool/config/recipes/backups"
backup_root="/mnt/main-pool/config/recipes/backups"
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
sudo zfs snapshot -r "main-pool/config/recipes@$snapshot"

echo "Database dump: $dump_path"
echo "Recursive ZFS snapshot: main-pool/config/recipes@$snapshot"
REMOTE
