import { resolveEmbedderName, resolveEmbedderModel, type ModelEndpointResponse } from "@/lib/api/models";
import type { IndexedEmbedderCount } from "@/lib/api/partitions";

/** One model that produced some of a partition's files. Not one endpoint: two
 *  endpoint names running the same model are one entry, because the model is
 *  what decides the vector space.
 */
type EmbedderGroup = {
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
) {
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
    const key = !recorded ? "unrecorded" : (model ?? shown);
    let group = byKey.get(key);
    if (group === undefined) {
      group = {
        key,
        // When either side's model is unknown, the labels are all that is left.
        drifted:
          recorded &&
          (model !== null && currentModel !== null ? model !== currentModel : shown !== currentName),
        recorded,
        dimension: r.dimension,
        file_count: 0,
      };
      byKey.set(key, group);
      groups.push(group);
    }
    group.file_count += r.file_count;
    if (group.dimension === null) group.dimension = r.dimension;
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
