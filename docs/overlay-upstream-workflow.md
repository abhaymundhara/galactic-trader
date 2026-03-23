# Overlay + Upstream Sync Workflow

This repo is under active upstream development. To reduce merge churn, keep strategy-specific code in an overlay layer and leave `tradingagents/*` untouched unless absolutely necessary.

## 1) Remote Layout

Target layout:

- `origin`: your fork
- `upstream`: TauricResearch/TradingAgents

Setup:

```bash
scripts/configure_remotes.sh --fork-url https://github.com/<you>/TradingAgents.git
```

Optional:

```bash
scripts/configure_remotes.sh \
  --fork-url https://github.com/<you>/TradingAgents.git \
  --upstream-url https://github.com/TauricResearch/TradingAgents.git
```

## 2) Extension Boundary

Custom behavior lives in:

- `extensions/gold_paper/`

The overlay imports and orchestrates `TradingAgentsGraph` from upstream code without editing core modules.

## 3) Sync Procedure

Use guarded sync:

```bash
scripts/sync_upstream.sh --mode rebase
```

What it does:

1. Requires a clean worktree.
2. Fetches `upstream` (including tags).
3. Creates a rollback branch: `backup/<current-branch>-<timestamp>`.
4. Rebases (or merges) onto `upstream/main`.

If conflicts occur, resolve, run verification, and continue.

## 4) Verification Gate

Before promoting synced code:

```bash
python scripts/smoke_overlay.py
pytest -q
```

Notes:

- Current upstream test suite has known import-path issues in this clone.
- Keep a small overlay-specific smoke test so sync regressions are caught quickly.

## 5) Suggested Branch Policy

- `main`: your stable branch for paper trading.
- `codex/integration-*`: temporary sync/integration branches.
- `backup/*`: auto rollback points from sync script.
