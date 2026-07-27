#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${1:-${REMOTE_HOST:-root@178.104.153.106}}"
APP_DIR="${APP_DIR:-/opt/chm-api}"
BRANCH="${BRANCH:-main}"

if [[ -z "${REMOTE_HOST}" ]]; then
  echo "Usage: ./scripts/update-server.sh <user@host>" >&2
  echo "Default: ./scripts/update-server.sh" >&2
  exit 1
fi

echo "Updating ${REMOTE_HOST} at ${APP_DIR} on branch ${BRANCH}..."

ssh "$REMOTE_HOST" bash -s -- "$APP_DIR" "$BRANCH" <<'REMOTE'
set -euo pipefail

APP_DIR="$1"
BRANCH="$2"

if [[ -z "$APP_DIR" || -z "$BRANCH" ]]; then
  echo "Remote update failed: missing app dir or branch" >&2
  exit 1
fi

sudo mkdir -p "$APP_DIR"
cd "$APP_DIR"

sudo git fetch origin "$BRANCH"
sudo git checkout "$BRANCH"
sudo git pull --ff-only origin "$BRANCH"
sudo docker compose up -d --build

echo "Deployment update complete."
REMOTE
