# Releasing OpenRag — verifying the GA images actually published

Run this **after** pushing a `vX.Y.Z` tag to `main`. It exists because the
v2.0.1 release produced a **green workflow run that built nothing**: the three
build jobs were guarded on `github.event.base_ref`, which is empty for a tag
pushed to a branch-protected `main`, so every job reported `skipped` and the run
was still green. Nothing in our process caught it.

The rule this checklist enforces: **a green run is not proof. A digest in the
registry is proof.**

Set the version once, and define the digest helper every step below uses:

```bash
export VER=v2.1.0   # the tag you just pushed

# Resolve a tag's manifest digest. Returns non-zero and prints nothing when the
# tag does not exist — do NOT pipe inspect straight into sha256sum: on a failed
# lookup it hashes empty input and returns
# sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855,
# a real-looking digest. Steps 2-4 would then report a missing image as present,
# and two missing tags would compare equal and pass.
dg() {
  local raw
  raw=$(docker buildx imagetools inspect --raw "$1" 2>/dev/null) || return 1
  [ -n "$raw" ] || return 1
  printf '%s' "$raw" | sha256sum | awk '{print "sha256:"$1}'
}
```

Check the helper itself before trusting it — this must print `MISSING`:

```bash
dg ghcr.io/linagora/openrag:v0.0.0-does-not-exist || echo MISSING
```

> **Why `--raw | sha256sum` and not `--format '{{.Manifest.Digest}}'`:** buildx
> v0.30.1 **silently ignores** that `--format` template and prints its default
> human output instead. Comparing those strings makes every image look like it
> drifted. A manifest digest *is* the sha256 of the raw manifest bytes, so this
> form is both correct and self-verifying. Validated against `v2.0.1`, where it
> reproduces the published digest exactly.

## What a release publishes

| Image | ghcr.io | Docker Hub |
|---|---|---|
| API | `ghcr.io/linagora/openrag` | `linagoraai/openrag` |
| Ray | `ghcr.io/linagora/openrag-ray` | — (ghcr only, by design) |
| Admin UI | `ghcr.io/linagora/openrag-admin-ui` | `linagoraai/openrag-admin-ui` |

Each gets two tags: `$VER` and `latest`.

---

## 1. The workflow ran — and did not skip

A skipped job is the exact v2.0.1 failure mode, and `gh run list` shows such a
run as `success`. Assert on **per-job conclusions**, never on the run's.

```bash
gh run list --workflow build.yml --limit 5 \
  --json databaseId,headBranch,event,status,conclusion,createdAt \
  --jq '.[] | "\(.databaseId)  \(.event)  \(.headBranch)  \(.status)/\(.conclusion)  \(.createdAt)"'
```

Take the run id for the tag push, then:

```bash
RUN_ID=<id-from-above>
gh run view "$RUN_ID" --json jobs \
  --jq '.jobs[] | "\(.conclusion)\t\(.name)"'
```

**PASS** requires all four jobs `success`:
`verify-tag`, `build-and-push-image`, `build-and-push-image-ray`,
`build-and-push-image-admin-ui`.

**FAIL** on any `skipped` — that is the v2.0.1 bug recurring. A hard gate:

```bash
bad=$(gh run view "$RUN_ID" --json jobs \
        --jq '[.jobs[] | select(.conclusion != "success")] | length') || exit 1
if [ "$bad" -ne 0 ]; then
  echo "FAIL: $bad job(s) did not conclude success — do not continue" >&2
  exit 1
fi
echo "OK: every job concluded success"
```

Written as a gate, not a print: a command that only reports the count still
exits 0 when the count is non-zero, so a release could continue straight past a
skipped build job — the very thing this step exists to stop.

If `verify-tag` failed loudly, the tag is not an ancestor of `origin/main` —
fix the tag placement, do not rerun.

## 2. The tag exists in every registry

`imagetools inspect` reads the registry directly (anonymous, no pull, no
`docker login`). If the tag was never pushed, this errors — which is the point.

```bash
for img in ghcr.io/linagora/openrag ghcr.io/linagora/openrag-ray \
           ghcr.io/linagora/openrag-admin-ui \
           linagoraai/openrag linagoraai/openrag-admin-ui; do
  d=$(dg "$img:$VER"); printf '%-42s %s\n' "$img:$VER" "${d:-MISSING}"
done
```

**PASS**: five `sha256:…` digests, zero `MISSING`.

## 3. `latest` points at the release, not something older

