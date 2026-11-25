#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -z "${TEST_DATABASE_URL:-}" && -z "${SUPABASE_DB_URL:-}" ]]; then
  printf '%s\n' 'Integration tests require TEST_DATABASE_URL or SUPABASE_DB_URL' >&2
  exit 2
fi

export SUPABASE_DB_URL="${SUPABASE_DB_URL:-$TEST_DATABASE_URL}"
uv run pytest tests/integration/test_workflow_lock.py::test_drive_lock_is_mutually_exclusive_and_released -m integration
