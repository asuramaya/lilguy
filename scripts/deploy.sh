#!/usr/bin/env bash
# Test, then deploy. Refuses to push if the suite is red.
#
# The deploy path itself (git push deploy master:main; docker compose up
# -d --build) is easy to run by hand and was, repeatedly -- which is how
# a change once reached production and crash-looped the discovery
# container. The tests existed; nothing made running them a precondition.
# This does.
#
#   scripts/deploy.sh                    # test, push, rebuild everything
#   scripts/deploy.sh api                # test, push, rebuild just api
#   SKIP_TESTS=1 scripts/deploy.sh       # explicit, deliberate override
#
set -euo pipefail

cd "$(dirname "$0")/.."

SERVICES=("$@")

if [ "${SKIP_TESTS:-0}" = "1" ]; then
  echo "!! SKIP_TESTS=1 -- deploying WITHOUT running the suite"
else
  scripts/run_tests.sh
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "!! working tree is dirty -- commit before deploying:" >&2
  git status --short >&2
  exit 1
fi

echo "==> pushing to deploy remote"
git push deploy master:main

echo "==> rebuilding on hyper-docker"
if [ ${#SERVICES[@]} -eq 0 ]; then
  ssh hd-agent "cd /srv/internships && docker compose up -d --build"
else
  ssh hd-agent "cd /srv/internships && docker compose up -d --build ${SERVICES[*]}"
fi

# `docker compose up -d` returns when containers START, not when they
# work. Without this block a container that starts and then crashloops
# reported a clean deploy -- and that is not hypothetical here: a bad
# candidate row once crashlooped the discovery container, and Docker's
# restart policy dutifully brought it back up to re-crash forever. A gate
# that stops at "started" would have called that a success.
#
# So: watch for the gap between started and working. A climbing
# RestartCount is the signal that catches a crashloop, because a
# crashlooping container is always "running" when you look at it.
echo "==> verifying the deploy is actually up"
WATCH=("${SERVICES[@]}")
if [ ${#WATCH[@]} -eq 0 ]; then WATCH=(api scheduler discovery); fi

restart_counts() {
  ssh hd-agent "for s in ${WATCH[*]}; do
      printf '%s=%s ' \"\$s\" \"\$(docker inspect -f '{{.RestartCount}}' internships-\${s}-1 2>/dev/null || echo NA)\"
    done"
}

BEFORE="$(restart_counts)"
sleep 20
AFTER="$(restart_counts)"

if [ "$BEFORE" != "$AFTER" ]; then
  echo "!! a container restarted during the settle window -- likely crashlooping" >&2
  echo "   before: $BEFORE" >&2
  echo "   after:  $AFTER" >&2
  ssh hd-agent "cd /srv/internships && docker compose logs --tail 30 ${WATCH[*]}" >&2
  exit 1
fi

for s in "${WATCH[@]}"; do
  state="$(ssh hd-agent "docker inspect -f '{{.State.Status}}' internships-${s}-1 2>/dev/null || echo missing")"
  if [ "$state" != "running" ]; then
    echo "!! internships-${s}-1 is '$state', not running" >&2
    ssh hd-agent "cd /srv/internships && docker compose logs --tail 30 ${s}" >&2
    exit 1
  fi
done

# The api is the only service with an HTTP surface, so it's the only one
# that can be asked whether it actually serves rather than merely runs.
if printf '%s\n' "${WATCH[@]}" | grep -qx api; then
  if ! ssh hd-agent "curl -sf -m 10 http://127.0.0.1:8000/health >/dev/null"; then
    echo "!! api is running but /health does not answer" >&2
    exit 1
  fi
  echo "   api /health ok"
fi

echo "==> deployed $(git rev-parse --short HEAD), services settled"
