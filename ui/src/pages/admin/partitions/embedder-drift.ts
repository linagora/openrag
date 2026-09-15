import { resolveEmbedderName, resolveEmbedderModel, type ModelEndpointResponse } from "@/lib/api/models";
import type { IndexedEmbedderCount } from "@/lib/api/partitions";

/** One model that produced some of a partition's files. Not one endpoint: two
 *  endpoint names running the same model are one entry, because the model is
 *  what decides the vector space.
 */
type EmbedderGroup = {
  /** Unique per group, for list keys. `key` is the model shown, which one model
   *  at two widths, or in two fields, shares across groups. */
  id: string;
  key: string;
  drifted: boolean;
  /** False only for files indexed before provenance existed. */
  recorded: boolean;
  dimension: number | null;
  file_count: number;
};

/** One answer to "did the embedder change under this partition", shared by the
 *  panel and the dialog so the two can never disagree on screen.
 */
export function computeEmbedderDrift(
  configured: string,
  indexed: IndexedEmbedderCount[] | undefined,
  endpoints: ModelEndpointResponse[] | undefined,
  /** Width of the vectors queries search today, read from the live collection;
   *  null when it cannot be read. */
  liveDimension: number | null = null,
) {
  // The dense field queries read today. Two endpoints can run the same model
  // and still own different fields, so a file can record the right model and
  // sit somewhere no search looks — the same invisibility a model change
  // causes, and the swap's own skip test compares both.
  const currentField = endpoints?.find((e) => e.name === resolveEmbedderName(configured, endpoints))?.vector_field;
  const rows = indexed ?? [];
  // The stored reference may be the "default" alias; compare on what it
  // resolves to, or a partition on the alias would look drifted from itself.
  const currentName = resolveEmbedderName(configured, endpoints);
  const label = (r: IndexedEmbedderCount) =>
    r.embedder === null ? "unrecorded" : resolveEmbedderName(r.embedder, endpoints);
  // What queries actually embed with. Compared on the *model*, not the endpoint
  // label: renaming an endpoint cascades to `partitions.embedder` but never
  // rewrites a file's recorded provenance, so name comparison reports every
  // file indexed before a rename as drifted when the same model produced them.
  const currentModel = resolveEmbedderModel(configured, endpoints);
  // And named by the model too, so the panel does not compare two models and
  // then report the verdict against an endpoint label the rows never mention.
  const current = currentModel ?? currentName;
  const rowModel = (r: IndexedEmbedderCount) => r.model_name ?? resolveEmbedderModel(r.embedder, endpoints);
  // Merged by model, because the model is what the reader is actually being
  // told about. The backend groups by endpoint *reference*, so a renamed
  // endpoint splits one model across two rows that both say "current" — which
  // reads as two embedders and invites exactly the alarm this panel exists to
  // raise honestly. Rows with no record predate provenance: unknown, not
  // known-bad, so they never count as drift.
  const groups: EmbedderGroup[] = [];
  const byKey = new Map<string, EmbedderGroup>();
  for (const r of rows) {
    const recorded = r.embedder !== null;
    const model = recorded ? rowModel(r) : null;
    const shown = label(r);
    const name = !recorded ? "unrecorded" : (model ?? shown);
    // Width and field are part of the identity, not details of it: the same
    // model at 768 and at 1024 produced two vector spaces, and the same model
    // in two fields is in one index queries read and one they do not. Merging
    // either would report a single healthy group and drop the rest.
    const id = `${name}\u0000${r.dimension ?? ""}\u0000${r.vector_field ?? ""}`;
    let group = byKey.get(id);
    if (group === undefined) {
      // A width the embedder's field does not have is not in the index queries
      // search, whichever model produced it. Unknown on either side is not
      // drift: a field that cannot be read says nothing about the files.
      const widthDrifted = r.dimension !== null && liveDimension !== null && r.dimension !== liveDimension;
      // Unrecorded on either side is unknown, not wrong: files predating the
      // field are shown as they always were.
      const fieldDrifted =
        r.vector_field != null && currentField != null && r.vector_field !== currentField;
      group = {
        id,
        key: name,
        // A model unknown on either side is unknown drift, not drift. The
        // endpoint list is admin-only and loads after the partition, so the
        // current model is often unresolvable, and the labels left to compare
        // disagree for healthy files: a file keeps the endpoint name it was
        // indexed under through a rename, and may have recorded the `default`
        // alias itself. A width or field the partition does not read is still
        // proof on its own.
        drifted:
          recorded &&
          (widthDrifted || fieldDrifted || (model !== null && currentModel !== null && model !== currentModel)),
        recorded,
        dimension: r.dimension,
        file_count: 0,
      };
      byKey.set(id, group);
      groups.push(group);
    }
    group.file_count += r.file_count;
  }
  groups.sort((a, b) => b.file_count - a.file_count);
  const drifted = groups.filter((g) => g.drifted);
  return {
    groups,
    current,
    drifted,
    hasDrift: drifted.length > 0,
    driftedFiles: drifted.reduce((sum, g) => sum + g.file_count, 0),
  };
}

/** Identifies a drift state, so an acknowledgement covers that state and no
 *  later one. Width is part of it: the same model and file count at another
 *  dimension is a new drift, not the one already dismissed.
 */
export function driftSignature(drift: ReturnType<typeof computeEmbedderDrift>): string {
  if (!drift.hasDrift) return "";
  return `${drift.current}<-${drift.drifted.map((g) => `${g.key}@${g.dimension ?? "?"}:${g.file_count}`).join(",")}`;
}
