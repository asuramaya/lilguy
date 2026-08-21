#!/usr/bin/env bash
# Generates edge static bundle and publishes to Cloudflare Pages (lilguy.win).
#
# Usage:
#   scripts/publish_edge.sh
#   scripts/publish_edge.sh --skip-deploy
#
set -euo pipefail

cd "$(dirname "$0")/.."

OUT_DIR="${OUT_DIR:-dist}"
SKIP_DEPLOY="${1:-}"

echo "==> Exporting postings & edge assets to ${OUT_DIR}..."
if [ -f "/.dockerenv" ] || [ -f "/srv/internships/.env" ]; then
  # Running on hyper-docker or container environment
  python3 service/edge_export.py --out-dir "${OUT_DIR}"
else
  # Running locally
  PY_BIN="python3"
  if [ -x ".venv/bin/python" ]; then
    PY_BIN=".venv/bin/python"
  elif [ -x ".venv-test/bin/python" ]; then
    PY_BIN=".venv-test/bin/python"
  fi
  $PY_BIN service/edge_export.py --out-dir "${OUT_DIR}" || {
    echo "--> Trying via ssh to hd-agent to export database..."
    ssh hd-agent "cd /srv/internships && docker compose run --rm -v /srv/internships:/srv/internships -w /srv/internships api python service/edge_export.py --out-dir /srv/internships/dist"
    echo "--> Syncing dist from hd-agent..."
    rsync -avz hd-agent:/srv/internships/dist/ "${OUT_DIR}/"
  }
fi

if [ "$SKIP_DEPLOY" = "--skip-deploy" ]; then
  echo "==> Skip deploy flag set. Output preserved in ${OUT_DIR}."
  exit 0
fi

echo "==> Deploying to Cloudflare Pages (project: lilguy)..."
if command -v wrangler >/dev/null 2>&1; then
  wrangler pages deploy "${OUT_DIR}" --project-name lilguy --branch main --commit-dirty=true
  echo "==> Deployed to https://lilguy.pages.dev / https://lilguy.win"
else
  echo "!! wrangler not found on this host. Run from a host with wrangler or install wrangler."
fi