`latest` is published unconditionally by `build.yml`, so a mismatch here means
`latest` is stale and every `docker pull` without a tag gets the wrong build.

```bash
for img in ghcr.io/linagora/openrag ghcr.io/linagora/openrag-ray \
           ghcr.io/linagora/openrag-admin-ui \
           linagoraai/openrag linagoraai/openrag-admin-ui; do
  v=$(dg "$img:$VER"); l=$(dg "$img:latest")
  if [ -n "$v" ] && [ "$v" = "$l" ]; then echo "OK    $img"
  else echo "DRIFT $img"; echo "        $VER  = ${v:-none}"; echo "        latest= ${l:-none}"; fi
done
```

**PASS**: all `OK`.

## 4. ghcr and Docker Hub are the same build

Both registries are pushed from one `docker/build-push-action` step, so the
digests must be identical. A difference means one push failed and was
back-filled from a different build.

```bash
for pair in "ghcr.io/linagora/openrag linagoraai/openrag" \
            "ghcr.io/linagora/openrag-admin-ui linagoraai/openrag-admin-ui"; do
  set -- $pair; a=$(dg "$1:$VER"); b=$(dg "$2:$VER")
  # The -n guards matter: without them two MISSING tags are both empty, compare
  # equal, and print OK.
  if [ -n "$a" ] && [ -n "$b" ] && [ "$a" = "$b" ]; then
    echo "OK    $1 == $2"
  else
    echo "MISMATCH $1=${a:-MISSING} $2=${b:-MISSING}"
  fi
done
```

**PASS**: both `OK`.

## 5. Pull it, and confirm the digest is the one you verified

Steps 2–4 read metadata. This proves the bytes are actually fetchable.

`RepoDigests` entries are `repo@sha256:…`, while `dg` returns a bare
`sha256:…` — strip the repository prefix before comparing, or the two can never
match literally.

```bash
docker pull "linagoraai/openrag:$VER"
pulled=$(docker image inspect "linagoraai/openrag:$VER" \
           --format '{{index .RepoDigests 0}}' | cut -d@ -f2)
registry=$(dg "linagoraai/openrag:$VER") || { echo "FAIL: tag not in registry" >&2; exit 1; }
[ "$pulled" = "$registry" ] \
  && echo "OK: pulled digest matches the registry ($pulled)" \
  || { echo "FAIL: pulled=$pulled registry=$registry" >&2; exit 1; }
```

**PASS**: `OK`.

## 6. The image contains the released code

The strongest check, and the one that catches a build from the wrong commit:
the version baked into the image must equal the tag. `app.version` comes from
`importlib.metadata`, i.e. from `pyproject.toml` at build time.

```bash
docker run --rm --entrypoint grep "linagoraai/openrag:$VER" -m1 '^version' /app/pyproject.toml
# expect: version = "2.1.0"   (tag minus the leading v)
```

And on a running stack (the endpoint is unauthenticated):

```bash
curl -fsS http://<host>:8080/version
# {"version":"2.1.0"}
```

**PASS**: both report the release version. A mismatch means the tag sat on a
commit that predates the version bump — the images are mislabelled and must be
rebuilt from a corrected tag.

## 7. The tag is where it should be

```bash
git fetch origin main --tags
git tag --contains "$VER" >/dev/null 2>&1
git merge-base --is-ancestor "$VER" origin/main && echo "OK: $VER is on main" || echo "FAIL: not on main"
git log -1 --format='%H %s' "$VER"
```

**PASS**: `OK`, and the commit is the release-branch merge commit.

## 8. Chart and compose reference the published tags

The chart and compose pins are part of the release surface; shipping them
pointing at the previous version is a silent regression for anyone deploying
from the tag.

Compare against `$VER` exactly. A filter that merely matches something
version-shaped is satisfied by a stale pin left at the previous release — which
is the regression this step is meant to catch.

