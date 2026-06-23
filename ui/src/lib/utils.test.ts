import { describe, it, expect } from "vitest";
import { intOr, numOr } from "./utils";

// These guard against `parseInt("")` / `parseFloat("")` → NaN, which
// JSON.stringify serializes as `null` when sent to the API.
describe("intOr", () => {
  it("parses a valid integer", () => {
    expect(intOr("5", 0)).toBe(5);
    expect(intOr("12", 0)).toBe(12);
  });

  it("returns 0 for '0' (not the fallback)", () => {
    expect(intOr("0", 9)).toBe(0);
  });

  it("falls back on an empty string", () => {
    expect(intOr("", 7)).toBe(7);
  });

  it("falls back on non-numeric input", () => {
    expect(intOr("abc", 3)).toBe(3);
  });
});

describe("numOr", () => {
  it("parses a float", () => {
    expect(numOr("1.5", 0)).toBe(1.5);
  });

  it("returns 0 for '0' (not the fallback)", () => {
    expect(numOr("0", 9)).toBe(0);
  });

  it("falls back on an empty string", () => {
    expect(numOr("", 30)).toBe(30);
  });

  it("falls back on non-numeric input", () => {
    expect(numOr("x", 2.5)).toBe(2.5);
  });
});
