// Client-side route builders. Centralizing these keeps dynamic-segment encoding
// in one place: partition (and other) names are caller-chosen and may contain
// `/ # ? %`, which break a route if interpolated raw.

/** Route to a partition's detail page, with the name URL-encoded. */
export function partitionDetailPath(name: string): string {
  return `/partitions/${encodeURIComponent(name)}`;
}
