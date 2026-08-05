#!/usr/bin/env bash
# Shared finalizer for Python changes. `--no-sync` preserves a manually
# installed editable hbrowser checkout in the project environment.

set -eu
trap 'exit 2' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

# Discover every tracked or non-ignored untracked Python source, at any depth.
# NUL delimiters keep filenames with whitespace and other shell characters safe.
PY_FILES=()
while IFS= read -r -d '' file; do
    if [[ -f "$file" ]]; then
        PY_FILES+=("$file")
    fi
done < <(
    git ls-files --cached --others --exclude-standard -z -- '*.py' '*.pyi'
)

if [[ ${#PY_FILES[@]} -eq 0 ]]; then
    exit 0
fi

# Tests use dynamic mocks extensively, so the production type-checking boundary
# remains narrower than the formatting and linting boundary above.
TYPE_PATHS=(src/hvbrowser scripts/live_readonly_smoke.py)

uv run --no-sync black "${PY_FILES[@]}" >&2
uv run --no-sync ruff check --fix "${PY_FILES[@]}" >&2
uv run --no-sync black "${PY_FILES[@]}" >&2
uv run --no-sync mypy "${TYPE_PATHS[@]}" >&2
