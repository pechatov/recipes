#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="recipes:check"

cd "$ROOT"
bash -n scripts/*.sh scripts/deploy/*.sh
node --check scripts/cart-adapter-server.mjs
node scripts/cart-adapter-server.test.mjs
python3 -m py_compile scripts/deploy/render-compose.py
rendered_compose="$(mktemp)"
trap 'rm -f "$rendered_compose"' EXIT
RECIPES_IMAGE_REF="registry.example/recipes:check" \
RECIPES_APP_ROOT="/mnt/example/apps/recipes" \
RECIPES_BIND_ADDRESS="127.0.0.1" \
RECIPES_PORT="8000" \
  python3 scripts/deploy/render-compose.py \
  deploy/compose.truenas.yaml "$rendered_compose"
if grep -q '__RECIPES_' "$rendered_compose"; then
  echo "Unrendered deployment compose marker found." >&2
  exit 1
fi
docker build -t "$IMAGE" .
docker run --rm --entrypoint python -e DEBUG=true -v "$ROOT:/app" "$IMAGE" manage.py check
docker run --rm --entrypoint python -e DEBUG=true -v "$ROOT:/app" "$IMAGE" manage.py makemigrations --check --dry-run
docker run --rm --entrypoint python -e DEBUG=true -v "$ROOT:/app" "$IMAGE" manage.py test
