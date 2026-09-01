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

#### Bumping the version

The version is typed in exactly one place, `pyproject.toml` — `/version` is served from that file's
installed metadata, so it is also the number the running app reports.

One other file has to follow it. `ui/src/lib/whats-new.ts` drives the **NEW** badges in the admin UI
([FEATURE_TAG.md](FEATURE_TAG.md) explains the mechanism and how to tag a feature):
each entry records the version its feature shipped in, and the badge expires on its own a couple of
minors later, so nobody has to remember to delete it. A feature PR cannot know its release number —
this branch is where that number is chosen — so feature PRs write `UNRELEASED` and the release
resolves it.

Do both in the `chore(release): bump version to X.Y.Z` commit, and read the second from the first
rather than typing the number twice:

```bash
# 1. edit pyproject.toml's `version` to X.Y.Z, then read it back:
VER=$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)
[ -n "$VER" ] || { echo "no version found in pyproject.toml — aborting"; exit 1; }

# 2. resolve every pending NEW badge to it
registry=ui/src/lib/whats-new.ts
sed -E "s/^( *\"[^\"]+\": *)\"UNRELEASED\"(,?) *\$/\1\"$VER\"\2/" "$registry" > "$registry.new" \
  && mv "$registry.new" "$registry"
```

The guard is not ceremony: an empty `$VER` — a renamed field, a moved `version` line — would otherwise
write `""` into every pending entry, which parses as nothing and silences the badges it was meant to
switch on. The rewrite goes through a temporary file rather than `sed -i`, whose in-place flag differs
between GNU and BSD `sed` and fails outright on macOS.

The pattern is anchored to a whole registry line — indent, quoted key, colon, and an optional
trailing comma (the last entry in the object may legally omit it, and the replacement keeps whichever
form was there) — so it cannot touch the `UNRELEASED` constant itself or this paragraph. A looser
match on the bare string corrupts both, silently.

An entry left unresolved badges every user forever, and nobody notices, because nobody inspects a
dropdown expecting an option *not* to say NEW. So `verify-tag` in `.github/workflows/build.yml`
refuses to publish GA images while any entry is still unresolved.

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
