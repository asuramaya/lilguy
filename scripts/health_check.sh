#!/usr/bin/env bash
# Runs ON hd-agent (via cron), checks a handful of things a silent failure
# actually took this project down over: disk space, whether the core
# containers are running, whether Docker's build cache has grown back
# large enough to risk another disk-full crash, and whether the live
# edge (lilguy.win) has actually been republished recently -- discovered
# live, 2026-09-22: the site sat 20 days stale not because scraping
# stopped, but because publish_edge.sh has always been a manual-only
# step with nothing to notice when nobody ran it. Posts to Discord ONLY
# when something's wrong -- a channel that pings on every healthy run
# trains everyone to ignore it, which defeats the point of having one.
#
#   DISCORD_WEBHOOK_URL=... scripts/health_check.sh
#
# Cron entry lives in hd-agent's own crontab (not committed -- it embeds
# the webhook URL; logs go under /home/agent, NOT /var/log -- user agent cannot
# create files there, so a >> redirect fails and cron silently never runs the
# command, which is how this check sat dead for 17 days), alongside the existing backup/audit jobs:
#   */15 * * * * DISCORD_WEBHOOK_URL=... /srv/internships/scripts/health_check.sh >> /home/agent/logs/internships-health.log 2>&1
set -uo pipefail

PROBLEMS=()

# 1. Disk space -- the actual root cause of the 2026-09-22 outage
#    (Docker build cache grew to 69.91GB unpruned, filled the disk,
#    crashed Postgres with DiskFull, which crash-looped scheduler and
#    discovery whenever they tried to connect).
DISK_PCT="$(df --output=pcent / | tail -1 | tr -dc '0-9')"
if [ -n "$DISK_PCT" ] && [ "$DISK_PCT" -ge 85 ]; then
  PROBLEMS+=("Disk at ${DISK_PCT}% on /. Docker build cache is the usual culprit -- check \`docker system df\` and \`docker builder prune -af\` if it's large.")
fi

# 2. Docker's build cache specifically -- catch it building back up
#    toward the same failure mode before disk% alone would flag it.
BUILD_CACHE_GB="$(docker system df --format '{{.Type}} {{.Size}}' 2>/dev/null | awk '$1=="Build" {print $3}' | grep -oE '^[0-9.]+' | head -1)"
if [ -n "${BUILD_CACHE_GB:-}" ]; then
  BUILD_CACHE_INT="${BUILD_CACHE_GB%.*}"
  if [ "${BUILD_CACHE_INT:-0}" -ge 40 ]; then
    PROBLEMS+=("Docker build cache at ${BUILD_CACHE_GB}GB -- prune before it repeats the disk-full crash.")
  fi
fi

# 3. Core containers actually running, not just present. A crash-looping
#    container reads 'running' between crashes (verify_deploy.sh's own
#    lesson, from the same incident class) -- RestartCount is checked
#    separately by verify_deploy.sh right after a deploy; this is the
#    ongoing background check, so state alone is enough here.
cd /srv/internships || exit 1
for s in postgres discovery scheduler; do
  state="$(docker inspect -f '{{.State.Status}}' "internships-${s}-1" 2>/dev/null || echo missing)"
  if [ "$state" != "running" ]; then
    PROBLEMS+=("internships-${s}-1 is '${state}', not running.")
  fi
done

# 4. Postgres actually accepting connections (state=running doesn't
#    guarantee this -- confirmed live during the same incident, it read
#    'running' while logging \"database system is in recovery mode\").
if ! docker compose exec -T postgres pg_isready -U internships >/dev/null 2>&1; then
  PROBLEMS+=("Postgres container is running but not accepting connections (pg_isready failed).")
fi

# 5. The edge itself -- the thing a human actually sees. Stale here is
#    exactly what a 20-day-silent outage looks like from outside, even
#    when scraping never stopped.
META="$(curl -sf -m 15 https://lilguy.win/data/meta.json 2>/dev/null)"
if [ -z "$META" ]; then
  PROBLEMS+=("lilguy.win/data/meta.json did not respond.")
else
  GEN_AT="$(echo "$META" | python3 -c "import json,sys; print(json.load(sys.stdin).get('generated_at',''))" 2>/dev/null)"
  if [ -z "$GEN_AT" ]; then
    PROBLEMS+=("lilguy.win/data/meta.json has no generated_at field.")
  else
    AGE_HOURS="$(python3 -c "
from datetime import datetime, timezone
try:
    gen = datetime.fromisoformat('$GEN_AT'.replace('Z', '+00:00'))
    print(int((datetime.now(timezone.utc) - gen).total_seconds() / 3600))
except Exception:
    print(-1)
")"
    if [ "$AGE_HOURS" -lt 0 ]; then
      PROBLEMS+=("Could not parse lilguy.win's generated_at ('$GEN_AT').")
    elif [ "$AGE_HOURS" -ge 6 ]; then
      PROBLEMS+=("Live edge is ${AGE_HOURS}h stale (generated_at=$GEN_AT). publish_edge.sh's cron entry may not be running.")
    fi
  fi
fi

# Report
#
# Discord dedupe: the check runs every 15 min, so a single multi-hour
# staleness episode used to post ~40 identical messages (83 for two episodes,
# 2026-10-10..14). Post when the SET of problems changes (hour counts are
# stripped so "6h stale" and "7h stale" are the same problem), repeat at most
# every REPEAT_HOURS while it persists, and post one "recovered" note when it
# clears. Everything is still written to the log every run.
STATE_FILE="${HEALTH_STATE_FILE:-/home/agent/logs/internships-health.state}"
REPEAT_HOURS="${HEALTH_REPEAT_HOURS:-6}"
NOW_EPOCH="$(date +%s)"

post_discord() {
  [ -n "${DISCORD_WEBHOOK_URL:-}" ] || { echo "!! DISCORD_WEBHOOK_URL not set -- problems logged here only" >&2; return 0; }
  # jq builds the JSON payload (not printf/string interpolation) so
  # text containing quotes or newlines can't break the request.
  local payload
  payload="$(jq -n --arg content "$1" '{content: $content}')"
  curl -sf -m 15 -H "Content-Type: application/json" -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null || \
    echo "!! also failed to post to Discord" >&2
}

if [ "${#PROBLEMS[@]}" -eq 0 ]; then
  echo "==> health check ok ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
  if [ -s "$STATE_FILE" ]; then
    post_discord "$(printf '**lilguy health check recovered** (%s) -- all checks passing again.' "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"
    rm -f "$STATE_FILE"
  fi
  exit 0
fi

echo "!! health check found ${#PROBLEMS[@]} problem(s):"
for p in "${PROBLEMS[@]}"; do echo "   - $p"; done

KEY="$(printf '%s\n' "${PROBLEMS[@]}" | sed -E 's/[0-9]+h stale/Nh stale/; s/generated_at=[^)]*/generated_at=X/' | sha1sum | cut -d' ' -f1)"
LAST_KEY=""; LAST_EPOCH=0
if [ -s "$STATE_FILE" ]; then read -r LAST_KEY LAST_EPOCH < "$STATE_FILE" || true; fi
if [ "$KEY" != "$LAST_KEY" ] || [ $(( NOW_EPOCH - ${LAST_EPOCH:-0} )) -ge $(( REPEAT_HOURS * 3600 )) ]; then
  BODY="$(printf '**lilguy health check failed** (%s)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"
  for p in "${PROBLEMS[@]}"; do BODY="${BODY}$(printf '\n- %s' "$p")"; done
  post_discord "$BODY"
  echo "$KEY $NOW_EPOCH" > "$STATE_FILE"
fi

exit 1
