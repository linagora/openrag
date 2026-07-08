/**
 * Resolve which partition the Documents view should show.
 *
 * The view keeps a "sticky" choice (the URL `?partition=` param, or the last
 * selection remembered in sessionStorage) so navigating away and back lands on
 * the same partition. But that sticky value can go stale — the partition may be
 * deleted by its owner or an admin — in which case using it verbatim 404s the
 * files query ("Partition 'X' not found").
 *
 * Rule: prefer the candidate, but once the partition list has loaded, fall back
 * to the first available partition if the candidate no longer exists. While the
 * list is still loading we keep the candidate (the files query is gated on a
 * confirmed-existing selection separately, so nothing is fetched prematurely).
 * Returns "" when there is no candidate and no partitions.
 */
export function resolveDocumentsPartition(
  candidate: string,
  partitions: { partition: string }[],
  loaded: boolean,
): string {
  if (candidate && (!loaded || partitions.some((p) => p.partition === candidate))) {
    return candidate;
  }
  return partitions[0]?.partition ?? "";
}
