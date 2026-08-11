#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="recipes:check"

cd "$ROOT"
docker build -t "$IMAGE" .
docker run --rm --entrypoint python -e DEBUG=true -v "$ROOT:/app" "$IMAGE" manage.py check
docker run --rm --entrypoint python -e DEBUG=true -v "$ROOT:/app" "$IMAGE" manage.py makemigrations --check --dry-run
docker run --rm --entrypoint python -e DEBUG=true -v "$ROOT:/app" "$IMAGE" manage.py test
