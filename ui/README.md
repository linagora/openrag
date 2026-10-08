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

All `VITE_*` settings are build-time (Vite inlines `import.meta.env.*` — rebuild after changing).
The UI discovers the auth mode (`token` / `oidc`) from the backend at runtime, not from a build var.

| Var | Purpose |
|-----|---------|
| `VITE_API_BASE_URL` | OpenRAG API base (empty = same origin) |
| `VITE_BASE_PATH` | base path when served under a sub-path (default `/`) |
| `VITE_GRAFANA_URL` | Build-time fallback for the Grafana dashboard link |
| `VITE_APP_NAME` | app display name / branding (default `OpenRAG`) |
| `VITE_MOCK_API` | `true` to serve MSW mocks in dev |

Production deployments should set `GRAFANA_URL` on the OpenRAG API. The Admin UI reads it at runtime, so the same prebuilt UI image can point to different dashboards.
