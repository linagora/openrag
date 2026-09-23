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

Other files have to follow it. `ui/src/lib/whats-new.ts` drives the **NEW** badges in the admin UI
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

Three deployment files name the released image tag and are bumped by hand in the same commit — they
are what an operator actually pulls, so a missed one ships the previous release under the new
number:

| File | What carries the version |
|------|--------------------------|
| `infra/charts/openrag-stack/Chart.yaml` | `appVersion` (and see [Helm chart](#helm-chart) — its own `version` is separate) |
| `infra/charts/openrag-stack/values.yaml` | the `tag: "vX.Y.Z"` of each OpenRag image |
| `infra/compose/docker-compose.yaml` | the `linagoraai/openrag*:vX.Y.Z` images |

Before opening the release PR, check the bump from both sides. First, nothing under `infra/` should
still name the version you are replacing — this is what finds a file the table doesn't list:

```bash
PREV=2.2.0   # the version you are replacing
git grep -nF "$PREV" -- infra/ || echo "clean"
```

That sweep alone can't prove the bump: a file that was already older than `$PREV` never matches it,
so the sweep reports clean while that file ships an even older image. So also read every site in the
table back and compare it with `pyproject.toml`:

```bash
VER=$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)
chart=infra/charts/openrag-stack
compose=infra/compose/docker-compose.yaml
# every site that must name $VER, as file:what — each must be found exactly once
sites="$chart/Chart.yaml:appVersion
       $chart/values.yaml:linagora/openrag-ray
       $chart/values.yaml:linagoraai/openrag-admin-ui
       $chart/values.yaml:linagoraai/openrag
       ${compose}:linagoraai/openrag
       ${compose}:linagoraai/openrag-admin-ui"
{
  awk '$1 == "appVersion:" { print FILENAME ":appVersion", $2 }' "$chart/Chart.yaml"
  awk '$1 == "repository:" { repo = $2 }
       $1 == "tag:" && repo ~ /openrag/ { print FILENAME ":" repo, $2; repo = "" }' "$chart/values.yaml"
  awk '$1 == "image:" && $2 ~ /^linagoraai\/openrag/ { split($2, img, ":"); print FILENAME ":" img[1], img[2] }' \
    "$compose"
} | tr -d '"' | awk -v want="$VER" -v sites="$sites" '
  BEGIN { n = split(sites, s); for (i = 1; i <= n; i++) expected[s[i]] }
  { found[$1]++; v = $2; sub(/^v/, "", v)
    if (!($1 in expected)) { print "EXTRA", $0; bad = 1 }
    else if (v != want)    { print "STALE", $0; bad = 1 }
    else                     print "ok   ", $0 }
  END { for (k in expected) if (found[k] != 1) { print "want 1 site at " k ", found " found[k] + 0; bad = 1 }
        exit bad }' \
  && echo "all at $VER"
```

`sites` is the table spelled out: `appVersion`, the three chart tags, the two compose images. Each
must be found exactly once and name `$VER`. A `STALE` site fails the check, and so does one that is
missing or duplicated (an image that lost its `tag:`), or an `EXTRA` OpenRag image nobody listed —
adding an image means a row in the table and a line in `sites`. An empty `$VER` marks every site
`STALE`, so a misread `pyproject.toml` fails loudly rather than passing.
Once the tag exists, step 8 of [`.github/RELEASING.md`](.github/RELEASING.md) re-checks the same pins
against it with a YAML parser; this is the version of that check you can run before there is a tag.

#### Writing the release notes

`ui/src/lib/release-notes.ts` backs the Release Notes dialog in the admin sidebar. Unlike
`whats-new.ts`, it can't be resolved mechanically: its `version`, `summary`, and `newFeatures`
describe the shipped release in prose, and only a human can write that. Feature PRs leave those
fields empty; writing them is release-preparation work, done on `release/X.Y.Z` once the feature
set for `X.Y.Z` is actually final — so the notes describe what shipped, not what a feature PR
predicted would ship.

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

## Helm chart

`infra/charts/openrag-stack/` carries a version of its own, and it is not the application's:

- **`version`** (`0.6.x`) — the chart's version. Bump it in *any* PR that changes the chart.
- **`appVersion`** — the OpenRag release the chart deploys. Only a release bump moves it.

`.github/workflows/helm.yaml` packages and pushes to `oci://ghcr.io/linagora/openrag-stack` on every
push to `main` or `develop` that touches `infra/charts/**` — `X.Y.Z` from `main`, `X.Y.Z-dev` from
`develop`. OCI tags are mutable, so a chart change that forgets to bump `version` silently
republishes a tag someone has already pinned, with different content underneath. That is the failure
the bump prevents; it is not bookkeeping.

### Sub-chart dependencies

Prefer moving the dependency to overriding its values. Pinning a sub-chart's image tag — as we did
for `milvus.image.all.tag` while no Milvus 3 chart was resolvable — runs that chart's templates
against a different binary, so the manifests and the image no longer come from the same place.
Override only when upstream has no release that covers what you need, and leave a comment naming the
condition that retires the override.

To move one:

```bash
# 1. edit the dependency's `version` in Chart.yaml, then
helm dependency update infra/charts/openrag-stack
# 2. commit the regenerated Chart.lock alongside Chart.yaml
```

`Chart.lock` is generated output, never edited by hand. Its `digest` covers both the constraints in
`Chart.yaml` and the versions that were resolved from them, so changing either side without re-running
the command leaves the two out of sync — and `helm dependency build` stops rather than installing
something nobody pinned:

```console
$ helm dependency build infra/charts/openrag-stack
Error: the lock file (Chart.lock) is out of sync with the dependencies file (Chart.yaml). Please update the dependencies
```

That is the same refusal whether you bumped `Chart.yaml` and forgot the lock or edited a resolved
version in the lock directly, which is why there is no such thing as a hand-narrowed lock diff:
if you want fewer things to move, pin the constraint in `Chart.yaml` — the lock only ever records
what the resolver did.

`helm dependency update` re-resolves **every** dependency, not the one you edited: `postgresql` and
`vllm-stack` are pinned by range, so unrelated versions will move in `Chart.lock`. That is drift being
written down rather than drift being introduced — the workflow runs `helm dependency update` itself
before packaging, so the published chart already floats to the newest in-range versions. Read those
lines, and say what they are in the PR so a reviewer doesn't have to guess.

### Verifying a chart change

Render the chart before and after and diff the manifests. Neither side renders from a clean checkout
as it stands: the sub-charts are not committed (`.gitignore` drops `charts/*.tgz`) and `helm template`
does not fetch them, and the chart refuses to template without its required secrets. So build the
locked dependencies before each render, and hold the secrets in one place for both. Any value works
as long as it passes the chart's secret checks (`templates/secrets-env.yaml`): at least 12
characters, and not one of the example values it rejects.

```bash
SECRETS=(--set env.secrets.AUTH_TOKEN=dummy-for-render
         --set env.secrets.POSTGRES_PASSWORD=dummy-for-render
         --set postgresql.auth.password=dummy-for-render)
WORK=$(mktemp -d)   # fresh per run, so nothing from an earlier comparison leaks into this one

helm dependency build infra/charts/openrag-stack
helm template openrag infra/charts/openrag-stack "${SECRETS[@]}" > "$WORK/after.yaml"
```

For the "before" side, materialize the base branch's chart in the same directory and build it the
same way. Both sides use `build`, not `update`, so you compare what the two lockfiles pinned rather
than whatever is newest today — and if `build` stops on your side with the out-of-sync error above,
your `Chart.lock` was never regenerated:

```bash
git archive origin/develop infra/charts/openrag-stack | tar -x -C "$WORK"
helm dependency build "$WORK/infra/charts/openrag-stack"
helm template openrag "$WORK/infra/charts/openrag-stack" "${SECRETS[@]}" > "$WORK/before.yaml"
diff "$WORK/before.yaml" "$WORK/after.yaml"
```

The generated `postgres-password` differs on every render — ignore that line, it is not your change.
Remove `$WORK` once you are done with the two renders.

## Pull requests

- Target `develop` for features and fixes; `main` only receives release and hotfix merges.
- Keep the branch focused and the history clean.
- Ensure CI is green before requesting review (lint, tests, layer-import check, security, integration).

## Security checks

`.github/workflows/security.yml` fails a pull request only on what the pull request itself
introduces. Findings that predate it are recorded, not gated on — where depends on the check.

- **Dependencies** fails when `uv.lock` or `ui/package-lock.json` gains a HIGH or CRITICAL
  advisory the base branch does not have. Upgrade to the fixed version shown in the annotation.
  If no fix exists or the vulnerable path is unreachable, accept it in `.trivyignore.yaml`
  in the same pull request, with a `statement` saying why; the check lists every accepted
  advisory, with its statement, in its summary. Pre-existing advisories are in the Security tab.
- **Secrets** fails when any commit in the pull request contains a credential, even one a later
  commit removes. Revoke it at its issuer first — the history of a public repository cannot be
  recalled — then rewrite the branch so the value never merges. Placeholders in docs should use
  `YOUR_AUTH_TOKEN`, `__GENERATE_ME__` or `or-xxx...`, which `.gitleaks.toml` allows.
  Findings already in history are not in the Security tab: they are fingerprinted in
  `.gitleaksignore`, which is the record of them.
- **CodeQL** reports to the Security tab and annotates the pull request.

## Local checks before pushing

```bash
uv run ruff check openrag/ tests/
uv run ruff format --check openrag/ tests/
uv run python scripts/check_layer_imports.py
uv run pytest tests/unit/
```
