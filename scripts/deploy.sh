#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 user@host /remote/path/to/pridurok [--env-file ./path/.env]"
  exit 2
fi

REMOTE=$1
REMOTE_PATH=$2
ENV_FILE=./.env
if [ "$#" -ge 3 ] && [ "$3" = "--env-file" ]; then
  ENV_FILE=$4
fi

echo "Deploying to ${REMOTE}:${REMOTE_PATH}"

# Upload code to a temporary dir on remote
rsync -avz --delete --exclude '.venv' --exclude 'memory_db' --exclude 'logs' --exclude '.git' ./ ${REMOTE}:${REMOTE_PATH}/tmp_deploy/

echo "Copying .env to remote /tmp/pridurok.env"
scp "${ENV_FILE}" ${REMOTE}:/tmp/pridurok.env

echo "Moving files into place and restarting service (requires sudo on remote)"
ssh ${REMOTE} "sudo rsync -av --delete ${REMOTE_PATH}/tmp_deploy/ ${REMOTE_PATH}/ && sudo mv /tmp/pridurok.env ${REMOTE_PATH}/.env && perl -0pi -e 's/^(?:\\xEF\\xBB\\xBF)+//' ${REMOTE_PATH}/*.py || true && sudo systemctl restart pridurok.service && sudo systemctl status pridurok.service --no-pager -l && sudo journalctl -u pridurok.service -n 200 --no-pager"

echo "Deploy finished. Review the remote service status above."
