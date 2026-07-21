import { useMemo, useState, useCallback, useEffect } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Pencil, Trash2, Search, ArrowUpDown, ArrowUp, ArrowDown, CheckCircle, XCircle, Loader2, RefreshCw, ChevronLeft, ChevronRight, FilePlus, Info } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { PageHeader } from "@/components/shared/page-header";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import {
  listPartitions,
  createPartition,
  deletePartition,
} from "@/lib/api/partitions";
import type { PartitionResponse } from "@/lib/api/partitions";
import { listPresets } from "@/lib/api/presets";
import {
  listModelEndpoints,
  validateStoredModelEndpoint,
  pickDefaultEndpoint,
  resolveEmbedderName,
} from "@/lib/api/models";
import { usePermissions } from "@/lib/permissions";
import { intOr } from "@/lib/utils";
import { partitionDetailPath } from "@/lib/routes";

type SortDir = "asc" | "desc" | null;
const PARTITIONS_PAGE_SIZE = 10;
const PARTITIONS_REFETCH_INTERVAL_MS = 5000;

function RowActions({
  partition,
  showUpload,
  showEdit,
  showDelete,
}: {
  partition: PartitionResponse;
  showUpload: boolean;
  showEdit: boolean;
  showDelete: boolean;
}) {
  const queryClient = useQueryClient();

  const deleteMutation = useMutation({
    mutationFn: () => deletePartition(partition.name),
    onSuccess: () => {
      toast.success(`Partition "${partition.name}" deleted`);
      queryClient.invalidateQueries({ queryKey: ["partitions"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to delete: ${error.message}`);
    },
  });

  return (
    <div className="flex items-center gap-1">
      {showUpload && (
        <Button variant="ghost" size="icon-xs" asChild>
          <Link
            to={`/documents?partition=${encodeURIComponent(partition.name)}&upload=1`}
            aria-label={`Upload documents to ${partition.name}`}
            title={`Upload documents to ${partition.name}`}
          >
            <FilePlus className="h-3.5 w-3.5" />
          </Link>
        </Button>
      )}
      {showEdit && (
        <Button variant="ghost" size="icon-xs" asChild>
          <Link
            to={partitionDetailPath(partition.name)}
            aria-label={`Edit ${partition.name}`}
            title={`Edit ${partition.name}`}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Link>
        </Button>
      )}
      {showDelete && (
        <ConfirmDialog
          title="Delete Partition"
          description={`Delete "${partition.name}"? This cannot be undone.`}
          onConfirm={() => deleteMutation.mutate()}
        >
          <Button
            variant="ghost"
            size="icon-xs"
            disabled={deleteMutation.isPending}
            aria-label={`Delete ${partition.name}`}
            title={`Delete ${partition.name}`}
          >
            <Trash2 className="h-3.5 w-3.5 text-destructive" />
          </Button>
        </ConfirmDialog>
      )}
    </div>
  );
}

function SortButton({ label, active, direction, onClick }: { label: string; active: boolean; direction: SortDir; onClick: () => void }) {
  return (
    <Button variant="ghost" size="sm" className="-ml-3 h-8" onClick={onClick}>
      {label}
      {active && direction === "asc" ? (
        <ArrowUp className="ml-2 h-3 w-3" />
      ) : active && direction === "desc" ? (
        <ArrowDown className="ml-2 h-3 w-3" />
      ) : (
        <ArrowUpDown className="ml-2 h-3 w-3" />
      )}
    </Button>
  );
}

export default function PartitionListPage() {
  const queryClient = useQueryClient();
  const { canManagePartitions, canConfigurePartition, canWrite } = usePermissions();
  const [dialogOpen, setDialogOpen] = useState(false);

  // Open the create dialog directly when arriving from the Overview quick action
  // (/partitions?create=1), then strip the param so refresh/back doesn't reopen it.
  const [searchParams, setSearchParams] = useSearchParams();
  useEffect(() => {
    if (searchParams.has("create")) {
      setDialogOpen(true);
      setSearchParams(
        (prev) => {
          prev.delete("create");
          return prev;
        },
        { replace: true },
      );
    }
  }, [searchParams, setSearchParams]);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [embedder, setEmbedder] = useState("");
  const [indexationPreset, setIndexationPreset] = useState("default");
  const [retrievalPreset, setRetrievalPreset] = useState("default");
  const [chatHistoryDepth, setChatHistoryDepth] = useState("4");
  const [chatLlm, setChatLlm] = useState("__default__");
  const [llmValidated, setLlmValidated] = useState<boolean | null>(true);
  const [llmValidating, setLlmValidating] = useState(false);

  const [search, setSearch] = useState("");
  const [sortDir, setSortDir] = useState<SortDir>(null);
  const [sortColumn, setSortColumn] = useState<"name" | "created_at">("created_at");
  const [selectedPartitionNames, setSelectedPartitionNames] = useState<string[]>([]);
  const [pageIndex, setPageIndex] = useState(0);
  const [manualRefreshing, setManualRefreshing] = useState(false);

  // `GET /partition/` is already membership-scoped server-side (admins with
  // SUPER_ADMIN_MODE see all; regular users see their memberships), so a single
  // query serves both — canManagePartitions only gates the admin-only columns/fields below.
  const partitionsQuery = useQuery({
    queryKey: ["partitions"],
    queryFn: listPartitions,
    refetchInterval: PARTITIONS_REFETCH_INTERVAL_MS,
  });

  const { data: presetsData } = useQuery({
    queryKey: ["presets"],
    queryFn: () => listPresets(),
    enabled: canManagePartitions,
  });

  const { data: llmEndpoints } = useQuery({
    queryKey: ["model-endpoints", "llm"],
    queryFn: () => listModelEndpoints("llm"),
    enabled: canManagePartitions,
  });

  const { data: embedderEndpoints } = useQuery({
    queryKey: ["model-endpoints", "embedder"],
    queryFn: () => listModelEndpoints("embedder"),
    enabled: canManagePartitions,
  });

  // Pre-select the default (or only) embedder so the Create button is active
  // immediately when the choice is unambiguous.
  useEffect(() => {
    if (embedder) return;
    const def = pickDefaultEndpoint(embedderEndpoints);
    if (def) setEmbedder(def.name);
  }, [embedderEndpoints, embedder]);

  const indexationPresets = presetsData?.filter((p) => p.preset_type === "indexation") ?? [];
  const retrievalPresets = presetsData?.filter((p) => p.preset_type === "retrieval") ?? [];

  const validateLlm = useCallback(
    async (name: string) => {
      const ep = llmEndpoints?.find((e) => e.name === name);
      if (!ep) return;
      setLlmValidating(true);
      try {
        const res = await validateStoredModelEndpoint("llm", name);
        setLlmValidated(res.reachable);
        if (!res.reachable) {
          toast.error(res.detail || "LLM endpoint is unreachable");
        }
      } catch {
        setLlmValidated(false);
        toast.error("Failed to validate LLM endpoint");
      } finally {
        setLlmValidating(false);
      }
    },
    [llmEndpoints],
  );

  const handleChatLlmChange = (value: string) => {
    setChatLlm(value);
    if (value === "__default__") {
      setLlmValidated(true);
    } else {
      setLlmValidated(null);
      validateLlm(value);
    }
  };

  const filteredAndSorted = useMemo(() => {
    let items: PartitionResponse[] = partitionsQuery.data?.partitions ?? [];

    // Filter by search
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      items = items.filter(
        (p) =>
          p.name.toLowerCase().includes(q) ||
          p.description.toLowerCase().includes(q),
      );
    }

    // Sort
    if (sortDir) {
      const dir = sortDir === "asc" ? 1 : -1;
      items = [...items].sort((a, b) => {
        if (sortColumn === "name") {
          return a.name.localeCompare(b.name) * dir;
        }
        // created_at
        const aDate = a.created_at ?? "";
        const bDate = b.created_at ?? "";
        return aDate.localeCompare(bDate) * dir;
      });
    }

    return items;
  }, [partitionsQuery.data, search, sortDir, sortColumn]);

  // Show the actions column whenever the row has at least one available row
  // action: upload for writable partitions, or edit/delete for admins/owners.
  const showActions =
    canManagePartitions ||
    filteredAndSorted.some((p) => canWrite(p.role) || canConfigurePartition(p.role));

  const pageCount = Math.ceil(filteredAndSorted.length / PARTITIONS_PAGE_SIZE);
  useEffect(() => {
    if (pageIndex > 0 && pageIndex > pageCount - 1) {
      setPageIndex(Math.max(0, pageCount - 1));
    }
  }, [pageCount, pageIndex]);

  const visiblePartitions = useMemo(() => {
    const start = pageIndex * PARTITIONS_PAGE_SIZE;
    return filteredAndSorted.slice(start, start + PARTITIONS_PAGE_SIZE);
  }, [filteredAndSorted, pageIndex]);
  const visibleDeletablePartitionNames = useMemo(
    () => visiblePartitions.filter((p) => canConfigurePartition(p.role)).map((p) => p.name),
    [visiblePartitions, canConfigurePartition],
  );
  const hasDeletablePartitions = useMemo(
    () => filteredAndSorted.some((p) => canConfigurePartition(p.role)),
    [filteredAndSorted, canConfigurePartition],
  );
  const visibleDeletablePartitionNameSet = useMemo(
    () => new Set(visibleDeletablePartitionNames),
    [visibleDeletablePartitionNames],
  );
  const selectedPartitionNameSet = useMemo(() => new Set(selectedPartitionNames), [selectedPartitionNames]);
  const selectedPartitions = useMemo(
    () =>
      filteredAndSorted.filter(
        (p) => selectedPartitionNameSet.has(p.name) && canConfigurePartition(p.role),
      ),
    [filteredAndSorted, selectedPartitionNameSet, canConfigurePartition],
  );
  const allDeletableSelected =
    visibleDeletablePartitionNames.length > 0 &&
    visibleDeletablePartitionNames.every((name) => selectedPartitionNameSet.has(name));
  const someDeletableSelected =
    visibleDeletablePartitionNames.some((name) => selectedPartitionNameSet.has(name)) &&
    !allDeletableSelected;

  useEffect(() => {
    const allowed = new Set(
      filteredAndSorted.filter((p) => canConfigurePartition(p.role)).map((p) => p.name),
    );
    setSelectedPartitionNames((prev) => {
      const next = prev.filter((name) => allowed.has(name));
      return next.length === prev.length ? prev : next;
    });
  }, [filteredAndSorted, canConfigurePartition]);

  const handleSort = (column: "name" | "created_at") => {
    if (sortColumn !== column) {
      setSortColumn(column);
      setSortDir("asc");
    } else if (sortDir === null) {
      setSortDir("asc");
    } else if (sortDir === "asc") {
      setSortDir("desc");
    } else {
      setSortDir(null);
    }
  };

  const createMutation = useMutation({
    mutationFn: () =>
      createPartition({
        name,
        description: description || undefined,
        embedder: embedder || undefined,
        indexation_preset: indexationPreset || undefined,
        retrieval_preset: retrievalPreset || undefined,
        chat_history_depth: intOr(chatHistoryDepth, 0),
        chat_llm: chatLlm === "__default__" ? null : chatLlm,
      }),
    onSuccess: (data) => {
      toast.success(`Partition "${data.name}" created`);
      queryClient.invalidateQueries({ queryKey: ["partitions"] });
      setDialogOpen(false);
      setName("");
      setDescription("");
      setEmbedder("");
      setIndexationPreset("default");
      setRetrievalPreset("default");
      setChatHistoryDepth("4");
      setChatLlm("__default__");
      setLlmValidated(true);
    },
    onError: (error: Error) => {
      toast.error(`Failed to create: ${error.message}`);
    },
  });

  const bulkDeleteMutation = useMutation({
    mutationFn: async (names: string[]) => {
      const results = await Promise.allSettled(names.map((partitionName) => deletePartition(partitionName)));
      const ok = results.filter((r) => r.status === "fulfilled").length;
      return { ok, failed: results.length - ok };
    },
    onSuccess: ({ ok, failed }) => {
      if (ok) toast.success(`${ok} partition(s) deleted`);
      if (failed) toast.error(`${failed} partition(s) failed to delete`);
      queryClient.invalidateQueries({ queryKey: ["partitions"] });
    },
    onError: (error: Error) => {
      toast.error(`Bulk delete failed: ${error.message}`);
    },
    onSettled: () => setSelectedPartitionNames([]),
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      toast.error("Name is required");
      return;
    }
    if (canManagePartitions && intOr(chatHistoryDepth, 0) < 1) {
      toast.error("Chat history depth must be at least 1");
      return;
    }
    createMutation.mutate();
  };

  const createPartitionLabel = canManagePartitions ? "Create Partition" : "Create Personal Partition";
  const createPartitionDescription = canManagePartitions
    ? "Create a new document partition with its configuration."
    : "Create a personal document partition. You will be the owner and can upload documents to it.";

  return (
    <div>
      <PageHeader
        title="Partitions"
        description={canManagePartitions ? "Manage document partitions and their configurations" : "Your assigned document partitions"}
        actions={
          <Button onClick={() => setDialogOpen(true)}>
            <Plus className="mr-2 h-4 w-4" /> {createPartitionLabel}
          </Button>
        }
      />

      <div className="mb-4 flex items-center gap-2">
        <div className="relative max-w-sm">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search partitions..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>
        {selectedPartitions.length > 0 && (
          <>
            <ConfirmDialog
              title="Delete partitions"
              description={`Delete ${selectedPartitions.length} partition(s)? This cannot be undone.`}
              onConfirm={() => bulkDeleteMutation.mutate(selectedPartitions.map((p) => p.name))}
            >
              <Button
                variant="outline"
                size="icon-sm"
                disabled={bulkDeleteMutation.isPending}
                aria-label="Delete selected partitions"
                title="Delete selected partitions"
              >
                <Trash2 className="h-3.5 w-3.5 text-destructive" />
              </Button>
            </ConfirmDialog>
            <p className="text-sm text-muted-foreground">
              {selectedPartitions.length} selected
            </p>
          </>
        )}
        <div className="ml-auto flex items-center gap-2">
          <p className="text-sm text-muted-foreground">
            {filteredAndSorted.length} partition{filteredAndSorted.length === 1 ? "" : "s"}
          </p>
          <Button
            variant="outline"
            size="icon-sm"
            onClick={() => {
              setManualRefreshing(true);
              partitionsQuery.refetch().finally(() => setManualRefreshing(false));
            }}
            disabled={manualRefreshing}
            aria-label="Refresh partitions"
            title="Refresh partitions"
          >
            <RefreshCw className={manualRefreshing ? "animate-spin" : ""} />
          </Button>
        </div>
      </div>

      {partitionsQuery.isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-12 rounded-md bg-accent animate-pulse" />
          ))}
        </div>
      ) : (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                {hasDeletablePartitions && (
                  <TableHead>
                    <Checkbox
                      checked={allDeletableSelected || (someDeletableSelected && "indeterminate")}
                      disabled={visibleDeletablePartitionNames.length === 0}
                      onCheckedChange={(value) => {
                        setSelectedPartitionNames((prev) =>
                          value
                            ? Array.from(new Set([...prev, ...visibleDeletablePartitionNames]))
                            : prev.filter((name) => !visibleDeletablePartitionNameSet.has(name)),
                        );
                      }}
                      aria-label="Select visible deletable partitions"
                    />
                  </TableHead>
                )}
                <TableHead>
                  <SortButton
                    label="Name"
                    active={sortColumn === "name"}
                    direction={sortColumn === "name" ? sortDir : null}
                    onClick={() => handleSort("name")}
                  />
                </TableHead>
                <TableHead>Description</TableHead>
                <TableHead>Documents</TableHead>
                {canManagePartitions && <TableHead>Embedder</TableHead>}
                {canManagePartitions && <TableHead>Indexation Preset</TableHead>}
                {canManagePartitions && <TableHead>Retrieval Preset</TableHead>}
                <TableHead>
                  <SortButton
                    label="Created"
                    active={sortColumn === "created_at"}
                    direction={sortColumn === "created_at" ? sortDir : null}
                    onClick={() => handleSort("created_at")}
                  />
                </TableHead>
                {showActions && <TableHead>Actions</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {visiblePartitions.length ? (
                visiblePartitions.map((p) => (
                  <TableRow key={p.name}>
                    {hasDeletablePartitions && (
                      <TableCell>
                        <Checkbox
                          checked={selectedPartitionNameSet.has(p.name)}
                          disabled={!canConfigurePartition(p.role)}
                          onCheckedChange={(value) =>
                            setSelectedPartitionNames((prev) =>
                              value
                                ? Array.from(new Set([...prev, p.name]))
                                : prev.filter((name) => name !== p.name),
                            )
                          }
                          aria-label={`Select partition ${p.name}`}
                        />
                      </TableCell>
                    )}
                    <TableCell>
                      <div className="flex min-w-0 items-center gap-2">
                        <Link
                          to={partitionDetailPath(p.name)}
                          className="block max-w-[220px] truncate font-medium text-primary hover:underline"
                          title={p.name}
                        >
                          {p.name}
                        </Link>
                        {!canManagePartitions && (
                          <Badge variant="outline" className="text-xs capitalize">
                            {p.role?.toLowerCase()}
                          </Badge>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>
                      {p.description ? (
                        <span className="max-w-[200px] truncate block" title={p.description}>
                          {p.description.length > 50
                            ? `${p.description.slice(0, 50)}...`
                            : p.description}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">--</span>
                      )}
                    </TableCell>
                    <TableCell>{p.document_count}</TableCell>
                    {canManagePartitions && (
                      <TableCell className="text-sm">
                        <span className="block max-w-[180px] truncate" title={resolveEmbedderName(p.embedder, embedderEndpoints)}>
                          {resolveEmbedderName(p.embedder, embedderEndpoints)}
                        </span>
                      </TableCell>
                    )}
                    {canManagePartitions && (
                      <TableCell>
                        <span className="block max-w-[180px] truncate" title={p.indexation_preset}>
                          {p.indexation_preset}
                        </span>
                      </TableCell>
                    )}
                    {canManagePartitions && (
                      <TableCell>
                        <span className="block max-w-[180px] truncate" title={p.retrieval_preset}>
                          {p.retrieval_preset}
                        </span>
                      </TableCell>
                    )}
                    <TableCell className="text-sm text-muted-foreground">
                      {p.created_at
                        ? new Date(p.created_at).toLocaleDateString()
                        : "--"}
                    </TableCell>
                    {showActions && (
                      <TableCell>
                        <RowActions
                          partition={p}
                          showUpload={canWrite(p.role)}
                          showEdit={canManagePartitions}
                          showDelete={canConfigurePartition(p.role)}
                        />
                      </TableCell>
                    )}
                  </TableRow>
                ))
              ) : (
                <TableRow>
                  <TableCell
                    colSpan={
                      (canManagePartitions ? 7 : 4) +
                      (showActions ? 1 : 0) +
                      (hasDeletablePartitions ? 1 : 0)
                    }
                    className="h-24 text-center text-muted-foreground"
                  >
                    {search.trim() ? "No partitions match your search." : "No partitions."}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
      )}

      {pageCount > 1 && (
        <div className="flex items-center justify-between py-4">
          <p className="text-sm text-muted-foreground">
            Page {pageIndex + 1} of {pageCount}
          </p>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPageIndex((prev) => Math.max(0, prev - 1))}
              disabled={pageIndex === 0}
              aria-label="Previous page"
            >
              <ChevronLeft className="h-4 w-4" />
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPageIndex((prev) => Math.min(pageCount - 1, prev + 1))}
              disabled={pageIndex >= pageCount - 1}
              aria-label="Next page"
            >
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="flex max-h-[calc(100vh-2rem)] flex-col overflow-hidden sm:max-w-lg">
          <DialogHeader className="shrink-0">
            <DialogTitle>{createPartitionLabel}</DialogTitle>
            <DialogDescription>
              {createPartitionDescription}
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={handleSubmit} className="flex min-h-0 flex-1 flex-col">
            <div className="-mx-1 min-h-0 flex-1 space-y-4 overflow-y-auto px-1 pb-1">
              <div className="space-y-2">
                <Label>Name *</Label>
                <Input
                  placeholder="my-partition"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  required
                />
              </div>
              <div className="space-y-2">
                <Label>Description</Label>
                <Textarea
                  placeholder="Optional description..."
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                />
              </div>
              {canManagePartitions && (
                <>
                  <div className="space-y-2">
                    <Label>Embedder *</Label>
                    <Select value={embedder} onValueChange={setEmbedder}>
                      <SelectTrigger>
                        <SelectValue placeholder="Select embedder..." />
                      </SelectTrigger>
                      <SelectContent>
                        {(embedderEndpoints ?? []).map((ep) => (
                          <SelectItem key={ep.name} value={ep.name}>
                            {ep.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="grid grid-cols-2 gap-4">
                    <div className="space-y-2">
                      <Label>Indexation Preset</Label>
                      <Select value={indexationPreset} onValueChange={setIndexationPreset}>
                        <SelectTrigger>
                          <SelectValue placeholder="Select preset" />
                        </SelectTrigger>
                        <SelectContent>
                          {indexationPresets.map((p) => (
                            <SelectItem key={p.name} value={p.name}>
                              {p.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="space-y-2">
                      <Label>Retrieval Preset</Label>
                      <Select value={retrievalPreset} onValueChange={setRetrievalPreset}>
                        <SelectTrigger>
                          <SelectValue placeholder="Select preset" />
                        </SelectTrigger>
                        <SelectContent>
                          {retrievalPresets.map((p) => (
                            <SelectItem key={p.name} value={p.name}>
                              {p.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                  </div>
                  <div className="space-y-2">
                    <Label className="flex items-center gap-1.5">
                      Chat LLM
                      {llmValidating && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
                      {!llmValidating && llmValidated === true && chatLlm !== "__default__" && (
                        <CheckCircle className="h-3.5 w-3.5 text-green-500" />
                      )}
                      {!llmValidating && llmValidated === false && (
                        <XCircle className="h-3.5 w-3.5 text-destructive" />
                      )}
                    </Label>
                    <Select value={chatLlm} onValueChange={handleChatLlmChange}>
                      <SelectTrigger>
                        <SelectValue placeholder="Select LLM" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="__default__">Default (from retrieval config)</SelectItem>
                        {llmEndpoints?.map((ep) => (
                          <SelectItem key={ep.name} value={ep.name}>
                            {ep.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="space-y-2">
                    <Label className="flex items-center gap-1.5">
                      Chat History Depth *
                      <TooltipProvider>
                        <Tooltip>
                          <TooltipTrigger type="button" className="cursor-help">
                            <Info className="h-3.5 w-3.5 text-muted-foreground" />
                          </TooltipTrigger>
                          <TooltipContent className="max-w-xs">
                            Number of previous chat messages (including the current question) kept as
                            context for follow-up questions in this partition. Low values (e.g. 1) mean the
                            assistant won&apos;t see earlier turns in the conversation. Default: 4.
                          </TooltipContent>
                        </Tooltip>
                      </TooltipProvider>
                    </Label>
                    <Input
                      type="number"
                      min={1}
                      required
                      value={chatHistoryDepth}
                      onChange={(e) => setChatHistoryDepth(e.target.value)}
                    />
                  </div>
                </>
              )}
            </div>
            <DialogFooter className="shrink-0 pt-4">
              <Button type="button" variant="outline" onClick={() => setDialogOpen(false)}>
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={
                  createMutation.isPending ||
                  (canManagePartitions &&
                    (llmValidating || llmValidated === false || !embedder || intOr(chatHistoryDepth, 0) < 1))
                }
              >
                {createMutation.isPending ? "Creating..." : "Create"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
