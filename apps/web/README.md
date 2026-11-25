# ReproAgent Web

Next.js frontend for paper reproduction, evidence review, experiment approval, execution monitoring, and reports.

## Development

Install dependencies from the repository root with `pnpm install --frozen-lockfile`, then run:

```bash
pnpm dev:web
```

The default mode is `live`. Configure `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_SUPABASE_URL`, and `NEXT_PUBLIC_SUPABASE_ANON_KEY` in `apps/web/.env.local` for the local backend.

## Production builds

Run these commands from the repository root:

```bash
pnpm build:web              # live server artifact in apps/web/.next/
pnpm build:web:static       # static export in apps/web/out/
pnpm verify:web:live
pnpm verify:web:static
```

`NEXT_PUBLIC_SITE_MODE` is the only mode switch. The build commands set it explicitly, write a `site-mode.json` marker, and validate the resulting artifact. Direct `next build` is supported for local experimentation, but release automation should use the mode-specific commands.

- `live` enables server rendering, `cacheComponents`, and `partialPrefetching`. Start the verified build with `pnpm --filter web start`.
- `static` uses `output: "export"` and omits server-only rendering options. Backend authentication and workspace actions are disabled at build time; the public example remains available.

For the current Qoder Sites static host, publish `apps/web/out/` and use `.html` in deep links, such as `/papers.html?id=<paper_id>`. Static builds automatically add `.html` to internal navigation and use full-page loads so client-side routing never requests extensionless paths. The artifact verifier requires all exported routes and the `_next/static` asset directory.

## Offline checks

```bash
pnpm lint:web
pnpm --filter web test
pnpm --filter web exec tsc --noEmit
```

The repository-wide `bash scripts/check.sh` gate also builds and verifies both modes.
