#!/usr/bin/env bash
# Confirms deployed services are actually WORKING, not merely started.
#
#   scripts/verify_deploy.sh scheduler discovery
#
# Split out of deploy.sh so it can be RUN against a deliberately broken
# container. A gate that can only be exercised by doing a real deploy is
# a gate nobody ever watches fail, which is how you end up trusting a
# check whose failure path has never executed.
#
# WHY THE DELTA AND NOT THE STATE. `docker compose up -d` returns when
# containers start, not when they work, and a crashlooping container
# reads as `running` every time you look at it -- measured on this host
# by cupid against a container crashing every 3s: status was `running` at
# all five samples while RestartCount climbed 0,1,2,3,4. So `docker ps`
# passes five times out of five on a service that has never once stayed
# up. Only the CHANGE in RestartCount over a window carries the signal;
# reading it once tells you nothing either.
set -euo pipefail

SETTLE_SECONDS="${SETTLE_SECONDS:-20}"
HOST="${DEPLOY_HOST:-hd-agent}"
PREFIX="${CONTAINER_PREFIX:-internships-}"

if [ $# -eq 0 ]; then
  echo "usage: $0 <service> [service...]" >&2
  exit 2
fi
SERVICES=("$@")

restart_counts() {
  ssh "$HOST" "for s in ${SERVICES[*]}; do
      printf '%s=%s ' \"\$s\" \"\$(docker inspect -f '{{.RestartCount}}' ${PREFIX}\${s}-1 2>/dev/null || echo NA)\"
    done"
}

echo "==> watching ${SERVICES[*]} for ${SETTLE_SECONDS}s"
BEFORE="$(restart_counts)"
sleep "$SETTLE_SECONDS"
AFTER="$(restart_counts)"

if [ "$BEFORE" != "$AFTER" ]; then
  echo "!! a container restarted during the settle window -- likely crashlooping" >&2
  echo "   before: $BEFORE" >&2
  echo "   after:  $AFTER" >&2
  # Logs go to whoever ran the deploy. A gate that fails silently and
  # leaves the logs for the next person has only moved the mystery.
  ssh "$HOST" "cd /srv/internships && docker compose logs --tail 30 ${SERVICES[*]}" >&2 || true
  exit 1
fi

for s in "${SERVICES[@]}"; do
  # An unresolvable name returns `missing` rather than being skipped --
  # that turns a typo'd service into a failure instead of a decoration.
  state="$(ssh "$HOST" "docker inspect -f '{{.State.Status}}' ${PREFIX}${s}-1 2>/dev/null || echo missing")"
  if [ "$state" != "running" ]; then
    echo "!! ${PREFIX}${s}-1 is '$state', not running" >&2
    ssh "$HOST" "cd /srv/internships && docker compose logs --tail 30 ${s}" >&2 || true
    exit 1
  fi
done

# api is deliberately not watched here: confirmed live (2026-09-06) this
# deployment's job is to scan and hand results to the edge, not serve
# HTTP, so api runs on-demand only (docker compose run, never `up`) and
# always shows Exited(0) -- the "running" check above would fail it
# every time if it were passed in. If a future service DOES serve HTTP
# and needs an is-it-actually-answering probe (not just is-it-running),
# add one scoped to that service's own name here.

echo "   ${SERVICES[*]} settled"
