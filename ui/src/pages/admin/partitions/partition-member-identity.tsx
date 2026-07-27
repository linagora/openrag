import type { PartitionMember } from "@/lib/api/partitions";

export function PartitionMemberIdentity({ member }: { member: PartitionMember }) {
  const primaryIdentity = member.display_name || member.email || `User ${member.user_id}`;
  const showEmailSeparately = member.display_name && member.email;

  return (
    <div className="min-w-0">
      <div className="truncate font-medium" title={primaryIdentity}>
        {primaryIdentity}
      </div>
      {showEmailSeparately && (
        <div className="truncate text-xs text-muted-foreground" title={member.email ?? undefined}>
          {member.email}
        </div>
      )}
      <div className="font-mono text-xs text-muted-foreground">User ID {member.user_id}</div>
    </div>
  );
}
