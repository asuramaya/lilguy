#!/usr/bin/env bash
set -euo pipefail

# lilguy Container Worker Entrypoint
# Modes:
#   scheduler   - Runs continuous ingestion scheduler
#   api         - Runs self-hosted FastAPI REST API
#   sync-once   - Scrapes, runs liveness audit, exports edge shards, and exits
#   publish     - Exports and publishes edge bundle to Cloudflare Pages
#   audit       - Runs concurrent liveness audit sweep across corpus

MODE="${1:-scheduler}"

echo "==> lilguy container worker starting in mode: ${MODE}"

case "${MODE}" in
  api)
    echo "==> Starting FastAPI service on 0.0.0.0:8000..."
    exec python3 -m uvicorn service.api:app --host 0.0.0.0 --port 8000
    ;;

  scheduler)
    echo "==> Starting continuous ingestion scheduler..."
    exec python3 service/scheduler.py
    ;;

  sync-once)
    echo "==> Running one-shot sync & edge build..."
    python3 service/standardize.py
    python3 scripts/audit_liveness.py --limit 100 --workers 16 || true
    python3 service/edge_export.py
    echo "==> One-shot sync complete!"
    ;;

  publish)
    echo "==> Running edge export and Cloudflare Pages deployment..."
    ./scripts/publish_edge.sh
    ;;

  audit)
    LIMIT="${2:-200}"
    WORKERS="${3:-20}"
    echo "==> Running liveness audit (limit: ${LIMIT}, workers: ${WORKERS})..."
    exec python3 scripts/audit_liveness.py --limit "${LIMIT}" --workers "${WORKERS}"
    ;;

  *)
    echo "==> Executing custom command: $@"
    exec "$@"
    ;;
esac
