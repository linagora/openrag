import { useMemo } from "react";
import { Search, X } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import type { PartitionMemberCandidate } from "@/lib/api/partitions";

interface MemberPickerProps {
  candidates: PartitionMemberCandidate[];
  isLoading: boolean;
  isInitialError: boolean;
  isRefreshError: boolean;
  isLoadMoreError: boolean;
  search: string;
  searchReady: boolean;
  onSearchChange: (search: string) => void;
  onRetry: () => void;
  hasMore: boolean;
  isLoadingMore: boolean;
  onLoadMore: () => void;
  selectedCandidates: PartitionMemberCandidate[];
  onSelectionChange: (candidates: PartitionMemberCandidate[]) => void;
}

function candidateLabel(candidate: PartitionMemberCandidate): string {
  return candidate.display_name?.trim() || `User #${candidate.user_id}`;
}

export function MemberPicker({
  candidates,
  isLoading,
  isInitialError,
  isRefreshError,
  isLoadMoreError,
  search,
  searchReady,
  onSearchChange,
  onRetry,
  hasMore,
  isLoadingMore,
  onLoadMore,
  selectedCandidates,
  onSelectionChange,
}: MemberPickerProps) {
  const selected = useMemo(
    () => new Set(selectedCandidates.map((candidate) => candidate.user_id)),
    [selectedCandidates],
  );

  const toggleCandidate = (candidate: PartitionMemberCandidate, checked: boolean) => {
    if (checked) {
      onSelectionChange(
        selected.has(candidate.user_id) ? selectedCandidates : [...selectedCandidates, candidate],
      );
    } else {
      onSelectionChange(
        selectedCandidates.filter((selectedCandidate) => selectedCandidate.user_id !== candidate.user_id),
      );
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <Label htmlFor="partition-member-search">Users</Label>
        <span className="text-xs text-muted-foreground" aria-live="polite">
          {selectedCandidates.length} selected
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
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder="Search by name or user ID..."
          className="pl-9"
        />
      </div>

      {selectedCandidates.length > 0 && (
        <div
          className="space-y-1.5 rounded-md border bg-muted/30 p-2"
          role="region"
          aria-label="Selected users"
        >
          <p className="text-xs font-medium text-muted-foreground">Selected users</p>
          {selectedCandidates.map((candidate) => {
            const label = candidateLabel(candidate);
            return (
              <div
                key={candidate.user_id}
                className="flex items-center justify-between gap-2 rounded bg-background px-2 py-1.5 text-sm"
              >
                <span className="min-w-0 truncate">
                  {label} <span className="font-mono text-xs text-muted-foreground">#{candidate.user_id}</span>
                </span>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6 shrink-0"
                  onClick={() => toggleCandidate(candidate, false)}
                  aria-label={`Remove ${label}, user ID ${candidate.user_id}`}
                >
                  <X className="h-3.5 w-3.5" />
                </Button>
              </div>
            );
          })}
        </div>
      )}

      {isRefreshError && (
        <Alert variant="destructive">
          <AlertDescription className="flex-row items-center justify-between gap-3">
            <span>Results could not be refreshed. The previous results remain available.</span>
            <Button type="button" variant="outline" size="sm" onClick={onRetry}>
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      )}

      <div className="max-h-64 overflow-y-auto rounded-md border" role="group" aria-label="Available users">
        {!searchReady ? (
          <p className="p-4 text-center text-sm text-muted-foreground">
            Enter at least 3 characters or an exact user ID.
          </p>
        ) : isLoading ? (
          <div className="space-y-2 p-3">
            {Array.from({ length: 3 }).map((_, index) => (
              <Skeleton key={index} className="h-11 w-full" />
            ))}
          </div>
        ) : isInitialError ? (
          <div className="space-y-2 p-4 text-center text-sm text-destructive">
            <p>Users could not be loaded.</p>
            <Button type="button" variant="outline" size="sm" onClick={onRetry}>
              Retry
            </Button>
          </div>
        ) : candidates.length === 0 ? (
          <p className="p-4 text-center text-sm text-muted-foreground">
            No available users match this search.
          </p>
        ) : (
          <>
            <div className="divide-y">
              {candidates.map((candidate) => {
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
                      onCheckedChange={(checked) => toggleCandidate(candidate, checked === true)}
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
            {(hasMore || isLoadMoreError) && (
              <div className="border-t p-2">
                {isLoadMoreError && (
                  <p className="mb-2 text-center text-xs text-destructive">
                    More users could not be loaded. You can retry without losing this page.
                  </p>
                )}
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="w-full"
                  onClick={onLoadMore}
                  disabled={isLoadingMore}
                >
                  {isLoadingMore ? "Loading..." : isLoadMoreError ? "Retry loading more" : "Load more users"}
                </Button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
