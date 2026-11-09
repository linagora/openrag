import { useEffect, useState } from "react";

const STAGE_ELAPSED_UPDATE_MS = 5_000;

function formatDuration(durationMs: number): string {
  const totalSeconds = Math.floor(durationMs / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  if (hours > 0) return `${hours}h${minutes > 0 ? ` ${minutes}m` : ""}`;
  if (minutes > 0) return `${minutes}m${seconds > 0 ? ` ${seconds}s` : ""}`;
  return `${seconds}s`;
}

function formatStageDuration(durationMs: number): string {
  return durationMs < STAGE_ELAPSED_UPDATE_MS ? "<5s" : formatDuration(durationMs);
}

function elapsedFromSample(elapsedMs: number, elapsedSampledAt: string | null): number {
  if (!elapsedSampledAt) return elapsedMs;
  const sampledAtMs = Date.parse(elapsedSampledAt);
  if (!Number.isFinite(sampledAtMs)) return elapsedMs;
  return elapsedMs + Math.max(0, Date.now() - sampledAtMs);
}

export function StageElapsedTime({
  elapsedMs,
  elapsedSampledAt,
}: {
  elapsedMs: number;
  elapsedSampledAt: string | null;
}) {
  const [elapsed, setElapsed] = useState(() => elapsedFromSample(elapsedMs, elapsedSampledAt));

  useEffect(() => {
    const sampledElapsedMs = elapsedFromSample(elapsedMs, elapsedSampledAt);
    const startedAt = performance.now();
    setElapsed(sampledElapsedMs);
    const interval = window.setInterval(() => {
      setElapsed(sampledElapsedMs + (performance.now() - startedAt));
    }, STAGE_ELAPSED_UPDATE_MS);
    return () => window.clearInterval(interval);
  }, [elapsedMs, elapsedSampledAt]);

  return <span>{formatStageDuration(elapsed)}</span>;
}

export const stageBadgeColors: Record<string, string> = {
  parse: "border-amber-200 bg-amber-100 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200",
  caption: "border-violet-200 bg-violet-100 text-violet-900 dark:border-violet-900 dark:bg-violet-950 dark:text-violet-200",
  chunk: "border-cyan-200 bg-cyan-100 text-cyan-900 dark:border-cyan-900 dark:bg-cyan-950 dark:text-cyan-200",
  contextualize: "border-blue-200 bg-blue-100 text-blue-900 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-200",
  topic_tag: "border-pink-200 bg-pink-100 text-pink-900 dark:border-pink-900 dark:bg-pink-950 dark:text-pink-200",
  embed: "border-emerald-200 bg-emerald-100 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
  store: "border-indigo-200 bg-indigo-100 text-indigo-900 dark:border-indigo-900 dark:bg-indigo-950 dark:text-indigo-200",
};

export const defaultStageBadgeColor =
  "border-slate-200 bg-slate-100 text-slate-800 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-200";
