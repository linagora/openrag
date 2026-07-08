import { Badge } from "@/components/ui/badge";

/**
 * Read-only file-quota usage meter: indexed count / effective quota, a fill bar,
 * and a remaining / over / at-limit / unlimited indicator. Shared by the admin
 * user-detail "Storage & quota" card and the regular-user Overview card so the
 * two render identically.
 *
 * `quota` is the *effective* quota: a number, `"unlimited"`, or `"unknown"`
 * (the global default hasn't loaded yet).
 */
export function QuotaUsageMeter({
  fileCount,
  quota,
}: {
  fileCount: number;
  quota: number | "unlimited" | "unknown";
}) {
  const cap = typeof quota === "number" ? quota : null;
  const capText = quota === "unlimited" ? "∞" : quota === "unknown" ? "default" : String(quota);
  const usagePct =
    cap == null
      ? 0
      : cap === 0
        ? fileCount > 0
          ? 100
          : 0
        : Math.min(100, Math.round((fileCount / cap) * 100));
  const remaining = cap == null ? 0 : cap - fileCount;
  const over = cap != null && fileCount > cap;
  const atLimit = cap != null && remaining === 0;

  return (
    <div className="rounded-lg border bg-muted/40 px-4 py-3">
      <div className="flex items-baseline justify-between gap-3">
        <div className="flex items-baseline gap-1.5">
          <span className="text-2xl font-semibold leading-none tabular-nums">{fileCount}</span>
          <span className="text-sm text-muted-foreground">/ {capText} files indexed</span>
        </div>
        {quota === "unlimited" ? (
          <Badge variant="secondary">Unlimited</Badge>
        ) : cap != null ? (
          <span className="text-xs tabular-nums text-muted-foreground">{usagePct}%</span>
        ) : null}
      </div>
      {cap != null && (
        <>
          <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-muted">
            <div
              className={
                over || atLimit
                  ? "h-full rounded-full bg-destructive"
                  : "h-full rounded-full bg-primary"
              }
              style={{ width: `${usagePct}%` }}
            />
          </div>
          <div className="mt-2">
            {over ? (
              <Badge variant="destructive">Over by {fileCount - cap}</Badge>
            ) : atLimit ? (
              <Badge variant="destructive">At limit</Badge>
            ) : (
              <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-0.5 text-xs font-medium text-emerald-700">
                {remaining} remaining
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
