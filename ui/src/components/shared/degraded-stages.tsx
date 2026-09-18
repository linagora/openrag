import { Badge } from "@/components/ui/badge";

export const DEGRADED_STAGE_OPTIONS = [
  { value: "caption", label: "Caption" },
  { value: "contextualize", label: "Contextualization" },
  { value: "topic_tag", label: "Topic tagging" },
] as const;

export type DegradedStage = (typeof DEGRADED_STAGE_OPTIONS)[number]["value"];

const stageLabels = new Map<string, string>(
  DEGRADED_STAGE_OPTIONS.map(({ value, label }) => [value, label]),
);

export function normalizeDegradedStages(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [
    ...new Set(value.filter((stage): stage is string => typeof stage === "string" && stage.length > 0)),
  ];
}

export function formatDegradedStage(stage: string): string {
  return stageLabels.get(stage) ?? stage.replace(/_/g, " ").replace(/^./, (letter) => letter.toUpperCase());
}

export function DegradedStageBadges({ stages }: { stages: unknown }) {
  const normalized = normalizeDegradedStages(stages);
  if (!normalized.length) return null;

  return (
    <div className="flex flex-wrap gap-1">
      {normalized.map((stage) => (
        <Badge
          key={stage}
          variant="outline"
          className="border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200"
        >
          {formatDegradedStage(stage)}
        </Badge>
      ))}
    </div>
  );
}

export function DegradedCompletionStatus({ stages }: { stages: unknown }) {
  const normalized = normalizeDegradedStages(stages);

  return (
    <div className="space-y-1">
      <Badge
        variant="outline"
        className="border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200"
      >
        Completed with degradation
      </Badge>
      {normalized.length > 0 && (
        <p className="text-xs text-muted-foreground">
          {normalized.map(formatDegradedStage).join(", ")}
        </p>
      )}
    </div>
  );
}
