#!/usr/bin/env bash
set -euo pipefail

# Sync current branch with upstream/main while preserving a rollback point.
#
# Usage:
#   scripts/sync_upstream.sh
#   scripts/sync_upstream.sh --base-branch main --mode rebase
#   scripts/sync_upstream.sh --mode merge

BASE_BRANCH="main"
MODE="rebase"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-branch)
      BASE_BRANCH="${2:-}"
      shift 2
      ;;
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "$MODE" != "rebase" && "$MODE" != "merge" ]]; then
  echo "--mode must be 'rebase' or 'merge'" >&2
  exit 1
fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "Not inside a git repository." >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is not clean. Commit/stash first." >&2
  exit 1
fi

if ! git remote get-url upstream >/dev/null 2>&1; then
  echo "Missing 'upstream' remote. Run scripts/configure_remotes.sh first." >&2
  exit 1
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_BRANCH="backup/${CURRENT_BRANCH}-${STAMP}"

echo "Fetching upstream..."
git fetch upstream --tags

echo "Creating rollback branch: ${BACKUP_BRANCH}"
git branch "${BACKUP_BRANCH}"

if [[ "$MODE" == "rebase" ]]; then
  echo "Rebasing ${CURRENT_BRANCH} onto upstream/${BASE_BRANCH}"
  git rebase "upstream/${BASE_BRANCH}"
else
  echo "Merging upstream/${BASE_BRANCH} into ${CURRENT_BRANCH}"
  git merge --no-ff "upstream/${BASE_BRANCH}" -m "Merge upstream/${BASE_BRANCH} into ${CURRENT_BRANCH}"
fi

echo "Sync complete."
echo "Rollback branch available at: ${BACKUP_BRANCH}"
