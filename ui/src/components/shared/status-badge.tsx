import { Badge } from "@/components/ui/badge";

const statusStyles: Record<string, string> = {
  QUEUED: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200",
  PROCESSING: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
  RUNNING: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
  INDEXING: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
  // OpenRag indexing-task states (TaskStateManager)
  SERIALIZING: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
  CHUNKING: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
  INSERTING: "bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200",
  CANCELLED: "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200",
  COMPLETED: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
  SUCCESS: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
  FAILED: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200",
  PENDING: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200",
  INDEXED: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
  PARTIAL: "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200",
};

export function StatusBadge({ status }: { status: string }) {
  const upper = status.toUpperCase();
  const className = statusStyles[upper] || "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-200";
  return (
    <Badge variant="outline" className={className}>
      {upper}
    </Badge>
  );
}
