#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

uv run ruff check .
uv run pytest
uv run pytest scripts/test_audit_legacy_consumers.py scripts/test_smoke_live_v2.py
uv run python scripts/audit_legacy_consumers.py --format json --output _bmad-output/acceptance/v2/legacy-consumers.json
uv run python scripts/audit_legacy_consumers.py --check-migrations
pnpm lint:web
pnpm --filter web test
pnpm --filter web exec tsc --noEmit
pnpm build:web
pnpm verify:web:live
pnpm build:web:static
pnpm verify:web:static
for script in scripts/*.sh; do bash -n "$script"; done
