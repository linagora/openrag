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

// An indexation preset may omit its STT endpoint and follow the global default
// managed in Model Endpoints. Select values cannot be empty, so keep a private
// sentinel that is never persisted as an endpoint name.
export const STT_ENDPOINT_DEFAULT_OPTION = "__use_default_stt_endpoint__";

// Map a parsing-strategy selection to the next config. Kept pure so the
// persistence behavior is unit-testable without driving the Radix Select.
//
// Fixes the bug where the dropdown *displayed* a default of "marker" that was
// never written to config unless the user actively changed the selection — a
// preset created while leaving "marker" showing saved no parsing_strategy and
// silently fell back to the global loader (e.g. pymupdf).
export function applyParsingStrategyChange(config: Config, value: string): Config {
  // While pymupdf is selected the captioning toggle is disabled, so a stored
  // enable_image_captioning:false is always system-forced (never a user choice).
  const leavingPymupdf = config.parsing_strategy === "pymupdf" && value !== "pymupdf";

  let next: Config;
  if (value === PARSING_STRATEGY_INHERIT) {
    // Explicitly clear so the preset inherits the global loader.
    next = configUnset(config, "parsing_strategy");
  } else {
    next = configSet(config, "parsing_strategy", value);
    // pymupdf is text-only (no images), so captioning can't apply — force it off.
    if (value === "pymupdf") next = configSet(next, "enable_image_captioning", false);
  }

  // Leaving pymupdf (for any other strategy, including inherit): drop the
  // forced-off flag so captioning reverts to the sparse/default (enabled)
  // behavior instead of staying stuck off on a backend that can caption.
  if (leavingPymupdf) next = configUnset(next, "enable_image_captioning");

  return next;
}
