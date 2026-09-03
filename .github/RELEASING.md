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
jobs=$(gh run view "$RUN_ID" --json jobs) || exit 1
fail=0

# Each required job must be PRESENT exactly once and conclude success. Checking
# only the conclusions of the jobs GitHub returned is not enough: if a job never
# ran at all it contributes no failing conclusion, so the gate passes on the
# strength of the jobs that did run.
for j in verify-tag build-and-push-image build-and-push-image-ray \
         build-and-push-image-admin-ui; do
  c=$(printf '%s' "$jobs" | jq -r --arg n "$j" \
        '[.jobs[] | select(.name == $n)]
         | if length == 1 then .[0].conclusion else "MISSING(\(length))" end')
  [ "$c" = success ] && echo "OK    $j" || { echo "FAIL  $j -> $c"; fail=1; }
done

# And nothing else in the run may have failed either.
bad=$(printf '%s' "$jobs" \
        | jq '[.jobs[] | select(.conclusion != "success")] | length')
[ "$bad" -eq 0 ] || { echo "FAIL  $bad job(s) did not conclude success"; fail=1; }

[ "$fail" -eq 0 ] && echo "step 1 PASS" || { echo "step 1 FAIL" >&2; exit 1; }
```

Written as a gate, not a print: a command that only reports the count still
exits 0 when the count is non-zero, so a release could continue straight past a
skipped build job — the very thing this step exists to stop.

If `verify-tag` failed loudly, read its error before touching the tag — it
guards three different things:

- **not an exact GA tag** (`vMAJOR.MINOR.PATCH`): a near-miss like `v1.2.3-rc1`
  is rejected outright — note the missing dot. If you meant a release candidate,
  retag as `v1.2.3-rc.1`, the shape `build_rc.yml` triggers on (`v*-rc.*`); a
  correctly-shaped rc tag never reaches this guard, it is filtered out by the
  job-level `if`.
- **the tag is not an ancestor of `origin/main`**: fix the tag placement, do not
  rerun.
- **unresolved `NEW` badge entries**: `ui/src/lib/whats-new.ts` still contains an
  entry reading `UNRELEASED`, which would badge that feature forever. The release
  branch resolves these in the same commit that bumps `pyproject.toml` — see
  "Bumping the version" under **Releases** in
  [CONTRIBUTING.md](../CONTRIBUTING.md), which carries the command. Resolve them
  on the release branch, then retag; do not rerun the workflow against the old
  commit.

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

Gate on the pull itself. If the pull fails while that tag is already in the
local cache, `docker image inspect` happily reads the **stale** image and the
step can print `OK` for bytes that were never fetched. And select the
`RepoDigests` entry **by repository**: it is an unordered list with one entry
per registry the image has been pulled from or pushed to, so `index 0` is not
necessarily the repository asked for.

```bash
docker pull "linagoraai/openrag:$VER" \
  || { echo "FAIL: pull failed — the local cache may hold an older image" >&2; exit 1; }
pulled=$(docker image inspect "linagoraai/openrag:$VER" \
           --format '{{range .RepoDigests}}{{println .}}{{end}}' \
           | awk -F@ '$1 == "linagoraai/openrag" { print $2; exit }')
[ -n "$pulled" ] \
  || { echo "FAIL: no RepoDigest for linagoraai/openrag" >&2; exit 1; }
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

The chart half needs `python3` with PyYAML (`pip install pyyaml`). If it is
missing the check exits non-zero and step 8 fails — it does not skip.

```bash
fail=0
# appVersion must be $VER without its leading v
want_app=${VER#v}
got_app=$(git show "$VER:infra/charts/openrag-stack/Chart.yaml" \
            | awk -F'"' '/^appVersion:/{print $2}')
[ "$got_app" = "$want_app" ] \
  && echo "OK    appVersion=$got_app" \
  || { echo "FAIL  appVersion=$got_app want=$want_app"; fail=1; }

# The chart pins are read with a real YAML parser, not by pattern-matching
# lines. Two earlier text-based attempts both shipped false passes: `grep -A4`
# matched a repository named inside a comment, and an awk scanner whose
# `pending` state outlived its `image:` block consumed a *later* block's tag —
# so a repository with no `tag:` at all reported the next block's version and
# the gate passed. Parsing structurally removes that whole class: comments are
# gone by construction, block boundaries are real, and a duplicate key raises
# instead of silently resolving to one of the two values.
#
# Duplicate keys matter here: YAML resolves them last-wins, which is the value
# Helm would deploy, while a scanner that stops at the first match reads the
# other one. Rather than pick a side, reject the file.
chart_pins() {
  # NB: the program is held in a variable and run with `-c`. Writing
  # `python3 - "$1"` would read the *program* from stdin, leaving nothing for
  # the piped values.yaml — the check then finds no image blocks at all.
  local prog
  prog=$(cat <<'PY'
import sys, yaml

VER = sys.argv[1]


class StrictLoader(yaml.SafeLoader):
    pass


def no_duplicates(loader, node, deep=False):
    seen = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ValueError(
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1} "
                "— refusing to guess which value Helm would use"
            )
        seen[key] = loader.construct_object(value_node, deep=deep)
    return seen


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_duplicates
)


def image_blocks(node):
    """Every mapping that declares a `repository` key."""
    if isinstance(node, dict):
        if "repository" in node:
            yield node
        for value in node.values():
            yield from image_blocks(value)
    elif isinstance(node, list):
        for value in node:
            yield from image_blocks(value)


try:
    doc = yaml.load(sys.stdin.read(), Loader=StrictLoader)
except Exception as exc:
    print(f"FAIL  values.yaml did not parse: {exc}")
    sys.exit(1)

# Checked by repository. Do NOT just count version-shaped tags: values.yaml
# also pins third-party images (vllm, milvus, infinity) whose versions have
# nothing to do with this release.
fail = 0
for repo in ("linagora/openrag-ray", "linagoraai/openrag-admin-ui", "linagoraai/openrag"):
    found = [b for b in image_blocks(doc) if b.get("repository") == repo]
    if len(found) != 1:
        print(f"FAIL  {repo} -> {len(found)} image block(s) (want exactly 1)")
        fail = 1
        continue
    tag = found[0].get("tag")
    if tag is None:
        print(f"FAIL  {repo} -> no sibling tag: (image is unpinned)")
        fail = 1
    elif tag != VER:
        print(f"FAIL  {repo} -> {tag} (want {VER})")
        fail = 1
    else:
        print(f"OK    {repo} -> {tag}")

sys.exit(fail)
PY
)
  python3 -c "$prog" "$1"
}

git show "$VER:infra/charts/openrag-stack/values.yaml" | chart_pins "$VER" || fail=1

# compose pins. Take the value of every *active* `image:` field, then match it
# as a fixed whole string: interpolating $VER into a regex would let the dots in
# v2.1.0 match any character, so a pin reading v2x1y0 would satisfy the check.
cimg=$(git show "$VER:infra/compose/docker-compose.yaml" | sed 's/#.*//' \
         | sed -n 's/^[[:space:]]*image:[[:space:]]*\([^[:space:]]*\).*$/\1/p')

# Exactly one pin per expected repository — a count of two is also reached by
# two copies of the same pin with the other one missing entirely.
for repo in linagoraai/openrag linagoraai/openrag-admin-ui; do
  n=$(printf '%s\n' "$cimg" | grep -Fxc "$repo:$VER" || true)
  [ "$n" -eq 1 ] \
    && echo "OK    compose $repo:$VER" \
    || { echo "FAIL  compose $repo:$VER -> $n active pin(s) (want 1)"; fail=1; }
done

# And no other OpenRag image may be pinned at some other version.
stray=$(printf '%s\n' "$cimg" | grep -F 'linagoraai/openrag' \
          | grep -Fxv "linagoraai/openrag:$VER" \
          | grep -Fxv "linagoraai/openrag-admin-ui:$VER" || true)
[ -z "$stray" ] \
  && echo "OK    no stray OpenRag compose pins" \
  || { echo "FAIL  stray OpenRag compose pin(s): $stray"; fail=1; }

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
