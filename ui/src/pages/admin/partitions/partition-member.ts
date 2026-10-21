import type { PartitionMember } from "@/lib/api/partitions";

export function describePartitionMember(member: PartitionMember): string {
  const knownIdentity = [member.display_name, member.email].filter(Boolean).join(", ");
  return knownIdentity
    ? `${knownIdentity} (user ID ${member.user_id})`
    : `user ID ${member.user_id}`;
}
