import { describe, it, expect } from "vitest";
import { applyParsingStrategyChange, PARSING_STRATEGY_INHERIT } from "./preset-config";

// Regression guard for the bug where the parsing Strategy dropdown displayed a
// fabricated "marker" default that was never written to config unless the user
// actively changed the selection — so a marker preset saved no parsing_strategy
// and silently fell back to the deployment's global PDF loader (e.g. pymupdf).
describe("applyParsingStrategyChange", () => {
  it("persists an explicitly chosen strategy into config", () => {
    expect(applyParsingStrategyChange({}, "marker")).toEqual({ parsing_strategy: "marker" });
    expect(applyParsingStrategyChange({}, "docling")).toEqual({ parsing_strategy: "docling" });
  });

  it("clears parsing_strategy when choosing the inherit-global default", () => {
    const cleared = applyParsingStrategyChange({ parsing_strategy: "marker" }, PARSING_STRATEGY_INHERIT);
    expect(cleared).not.toHaveProperty("parsing_strategy");
    expect(cleared).toEqual({});
  });

  it("keeps unrelated config keys intact when changing strategy", () => {
    expect(
      applyParsingStrategyChange({ topic_tagging_llm: "some-llm" }, "marker"),
    ).toEqual({ parsing_strategy: "marker", topic_tagging_llm: "some-llm" });
  });

  it("disables image captioning when switching to pymupdf (text-only backend)", () => {
    expect(applyParsingStrategyChange({}, "pymupdf")).toEqual({
      parsing_strategy: "pymupdf",
      enable_image_captioning: false,
    });
  });

  it("does not toggle image captioning for marker/docling", () => {
    expect(applyParsingStrategyChange({}, "marker")).not.toHaveProperty("enable_image_captioning");
    expect(applyParsingStrategyChange({}, "docling")).not.toHaveProperty("enable_image_captioning");
  });

  it("clears the forced-off captioning flag when leaving pymupdf for another backend", () => {
    // pymupdf forced captioning off; switching to marker/docling (which can
    // caption) must revert to the sparse/default (enabled) behavior.
    expect(
      applyParsingStrategyChange({ parsing_strategy: "pymupdf", enable_image_captioning: false }, "marker"),
    ).toEqual({ parsing_strategy: "marker" });
    expect(
      applyParsingStrategyChange({ parsing_strategy: "pymupdf", enable_image_captioning: false }, "docling"),
    ).toEqual({ parsing_strategy: "docling" });
  });

  it("clears the forced-off captioning flag when leaving pymupdf for inherit-default", () => {
    expect(
      applyParsingStrategyChange(
        { parsing_strategy: "pymupdf", enable_image_captioning: false },
        PARSING_STRATEGY_INHERIT,
      ),
    ).toEqual({});
  });

  it("preserves an explicit captioning choice when not coming from pymupdf", () => {
    // marker -> docling is not a pymupdf exit, so a user-set false is kept.
    expect(
      applyParsingStrategyChange({ parsing_strategy: "marker", enable_image_captioning: false }, "docling"),
    ).toEqual({ parsing_strategy: "docling", enable_image_captioning: false });
  });

  it("does not mutate the input config", () => {
    const input = { parsing_strategy: "marker" };
    applyParsingStrategyChange(input, PARSING_STRATEGY_INHERIT);
    expect(input).toEqual({ parsing_strategy: "marker" });
  });
});
