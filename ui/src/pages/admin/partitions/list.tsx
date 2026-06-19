import { useMemo, useState, useCallback, useEffect } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Pencil, Trash2, Search, ArrowUpDown, ArrowUp, ArrowDown, CheckCircle, XCircle, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
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

type SortDir = "asc" | "desc" | null;

function RowActions({ partition }: { partition: PartitionResponse }) {
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
      <Button variant="ghost" size="sm" asChild>
        <Link to={`/partitions/${partition.name}`}>
          <Pencil className="h-3 w-3" />
        </Link>
      </Button>
      <ConfirmDialog
        title="Delete Partition"
        description={`Delete "${partition.name}"? This cannot be undone.`}
        onConfirm={() => deleteMutation.mutate()}
      >
        <Button variant="ghost" size="sm" disabled={deleteMutation.isPending}>
          <Trash2 className="h-3 w-3 text-destructive" />
        </Button>
      </ConfirmDialog>
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
  const { canManagePartitions } = usePermissions();
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
  const [chatHistoryDepth, setChatHistoryDepth] = useState("0");
  const [chatLlm, setChatLlm] = useState("__default__");
  const [llmValidated, setLlmValidated] = useState<boolean | null>(true);
  const [llmValidating, setLlmValidating] = useState(false);

  const [search, setSearch] = useState("");
  const [sortDir, setSortDir] = useState<SortDir>(null);
  const [sortColumn, setSortColumn] = useState<"name" | "created_at">("created_at");

  // `GET /partition/` is already membership-scoped server-side (admins with
  // SUPER_ADMIN_MODE see all; regular users see their memberships), so a single
  // query serves both — canManagePartitions only gates the admin-only columns/fields below.
  const partitionsQuery = useQuery({
    queryKey: ["partitions"],
    queryFn: listPartitions,
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
        chat_history_depth: parseInt(chatHistoryDepth),
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
      setChatHistoryDepth("0");
      setChatLlm("__default__");
      setLlmValidated(true);
    },
    onError: (error: Error) => {
      toast.error(`Failed to create: ${error.message}`);
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      toast.error("Name is required");
      return;
    }
    createMutation.mutate();
  };

  return (
    <div>
      <PageHeader
        title="Partitions"
        description={canManagePartitions ? "Manage document partitions and their configurations" : "Your assigned document partitions"}
        actions={
          <Button onClick={() => setDialogOpen(true)}>
            <Plus className="mr-2 h-4 w-4" /> Create Partition
          </Button>
        }
      />

      <div className="mb-4">
        <div className="relative max-w-sm">
          <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search partitions..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
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
                {canManagePartitions && <TableHead>Actions</TableHead>}
              </TableRow>
            </TableHeader>
            <TableBody>
              {filteredAndSorted.length ? (
                filteredAndSorted.map((p) => (
                  <TableRow key={p.name}>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <Link
                          to={`/partitions/${p.name}`}
                          className="font-medium text-primary hover:underline"
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
                        {resolveEmbedderName(p.embedder, embedderEndpoints)}
                      </TableCell>
                    )}
                    {canManagePartitions && <TableCell>{p.indexation_preset}</TableCell>}
                    {canManagePartitions && <TableCell>{p.retrieval_preset}</TableCell>}
                    <TableCell className="text-sm text-muted-foreground">
                      {p.created_at
                        ? new Date(p.created_at).toLocaleDateString()
                        : "--"}
                    </TableCell>
                    {canManagePartitions && (
                      <TableCell>
                        <RowActions partition={p} />
                      </TableCell>
                    )}
                  </TableRow>
                ))
              ) : (
                <TableRow>
                  <TableCell
                    colSpan={canManagePartitions ? 8 : 4}
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

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Create Partition</DialogTitle>
            <DialogDescription>
              Create a new document partition with its configuration.
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={handleSubmit} className="space-y-4">
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
                  <Label>Chat History Depth</Label>
                  <Input
                    type="number"
                    min={0}
                    value={chatHistoryDepth}
                    onChange={(e) => setChatHistoryDepth(e.target.value)}
                  />
                </div>
              </>
            )}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setDialogOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={createMutation.isPending || (canManagePartitions && (llmValidating || llmValidated === false || !embedder))}>
                {createMutation.isPending ? "Creating..." : "Create"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
