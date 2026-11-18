import { useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getUser, deleteUserDescription } from "@/lib/api/users";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";

// Ownership can change while a page sits open (an upload auto-creates a
// partition, another admin grants owner), so the user is refetched when the
// dialog opens. Partitions are only named from a fetch that finished after
// that; until then, or if it fails, the generic text is shown, which is
// always true.
export function DeleteUserAction({
  userId,
  label,
  onConfirm,
  children,
}: {
  userId: number;
  label: string;
  onConfirm: () => void;
  children: ReactNode;
}) {
  const queryClient = useQueryClient();
  const [openedAt, setOpenedAt] = useState(0);
  const { data, dataUpdatedAt, isError, refetch } = useQuery({
    queryKey: ["user", String(userId)],
    queryFn: () => getUser(userId),
    enabled: false,
  });
  const fresh = openedAt > 0 && dataUpdatedAt >= openedAt && !isError;

  return (
    <ConfirmDialog
      title="Delete user?"
      description={deleteUserDescription(label, fresh ? data?.memberships : undefined)}
      onConfirm={onConfirm}
      onOpenChange={(open) => {
        if (open) {
          setOpenedAt(Date.now());
          // With no data yet, refetch() joins a request still running from an
          // earlier opening; cancel it so the answer comes from this one.
          void queryClient
            .cancelQueries({ queryKey: ["user", String(userId)] })
            .then(() => refetch());
        }
      }}
    >
      {children}
    </ConfirmDialog>
  );
}
