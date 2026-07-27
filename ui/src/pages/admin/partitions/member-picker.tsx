import { useMemo, useState } from "react";
import { Search } from "lucide-react";

import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import type { PartitionMemberCandidate } from "@/lib/api/partitions";

interface MemberPickerProps {
  candidates: PartitionMemberCandidate[];
  isLoading: boolean;
  isError: boolean;
  selectedUserIds: number[];
  onSelectionChange: (userIds: number[]) => void;
}

function candidateLabel(candidate: PartitionMemberCandidate): string {
  return candidate.display_name?.trim() || `User #${candidate.user_id}`;
}

export function MemberPicker({
  candidates,
  isLoading,
  isError,
  selectedUserIds,
  onSelectionChange,
}: MemberPickerProps) {
  const [search, setSearch] = useState("");
  const selected = useMemo(() => new Set(selectedUserIds), [selectedUserIds]);
  const filteredCandidates = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    if (!query) return candidates;
    return candidates.filter((candidate) =>
      `${candidateLabel(candidate)} ${candidate.user_id}`.toLocaleLowerCase().includes(query),
    );
  }, [candidates, search]);

  const toggleCandidate = (userId: number, checked: boolean) => {
    const next = new Set(selected);
    if (checked) {
      next.add(userId);
    } else {
      next.delete(userId);
    }
    onSelectionChange([...next]);
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <Label htmlFor="partition-member-search">Users</Label>
        <span className="text-xs text-muted-foreground" aria-live="polite">
          {selectedUserIds.length} selected
        </span>
      </div>
      <div className="relative">
        <Search
          className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden="true"
        />
        <Input
          id="partition-member-search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search by name or user ID..."
          className="pl-9"
        />
      </div>

      <div className="max-h-64 overflow-y-auto rounded-md border" role="group" aria-label="Available users">
        {isLoading ? (
          <div className="space-y-2 p-3">
            {Array.from({ length: 3 }).map((_, index) => (
              <Skeleton key={index} className="h-11 w-full" />
            ))}
          </div>
        ) : isError ? (
          <p className="p-4 text-center text-sm text-destructive">
            Users could not be loaded. Close the dialog and try again.
          </p>
        ) : candidates.length === 0 ? (
          <p className="p-4 text-center text-sm text-muted-foreground">
            All users are already members of this partition.
          </p>
        ) : filteredCandidates.length === 0 ? (
          <p className="p-4 text-center text-sm text-muted-foreground">
            No users match this search.
          </p>
        ) : (
          <div className="divide-y">
            {filteredCandidates.map((candidate) => {
              const label = candidateLabel(candidate);
              const checkboxId = `partition-member-${candidate.user_id}`;
              return (
                <label
                  key={candidate.user_id}
                  htmlFor={checkboxId}
                  className="flex min-h-11 cursor-pointer items-center gap-3 px-3 py-2 hover:bg-muted/50"
                >
                  <Checkbox
                    id={checkboxId}
                    checked={selected.has(candidate.user_id)}
                    onCheckedChange={(checked) => toggleCandidate(candidate.user_id, checked === true)}
                    aria-label={`Select ${label}, user ID ${candidate.user_id}`}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium">{label}</span>
                    <span className="block font-mono text-xs text-muted-foreground">
                      User ID {candidate.user_id}
                    </span>
                  </span>
                </label>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
