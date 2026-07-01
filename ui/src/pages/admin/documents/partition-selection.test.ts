import { describe, expect, it } from "vitest";

import { resolveDocumentsPartition } from "./partition-selection";

const parts = (...names: string[]) => names.map((partition) => ({ partition }));

describe("resolveDocumentsPartition", () => {
  it("keeps the candidate when it still exists", () => {
    expect(resolveDocumentsPartition("perso", parts("shared", "perso"), true)).toBe("perso");
  });

  it("falls back to the first partition when the candidate was deleted", () => {
    // 'perso' was deleted (by its owner or an admin) — don't 404, pick a valid one.
    expect(resolveDocumentsPartition("perso", parts("shared", "team"), true)).toBe("shared");
  });

  it("returns empty when the candidate is gone and no partitions remain", () => {
    expect(resolveDocumentsPartition("perso", parts(), true)).toBe("");
  });

  it("keeps the candidate while the list is still loading", () => {
    // Don't discard a valid remembered value before partitions have loaded.
    expect(resolveDocumentsPartition("perso", parts(), false)).toBe("perso");
  });

  it("defaults to the first partition when there is no candidate", () => {
    expect(resolveDocumentsPartition("", parts("shared", "team"), true)).toBe("shared");
  });

  it("returns empty when there is no candidate and no partitions", () => {
    expect(resolveDocumentsPartition("", parts(), true)).toBe("");
  });
});
