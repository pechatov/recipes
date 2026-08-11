#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY="${REPOSITORY:-pechatov/recipes}"
: "${RUNNER_HOST:?Set RUNNER_HOST}"
: "${RUNNER_SOURCE_DIR:?Set RUNNER_SOURCE_DIR}"
: "${RUNNER_TARGET_DIR:?Set RUNNER_TARGET_DIR}"
RUNNER_NAME="${RUNNER_NAME:-recipes-deploy}"
RUNNER_LABEL="${RUNNER_LABEL:-recipes-truenas-deploy}"

if [[ ! "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] \
  || [[ ! "$RUNNER_NAME" =~ ^[A-Za-z0-9_.-]+$ ]] \
  || [[ ! "$RUNNER_LABEL" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "Invalid runner identity settings." >&2
  exit 1
fi
for runner_path in "$RUNNER_SOURCE_DIR" "$RUNNER_TARGET_DIR"; do
  if [[ ! "$runner_path" =~ ^/[A-Za-z0-9._/-]+$ ]] \
    || [[ "$runner_path" == *"/../"* || "$runner_path" == *"/./"* ]]; then
    echo "Invalid runner path." >&2
    exit 1
  fi
done

command -v gh >/dev/null
gh auth status >/dev/null
registration_token="$(gh api --method POST "repos/$REPOSITORY/actions/runners/registration-token" --jq .token)"

ssh "$RUNNER_HOST" bash -s -- \
  "$REPOSITORY" "$RUNNER_NAME" "$RUNNER_LABEL" "$registration_token" \
  "$RUNNER_SOURCE_DIR" "$RUNNER_TARGET_DIR" <<'REMOTE'
set -Eeuo pipefail
REPOSITORY="$1"
RUNNER_NAME="$2"
RUNNER_LABEL="$3"
RUNNER_TOKEN="$4"
source_runner="$5"
target_runner="$6"

if [[ ! -x "$source_runner/config.sh" ]]; then
  echo "Existing task-tracker Actions runner distribution was not found" >&2
  exit 1
fi

if [[ -f "$target_runner/.runner" ]]; then
  cd "$target_runner"
  sudo ./svc.sh start >/dev/null 2>&1 || true
  echo "Recipes Actions runner is already configured."
  exit 0
fi

install -d -m 0700 "$target_runner"
rsync -a \
  --exclude='.runner' \
  --exclude='.runner_migrated' \
  --exclude='.credentials' \
  --exclude='.credentials_rsaparams' \
  --exclude='.service' \
  --exclude='_diag/' \
  --exclude='_work/' \
  "$source_runner/" "$target_runner/"

# The runner updater keeps `bin` and `externals` as absolute symlinks. Point
# the copied distribution at its own versioned directories so Runner.Listener
# does not discover the task-tracker runner's .runner file.
bin_version="$(basename "$(readlink "$source_runner/bin")")"
externals_version="$(basename "$(readlink "$source_runner/externals")")"
ln -sfn "$bin_version" "$target_runner/bin"
ln -sfn "$externals_version" "$target_runner/externals"
rm -f "$target_runner/.path" "$target_runner/.service"

cd "$target_runner"
./config.sh \
  --unattended \
  --replace \
  --url "https://github.com/$REPOSITORY" \
  --token "$RUNNER_TOKEN" \
  --name "$RUNNER_NAME" \
  --labels "$RUNNER_LABEL" \
  --work _work
sudo ./svc.sh install "$(id -un)"
sudo ./svc.sh start
echo "Recipes Actions runner configured."
REMOTE
