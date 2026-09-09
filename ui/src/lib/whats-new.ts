/**
 * "NEW" badge registry.
 *
 * A badge is data, not markup, so that removing it is never a manual step that
 * someone has to remember in a release which otherwise doesn't touch the file.
 * Each entry records the version a feature shipped in; `isNew` compares that
 * against the running app version and stops returning true once the feature is
 * `NEW_FOR_MINORS` minor releases old. Forgetting to delete an entry therefore
 * costs nothing — the badge disappears on its own, and the line can be swept up
 * whenever anyone happens to notice.
 *
 * A key is any stable dotted string — `nav.workspaces`, `presets.tab.retrieval`,
 * `chunking.structured_section` — and nothing here knows what surface it names.
 * For an option inside a control the convention is `"<group>.<value>"` rather
 * than the bare option value, so that `useNewOptions(group, values)` can derive
 * every key from the group alone, and so that two dropdowns offering the same
 * string do not badge each other the day that happens.
 *
 * Attaching one is components/shared/new-badge.tsx. FEATURE_TAG.md, at the repo
 * root, is the worked guide: registering a key, the two ways to attach it, and
 * what the release branch does with it.
 */

/** How many minor releases a feature keeps its badge. */
export const NEW_FOR_MINORS = 2;

/**
 * Placeholder for a feature that has merged but is not in a tagged release yet.
 *
 * The version a feature ships in is not knowable when the feature PR is
 * written: a `release/X.Y.Z` branch is cut once the features intended for that
 * version are complete, so the number is chosen after the feature merges —
 * sometimes weeks later. Any pin written during development is a prediction,
 * and wrong the moment the release plan moves. Author entries with this
 * sentinel instead, and let the release branch resolve them in the same commit
 * that bumps `pyproject.toml`, reading the version back from there rather than
 * retyping it. See "Releases" in CONTRIBUTING.md for the command; it is kept
 * there alone so the two cannot drift.
 *
 * Entries are written as the string literal, not as a reference to the constant
 * below, so that a one-liner can find them. That one-liner anchors on a whole
 * registry line — indent, quoted key, colon, and an optional trailing comma,
 * since the last entry in the object may legally omit it — so it matches
 * nothing else. A looser pattern silently corrupts two things at once: this
 * constant's own definition, turning the sentinel into a version and breaking
 * every future entry, and any prose that mentions the sentinel while explaining
 * the step.
 *
 * An unresolved entry badges unconditionally, which is correct — everyone
 * running a build that contains the feature is seeing it for the first time —
 * but it also never expires. Forgetting the step is therefore silent and
 * permanent, so `verify-tag` in .github/workflows/build.yml refuses to publish
 * GA images while any entry is still unresolved.
 */
export const UNRELEASED = "UNRELEASED";

/**
 * Feature key -> the app version it first shipped in (`major.minor[.patch]`),
 * or `UNRELEASED` until a release pins it.
 */
export const NEW_SINCE: Readonly<Record<string, string>> = {
  // A commented example is invisible to both the release one-liner and the CI
  // gate: each anchors on a whole registry line, which starts with the quoted
  // key and nothing else — see FEATURE_TAG.md.
  //   "models.reranker": "UNRELEASED",
  "chunking.structured_section": "v2.2.0",
  "models.stt": "v2.2.0",
  "models.stt.moss_speaker_aware": "v2.2.0",
  "prompts.asr_transcription": "v2.2.0",
};

type Version = { major: number; minor: number };

