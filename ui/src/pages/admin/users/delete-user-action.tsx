import { useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { getUser, deleteUserDescription } from "@/lib/api/users";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";

// Ownership can change while a page sits open (an upload auto-creates a
// partition, another admin grants owner), so each opening fetches the user
// under its own key: nothing cached, nor a request from an earlier opening,
// can name partitions, and the profile query is left alone. Until that fetch
// succeeds, or if it fails, the generic text is shown, which is always true.
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
  const openings = useRef(0);
  const [opening, setOpening] = useState(0);
  const { data, isSuccess } = useQuery({
    queryKey: ["user-delete-snapshot", userId, opening],
    queryFn: () => getUser(userId),
    enabled: opening > 0,
    gcTime: 0,
  });

  return (
    <ConfirmDialog
      title="Delete user?"
      description={deleteUserDescription(label, opening > 0 && isSuccess ? data.memberships : undefined)}
      onConfirm={onConfirm}
      onOpenChange={(open) => setOpening(open ? ++openings.current : 0)}
    >
      {children}
    </ConfirmDialog>
  );
}
