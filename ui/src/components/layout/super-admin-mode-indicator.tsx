import { ShieldAlert } from "lucide-react";
import { useQuery } from "@tanstack/react-query";

import { getConfig } from "@/lib/api/system";
import { useAuth } from "@/lib/auth";

export function SuperAdminModeIndicator() {
  const { isAdmin } = useAuth();
  const { data: config } = useQuery({
    queryKey: ["system-config"],
    queryFn: getConfig,
    enabled: isAdmin,
    staleTime: Infinity,
  });

  if (!isAdmin || config?.super_admin_mode !== true) {
    return null;
  }

  return (
    <div
      role="status"
      className="border-b border-amber-200 bg-amber-50 px-6 py-3 text-amber-950 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-100"
    >
      <div className="flex items-start gap-3">
        <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
        <div className="min-w-0 text-sm">
          <div className="font-medium">Super Admin Mode enabled for this deployment.</div>
          <div className="text-amber-900/80 dark:text-amber-100/80">
            Admins can access all partitions and documents in this deployment.
          </div>
        </div>
      </div>
    </div>
  );
}
