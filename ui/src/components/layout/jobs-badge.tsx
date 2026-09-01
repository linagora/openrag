import type { ActiveJobsCountResult } from "@/lib/jobs-queries";

export function JobsBadge({ result }: { result: ActiveJobsCountResult }) {
  if (!result.hasResolvedOnce || result.count <= 0) return null;

  const label = `${result.count} active job${result.count === 1 ? "" : "s"}`;

  return (
    <span
      data-slot="jobs-badge"
      className="pointer-events-none absolute right-2 top-1/2 z-10 -translate-y-1/2 select-none group-data-[collapsible=icon]:right-0 group-data-[collapsible=icon]:top-0 group-data-[collapsible=icon]:translate-y-0"
    >
      <span
        aria-hidden="true"
        className="flex h-5 min-w-5 items-center justify-center rounded-full bg-destructive px-1.5 text-[10px] leading-none font-bold tabular-nums text-white shadow-sm group-data-[collapsible=icon]:h-4 group-data-[collapsible=icon]:min-w-4 group-data-[collapsible=icon]:px-1 group-data-[collapsible=icon]:text-[9px]"
      >
        {result.count > 99 ? "99+" : result.count}
      </span>
      <span className="sr-only">{`, ${label}`}</span>
    </span>
  );
}
