# Contributing to OpenRag

Thanks for contributing! This guide covers how we branch, review, and release.

## Branching model (git flow)

OpenRag follows [git flow](https://nvie.com/posts/a-successful-git-branching-model/). There are two
long-lived branches, plus short-lived branches for features, releases, and hotfixes.

| Branch | Purpose | Branches from | Merges into |
|--------|---------|---------------|-------------|
| `main` | Production. Every commit is a released, tagged version. **Never commit directly.** | — | — |
| `develop` | Integration branch. The next release accumulates here. | — | — |
| `feature/*` | A single feature or fix. | `develop` | `develop` |
| `release/X.Y.Z` | Stabilize a version before shipping. | `develop` | `main` **and** `develop` |
| `hotfix/*` | Urgent production fix that can't wait for the next release. | `main` | `main` **and** `develop` |

```text
feature/*  ──► develop ──► release/X.Y.Z ──► main   (tag vX.Y.Z)
                   ▲                            │
hotfix/*  ─────────┴────────── main ────────────┘
```

### Feature work

Everyday work — features and non-urgent bug fixes — happens on `feature/*` branches cut from `develop`,
and is merged back into `develop` via pull request.

```bash
git switch develop
git pull
git switch -c feature/my-change
# ...commit...
git push -u origin feature/my-change
# open a PR with base = develop
```

Open the PR against **`develop`**, not `main`.

### Releases

Cut a `release/X.Y.Z` branch off `develop` **when the features intended for that version are complete
and you want to stabilize without freezing `develop`.** A release branch is tied to a *version you are
shipping* — not to a unit of work, and not one per PR.

Once cut:

- `release/X.Y.Z` takes **only** release preparation — bug fixes, version bumps, changelog, docs. **No new features.**
- `develop` reopens immediately for the next cycle's features (they proceed in parallel).

When the release is ready:

1. Merge `release/X.Y.Z` into `main`.
2. Tag `main` with `vX.Y.Z` — this triggers the GA image build (`.github/workflows/build.yml`).
3. Merge `release/X.Y.Z` back into `develop` so the stabilization fixes and version bump aren't lost.

Name the branch to match the tag scheme (`release/2.1.0` → tag `v2.1.0`).

### Hotfixes

For an urgent fix to something already in production that can't wait for the next release, branch
`hotfix/*` off `main`, then merge it into **both** `main` (tag a new patch version) **and** `develop`.

## Rule of thumb

- Building something? → `feature/*` off `develop`.
- Decided to ship a version? → cut `release/X.Y.Z` off `develop`.
- Production is broken and it can't wait? → `hotfix/*` off `main`.

## Pull requests

- Target `develop` for features and fixes; `main` only receives release and hotfix merges.
- Keep the branch focused and the history clean.
- Ensure CI is green before requesting review (lint, tests, layer-import check, integration).

## Local checks before pushing

```bash
uv run ruff check openrag/ tests/
uv run ruff format --check openrag/ tests/
uv run python scripts/check_layer_imports.py
uv run pytest tests/unit/
```
