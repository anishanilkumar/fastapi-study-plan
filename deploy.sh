#!/bin/sh
# Copy the app to a server over SSH and restart its service.
#
# Reads DEPLOY_HOST (user@host), DEPLOY_DIR and optionally DEPLOY_SERVICE
# (a systemd unit to restart) from .deploy.env, which is gitignored; see
# .deploy.env.example. Your config.json goes too, if you have one.
# Progress (the SQLite file) lives wherever STUDY_DB points on the server and
# is never touched.
set -eu
cd "$(dirname "$0")"
[ -f .deploy.env ] || { echo "Missing .deploy.env: copy .deploy.env.example and fill it in." >&2; exit 1; }
. ./.deploy.env
: "${DEPLOY_HOST:?set in .deploy.env}" "${DEPLOY_DIR:?set in .deploy.env}"
files="app.py study-plan.html sw.js manifest.webmanifest quizzes.json drills.json lessons fonts icons"
[ -f config.json ] && files="$files config.json"
# shellcheck disable=SC2086
rsync -av --delete --exclude __pycache__ $files "$DEPLOY_HOST:$DEPLOY_DIR/"
if [ -n "${DEPLOY_SERVICE:-}" ]; then
  ssh "$DEPLOY_HOST" "sudo systemctl restart $DEPLOY_SERVICE && systemctl is-active $DEPLOY_SERVICE"
fi
echo "Deployed to $DEPLOY_HOST:$DEPLOY_DIR"