/**
 * Parse `major.minor` out of a version string. Returns null for anything
 * unparseable so callers fail closed (no badge) rather than badging everything.
 *
 * The whole string has to be a version, not merely start like one: a prefix
 * match reads `2.2rubbish` as `2.2` and badges on it, which is the opposite of
 * failing closed.
 *
 * What counts as a version is wider than SemVer, because `/version` serves
 * `importlib.metadata`, which reports **PEP 440**: a release candidate arrives
 * as `2.2.0rc1`, not `2.2.0-rc.1`, and `.post1` / `.dev0` builds are legal too.
 * Rejecting those would blank every badge on an RC deployment.
 *
 * Each accepted suffix is spelled out rather than admitted as "some dotted
 * thing after the digits" — that shortcut lets `2.2.0.wat` through and reads it
 * as 2.2, which is the prefix hole again, one dot along. In order:
 *
 *   1. numeric segments — `2.2`, `2.2.0`, or more;
 *   2. then **either** grammar's pre-release notation, never both — they are
 *      alternatives, not a menu, and a string wearing one of each belongs to
 *      neither scheme:
 *      - PEP 440 markers in their defined order — at most one pre-release
 *        (`a` `b` `rc`), then at most one `post`, then at most one `dev`, each
 *        attached or separated by `.` `-` `_` (`2.2.0rc1`, `2.1.1.post1`,
 *        `2.2.0a1.post2.dev3`). Ordering them is what rejects
 *        `2.2.0.dev1.post1.dev5` and `2.2.0.post1a1`;
 *      - or a SemVer pre-release — `-` then dot-separated alphanumerics, which
 *        is how a hand-written registry pin spells `2.2.0-rc.1`.
 *      Making these alternatives is what rejects `2.2.0rc1-beta` and
 *      `2.2.0.post1-rc.1`, hybrids that are version-shaped but not versions;
 *   3. an optional `+` build or local segment, legal after either
 *      (`2.2.0.dev3+local.7`, `2.2.0-rc.1+build.7`).
 */
const VERSION_PATTERN =
  /^\s*v?(\d+)\.(\d+)(?:\.\d+)*(?:(?:[._-]?(?:a|b|rc)\d*)?(?:[._-]?post\d*)?(?:[._-]?dev\d*)?|-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\s*$/i;

function parseVersion(raw: string | undefined | null): Version | null {
  if (!raw) return null;
  const match = VERSION_PATTERN.exec(raw);
  if (!match) return null;
  return { major: Number(match[1]), minor: Number(match[2]) };
}

/**
 * Whether `featureKey` should still be badged when the app reports
 * `appVersion`.
 *
 * False when either version is unknown or unparseable: an unbadged new feature
 * is a much smaller problem than every feature being badged forever on a
 * deployment whose `/version` we can't read.
 *
 * A major bump always expires the badge — 3.0 is not "two minors after 2.2" in
 * any sense a reader would accept.
 */
export function isNew(featureKey: string, appVersion: string | undefined | null): boolean {
  return isNewInRegistry(NEW_SINCE, featureKey, appVersion);
}

/**
 * The lookup itself, over any registry — `isNew` is this against `NEW_SINCE`.
 *
 * Separate for the same reason `isNewSince` is separate from the registry: the
 * registry is *data*, and legitimately empty between the day the last entry
 * expires and the day the next feature registers one. A test that reaches into
 * `NEW_SINCE` for a key to try can only skip itself on those days — which is
 * precisely when a regression in the lookup would go unnoticed. Given a
 * registry, this can be tested against a fixture on any day.
 */
export function isNewInRegistry(
  registry: Readonly<Record<string, string>>,
  featureKey: string,
  appVersion: string | undefined | null
): boolean {
  return isNewSince(registry[featureKey], appVersion);
}

/**
 * The window rule itself, independent of the registry — `isNew` is this plus a
 * lookup. Separate so the rule can be tested against arbitrary versions without
 * the tests having to be edited every time a registry entry is added, pinned by
 * a release, or retired.
 */
export function isNewSince(since: string | undefined, appVersion: string | undefined | null): boolean {
  if (since === undefined) return false;
  // Merged but not yet tagged: new to everyone running a build that has it, and
  // there is no version to compare against — not even the reader's own, since
  // they are ahead of the last release rather than behind it.
  if (since === UNRELEASED) return true;
  const from = parseVersion(since);
  const current = parseVersion(appVersion);
  if (!from || !current) return false;
  if (current.major !== from.major) return current.major < from.major;
  return current.minor - from.minor < NEW_FOR_MINORS;
}

/**
 * Whether any of `values` is still new within `group` — so a collapsed control
 * can carry the badge for options the reader would otherwise have to open a
 * dropdown to discover.
 */
export function hasNewIn(group: string, values: string[], appVersion: string | undefined | null): boolean {
  return hasNewInRegistry(NEW_SINCE, group, values, appVersion);
}

/** `hasNewIn` over any registry — see `isNewInRegistry` for why it is separate. */
export function hasNewInRegistry(
  registry: Readonly<Record<string, string>>,
  group: string,
  values: string[],
  appVersion: string | undefined | null
): boolean {
  return values.some((value) => isNewInRegistry(registry, `${group}.${value}`, appVersion));
}
