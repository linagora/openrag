import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Plus, Eye, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { PageHeader } from "@/components/shared/page-header";
import { DataTable, SortableHeader } from "@/components/shared/data-table";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { formatDate } from "@/lib/utils";
import { listPartitionFiles, type PartitionFile } from "@/lib/api/documents";
import { uploadFile, deleteFile, newFileId } from "@/lib/api/indexing";
import { listPartitions } from "@/lib/api/partitions";
import { usePermissions } from "@/lib/permissions";

const fileHref = (partition: string, fileId: string) =>
  `/documents/${encodeURIComponent(partition)}/${encodeURIComponent(fileId)}`;
const fileLabel = (f: PartitionFile) => (f.filename as string) || f.file_id;

export default function DocumentListPage() {
  const queryClient = useQueryClient();
  const { canWrite } = usePermissions();

  // OpenRag has no flat/cross-partition file list — files live inside a
  // partition, so the view is partition-scoped (pick one, see its files).
  const [partition, setPartition] = useState("");
  const [uploadOpen, setUploadOpen] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const partitionsQuery = useQuery({ queryKey: ["partitions"], queryFn: listPartitions });
  const partitions = partitionsQuery.data?.partitions ?? [];
  const selected = partition || partitions[0]?.partition || "";

  const role = partitions.find((p) => p.partition === selected)?.role;
  const writable = canWrite(role);

  const filesQuery = useQuery({
    queryKey: ["partition-files", selected],
    queryFn: () => listPartitionFiles(selected),
    enabled: !!selected,
  });
  const fileRows = filesQuery.data?.files ?? [];

  const deleteMutation = useMutation({
    mutationFn: (fileId: string) => deleteFile(selected, fileId),
    onSuccess: () => {
      toast.success("File deleted");
      queryClient.invalidateQueries({ queryKey: ["partition-files", selected] });
    },
    onError: (err: Error) => toast.error(`Failed to delete: ${err.message}`),
  });

  // Bulk delete: OpenRag deletes one file per request, so fan the selection out
  // concurrently and report how many succeeded/failed.
  const [bulkDeleting, setBulkDeleting] = useState(false);
  const bulkDeleteMutation = useMutation({
    mutationFn: async (fileIds: string[]) => {
      setBulkDeleting(true);
      const results = await Promise.allSettled(fileIds.map((id) => deleteFile(selected, id)));
      const ok = results.filter((r) => r.status === "fulfilled").length;
      return { ok, failed: results.length - ok };
    },
    onSuccess: ({ ok, failed }) => {
      if (ok) toast.success(`${ok} file(s) deleted`);
      if (failed) toast.error(`${failed} file(s) failed to delete`);
      queryClient.invalidateQueries({ queryKey: ["partition-files", selected] });
    },
    onError: (err: Error) => toast.error(`Bulk delete failed: ${err.message}`),
    onSettled: () => setBulkDeleting(false),
  });

  // OpenRag indexes one file per request; multi-file upload is a client-side
  // loop, each file becoming its own indexing task (track in Jobs).
  const uploadMutation = useMutation({
    mutationFn: async () => {
      if (!files.length) throw new Error("No file selected");
      setUploading(true);
      let ok = 0;
      const errors: string[] = [];
      for (const f of files) {
        try {
          await uploadFile(selected, newFileId(), f, {
            filename: f.name,
            ...(f.type ? { mimetype: f.type } : {}),
          });
          ok += 1;
        } catch (e) {
          errors.push(`${f.name}: ${(e as Error).message}`);
        }
      }
      return { ok, errors };
    },
    onSuccess: ({ ok, errors }) => {
      if (ok) toast.success(`${ok} file(s) queued for indexing — track progress in Jobs.`);
      if (errors.length) toast.error(`${errors.length} upload(s) failed: ${errors[0]}`);
      setUploadOpen(false);
      setFiles([]);
      if (fileRef.current) fileRef.current.value = "";
      queryClient.invalidateQueries({ queryKey: ["partition-files", selected] });
    },
    onError: (err: Error) => toast.error(`Upload failed: ${err.message}`),
    onSettled: () => setUploading(false),
  });

  const columns: ColumnDef<PartitionFile, unknown>[] = [
    {
      id: "filename",
      accessorFn: (f) => fileLabel(f).toLowerCase(),
      header: ({ column }) => <SortableHeader column={column} title="Filename" />,
      cell: ({ row }) => (
        <Link to={fileHref(selected, row.original.file_id)} className="text-primary hover:underline font-medium">
          {fileLabel(row.original)}
        </Link>
      ),
    },
    {
      accessorKey: "mimetype",
      header: "Type",
      cell: ({ row }) => (row.original.mimetype as string) || "—",
    },
    {
      id: "indexed_at",
      // ISO timestamps sort lexically = chronologically.
      accessorFn: (f) => (f.indexed_at as string) ?? (f.created_at as string) ?? "",
      header: ({ column }) => <SortableHeader column={column} title="Indexed" />,
      cell: ({ row }) =>
        formatDate((row.original.indexed_at as string) ?? (row.original.created_at as string) ?? null),
    },
    {
      id: "actions",
      header: "Actions",
      cell: ({ row }) => (
        <div className="flex items-center gap-1">
          <Button variant="ghost" size="icon-xs" asChild>
            <Link to={fileHref(selected, row.original.file_id)}>
              <Eye className="h-3.5 w-3.5" />
            </Link>
          </Button>
          {writable && (
            <ConfirmDialog
              title="Delete File"
              description={`Delete "${fileLabel(row.original)}"? This cannot be undone.`}
              onConfirm={() => deleteMutation.mutate(row.original.file_id)}
            >
              <Button variant="ghost" size="icon-xs">
                <Trash2 className="h-3.5 w-3.5 text-destructive" />
              </Button>
            </ConfirmDialog>
          )}
        </div>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title="Documents"
        description="Files indexed in a partition"
        actions={
          writable && selected ? (
            <Dialog open={uploadOpen} onOpenChange={setUploadOpen}>
              <DialogTrigger asChild>
                <Button>
                  <Plus className="h-4 w-4" /> Upload
                </Button>
              </DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>Upload files</DialogTitle>
                  <DialogDescription>
                    Index one or more files into <span className="font-medium">{selected}</span>. Each file is
                    processed as its own job.
                  </DialogDescription>
                </DialogHeader>
                <div className="space-y-2">
                  <Label htmlFor="files">Files</Label>
                  <Input
                    id="files"
                    type="file"
                    multiple
                    ref={fileRef}
                    onChange={(e) => setFiles(e.target.files ? Array.from(e.target.files) : [])}
                  />
                  {files.length > 0 && (
                    <p className="text-sm text-muted-foreground">{files.length} file(s) selected</p>
                  )}
                </div>
                <DialogFooter>
                  <Button
                    variant="outline"
                    onClick={() => {
                      setUploadOpen(false);
                      setFiles([]);
                      if (fileRef.current) fileRef.current.value = "";
                    }}
                  >
                    Cancel
                  </Button>
                  <Button onClick={() => uploadMutation.mutate()} disabled={!files.length || uploading}>
                    {uploading ? "Uploading..." : "Upload"}
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          ) : null
        }
      />

      <div className="flex items-center gap-2 mb-4">
        <Label className="text-sm font-medium">Partition</Label>
        <Select value={selected} onValueChange={setPartition}>
          <SelectTrigger className="w-[220px]">
            <SelectValue placeholder="Select partition" />
          </SelectTrigger>
          <SelectContent>
            {partitions.map((p) => (
              <SelectItem key={p.partition} value={p.partition}>
                {p.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {filesQuery.data && (
          <p className="text-sm text-muted-foreground ml-auto">{fileRows.length} file(s)</p>
        )}
      </div>

      {!selected ? (
        <div className="flex items-center justify-center py-12 text-muted-foreground">
          {partitionsQuery.isLoading ? "Loading…" : "No partitions available."}
        </div>
      ) : filesQuery.isLoading ? (
        <div className="flex items-center justify-center py-12 text-muted-foreground">Loading files…</div>
      ) : filesQuery.isError ? (
        <div className="flex items-center justify-center py-12 text-destructive">
          Failed to load files: {(filesQuery.error as Error).message}
        </div>
      ) : (
        <DataTable
          columns={columns}
          data={fileRows}
          initialSorting={[{ id: "indexed_at", desc: true }]}
          enableSelection={writable}
          getRowId={(f) => f.file_id}
          renderBulkActions={({ selected: rows, total, allSelected, selectAll, clear }) => {
            const names = rows.map(fileLabel);
            const description =
              names.length <= 5
                ? `Delete ${names.length} file(s)? This cannot be undone: ${names.join(", ")}`
                : `Delete ${names.length} files? This cannot be undone.`;
            return (
              <>
                <span className="text-sm font-medium text-primary">{rows.length} selected</span>
                <ConfirmDialog
                  title="Delete files"
                  description={description}
                  onConfirm={() => {
                    bulkDeleteMutation.mutate(rows.map((r) => r.file_id));
                    clear();
                  }}
                >
                  <Button variant="destructive" size="sm" disabled={bulkDeleting}>
                    <Trash2 className="h-3.5 w-3.5" /> Delete selected
                  </Button>
                </ConfirmDialog>
                <Button variant="ghost" size="sm" onClick={clear}>
                  Clear
                </Button>
                {!allSelected && rows.length < total && (
                  <Button variant="link" size="sm" className="ml-auto" onClick={selectAll}>
                    Select all {total}
                  </Button>
                )}
              </>
            );
          }}
        />
      )}
    </div>
  );
}
