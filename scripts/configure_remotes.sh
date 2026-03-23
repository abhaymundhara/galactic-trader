#!/usr/bin/env bash
set -euo pipefail

# Configure a fork-first remote layout:
# - origin   -> your fork
# - upstream -> TauricResearch/TradingAgents (or custom URL)
#
# Usage:
#   scripts/configure_remotes.sh --fork-url https://github.com/<you>/TradingAgents.git
#   scripts/configure_remotes.sh --fork-url ... --upstream-url https://github.com/TauricResearch/TradingAgents.git

FORK_URL=""
UPSTREAM_URL="https://github.com/TauricResearch/TradingAgents.git"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fork-url)
      FORK_URL="${2:-}"
      shift 2
      ;;
    --upstream-url)
      UPSTREAM_URL="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$FORK_URL" ]]; then
  echo "Missing required --fork-url" >&2
  exit 1
fi

if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$FORK_URL"
else
  git remote add origin "$FORK_URL"
fi

if git remote get-url upstream >/dev/null 2>&1; then
  git remote set-url upstream "$UPSTREAM_URL"
else
  git remote add upstream "$UPSTREAM_URL"
fi

echo "Configured remotes:"
git remote -v
