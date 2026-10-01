import { useState } from "react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

const ACKNOWLEDGED_KEY_PREFIX = "openrag:embedder-drift-acknowledged:";

function hasAcknowledgedDrift(partition: string): boolean {
  try {
    return localStorage.getItem(ACKNOWLEDGED_KEY_PREFIX + partition) !== null;
  } catch {
    // Storage disabled: the notice shows on each visit rather than never.
    return false;
  }
}

function acknowledgeDrift(partition: string): void {
  try {
    localStorage.setItem(ACKNOWLEDGED_KEY_PREFIX + partition, new Date().toISOString());
  } catch {
    // Storage is an enhancement: the notice closes either way.
  }
}

interface EmbedderDriftDialogProps {
  partition: string;
  // The model the partition's embedder runs now.
  currentModel: string;
  // The other models its files were indexed with.
  drifted: { label: string; file_count: number }[];
}

// Shown once per partition: after "I understand", never again for it.
export function EmbedderDriftDialog({ partition, currentModel, drifted }: EmbedderDriftDialogProps) {
  // Closed for this visit without acknowledging (Escape): it shows again next time.
  const [closedFor, setClosedFor] = useState<string | null>(null);
  const open = drifted.length > 0 && closedFor !== partition && !hasAcknowledgedDrift(partition);

  const fileCount = drifted.reduce((sum, e) => sum + e.file_count, 0);
  const models = drifted.map((e) => e.label).join(", ");

  return (
    <AlertDialog open={open} onOpenChange={(next) => !next && setClosedFor(partition)}>
      <AlertDialogContent>
        <AlertDialogHeader className="min-w-0">
          <AlertDialogTitle>Some files were indexed with another embedder</AlertDialogTitle>
          <AlertDialogDescription className="min-w-0 [overflow-wrap:anywhere]">
            Partition “{partition}” now embeds with {currentModel}, but {fileCount} of its file(s) were indexed with{" "}
            {models}. Search compares queries with vectors from a different model, so these files can be ranked
            poorly or missed. Index them again to fix it.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogAction
            onClick={() => {
              acknowledgeDrift(partition);
              setClosedFor(partition);
            }}
          >
            I understand
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
