# OpenRAG Admin UI

The admin console for OpenRAG (partitions, documents, jobs, users, models, presets,
system). React 19 + Vite + TypeScript + Tailwind 4 + shadcn/Radix. Authentication is
SSO (OIDC) with a bearer-token fallback for programmatic access; chat lives in Chainlit,
not here.

## Develop

```bash
npm install
npm run dev          # against a real OpenRAG API (set VITE_API_BASE_URL)
VITE_MOCK_API=true npm run dev   # against MSW mocks, no backend needed
```

> **MSW note:** mock-mode needs the generated service-worker stub at
> `public/mockServiceWorker.js`. It is **git-ignored** (a generated, dev/test-only
> artifact). Regenerate it once after cloning if you want mock mode:
>
> ```bash
> npx msw init public/
> ```

## Build & checks

```bash
npm run build        # tsc -b && vite build
npm run lint
```

## Environment

| Var | Purpose |
|-----|---------|
| `VITE_API_BASE_URL` | OpenRAG API base (empty = same origin) |
| `VITE_AUTH_MODE` | `oidc` (default) or `token` |
| `VITE_MOCK_API` | `true` to serve MSW mocks in dev |
| `VITE_BASE_PATH` | base path when served under a sub-path |
