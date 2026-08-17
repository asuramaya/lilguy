#!/usr/bin/env bash
# Runs the whole suite against a throwaway Postgres.
#
# Most of tests/service/ is skipped unless DATABASE_URL points at a real
# database -- which meant the DB-backed tests (the ones covering the SQL
# this project has actually been bitten by: the upsert, the dedup sweep,
# the source-sync sweep, probation transitions) only ran when someone
# remembered to stand a Postgres up by hand. In practice that meant they
# mostly did not run before a deploy. This makes running them the easy
# path.
#
#   scripts/run_tests.sh              # everything
#   scripts/run_tests.sh -k dedup     # extra args pass through to pytest
#
set -euo pipefail

cd "$(dirname "$0")/.."

PG_IMAGE="${PG_IMAGE:-postgres:16-alpine}"
CONTAINER="internships-test-pg-$$"
VENV=".venv-test"

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> starting scratch postgres ($PG_IMAGE)"
# -P publishes to a random free host port instead of a fixed one, so this
# can run alongside the real stack (or a second copy of itself) without
# fighting over 5432.
docker run -d --rm --name "$CONTAINER" -e POSTGRES_PASSWORD=test -P "$PG_IMAGE" >/dev/null
PORT="$(docker port "$CONTAINER" 5432/tcp | head -1 | sed 's/.*://')"

for _ in $(seq 1 60); do
  docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 0.5
done
docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1 || {
  echo "!! scratch postgres never became ready" >&2; exit 1; }

if [ ! -d "$VENV" ]; then
  echo "==> creating $VENV"
  python3 -m venv "$VENV"
fi
echo "==> installing deps"
"$VENV/bin/pip" install -q -r service/requirements.txt -r requirements-dev.txt

echo "==> running tests against scratch postgres on port $PORT"
DATABASE_URL="postgresql://postgres:test@127.0.0.1:$PORT/postgres" \
  "$VENV/bin/python" -m pytest tests/ "$@"
