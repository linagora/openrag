import { describe, it, expect } from "vitest";
import {
  hasNewIn,
  hasNewInRegistry,
  isNew,
  isNewInRegistry,
  isNewSince,
  NEW_FOR_MINORS,
  NEW_SINCE,
  UNRELEASED,
} from "./whats-new";

/**
 * A synthetic `major.minor.0`. The window rule is tested through `isNewSince`
 * with these rather than through the registry, so the tests never need editing
 * when an entry is added, pinned by a release, or retired; `isNew` / `hasNewIn`
 * are tested only for the lookup.
 */
const v = (major: number, minor: number) => `${major}.${minor}.0`;

describe("isNewSince — the version window", () => {
  it("badges the release the feature shipped in", () => {
    expect(isNewSince("2.2.0", v(2, 2))).toBe(true);
  });

  it("keeps the badge for NEW_FOR_MINORS - 1 further minors", () => {
    expect(isNewSince("2.2.0", v(2, 2 + NEW_FOR_MINORS - 1))).toBe(true);
  });

  it("drops the badge once the window has passed — no manual cleanup needed", () => {
    expect(isNewSince("2.2.0", v(2, 2 + NEW_FOR_MINORS))).toBe(false);
    expect(isNewSince("2.2.0", v(2, 2 + NEW_FOR_MINORS + 5))).toBe(false);
  });

  it("drops the badge on a major bump, however close the minor", () => {
    expect(isNewSince("2.2.0", v(3, 2))).toBe(false);
    expect(isNewSince("2.2.0", v(3, 0))).toBe(false);
  });

  it("still badges on an older deployment that predates the feature", () => {
    // Reading an older version must not mark a feature stale — it is not
    // shipped there at all, so the option will simply be absent.
    expect(isNewSince("2.2.0", v(2, 1))).toBe(true);
    expect(isNewSince("2.2.0", v(1, 9))).toBe(true);
  });

  it("rejects a string that merely starts like a version", () => {
    // A prefix match would read these as 2.2 and badge on them; the contract is
    // to fail closed on anything that is not a version.
    expect(isNewSince("2.2.0", "2.2rubbish")).toBe(false);
    expect(isNewSince("2.2.0", "2.2 (build 7)")).toBe(false);
    expect(isNewSince("2.2.0", "2.2.")).toBe(false);
    expect(isNewSince("2.2.0", "version 2.2.0")).toBe(false);
    expect(isNewSince("2.2rubbish", v(2, 2))).toBe(false);
  });

  it("rejects a malformed suffix, dotted or otherwise", () => {
    // Admitting "some dotted thing after the digits" is the prefix hole one dot
    // along: 2.2.0.wat would read as 2.2 and badge on a corrupt version.
    expect(isNewSince("2.2.0", "2.2.0.wat")).toBe(false);
    // Version-shaped but not versions: PEP 440 fixes the order of its markers,
    // so a repeated or out-of-order run is as malformed as a bare typo.
    expect(isNewSince("2.2.0", "2.2.0.dev1.post1.dev5")).toBe(false);
    expect(isNewSince("2.2.0", "2.2.0.post1a1")).toBe(false);
    // The two grammars are alternatives, not a menu: a string wearing a PEP 440
    // marker *and* a SemVer pre-release belongs to neither scheme.
    expect(isNewSince("2.2.0", "2.2.0rc1-beta")).toBe(false);
    expect(isNewSince("2.2.0", "2.2.0.post1-rc.1")).toBe(false);
    expect(isNewSince("2.2.0", "2.2.0.x9")).toBe(false);
    expect(isNewSince("2.2.0", "2.2.0 wat")).toBe(false);
    expect(isNewSince("2.2.0", "2.2.0-")).toBe(false);
    expect(isNewSince("2.2.0", "2.2.0+")).toBe(false);
    // A pin is read by the same parser, so a corrupt one badges nothing either.
    expect(isNewSince("2.2.0.wat", v(2, 2))).toBe(false);
  });

  it("reads the PEP 440 forms /version actually serves", () => {
    // The endpoint returns importlib.metadata's version, which is normalized to
    // PEP 440: an RC is `2.2.0rc1`, not `2.2.0-rc.1`. Refusing those would blank
    // every badge on an RC or dev deployment.
    expect(isNewSince("2.2.0", "2.2.0rc1")).toBe(true);
    expect(isNewSince("2.2.0", "2.2.0b2")).toBe(true);
    expect(isNewSince("2.2.0", "2.2.0.post1")).toBe(true);
    expect(isNewSince("2.2.0", "2.2.0.dev3")).toBe(true);
    expect(isNewSince("2.2.0", "2.2.0.dev3+local.7")).toBe(true);
    expect(isNewSince("2.2.0", "2.2.0.1")).toBe(true);
    expect(isNewSince("2.2.0", "2.2.0a1.post2.dev3")).toBe(true);
    expect(isNewSince("2.2.0", "2.2.0-next.3")).toBe(true);
    // A `+build` segment is legal after either grammar, so the alternation must
    // leave it outside itself.
    expect(isNewSince("2.2.0", "2.2.0-rc.1+build.7")).toBe(true);
  });

  it("tolerates a v prefix, a missing patch, and pre-release suffixes", () => {
    expect(isNewSince("2.2.0", "v2.2.0")).toBe(true);
    expect(isNewSince("2.2.0", "2.2")).toBe(true);
    expect(isNewSince("2.2.0", "2.2.0-rc.1")).toBe(true);
    expect(isNewSince("2.2.0", "2.2.0+build.7")).toBe(true);
    expect(isNewSince("v2.2", v(2, 2))).toBe(true);
  });

  it("fails closed when a pinned version cannot be compared", () => {
    // An unbadged new feature beats every feature badged forever on a
    // deployment whose /version cannot be read.
    expect(isNewSince("2.2.0", undefined)).toBe(false);
    expect(isNewSince("2.2.0", null)).toBe(false);
    expect(isNewSince("2.2.0", "")).toBe(false);
    expect(isNewSince("2.2.0", "unknown")).toBe(false);
    expect(isNewSince("nonsense", v(2, 2))).toBe(false);
  });

  it("returns false for a feature with no registry entry", () => {
    expect(isNewSince(undefined, v(2, 2))).toBe(false);
  });
});

