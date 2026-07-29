import { describe, it, expect } from "vitest";
import {
  PROMPT_DEFAULT_OPTION,
  promptOptionToName,
  promptOptionValue,
  promptSelectValue,
  renderPreview,
  scanTemplate,
  validatePlaceholders,
} from "./prompt-meta";

// The editor must reach the same verdict as the API's `string.Formatter`
// validation, or a template looks fine here and comes back 422 on save.
describe("scanTemplate", () => {
  it("collects simple fields", () => {
    expect(scanTemplate("Answer with {context} on {current_date}").fields).toEqual([
      "context",
      "current_date",
    ]);
  });

  it("treats doubled braces as literal text, not placeholders", () => {
    const scan = scanTemplate('emit {{"a": 1}} verbatim');
    expect(scan.malformed).toBe(false);
    expect(scan.fields).toEqual([]);
  });

  it("flags a lone opening brace the way Python does", () => {
    expect(scanTemplate("what { is this").malformed).toBe(true);
  });

  it("flags a lone closing brace", () => {
    expect(scanTemplate("what } is this").malformed).toBe(true);
  });

  it("reduces conversions, format specs and attribute/index access to the root", () => {
    expect(scanTemplate("{context!r} {context:>10} {context.attr} {context[0]}").fields).toEqual([
      "context",
    ]);
  });

  it("treats auto-numbering as an unnamed field, which the API rejects", () => {
    expect(validatePlaceholders("hello {}", "sys_prompt").unknown).toEqual([""]);
  });
});

describe("validatePlaceholders", () => {
  it("accepts a template using only its type's variables", () => {
    const v = validatePlaceholders("Use {context} on {current_date}", "sys_prompt");
    expect(v.unknown).toEqual([]);
    expect(v.malformed).toBe(false);
  });

  it("reports an unknown variable", () => {
    expect(validatePlaceholders("About {topic}", "sys_prompt").unknown).toEqual(["topic"]);
  });

  it("accepts any literal text for verbatim prompt types", () => {
    // chunk_contextualizer is sent to the model as-is and never `.format`-ed,
    // so braces in it are just characters.
    const v = validatePlaceholders('Return {"json": true} exactly', "chunk_contextualizer");
    expect(v.unknown).toEqual([]);
    expect(v.malformed).toBe(false);
  });
});

describe("prompt picker option values", () => {
  it("round-trips a name", () => {
    expect(promptOptionToName(promptOptionValue("legal-assistant"))).toBe("legal-assistant");
  });

  it("maps an unset name to the default sentinel", () => {
    expect(promptSelectValue(undefined)).toBe(PROMPT_DEFAULT_OPTION);
    expect(promptSelectValue("")).toBe(PROMPT_DEFAULT_OPTION);
    expect(promptOptionToName(PROMPT_DEFAULT_OPTION)).toBe("");
  });

  it("keeps a prompt whose name looks like the sentinel selectable", () => {
    // Names are free text, so the sentinel must not be able to collide with one.
    for (const name of ["__default__", "__use_default__"]) {
      const value = promptOptionValue(name);
      expect(value).not.toBe(PROMPT_DEFAULT_OPTION);
      expect(promptOptionToName(value)).toBe(name);
    }
  });
});

// The preview must agree with what the pipeline actually renders — it shares
// the tokenizer with validation so the two can't drift apart.
describe("renderPreview", () => {
  const flat = (content: string, type = "sys_prompt") =>
    renderPreview(content, type)
      .map((s) => s.value)
      .join("");

  it("substitutes a known variable with its sample", () => {
    const segments = renderPreview("Today is {current_date}.", "sys_prompt");
    const substituted = segments.find((s) => s.isVariable);
    expect(substituted?.varName).toBe("current_date");
    expect(flat("Today is {current_date}.")).toContain("2026-07-27");
  });

  it("unescapes doubled braces the way str.format does", () => {
    expect(flat('emit {{"a": 1}} verbatim')).toBe('emit {"a": 1} verbatim');
  });

  it("substitutes a field carrying a format spec or conversion", () => {
    expect(flat("Today is {current_date:>12}.")).toContain("2026-07-27");
    expect(flat("Today is {current_date!r}.")).toContain("2026-07-27");
  });

  it("leaves an unknown field visible rather than dropping it", () => {
    expect(flat("About {topic}")).toBe("About {topic}");
  });

  it("previews a verbatim prompt type unchanged", () => {
    const raw = 'Return {"json": true} exactly';
    expect(flat(raw, "chunk_contextualizer")).toBe(raw);
  });
});