```bash
fail=0
# appVersion must be $VER without its leading v
want_app=${VER#v}
got_app=$(git show "$VER:infra/charts/openrag-stack/Chart.yaml" \
            | awk -F'"' '/^appVersion:/{print $2}')
[ "$got_app" = "$want_app" ] \
  && echo "OK    appVersion=$got_app" \
  || { echo "FAIL  appVersion=$got_app want=$want_app"; fail=1; }

# Each OpenRag image in the chart, checked by repository. Do NOT just count
# version-shaped tags: values.yaml also pins third-party images (vllm, milvus,
# infinity) whose versions have nothing to do with this release.
# An empty result means the values layout changed and this check no longer finds
# the pin — that is a FAIL, not a pass.
for repo in 'linagora/openrag-ray' 'linagoraai/openrag-admin-ui' 'linagoraai/openrag'; do
  got=$(git show "$VER:infra/charts/openrag-stack/values.yaml" \
          | grep -A4 "repository: \"$repo\"$" \
          | awk -F'"' '/^[[:space:]]*tag:/{print $2; exit}')
  [ "$got" = "$VER" ] \
    && echo "OK    $repo -> $got" \
    || { echo "FAIL  $repo -> ${got:-NOT FOUND} (want $VER)"; fail=1; }
done

# compose pins (2 expected: openrag, openrag-admin-ui)
cpins=$(git show "$VER:infra/compose/docker-compose.yaml" \
          | grep -cE "image: linagoraai/openrag(-admin-ui)?:$VER$")
[ "$cpins" -eq 2 ] \
  && echo "OK    2 compose pins at $VER" \
  || { echo "FAIL  $cpins compose pins at $VER (expected 2)"; fail=1; }

[ "$fail" -eq 0 ] && echo "step 8 PASS" || { echo "step 8 FAIL" >&2; exit 1; }
```

Chart `version` is bumped independently of `appVersion` (it tracks chart
changes, not the app release), so check it by eye against the previous release
rather than against `$VER`:

```bash
git show "$VER:infra/charts/openrag-stack/Chart.yaml" | grep -E '^version:'
```

**PASS**: `step 8 PASS`, and chart `version` moved.

---

## What v2.0.1 actually did, and the rules that follow

Reconstructed from the run log and the tag, 2026-07-30. Three `build.yml` runs
fired for the same tag name:

| run | time (UTC) | commit | result |
|---|---|---|---|
| 30034799802 | 18:41 | `c08c5e9f` (release/2.0.1 → main merge) | 3 jobs **skipped**, run green |
| 30035356446 | 18:49 | `c08c5e9f` — tag re-pushed unchanged | 3 jobs **skipped** again |
| 30039985149 | 19:55 | `6a18a534` (CI hotfix #764 merge) | `verify-tag` + 3 builds **success** |

The shipped `v2.0.1` tag therefore sits on the **CI-hotfix merge commit**, not
on the release-branch merge. The tagged tree still carries the version bump
(`c08c5e9f` is its ancestor), so the images are correct — confirmed above.

Rules this produces:

1. **A skipped job is a failure.** The first two runs reported `success` at the
   run level. Only per-job conclusions revealed the truth. That is step 1.
2. **Never re-push a tag to "retry".** Run 2 proves it is deterministic: the
   workflow that executes is the one *at the tagged commit*, so re-pushing the
   same tag re-runs the same broken file. Move the tag to a fixed commit, or
   fix nothing and diagnose.
3. **Verify images before announcing.** `build.yml` does not create GitHub
   Releases. For v2.0.1 the Release was published at 20:01, six minutes after
   the images finally landed at 19:55. Keep that order: tag → images verified
   → Release notes.
4. **Dry-run the verification itself against the previous release.** Doing that
   for v2.0.1 is what exposed the broken `--format` flag above. A checklist
   that silently reports nonsense is worse than none.

### Open risk for the next release

`main`'s current `build.yml` is **not** the file that successfully built
v2.0.1. PR #767 hardened it afterwards (exact-tag regex, tag name/SHA passed as
env instead of `${{ }}` interpolation, `persist-credentials: false`). The next
GA tag is the **first time that hardened guard ever runs**.

Pre-flighted locally on 2026-07-30:

- Regex `^v[0-9]+\.[0-9]+\.[0-9]+$` — `v2.1.0` accepted; `v2.1`, `2.1.0`,
  `v1.2.3-rc1`, `v1.0-hardening` rejected loudly; `v2.1.0-rc.N` filtered out by
  the job-level `if` and left to `build_rc.yml`. Correct on all six.
- `persist-credentials: false` + `git fetch --no-tags origin main` — verified an
  anonymous fetch of this repo succeeds, so the guard can still reach `main`.
  This holds only while the repo is **public**; if it is ever made private,
  that fetch breaks and every GA build blocks.

The residual risk is acceptable because the hardened guard's failure mode is
`exit 1` — loud and blocking — not v2.0.1's silent skip. But treat step 1 as
mandatory, not a formality.

## Result

Record the outcome on the GitHub Release or the milestone. If any step fails,
the release is **not** done — publishing images is the deliverable, and the tag
alone delivers nothing.
