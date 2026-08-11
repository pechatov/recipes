#!/usr/bin/env bash
set -Eeuo pipefail

: "${DEPLOY_SSH_TARGET:?Set DEPLOY_SSH_TARGET, for example nas}"
: "${RECIPES_APP_ROOT:?Set RECIPES_APP_ROOT}"
: "${RECIPES_IMAGE:?Set RECIPES_IMAGE}"
: "${RECIPES_TAG:?Set RECIPES_TAG}"
: "${GHCR_USERNAME:?Set GHCR_USERNAME}"
: "${GHCR_TOKEN:?Set GHCR_TOKEN}"

APP_NAME="recipes"
EXPECTED_ROOT="/mnt/main-pool/config/recipes"
HEALTH_URL="${RECIPES_HEALTH_URL:-http://192.168.31.2:30111/healthz/}"
IMAGE_REF="${RECIPES_IMAGE}:${RECIPES_TAG}"
SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLATE="$SOURCE_ROOT/deploy/compose.truenas.yaml"
SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)

if [[ "$RECIPES_APP_ROOT" != "$EXPECTED_ROOT" ]]; then
  echo "Refusing to deploy to unexpected root: $RECIPES_APP_ROOT" >&2
  exit 1
fi
if [[ ! "$GHCR_USERNAME" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "Invalid GHCR username" >&2
  exit 1
fi
if [[ ! "$RECIPES_TAG" =~ ^sha-[0-9a-f]{40}$ ]]; then
  echo "Deploy tag must be an immutable sha-<commit> tag" >&2
  exit 1
fi

rendered_compose="$(mktemp)"
cleanup() {
  rm -f "$rendered_compose"
}
trap cleanup EXIT

TEMPLATE="$TEMPLATE" IMAGE_REF="$IMAGE_REF" RENDERED="$rendered_compose" python3 - <<'PY'
import os
from pathlib import Path

template = Path(os.environ["TEMPLATE"]).read_text()
marker = "${RECIPES_IMAGE:-recipes}:${RECIPES_TAG:-local}"
if marker not in template:
    raise SystemExit("TrueNAS compose image marker is missing")
Path(os.environ["RENDERED"]).write_text(template.replace(marker, os.environ["IMAGE_REF"]))
PY

remote_compose="$RECIPES_APP_ROOT/source/deploy/compose.truenas.yaml"
ssh "${SSH_OPTS[@]}" "$DEPLOY_SSH_TARGET" \
  "install -d -m 0750 '$RECIPES_APP_ROOT/source/deploy'"
scp "${SSH_OPTS[@]}" "$rendered_compose" "$DEPLOY_SSH_TARGET:$remote_compose"

run_id="${GITHUB_RUN_ID:-manual}"
if [[ ! "$run_id" =~ ^([0-9]+|manual)$ ]]; then
  echo "Invalid GitHub run ID" >&2
  exit 1
fi
auth_dir="/tmp/recipes-ghcr-$run_id"
printf '%s' "$GHCR_TOKEN" | ssh "${SSH_OPTS[@]}" "$DEPLOY_SSH_TARGET" \
  "sudo install -d -m 0700 '$auth_dir' && sudo env DOCKER_CONFIG='$auth_dir' docker login ghcr.io --username '$GHCR_USERNAME' --password-stdin >/dev/null"
ssh "${SSH_OPTS[@]}" "$DEPLOY_SSH_TARGET" \
  "set +e; sudo env DOCKER_CONFIG='$auth_dir' docker pull '$IMAGE_REF' >/dev/null; status=\$?; sudo rm -rf '$auth_dir'; exit \$status"

ssh "${SSH_OPTS[@]}" "$DEPLOY_SSH_TARGET" bash -s -- "$APP_NAME" "$remote_compose" "$IMAGE_REF" <<'REMOTE'
set -Eeuo pipefail
app_name="$1"
compose_path="$2"
image_ref="$3"

sudo docker image inspect "$image_ref" >/dev/null
test -f /mnt/main-pool/config/recipes/.env
payload="$(sudo jq -n --rawfile compose "$compose_path" '{custom_compose_config_string: $compose}')"
job_id="$(sudo midclt call app.update "$app_name" "$payload")"

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

sudo midclt call app.get_instance "$app_name" | jq -e \
  --arg image "$image_ref" \
  '.state == "RUNNING" and any(.active_workloads.images[]; . == $image)' >/dev/null
REMOTE

for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null; then
    echo "Recipes $RECIPES_TAG is healthy at $HEALTH_URL"
    exit 0
  fi
  sleep 2
done

echo "TrueNAS app updated but health check failed" >&2
exit 1
