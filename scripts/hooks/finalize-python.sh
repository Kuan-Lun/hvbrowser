#!/usr/bin/env bash
# Shared finalizer for Python changes. `--no-sync` preserves a manually
# installed editable hbrowser checkout in the project environment.

set -eu
trap 'exit 2' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

FORMAT_PATHS=(src/hvbrowser tests scripts/live_readonly_smoke.py)
TYPE_PATHS=(src/hvbrowser scripts/live_readonly_smoke.py)

uv run --no-sync black "${FORMAT_PATHS[@]}" >&2
uv run --no-sync ruff check --fix "${FORMAT_PATHS[@]}" >&2
uv run --no-sync black "${FORMAT_PATHS[@]}" >&2
uv run --no-sync mypy "${TYPE_PATHS[@]}" >&2
