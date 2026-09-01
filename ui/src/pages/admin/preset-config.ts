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
export const TABLE_RECONSTRUCTION_DISABLED = "disabled";

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

export function getTableReconstructionMode(config: Config): string {
  const tableReconstruction = config.table_reconstruction;
  if (!tableReconstruction || typeof tableReconstruction !== "object") {
    return TABLE_RECONSTRUCTION_DISABLED;
  }
  return configGet(
    tableReconstruction as Config,
    "mode",
    TABLE_RECONSTRUCTION_DISABLED,
  );
}

export function applyTableReconstructionMode(
  config: Config,
  mode: string,
): Config {
  const current =
    config.table_reconstruction &&
    typeof config.table_reconstruction === "object"
      ? (config.table_reconstruction as Config)
      : {};
  return {
    ...config,
    table_reconstruction: {
      ...current,
      mode,
    },
  };
}