describe("isNewSince — UNRELEASED", () => {
  it("badges unconditionally, since the reader is ahead of the last release", () => {
    // A merged-but-untagged feature has no version to compare against: the
    // reader is running a build newer than any release that mentions it.
    expect(isNewSince(UNRELEASED, v(2, 1))).toBe(true);
    expect(isNewSince(UNRELEASED, v(9, 9))).toBe(true);
  });

  it("badges even when the app version is unreadable", () => {
    expect(isNewSince(UNRELEASED, undefined)).toBe(true);
    expect(isNewSince(UNRELEASED, "unknown")).toBe(true);
  });
});

// A lint on the registry's *content*, so it has nothing to check while the
// registry is empty. The rules it applies are covered above against fixtures.
describe("NEW_SINCE registry hygiene", () => {
  it("holds only UNRELEASED or a parseable major.minor", () => {
    // Catches a typo'd pin ("2..2", "v2", "next") at test time rather than
    // silently badging nothing forever, since isNewSince fails closed.
    // Judged by the parser that will actually read the pin: a value only counts
    // as parseable if the rule can compare a deployment against it, and a
    // version is trivially inside its own window.
    for (const [key, value] of Object.entries(NEW_SINCE)) {
      expect(value === UNRELEASED || isNewSince(value, value), `${key} -> ${value}`).toBe(true);
    }
  });

  it("uses group-scoped keys, so a same-named option elsewhere is not badged", () => {
    for (const key of Object.keys(NEW_SINCE)) {
      expect(key, `${key} should be "<group>.<value>"`).toMatch(/^[a-z0-9_]+\.[a-z0-9_]+/i);
    }
  });
});

// The lookup, exercised against fixtures rather than against whatever the real
// registry happens to hold. Reaching into `NEW_SINCE` for a key to try means the
// tests can only run on days when something is registered — and the registry is
// legitimately empty between the last entry expiring and the next feature
// registering one, which is exactly when a regression here would go unnoticed.
describe("isNewInRegistry / hasNewInRegistry — the lookup", () => {
  const registry = {
    "chunking.structured_section": "2.2.0",
    "models.reranker": UNRELEASED,
    "parsing.typo": "next",
  };

  it("reads a pin back and applies the window to it", () => {
    expect(isNewInRegistry(registry, "chunking.structured_section", v(2, 2))).toBe(true);
    expect(isNewInRegistry(registry, "chunking.structured_section", v(2, 3))).toBe(true);
    expect(isNewInRegistry(registry, "chunking.structured_section", v(2, 4))).toBe(false);
  });

  it("badges an UNRELEASED entry whatever the deployment reports", () => {
    expect(isNewInRegistry(registry, "models.reranker", v(9, 9))).toBe(true);
    expect(isNewInRegistry(registry, "models.reranker", undefined)).toBe(true);
  });

  it("badges nothing for a key the registry does not carry", () => {
    expect(isNewInRegistry(registry, "chunking.recursive_splitter", v(2, 2))).toBe(false);
    expect(isNewInRegistry({}, "chunking.structured_section", v(2, 2))).toBe(false);
  });

  it("scopes a key to its group, so the same option elsewhere is untouched", () => {
    expect(isNewInRegistry(registry, "parsing.structured_section", v(2, 2))).toBe(false);
  });

  it("fails closed on a typo'd pin rather than badging on it", () => {
    expect(isNewInRegistry(registry, "parsing.typo", v(2, 2))).toBe(false);
  });

  it("marks a collapsed control when any option inside it is new", () => {
    const options = ["recursive_splitter", "structured_section"];
    expect(hasNewInRegistry(registry, "chunking", options, v(2, 2))).toBe(true);
    expect(hasNewInRegistry(registry, "chunking", ["recursive_splitter"], v(2, 2))).toBe(false);
    expect(hasNewInRegistry(registry, "chunking", options, v(2, 4))).toBe(false);
    expect(hasNewInRegistry(registry, "chunking", [], v(2, 2))).toBe(false);
  });

  it("derives each option's key from the group it was given", () => {
    // The one thing the group form adds over a bare lookup.
    expect(hasNewInRegistry(registry, "models", ["reranker"], v(2, 2))).toBe(true);
    expect(hasNewInRegistry(registry, "chunking", ["reranker"], v(2, 2))).toBe(false);
  });
});

describe("isNew / hasNewIn — bound to NEW_SINCE", () => {
  it("answers for the real registry, and badges nothing outside it", () => {
    expect(isNew("definitely.not.registered", v(2, 2))).toBe(false);
    expect(hasNewIn("definitely", ["not_registered"], v(2, 2))).toBe(false);
    // Whatever the registry holds today, the two entry points agree with the
    // lookup they delegate to — including when it holds nothing.
    for (const key of Object.keys(NEW_SINCE)) {
      expect(isNew(key, v(2, 2))).toBe(isNewInRegistry(NEW_SINCE, key, v(2, 2)));
    }
  });
});
