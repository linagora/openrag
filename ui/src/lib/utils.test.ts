import { describe, it, expect, afterEach, vi } from "vitest";
import { intOr, numOr, copyToClipboard } from "./utils";

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

describe("copyToClipboard", () => {
  const originalClipboard = navigator.clipboard;

  afterEach(() => {
    Object.defineProperty(navigator, "clipboard", {
      value: originalClipboard,
      configurable: true,
    });
    // jsdom doesn't implement execCommand; the fallback tests stub it directly.
    delete (document as unknown as { execCommand?: unknown }).execCommand;
    vi.restoreAllMocks();
  });

  // Simulates browsers/deployments without the async Clipboard API (plain
  // HTTP), forcing the execCommand fallback.
  function dropAsyncClipboard() {
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    });
  }

  it("inserts the fallback textarea next to the anchor, not document.body", async () => {
    dropAsyncClipboard();

    const container = document.createElement("div");
    const anchor = document.createElement("button");
    container.appendChild(anchor);
    document.body.appendChild(container);

    let taInContainerDuringCopy = false;
    document.execCommand = vi.fn(() => {
      taInContainerDuringCopy = container.querySelector("textarea") !== null;
      return true;
    });

    const ok = await copyToClipboard("or-secret-token", anchor);

    expect(ok).toBe(true);
    expect(taInContainerDuringCopy).toBe(true);
    // Cleaned up afterwards, and never leaked onto document.body.
    expect(container.querySelector("textarea")).toBeNull();
    expect(document.body.querySelector("textarea")).toBeNull();

    document.body.removeChild(container);
  });

  it("falls back to document.body when no anchor is given", async () => {
    dropAsyncClipboard();
    document.execCommand = vi.fn(() => true);

    const ok = await copyToClipboard("or-secret-token");

    expect(ok).toBe(true);
    expect(document.body.querySelector("textarea")).toBeNull();
  });

  it("uses the async Clipboard API when available, ignoring the anchor", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    const ok = await copyToClipboard("or-secret-token");

    expect(ok).toBe(true);
    expect(writeText).toHaveBeenCalledWith("or-secret-token");
  });
});
