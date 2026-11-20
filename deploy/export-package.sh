#!/usr/bin/env bash
# Export a shareable copy of a working tree (MSG-08). Never includes hidden local folders
# (tool state, .git, virtual environments; only .github is kept),
# caches, virtual environments, secrets or evidence folders, and refuses to finish if
# anything forbidden or a local absolute path slipped through.
#
#   deploy/export-package.sh [--dev] <source-tree> <output-dir>
#
# --dev: a developer share. Also leaves out the internal process records (reviews, task
# prompts, assessments, Kanban, historical test reports), which still contain local
# paths until they are redacted (see CLEANUP-TODO).
#
# Produces <output-dir>/<name>/ and <output-dir>/<name>.tar.gz. Read-only on the source.
set -euo pipefail
DEV=0
if [ "${1:-}" = "--dev" ]; then DEV=1; shift; fi
SRC="$(cd "${1:?source tree}" && pwd)"
OUT="${2:?output dir}"
NAME="$(basename "$SRC")-export"
[ "$DEV" -eq 1 ] && NAME="$(basename "$SRC")-dev-export"
DEST="$OUT/$NAME"
[ -e "$DEST" ] && { echo "refusing: $DEST exists" >&2; exit 1; }
mkdir -p "$DEST"

DEV_EXCLUDES=()
if [ "$DEV" -eq 1 ]; then
  DEV_EXCLUDES=(--exclude '/docs/reviews/' --exclude '/docs/tasks/' --exclude '/docs/implementation/'
    --exclude '/docs/assessments/' --exclude '/docs/decisions/*-prompt.md'
    --exclude '/docs/KANBAN.md' --exclude '/docs/test-report.md')
fi
rsync -a "${DEV_EXCLUDES[@]}" \
  --include '.github/' --include '.github/**' --exclude '.*/' --exclude 'venv/' \
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.pytest_cache/' \
  --exclude '.ruff_cache/' --exclude '.mypy_cache/' --exclude '.DS_Store' \
  --exclude '.env' --exclude '.env.*' --exclude 'secrets/' \
  --exclude '*.key' --exclude '*.pem' --exclude '*.p12' --exclude '*.crt' \
  --exclude '.msg07-evidence/' --exclude '.msg07-return/' --exclude 'dist/' \
  --exclude '*.zip' --exclude '*.tar.gz' \
  "$SRC/" "$DEST/"

fail=0
forbidden="$(find "$DEST" \( \( -type d -name '.*' ! -name '.github' \) -o -name '__pycache__' \
  -o -name '*.pyc' -o -name '.pytest_cache' -o -name '.ruff_cache' -o -name '.mypy_cache' \
  -o -name '.env' -o -name '*.pem' -o -name '*.key' \) -print)"
if [ -n "$forbidden" ]; then echo "FORBIDDEN files present:"; echo "$forbidden"; fail=1; fi
if grep -rIlE 'BEGIN [A-Z ]*PRIVATE KEY|oanmsg_[A-Za-z0-9_-]{43}' "$DEST"; then
  echo "FAIL: key or token material found (files above)"; fail=1
fi
paths="$(grep -rIlE '/Volumes/|/Users/[A-Za-z]|/home/[a-z]' "$DEST" \
  | grep -v -e 'tests/api/test_migrations.py' -e 'deploy/export-package.sh' || true)"
if [ -n "$paths" ]; then
  echo "FAIL: local absolute paths found in:"; echo "$paths"; fail=1
fi
if [ "$fail" -ne 0 ]; then
  echo "export NOT usable: fix the source (see CLEANUP-TODO) and re-run"; rm -rf "$DEST"; exit 1
fi
tar -C "$OUT" -czf "$OUT/$NAME.tar.gz" "$NAME"
echo "exported $DEST and $OUT/$NAME.tar.gz"
