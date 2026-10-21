import { addPartitionMember } from "@/lib/api/partitions";
import type { PartitionMemberCandidate, PartitionRole } from "@/lib/api/partitions";
import { ApiError } from "@/lib/api/client";

export interface MemberAddFailure {
  candidate: PartitionMemberCandidate;
  message: string;
}

interface AddPartitionMembersOptions {
  partitionName: string;
  candidates: PartitionMemberCandidate[];
  role: PartitionRole;
  addMember?: typeof addPartitionMember;
}

export async function addPartitionMembers({
  partitionName,
  candidates,
  role,
  addMember = addPartitionMember,
}: AddPartitionMembersOptions): Promise<{
  addedCandidates: PartitionMemberCandidate[];
  failures: MemberAddFailure[];
}> {
  const addedCandidates: PartitionMemberCandidate[] = [];
  const failures: MemberAddFailure[] = [];

  for (const [index, candidate] of candidates.entries()) {
    try {
      await addMember(partitionName, candidate.user_id, role);
      addedCandidates.push(candidate);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown error";
      failures.push({ candidate, message });

      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
        for (const remainingCandidate of candidates.slice(index + 1)) {
          failures.push({
            candidate: remainingCandidate,
            message: "Not attempted because permission to manage members was lost.",
          });
        }
        break;
      }
    }
  }

  return { addedCandidates, failures };
}
