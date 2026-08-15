#!/usr/bin/env bash
# Nightly pg_dump of the live internships Postgres, run via cron on the
# deployment host. Lands in /var/backups/internships/, where the existing
# host-level TrueNAS pull already collects everything under /var/backups/.
#
# Deliberately does NOT restore-test itself on every run (that would mean
# spinning up a scratch Postgres container nightly) -- restore-testing is a
# separate, periodic discipline, not something this script can assert about
# itself. See docs/service-architecture.md and task #13's handoff notes for
# why "the dump wrote successfully" and "the dump restores cleanly" are
# different claims that must both be checked, just not necessarily by the
# same cron run.
set -euo pipefail

COMPOSE_DIR="/srv/internships"
BACKUP_DIR="/var/backups/internships"
KEEP_DAYS=14
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_FILE="${BACKUP_DIR}/internships-${STAMP}.sql.gz"

mkdir -p "${BACKUP_DIR}"

cd "${COMPOSE_DIR}"
docker compose exec -T postgres pg_dump -U internships -d internships \
  | gzip > "${OUT_FILE}.tmp"
mv "${OUT_FILE}.tmp" "${OUT_FILE}"

# Sanity check: a truncated/empty dump is worse than no dump -- it would
# silently pass a "file exists" check while being useless to restore.
SIZE=$(stat -c%s "${OUT_FILE}")
if [ "${SIZE}" -lt 1024 ]; then
  echo "backup_postgres.sh: ${OUT_FILE} is suspiciously small (${SIZE} bytes) -- not trusting it" >&2
  rm -f "${OUT_FILE}"
  exit 1
fi

find "${BACKUP_DIR}" -name 'internships-*.sql.gz' -mtime "+${KEEP_DAYS}" -delete

echo "backup_postgres.sh: wrote ${OUT_FILE} (${SIZE} bytes)"
