#!/usr/bin/env bash
# Restore-tests the most recent backup_postgres.sh dump into a throwaway
# Postgres container and compares row counts against the live database.
# "The dump wrote successfully" is not "the dump restores" -- this session
# found a real bug (next_check_at never set on promotion) only by actually
# doing this, per Cupid's own warning that an un-restored backup is a
# rumour. Run this periodically (weekly cron, or by hand before trusting
# a backup for a real recovery), not on every dump.
set -euo pipefail

COMPOSE_DIR="/srv/internships"
BACKUP_DIR="/var/backups/internships"
SCRATCH_CONTAINER="internships-restore-test"
SCRATCH_PASSWORD="restore-test-scratch-password"

LATEST=$(ls -t "${BACKUP_DIR}"/internships-*.sql.gz 2>/dev/null | head -1)
if [ -z "${LATEST}" ]; then
  echo "restore_test_backup.sh: no backups found in ${BACKUP_DIR}" >&2
  exit 1
fi
echo "restore_test_backup.sh: testing ${LATEST}"

docker rm -f "${SCRATCH_CONTAINER}" >/dev/null 2>&1 || true
docker run -d --name "${SCRATCH_CONTAINER}" \
  -e POSTGRES_USER=internships \
  -e POSTGRES_PASSWORD="${SCRATCH_PASSWORD}" \
  -e POSTGRES_DB=internships \
  postgres:16-alpine >/dev/null

cleanup() { docker rm -f "${SCRATCH_CONTAINER}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "restore_test_backup.sh: waiting for scratch postgres..."
for _ in $(seq 1 30); do
  if docker exec "${SCRATCH_CONTAINER}" pg_isready -U internships >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

gunzip -c "${LATEST}" | docker exec -i "${SCRATCH_CONTAINER}" psql -U internships -d internships -q >/dev/null

echo "restore_test_backup.sh: row counts (restored vs live)"
for TABLE in sources postings discovery_candidates scrape_runs; do
  RESTORED=$(docker exec "${SCRATCH_CONTAINER}" psql -U internships -d internships -tAc "SELECT count(*) FROM ${TABLE}")
  LIVE=$(cd "${COMPOSE_DIR}" && docker compose exec -T postgres psql -U internships -d internships -tAc "SELECT count(*) FROM ${TABLE}")
  echo "  ${TABLE}: restored=${RESTORED} live=${LIVE}"
  if [ "${RESTORED}" != "${LIVE}" ]; then
    echo "restore_test_backup.sh: MISMATCH on ${TABLE} -- backup does not reflect live state (dump may predate recent writes; re-run right after a fresh dump before treating this as a failure)" >&2
  fi
done

echo "restore_test_backup.sh: done"
