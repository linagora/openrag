/* Pure helpers for the preset config editor (see presets.tsx).
 *
 * Kept in their own module (not presets.tsx) so they can be unit-tested and so
 * the component file only exports components (react-refresh/only-export-components).
 */

export type Config = Record<string, unknown>;

export function configGet<T>(config: Config, key: string, fallback: T): T {
  const v = config[key];
  if (v === undefined || v === null) return fallback;
  return v as T;
}

export function configSet(prev: Config, key: string, value: unknown): Config {
  return { ...prev, [key]: value };
}

export function configUnset(prev: Config, key: string): Config {
  const next = { ...prev };
  delete next[key];
  return next;
}

// Sentinel for "no explicit parsing_strategy". An absent key means the preset
// inherits the deployment's global PDF loader (file_loaders.pdf), so we surface
// that as an explicit "Default" choice instead of a fabricated concrete value.
// Mirrors the retrieval form's "__none__" convention so the shown value always
// matches what gets persisted (WYSIWYG).
export const PARSING_STRATEGY_INHERIT = "__inherit__";

// Map a parsing-strategy selection to the next config. Kept pure so the
// persistence behavior is unit-testable without driving the Radix Select.
//
// Fixes the bug where the dropdown *displayed* a default of "marker" that was
// never written to config unless the user actively changed the selection — a
// preset created while leaving "marker" showing saved no parsing_strategy and
// silently fell back to the global loader (e.g. pymupdf).
export function applyParsingStrategyChange(config: Config, value: string): Config {
  if (value === PARSING_STRATEGY_INHERIT) {
    // Explicitly clear so the preset inherits the global loader.
    return configUnset(config, "parsing_strategy");
  }
  let next = configSet(config, "parsing_strategy", value);
  // pymupdf is text-only (no images), so captioning can't apply — turn it off
  // when switching to it (see the disabled captioning toggle in presets.tsx).
  if (value === "pymupdf") next = configSet(next, "enable_image_captioning", false);
  return next;
}
