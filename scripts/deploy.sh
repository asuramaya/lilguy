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

echo "==> deployed $(git rev-parse --short HEAD)"
