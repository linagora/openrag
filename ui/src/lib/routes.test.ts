import { describe, it, expect } from "vitest";
import { partitionDetailPath } from "./routes";

describe("partitionDetailPath", () => {
  it("passes a plain name through", () => {
    expect(partitionDetailPath("legal")).toBe("/partitions/legal");
  });

  it("encodes a slash so it doesn't create a nested route", () => {
    expect(partitionDetailPath("a/b")).toBe("/partitions/a%2Fb");
  });

  it("encodes #, ?, space and % (would otherwise truncate or break the route)", () => {
    expect(partitionDetailPath("x#y?z %")).toBe("/partitions/x%23y%3Fz%20%25");
  });
});
