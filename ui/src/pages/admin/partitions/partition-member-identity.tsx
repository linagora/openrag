import type { PartitionMember } from "@/lib/api/partitions";

export function PartitionMemberIdentity({ member }: { member: PartitionMember }) {
  const primaryIdentity = member.display_name || `User ${member.user_id}`;

  return (
    <div className="min-w-0">
      <div className="truncate font-medium" title={primaryIdentity}>
        {primaryIdentity}
      </div>
      <div className="font-mono text-xs text-muted-foreground">User ID {member.user_id}</div>
    </div>
  );
}

export function PartitionMemberEmail({ member }: { member: PartitionMember }) {
  if (!member.email) {
    return <span className="text-muted-foreground">Not available</span>;
  }

  return (
    <span className="block max-w-64 truncate" title={member.email}>
      {member.email}
    </span>
  );
}
