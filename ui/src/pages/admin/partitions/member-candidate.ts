import type { PartitionMemberCandidate } from "@/lib/api/partitions";

export function candidateLabel(candidate: PartitionMemberCandidate): string {
  return candidate.display_name?.trim() || "Unnamed user";
}

export function candidateSecondaryLabel(candidate: PartitionMemberCandidate): string {
  return candidate.email?.trim() || `User ID ${candidate.user_id}`;
}
