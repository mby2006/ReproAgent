#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

uv run ruff check .
uv run pytest
pnpm lint:web
pnpm --filter web test
pnpm --filter web exec tsc --noEmit
pnpm build:web
pnpm verify:web:live
pnpm build:web:static
pnpm verify:web:static
for script in scripts/*.sh; do bash -n "$script"; done
