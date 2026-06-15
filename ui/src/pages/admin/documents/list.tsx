import { useState, useRef, useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Check, ChevronsUpDown, Plus, Eye, Trash2, Upload } from "lucide-react";
import { toast } from "sonner";

import { PageHeader } from "@/components/shared/page-header";
import { DataTable } from "@/components/shared/data-table";
import { StatusBadge } from "@/components/shared/status-badge";
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
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { formatDate, formatBytes } from "@/lib/utils";
import {
  listDocuments,
  deleteDocument,
  deletePartitionDocuments,
} from "@/lib/api/documents";
import type { DocumentResponse } from "@/lib/api/documents";
import { indexDocument, indexBatchChunked } from "@/lib/api/indexing";
import type { BatchChunkProgress } from "@/lib/api/indexing";
import {
  listPartitions,
  listCurrentUserPartitions,
} from "@/lib/api/partitions";
import type { UserPartitionDetailResponse } from "@/lib/api/partitions";
import { useAuth } from "@/lib/auth";

const STATUS_OPTIONS = ["All", "QUEUED", "PROCESSING", "INDEXED", "FAILED"] as const;

export default function DocumentListPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { isAdmin } = useAuth();

  const [partitionFilter, setPartitionFilter] = useState<string>("all");
  const [statusFilter, setStatusFilter] = useState<string>("All");

  // Upload dialog state
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadPartition, setUploadPartition] = useState("default");
  const uploadFileRef = useRef<HTMLInputElement>(null);

  // Batch upload dialog state
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchFiles, setBatchFiles] = useState<File[]>([]);
  const [batchPartition, setBatchPartition] = useState("default");
  const batchFileRef = useRef<HTMLInputElement>(null);
  const [batchProgress, setBatchProgress] = useState<BatchChunkProgress | null>(null);

  // Admin: fetch all partitions
  const adminPartitionsQuery = useQuery({
    queryKey: ["partitions"],
    queryFn: listPartitions,
    enabled: isAdmin,
  });

  // User: fetch only assigned partitions (with role info)
  const userPartitionsQuery = useQuery({
    queryKey: ["user-partitions"],
    queryFn: listCurrentUserPartitions,
    enabled: !isAdmin,
  });

  // Derive available partitions and role info
  const partitions = isAdmin
    ? (adminPartitionsQuery.data?.partitions ?? [])
    : (userPartitionsQuery.data?.partitions ?? []);

  // For regular users, resolve role for the currently selected partition
  const selectedPartitionRole = !isAdmin
    ? ((userPartitionsQuery.data?.partitions as UserPartitionDetailResponse[] | undefined)?.find(
        (p) => (p.internal_name || p.name) === partitionFilter
      )?.role ?? null)
    : null;

  const hasAnyOwnerPartition = !isAdmin && (
    (userPartitionsQuery.data?.partitions as UserPartitionDetailResponse[] | undefined)?.some(
      (p) => p.role.toUpperCase() === "OWNER"
    ) ?? false
  );
  const canUpload = isAdmin || hasAnyOwnerPartition;
  const canDelete = isAdmin || selectedPartitionRole?.toUpperCase() === "OWNER";

  // Keep "all" as default for both admin and regular users

  // columns defined inside component to access isAdmin and canDelete
  const columns: ColumnDef<DocumentResponse, unknown>[] = [
    {
      accessorKey: "filename",
      header: "Filename",
      cell: ({ row }) => (
        <Link
          to={`/documents/${row.original.id}`}
          className="text-primary hover:underline font-medium"
        >
          {row.original.filename}
        </Link>
      ),
    },
    {
      accessorKey: "partition",
      header: "Partition",
    },
    {
      accessorKey: "status",
      header: "Status",
      cell: ({ row }) => <StatusBadge status={row.original.status} />,
    },
    {
      accessorKey: "chunk_count",
      header: "Chunks",
    },
    {
      accessorKey: "file_size_bytes",
      header: "Size",
      cell: ({ row }) => formatBytes(row.original.file_size_bytes),
    },
    {
      accessorKey: "created_at",
      header: "Created",
      cell: ({ row }) => formatDate(row.original.created_at),
    },
    ...(canDelete
      ? [
          {
            id: "actions",
            header: "Actions",
            cell: function ActionsCell({ row }: { row: any }) {
              const queryClient = useQueryClient();
              const deleteMutation = useMutation({
                mutationFn: () => deleteDocument(row.original.id),
                onSuccess: () => {
                  toast.success(`Deleted "${row.original.filename}"`);
                  queryClient.invalidateQueries({ queryKey: ["documents"] });
                },
                onError: (err: Error) => {
                  toast.error(`Failed to delete: ${err.message}`);
                },
              });

              return (
                <div className="flex items-center gap-1">
                  <Button variant="ghost" size="icon-xs" asChild>
                    <Link to={`/documents/${row.original.id}`}>
                      <Eye className="h-3.5 w-3.5" />
                    </Link>
                  </Button>
                  <ConfirmDialog
                    title="Delete Document"
                    description={`Are you sure you want to delete "${row.original.filename}"? This action cannot be undone.`}
                    onConfirm={() => deleteMutation.mutate()}
                  >
                    <Button variant="ghost" size="icon-xs">
                      <Trash2 className="h-3.5 w-3.5 text-destructive" />
                    </Button>
                  </ConfirmDialog>
                </div>
              );
            },
          } as ColumnDef<DocumentResponse, unknown>,
        ]
      : [
          {
            id: "actions",
            header: "Actions",
            cell: function ActionsCell({ row }: { row: any }) {
              return (
                <div className="flex items-center gap-1">
                  <Button variant="ghost" size="icon-xs" asChild>
                    <Link to={`/documents/${row.original.id}`}>
                      <Eye className="h-3.5 w-3.5" />
                    </Link>
                  </Button>
                </div>
              );
            },
          } as ColumnDef<DocumentResponse, unknown>,
        ]),
  ];

  const documentsQuery = useQuery({
    queryKey: [
      "documents",
      partitionFilter,
      statusFilter,
    ],
    queryFn: () =>
      listDocuments({
        partition: partitionFilter === "all" ? undefined : partitionFilter,
        status: statusFilter === "All" ? undefined : statusFilter,
        limit: 200,
      }),
  });

  const uploadMutation = useMutation({
    mutationFn: () => {
      if (!uploadFile) throw new Error("No file selected");
      return indexDocument(uploadFile, uploadPartition);
    },
    onSuccess: (data: any) => {
      toast.success(
        data.chunk_count
          ? `Document indexed: ${data.chunk_count} chunks in partition "${data.partition}"`
          : `Indexation job started (${data.job_id?.slice(0, 8) ?? ""}). Track progress in Jobs tab.`,
      );
      setUploadOpen(false);
      resetUploadForm();
      queryClient.invalidateQueries({ queryKey: ["documents"] });
    },
    onError: (err: Error) => {
      toast.error(`Upload failed: ${err.message}`);
    },
  });

  const batchMutation = useMutation({
    mutationFn: () => {
      if (batchFiles.length === 0) throw new Error("No files selected");
      setBatchProgress(null);
      return indexBatchChunked(batchFiles, batchPartition, (progress) => {
        setBatchProgress(progress);
      });
    },
    onSuccess: (data) => {
      toast.success(
        `Batch upload complete: ${data.totalDocuments} documents across ${data.jobIds.length} job(s)`,
      );
      setBatchOpen(false);
      setBatchProgress(null);
      resetBatchForm();
      // Navigate to jobs list (not single job) since there may be multiple jobs
      navigate("/jobs");
    },
    onError: (err: Error) => {
      toast.error(`Batch upload failed: ${err.message}`);
      setBatchProgress(null);
    },
  });

  function resetUploadForm() {
    setUploadFile(null);
    const ownerPartition = !isAdmin
      ? (userPartitionsQuery.data?.partitions as UserPartitionDetailResponse[] | undefined)?.find(
          (p) => p.role.toUpperCase() === "OWNER"
        )
      : null;
    setUploadPartition(ownerPartition ? (ownerPartition.internal_name || ownerPartition.name) : "default");
    if (uploadFileRef.current) uploadFileRef.current.value = "";
  }

  function resetBatchForm() {
    setBatchFiles([]);
    const ownerPartition = !isAdmin
      ? (userPartitionsQuery.data?.partitions as UserPartitionDetailResponse[] | undefined)?.find(
          (p) => p.role.toUpperCase() === "OWNER"
        )
      : null;
    setBatchPartition(ownerPartition ? (ownerPartition.internal_name || ownerPartition.name) : "default");
    if (batchFileRef.current) batchFileRef.current.value = "";
  }

  const deleteAllMutation = useMutation({
    mutationFn: () => deletePartitionDocuments(partitionFilter),
    onSuccess: (data) => {
      toast.success(
        `Deleted ${data.documents_deleted} document(s) from "${data.partition}"`,
      );
      queryClient.invalidateQueries({ queryKey: ["documents"] });
    },
    onError: (err: Error) => {
      toast.error(`Failed to delete documents: ${err.message}`);
    },
  });

  const documents = documentsQuery.data?.documents ?? [];

  return (
    <div>
      <PageHeader
        title="Documents"
        description="Manage indexed documents across partitions"
        actions={
          canUpload ? (
            <div className="flex gap-2">
              <Dialog open={batchOpen} onOpenChange={setBatchOpen}>
                <DialogTrigger asChild>
                  <Button variant="outline">
                    <Upload className="h-4 w-4" />
                    Batch Upload
                  </Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>Batch Upload Documents</DialogTitle>
                    <DialogDescription>
                      Upload multiple files at once. A background job will be
                      created to process them.
                    </DialogDescription>
                  </DialogHeader>
                  <div className="space-y-4">
                    <div className="space-y-2">
                      <Label htmlFor="batch-files">Files</Label>
                      <Input
                        id="batch-files"
                        type="file"
                        multiple
                        ref={batchFileRef}
                        onChange={(e) => {
                          const files = e.target.files;
                          if (files) setBatchFiles(Array.from(files));
                        }}
                      />
                      {batchFiles.length > 0 && (
                        <p className="text-sm text-muted-foreground">
                          {batchFiles.length} file(s) selected
                          {batchFiles.length > 20 && ` (will be split into ${Math.ceil(batchFiles.length / 20)} batches of 20)`}
                        </p>
                      )}
                      {batchProgress && (
                        <div className="space-y-1">
                          <p className="text-sm font-medium">
                            Uploading batch {batchProgress.batchIndex + 1} of {batchProgress.totalBatches}...
                          </p>
                          <p className="text-xs text-muted-foreground">
                            Jobs created: {batchProgress.jobIds.join(", ").slice(0, 80)}{batchProgress.jobIds.join(", ").length > 80 ? "..." : ""}
                          </p>
                        </div>
                      )}
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="batch-partition">Partition</Label>
                      {isAdmin ? (
                        <PartitionPicker
                          partitions={adminPartitionsQuery.data?.partitions ?? []}
                          value={batchPartition}
                          onSelect={setBatchPartition}
                        />
                      ) : (
                        <Select value={batchPartition} onValueChange={setBatchPartition}>
                          <SelectTrigger>
                            <SelectValue placeholder="Select partition" />
                          </SelectTrigger>
                          <SelectContent>
                            {((userPartitionsQuery.data?.partitions as UserPartitionDetailResponse[] | undefined) ?? [])
                              .filter((p) => p.role.toUpperCase() === "OWNER")
                              .map((p) => (
                                <SelectItem key={p.internal_name || p.name} value={p.internal_name || p.name}>
                                  {p.name}
                                </SelectItem>
                              ))}
                          </SelectContent>
                        </Select>
                      )}
                    </div>
                  </div>
                  <DialogFooter>
                    <Button
                      variant="outline"
                      onClick={() => {
                        setBatchOpen(false);
                        setBatchProgress(null);
                        resetBatchForm();
                      }}
                    >
                      Cancel
                    </Button>
                    <Button
                      onClick={() => batchMutation.mutate()}
                      disabled={batchFiles.length === 0 || batchMutation.isPending}
                    >
                      {batchMutation.isPending
                        ? (batchProgress
                          ? `Uploading batch ${batchProgress.batchIndex + 1}/${batchProgress.totalBatches}...`
                          : "Preparing...")
                        : batchFiles.length > 20
                          ? `Start ${Math.ceil(batchFiles.length / 20)} Batch Jobs`
                          : "Start Batch Job"}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>

              <Dialog open={uploadOpen} onOpenChange={setUploadOpen}>
                <DialogTrigger asChild>
                  <Button>
                    <Plus className="h-4 w-4" />
                    Upload Document
                  </Button>
                </DialogTrigger>
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>Upload Document</DialogTitle>
                    <DialogDescription>
                      Upload a single document for immediate indexing.
                    </DialogDescription>
                  </DialogHeader>
                  <div className="space-y-4">
                    <div className="space-y-2">
                      <Label htmlFor="upload-file">File</Label>
                      <Input
                        id="upload-file"
                        type="file"
                        ref={uploadFileRef}
                        onChange={(e) => {
                          const file = e.target.files?.[0] ?? null;
                          setUploadFile(file);
                        }}
                      />
                    </div>
                    <div className="space-y-2">
                      <Label htmlFor="upload-partition">Partition</Label>
                      {isAdmin ? (
                        <PartitionPicker
                          partitions={adminPartitionsQuery.data?.partitions ?? []}
                          value={uploadPartition}
                          onSelect={setUploadPartition}
                        />
                      ) : (
                        <Select value={uploadPartition} onValueChange={setUploadPartition}>
                          <SelectTrigger>
                            <SelectValue placeholder="Select partition" />
                          </SelectTrigger>
                          <SelectContent>
                            {((userPartitionsQuery.data?.partitions as UserPartitionDetailResponse[] | undefined) ?? [])
                              .filter((p) => p.role.toUpperCase() === "OWNER")
                              .map((p) => (
                                <SelectItem key={p.internal_name || p.name} value={p.internal_name || p.name}>
                                  {p.name}
                                </SelectItem>
                              ))}
                          </SelectContent>
                        </Select>
                      )}
                    </div>
                  </div>
                  <DialogFooter>
                    <Button
                      variant="outline"
                      onClick={() => {
                        setUploadOpen(false);
                        resetUploadForm();
                      }}
                    >
                      Cancel
                    </Button>
                    <Button
                      onClick={() => uploadMutation.mutate()}
                      disabled={!uploadFile || uploadMutation.isPending}
                    >
                      {uploadMutation.isPending ? "Uploading..." : "Upload"}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            </div>
          ) : null
        }
      />

      <div className="flex items-center gap-4 mb-4">
        <div className="flex items-center gap-2">
          <Label className="text-sm font-medium">Partition</Label>
          <Select value={partitionFilter} onValueChange={setPartitionFilter}>
            <SelectTrigger className="w-[180px]">
              <SelectValue placeholder="All partitions" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All partitions</SelectItem>
              {partitions.map((p) => {
                const val = (!isAdmin && (p as UserPartitionDetailResponse).internal_name) || p.name;
                return (
                  <SelectItem key={val} value={val}>
                    {p.name}
                  </SelectItem>
                );
              })}
            </SelectContent>
          </Select>
        </div>
        <div className="flex items-center gap-2">
          <Label className="text-sm font-medium">Status</Label>
          <Select value={statusFilter} onValueChange={setStatusFilter}>
            <SelectTrigger className="w-[160px]">
              <SelectValue placeholder="All statuses" />
            </SelectTrigger>
            <SelectContent>
              {STATUS_OPTIONS.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        {isAdmin && partitionFilter !== "all" && (
          <ConfirmDialog
            title="Delete All Documents"
            description={`This will permanently delete ALL documents and their chunks in partition "${partitionFilter}". This action cannot be undone.`}
            onConfirm={() => deleteAllMutation.mutate()}
          >
            <Button
              variant="destructive"
              size="sm"
              disabled={deleteAllMutation.isPending}
            >
              <Trash2 className="h-4 w-4" />
              {deleteAllMutation.isPending ? "Deleting..." : "Delete All"}
            </Button>
          </ConfirmDialog>
        )}
        {documentsQuery.data && (
          <p className="text-sm text-muted-foreground ml-auto">
            {documentsQuery.data.total} document(s)
          </p>
        )}
      </div>

      {documentsQuery.isLoading ? (
        <div className="flex items-center justify-center py-12 text-muted-foreground">
          Loading documents...
        </div>
      ) : documentsQuery.isError ? (
        <div className="flex items-center justify-center py-12 text-destructive">
          Failed to load documents: {(documentsQuery.error as Error).message}
        </div>
      ) : (
        <DataTable columns={columns} data={documents} />
      )}
    </div>
  );
}

