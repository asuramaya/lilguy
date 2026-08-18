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
# Deliberately NOT --rm: the cleanup trap removes it, and keeping the
# container alive on failure is what makes the logs readable below. With
# --rm a startup failure deletes its own evidence, and the only thing
# left is this script's own uninformative "never became ready".
docker run -d --name "$CONTAINER" -e POSTGRES_PASSWORD=test -P "$PG_IMAGE" >/dev/null
PORT="$(docker port "$CONTAINER" 5432/tcp | head -1 | sed 's/.*://')"

# postgres:16-alpine starts a temporary server, runs initdb, stops it,
# then starts the real one -- so readiness can flicker true and then
# false during init. Requiring TWO consecutive successes avoids latching
# onto the temporary server, and 120 x 0.5s tolerates a slow start under
# load (seen intermittently on a busy host).
ready=0
for _ in $(seq 1 120); do
  if docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1; then
    ready=$((ready + 1))
    [ "$ready" -ge 2 ] && break
  else
    ready=0
  fi
  sleep 0.5
done

if [ "$ready" -lt 2 ]; then
  echo "!! scratch postgres never became ready -- its own logs follow" >&2
  docker logs "$CONTAINER" 2>&1 | tail -25 >&2
  docker inspect -f 'state={{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}' "$CONTAINER" >&2 || true
  exit 1
fi

if [ ! -d "$VENV" ]; then
  echo "==> creating $VENV"
  python3 -m venv "$VENV"
fi
echo "==> installing deps"
"$VENV/bin/pip" install -q -r service/requirements.txt -r requirements-dev.txt

# The frontend is a single static file with no build step, so its few
# testable pure functions are exercised by extracting them out of
# index.html with node. Skipped rather than failed where node is absent:
# node is not otherwise a dependency of this project, and making it one
# to run a handful of assertions would be a poor trade.
if command -v node >/dev/null 2>&1; then
  echo "==> running frontend tests"
  for js in tests/frontend/*.js; do node "$js"; done
else
  echo "==> skipping frontend tests (node not installed)"
fi

echo "==> running tests against scratch postgres on port $PORT"
DATABASE_URL="postgresql://postgres:test@127.0.0.1:$PORT/postgres" \
  "$VENV/bin/python" -m pytest tests/ "$@"
