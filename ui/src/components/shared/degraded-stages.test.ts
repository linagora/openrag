import { describe, expect, it } from "vitest";

import { normalizeDegradedStages } from "./degraded-stages";

describe("normalizeDegradedStages", () => {
  it("removes duplicate non-empty strings while preserving their first-seen order", () => {
    expect(
      normalizeDegradedStages([
        "caption",
        "",
        "topic_tag",
        "caption",
        null,
        "contextualize",
        "topic_tag",
      ]),
    ).toEqual(["caption", "topic_tag", "contextualize"]);
  });
});
