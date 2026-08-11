#!/usr/bin/env bash
set -Eeuo pipefail

REPOSITORY="${REPOSITORY:-pechatov/recipes}"
RUNNER_HOST="${RUNNER_HOST:-pi}"
RUNNER_NAME="${RUNNER_NAME:-raspberry-pi-recipes}"
RUNNER_LABEL="${RUNNER_LABEL:-recipes-truenas-deploy}"

command -v gh >/dev/null
gh auth status >/dev/null
registration_token="$(gh api --method POST "repos/$REPOSITORY/actions/runners/registration-token" --jq .token)"

ssh "$RUNNER_HOST" \
  "REPOSITORY='$REPOSITORY' RUNNER_NAME='$RUNNER_NAME' RUNNER_LABEL='$RUNNER_LABEL' RUNNER_TOKEN='$registration_token' bash -s" <<'REMOTE'
set -Eeuo pipefail
source_runner="$HOME/actions-runner/task-tracker"
target_runner="$HOME/actions-runner/recipes"

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