/** Searchable partition combobox for admin upload dialogs. */
function PartitionPicker({
  partitions,
  value,
  onSelect,
}: {
  partitions: { name: string }[];
  value: string;
  onSelect: (name: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");

  const filtered = useMemo(() => {
    if (!search) return partitions;
    const q = search.toLowerCase();
    return partitions.filter((p) => p.name.toLowerCase().includes(q));
  }, [partitions, search]);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          role="combobox"
          aria-expanded={open}
          className="w-full justify-between font-normal"
        >
          {value || <span className="text-muted-foreground">Select partition...</span>}
          <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-[--radix-popover-trigger-width] p-0" align="start">
        <Command shouldFilter={false}>
          <CommandInput
            placeholder="Search partitions..."
            value={search}
            onValueChange={setSearch}
          />
          <CommandList>
            {filtered.length > 0 ? (
              <CommandGroup>
                {filtered.map((p) => (
                  <CommandItem
                    key={p.name}
                    value={p.name}
                    onSelect={() => {
                      onSelect(p.name);
                      setOpen(false);
                      setSearch("");
                    }}
                  >
                    <span className="truncate flex-1">{p.name}</span>
                    {p.name === value && (
                      <Check className="ml-auto h-4 w-4 text-green-500" />
                    )}
                  </CommandItem>
                ))}
              </CommandGroup>
            ) : (
              <CommandEmpty>No partitions found.</CommandEmpty>
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
