import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "—";
  return new Date(dateStr).toLocaleString();
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

/**
 * Parse an integer from a form-field string, falling back to `fallback` when the
 * field is empty or non-numeric. Guards against `parseInt("")` → `NaN`, which
 * `JSON.stringify` would serialize as `null` and send to the API.
 */
export function intOr(value: string, fallback: number): number {
  const n = parseInt(value, 10);
  return Number.isNaN(n) ? fallback : n;
}

/** Float counterpart of {@link intOr} — never yields `NaN`. */
export function numOr(value: string, fallback: number): number {
  const n = parseFloat(value);
  return Number.isNaN(n) ? fallback : n;
}

/**
 * Copy text to the clipboard, working outside secure contexts too.
 * navigator.clipboard is only available over HTTPS/localhost; over plain HTTP
 * it's undefined, so fall back to a hidden textarea + execCommand("copy").
 *
 * `anchor` (typically the clicked button) anchors the hidden textarea inside
 * the same DOM subtree instead of `document.body`. This matters when the
 * caller lives inside a focus-trapped container (e.g. a Radix Dialog): the
 * trap treats a `document.body` child as "outside" and yanks focus straight
 * back before `execCommand("copy")` can read the selection, so the command
 * silently copies nothing while still reporting success.
 *
 * Returns whether the copy succeeded.
 */
export async function copyToClipboard(text: string, anchor?: Element | null): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to the legacy path (e.g. permission denied)
    }
  }
  try {
    const container = anchor?.parentElement ?? document.body;
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    container.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    container.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}
