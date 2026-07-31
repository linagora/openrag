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
dg() { docker buildx imagetools inspect --raw "$1" 2>/dev/null | sha256sum | awk '{print "sha256:"$1}'; }
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
gh run view "$RUN_ID" --json jobs --jq '[.jobs[] | select(.conclusion != "success")] | length'
# must print 0
```

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
  [ "$a" = "$b" ] && echo "OK    $1 == $2" || echo "MISMATCH $1=$a $2=$b"
done
```

**PASS**: both `OK`.

## 5. Pull it, and confirm the digest is the one you verified

Steps 2–4 read metadata. This proves the bytes are actually fetchable.

```bash
docker pull "linagoraai/openrag:$VER"
docker image inspect "linagoraai/openrag:$VER" --format '{{index .RepoDigests 0}}'
```

**PASS**: the printed digest equals the Docker Hub digest from step 2.

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

```bash
git show "$VER:infra/charts/openrag-stack/Chart.yaml" | grep -E '^(version|appVersion)'
git show "$VER:infra/charts/openrag-stack/values.yaml" | grep -nE 'tag: "v[0-9]' 
git show "$VER:infra/compose/docker-compose.yaml" | grep -nE 'image: linagoraai/'
```

**PASS**: every OpenRag image pin reads `$VER`, `appVersion` matches, chart
`version` was bumped.

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
