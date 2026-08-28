import { useState, useEffect, useCallback, useMemo } from "react";
import { useParams, Link } from "react-router-dom";
import { useInfiniteQuery, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Save, UserPlus, Trash2, CheckCircle, XCircle, Loader2, Info } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
  getPartitionConfig,
  updatePartition,
  listPartitions,
  listPartitionMembers,
  listPartitionMemberCandidates,
  removePartitionMember,
  updatePartitionMemberRole,
} from "@/lib/api/partitions";
import type { PartitionConfig, PartitionMemberCandidate, PartitionRole } from "@/lib/api/partitions";
import { listPresets } from "@/lib/api/presets";
import { listModelEndpoints, validateStoredModelEndpoint, resolveEmbedderName } from "@/lib/api/models";
import { listAllPrompts } from "@/lib/api/prompts";
import type { PromptResponse } from "@/lib/api/prompts";
import {
  PROMPT_DEFAULT_OPTION,
  PROMPT_GROUPS,
  promptOptionToName,
  promptOptionValue,
  promptSelectValue,
} from "@/lib/prompt-meta";
import { usePermissions } from "@/lib/permissions";
import { formatDate, intOr } from "@/lib/utils";
import { MemberPicker } from "./member-picker";
import { candidateLabel, candidateSecondaryLabel } from "./member-candidate";
import { addPartitionMembers } from "./member-batch";
import type { MemberAddFailure } from "./member-batch";
import {
  PartitionMemberEmail,
  PartitionMemberIdentity,
} from "./partition-member-identity";
import { describePartitionMember } from "./partition-member";

// Prompt types selected on a partition. Indexation/retrieval prompts belong to
// their presets; final-answer and transcription prompts belong to the document
// or chat partition itself.
const PARTITION_PROMPT_TYPES = PROMPT_GROUPS
  .filter((g) => g.name === "Final Answer" || g.name === "Transcription")
  .flatMap((g) => g.types);

// --- General Tab ---

function GeneralTab({ partition }: { partition: PartitionConfig }) {
  const queryClient = useQueryClient();
  const { canConfigurePartition, isAdmin } = usePermissions();

  // Saving partition settings is owner-gated (require_partition_owner); the preset
  // and model option lists come from admin-only registries. So non-owners get a
  // read-only form, and the preset/LLM pickers only populate for admins (the
  // registry calls are admin-gated to avoid guaranteed 403s).
  const partitionsQuery = useQuery({ queryKey: ["partitions"], queryFn: listPartitions });
  const callerRole = partitionsQuery.data?.partitions.find((p) => p.partition === partition.name)?.role;
  const canEdit = canConfigurePartition(callerRole);

  const [description, setDescription] = useState(partition.description ?? "");
  const [chatHistoryDepth, setChatHistoryDepth] = useState(String(partition.chat_history_depth));
  const [indexationPreset, setIndexationPreset] = useState(partition.indexation_preset);
  const [retrievalPreset, setRetrievalPreset] = useState(partition.retrieval_preset);
  const [chatLlm, setChatLlm] = useState(partition.chat_llm ?? "__default__");
  // Only carry the prompt types this editor manages — a stale key (e.g. a
  // pre-move query_contextualizer) would be rejected by the partition PATCH.
  const initialGenerationPrompts = useMemo(() => {
    const allowed = new Set<string>(PARTITION_PROMPT_TYPES.map((t) => t.value));
    return Object.fromEntries(
      Object.entries(partition.generation_prompt_names ?? {}).filter(([k]) => allowed.has(k)),
    );
  }, [partition.generation_prompt_names]);
  const [generationPrompts, setGenerationPrompts] =
    useState<Record<string, string>>(initialGenerationPrompts);
  const [llmValidated, setLlmValidated] = useState<boolean | null>(
    partition.chat_llm ? null : true,
  );
  const [llmValidating, setLlmValidating] = useState(false);

  // Preset/model registries are admin-only — only fetch them for admins so a
  // non-admin member's view doesn't fire requests the API answers with 403.
  const { data: presetsData } = useQuery({
    queryKey: ["presets"],
    queryFn: () => listPresets(),
    enabled: isAdmin,
  });

  const { data: llmEndpoints } = useQuery({
    queryKey: ["model-endpoints", "llm"],
    queryFn: () => listModelEndpoints("llm"),
    enabled: isAdmin,
  });

  const { data: embedderEndpoints } = useQuery({
    queryKey: ["model-endpoints", "embedder"],
    queryFn: () => listModelEndpoints("embedder"),
    enabled: isAdmin,
  });

  const { data: promptsData } = useQuery({
    queryKey: ["prompts-library"],
    queryFn: () => listAllPrompts(),
    enabled: isAdmin,
  });
  const promptsByType = (type: string): PromptResponse[] =>
    (promptsData ?? []).filter((p) => p.prompt_type === type);

  // Compared against what the partition was loaded with, so an untouched
  // mapping is omitted from the PATCH entirely.
  const generationPromptsChanged =
    JSON.stringify(generationPrompts) !== JSON.stringify(initialGenerationPrompts);

  const setGenerationPrompt = (type: string, name: string) => {
    setGenerationPrompts((prev) => {
      const next = { ...prev };
      if (name) next[type] = name;
      else delete next[type];
      return next;
    });
  };

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

  // Validate the initially-set LLM once endpoints are loaded
  useEffect(() => {
    if (partition.chat_llm && llmEndpoints && llmValidated === null && !llmValidating) {
      validateLlm(partition.chat_llm);
    }
  }, [partition.chat_llm, llmEndpoints, llmValidated, llmValidating, validateLlm]);

  const indexationPresets = presetsData?.filter((p) => p.preset_type === "indexation") ?? [];
  const retrievalPresets = presetsData?.filter((p) => p.preset_type === "retrieval") ?? [];

  const mutation = useMutation({
    mutationFn: () =>
      updatePartition(partition.name, {
        description: description || undefined,
        chat_history_depth: intOr(chatHistoryDepth, 0),
        indexation_preset: indexationPreset,
        retrieval_preset: retrievalPreset,
        chat_llm: chatLlm === "__default__" ? null : chatLlm,
        // Only sent when this editor actually changed it. The backend validates
        // every name in the mapping, so resubmitting an untouched-but-stale
        // reference (its prompt since renamed or deleted) would 422 the whole
        // PATCH and block unrelated edits like the description — and a non-admin
        // owner, for whom this picker is disabled, could never clear it.
        ...(generationPromptsChanged ? { generation_prompt_names: generationPrompts } : {}),
      }),
    onSuccess: () => {
      toast.success("Partition updated");
      queryClient.invalidateQueries({
        queryKey: ["partition", partition.name],
      });
    },
    onError: (error: Error) => {
      toast.error(`Failed to update: ${error.message}`);
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!indexationPreset.trim() || !retrievalPreset.trim()) {
      toast.error("Presets are required");
      return;
    }
    if (intOr(chatHistoryDepth, 0) < 1) {
      toast.error("Chat history depth must be at least 1");
      return;
    }
    mutation.mutate();
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">General Settings</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-6 max-w-5xl">
          {!canEdit && (
            <p className="rounded-md border bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
              You have read-only access to this partition's settings. Only an owner can change them.
            </p>
          )}
          {canEdit && !isAdmin && (
            <p className="rounded-md border bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
              Preset and model selection is managed by an administrator and can't be changed here.
            </p>
          )}
          <div className="space-y-2">
            <Label>Description</Label>
            <Textarea
              placeholder="Partition description..."
              className="min-h-[80px]"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              disabled={!canEdit}
            />
          </div>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-6">
            <div className="space-y-2">
              <Label className="text-muted-foreground">Dimension</Label>
              <p className="text-sm font-medium pt-1">{partition.dimension}</p>
            </div>
            <div className="space-y-2">
              <Label className="text-muted-foreground">Embedder</Label>
              <p className="text-sm font-medium pt-1">
                {resolveEmbedderName(partition.embedder, embedderEndpoints)}
              </p>
            </div>
            <div className="space-y-2">
              <Label className="text-muted-foreground">Documents</Label>
              <p className="text-sm font-medium pt-1">{partition.document_count}</p>
            </div>
          </div>
          <div className="pt-2 grid grid-cols-2 lg:grid-cols-4 gap-6">
            <div className="space-y-2">
              <Label>Indexation Preset</Label>
              <Select value={indexationPreset} onValueChange={setIndexationPreset} disabled={!canEdit || !isAdmin}>
                <SelectTrigger className="w-full">
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
              <Select value={retrievalPreset} onValueChange={setRetrievalPreset} disabled={!canEdit || !isAdmin}>
                <SelectTrigger className="w-full">
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
              <Select value={chatLlm} onValueChange={handleChatLlmChange} disabled={!canEdit || !isAdmin}>
                <SelectTrigger className="w-full">
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
            {PARTITION_PROMPT_TYPES.map((t) => {
              const options = promptsByType(t.value);
              return (
                <div key={t.value} className="space-y-2">
                  <Label>{t.label}</Label>
                  <Select
                    value={promptSelectValue(generationPrompts[t.value])}
                    onValueChange={(v) => setGenerationPrompt(t.value, promptOptionToName(v))}
                    disabled={!canEdit || !isAdmin}
                  >
                    <SelectTrigger className="w-full">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={PROMPT_DEFAULT_OPTION}>Use default</SelectItem>
                      {options.map((p) => (
                        <SelectItem key={p.id} value={promptOptionValue(p.name)}>
                          {p.name}{p.is_default ? " (default)" : ""}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              );
            })}
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
                    Number of previous chat messages (including the current question) kept as context
                    for follow-up questions in this partition. Low values (e.g. 1) mean the assistant
                    won&apos;t see earlier turns in the conversation. Default: 4.
                  </TooltipContent>
                </Tooltip>
              </TooltipProvider>
            </Label>
            <Input
              type="number"
              min={1}
              required
              className="max-w-[8rem]"
              value={chatHistoryDepth}
              onChange={(e) => setChatHistoryDepth(e.target.value)}
              disabled={!canEdit}
            />
          </div>
          {canEdit && (
            <Button
              type="submit"
              disabled={
                mutation.isPending || llmValidating || llmValidated === false || intOr(chatHistoryDepth, 0) < 1
              }
            >
              <Save className="h-4 w-4" />
              {mutation.isPending ? "Saving..." : "Save Changes"}
            </Button>
          )}
        </form>
      </CardContent>
    </Card>
  );
}

// --- Pipeline Config Tab ---

function PipelineConfigTab({ partition }: { partition: PartitionConfig }) {
  const renderConfigValue = (value: unknown): string => {
    if (value === null || value === undefined) return "—";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (typeof value === "object") return JSON.stringify(value, null, 0);
    return String(value);
  };

  const renderConfigSection = (
    title: string,
    presetName: string,
    pipeline: Record<string, unknown>,
  ) => (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}: {presetName}</CardTitle>
      </CardHeader>
      <CardContent>
        {Object.keys(pipeline).length === 0 ? (
          <p className="text-sm text-muted-foreground">No configuration data.</p>
        ) : (
          <dl className="space-y-2">
            {Object.entries(pipeline).map(([key, value]) => (
              <div key={key} className="flex justify-between gap-4 text-sm">
                <dt className="text-muted-foreground font-mono text-xs min-w-0 break-all">{key}</dt>
                <dd className="font-medium text-right shrink-0">{renderConfigValue(value)}</dd>
              </div>
            ))}
          </dl>
        )}
      </CardContent>
    </Card>
  );

  return (
    <div className="space-y-6">
      <div className="grid gap-6 lg:grid-cols-2">
        {renderConfigSection(
          "Indexation Preset",
          partition.indexation_preset,
          partition.indexation_pipeline ?? {},
        )}
        {renderConfigSection(
          "Retrieval Preset",
          partition.retrieval_preset,
          partition.retrieval_pipeline ?? {},
        )}
      </div>
      <div>
        <Link
          to="/presets"
          className="text-sm text-primary hover:underline inline-flex items-center gap-1"
        >
          Manage Presets
        </Link>
      </div>
    </div>
  );
}

// --- Users Tab ---

function UsersTab({ partitionName }: { partitionName: string }) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [selectedCandidates, setSelectedCandidates] = useState<PartitionMemberCandidate[]>([]);
  const [addFailures, setAddFailures] = useState<MemberAddFailure[]>([]);
  const [role, setRole] = useState("viewer");
  const [candidateSearch, setCandidateSearch] = useState("");
  const [debouncedCandidateSearch, setDebouncedCandidateSearch] = useState("");
  const queryClient = useQueryClient();
  const { canManageMembers } = usePermissions();

  const usersQuery = useQuery({
    queryKey: ["partition", partitionName, "users"],
    queryFn: () => listPartitionMembers(partitionName),
  });

  // Only a partition owner (or a platform admin) can manage members; the caller's
  // role comes from the membership-scoped partition list. Mirrors the server's
  // require_partition_owner check — non-owners get a read-only view.
  const partitionsQuery = useQuery({ queryKey: ["partitions"], queryFn: listPartitions });
  const callerRole = partitionsQuery.data?.partitions.find((p) => p.partition === partitionName)?.role;
  const canManage = canManageMembers(callerRole);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      setDebouncedCandidateSearch(candidateSearch.trim());
    }, 250);
    return () => window.clearTimeout(timeout);
  }, [candidateSearch]);

  const normalizedCandidateSearch = candidateSearch.trim();
  const candidateSearchReady =
    /^\d+$/.test(normalizedCandidateSearch) || normalizedCandidateSearch.length >= 3;

  const candidatesQuery = useInfiniteQuery({
    queryKey: ["partition", partitionName, "member-candidates", debouncedCandidateSearch],
    queryFn: ({ pageParam }) =>
      listPartitionMemberCandidates(partitionName, {
        search: debouncedCandidateSearch,
        cursor: pageParam ?? undefined,
        limit: 25,
      }),
    initialPageParam: null as number | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    enabled:
      dialogOpen &&
      canManage &&
      (/^\d+$/.test(debouncedCandidateSearch) || debouncedCandidateSearch.length >= 3),
  });
  const candidates = candidatesQuery.data?.pages.flatMap((page) => page.candidates) ?? [];
  const candidateSearchPending =
    candidateSearchReady && normalizedCandidateSearch !== debouncedCandidateSearch;

  const addMutation = useMutation({
    mutationFn: async ({
      candidates: selected,
      selectedRole,
    }: {
      candidates: PartitionMemberCandidate[];
      selectedRole: PartitionRole;
    }) =>
      addPartitionMembers({
        partitionName,
        candidates: selected,
        role: selectedRole,
      }),
    onError: (error: Error) => {
      toast.error(`Failed to add users: ${error.message}`);
    },
    onSuccess: ({ addedCandidates, failures }) => {
      queryClient.invalidateQueries({
        queryKey: ["partition", partitionName, "users"],
      });
      queryClient.invalidateQueries({
        queryKey: ["partition", partitionName, "member-candidates"],
      });

      if (failures.length > 0) {
        setSelectedCandidates(failures.map((failure) => failure.candidate));
        setAddFailures(failures);
        const firstFailure = failures[0];
        toast.error(
          `${addedCandidates.length > 0 ? `${addedCandidates.length} added; ` : ""}${failures.length} failed: ${firstFailure.message}`,
        );
        return;
      }

      toast.success(
        `${addedCandidates.length} user${addedCandidates.length === 1 ? "" : "s"} added to partition`,
      );
      setDialogOpen(false);
      setSelectedCandidates([]);
      setAddFailures([]);
      setRole("viewer");
      setCandidateSearch("");
      setDebouncedCandidateSearch("");
    },
  });

  const removeMutation = useMutation({
    mutationFn: (uid: number) => removePartitionMember(partitionName, uid),
    onSuccess: () => {
      toast.success("User removed from partition");
      queryClient.invalidateQueries({
        queryKey: ["partition", partitionName, "users"],
      });
    },
    onError: (error: Error) => {
      toast.error(`Failed to remove user: ${error.message}`);
    },
  });

  const roleMutation = useMutation({
    mutationFn: ({ uid, newRole }: { uid: number; newRole: PartitionRole }) =>
      updatePartitionMemberRole(partitionName, uid, newRole),
    onSuccess: () => {
      toast.success("Role updated");
      queryClient.invalidateQueries({ queryKey: ["partition", partitionName, "users"] });
    },
    onError: (error: Error) => {
      toast.error(`Failed to update role: ${error.message}`);
    },
  });

  const handleAddUsers = () => {
    if (selectedCandidates.length === 0) {
      toast.error("Select at least one user");
      return;
    }
    setAddFailures([]);
    addMutation.mutate({
      candidates: selectedCandidates,
      selectedRole: role as PartitionRole,
    });
  };

  const handleDialogOpenChange = (open: boolean) => {
    if (!open && addMutation.isPending) return;
    setDialogOpen(open);
    if (!open) {
      setSelectedCandidates([]);
      setAddFailures([]);
      setRole("viewer");
      setCandidateSearch("");
      setDebouncedCandidateSearch("");
    }
  };

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle className="text-base">Partition Users</CardTitle>
        {canManage && (
          <Button size="sm" onClick={() => setDialogOpen(true)}>
            <UserPlus className="h-4 w-4" />
            Add User
          </Button>
        )}
      </CardHeader>
      <CardContent>
        {usersQuery.isLoading ? (
          <div className="space-y-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : usersQuery.data && usersQuery.data.members.length > 0 ? (
          <div className="overflow-x-auto rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>User</TableHead>
                  <TableHead>Email</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead>Added</TableHead>
                  {canManage && <TableHead>Actions</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {usersQuery.data.members.map((user) => (
                  <TableRow key={user.user_id}>
                    <TableCell className="text-sm">
                      <PartitionMemberIdentity member={user} />
                    </TableCell>
                    <TableCell className="text-sm">
                      <PartitionMemberEmail member={user} />
                    </TableCell>
                    <TableCell>
                      {canManage ? (
                        <Select
                          value={String(user.role)}
                          onValueChange={(v) => {
                            if (v !== user.role)
                              roleMutation.mutate({ uid: user.user_id, newRole: v as PartitionRole });
                          }}
                        >
                          <SelectTrigger className="h-8 w-32 capitalize" disabled={roleMutation.isPending}>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="viewer">Viewer</SelectItem>
                            <SelectItem value="editor">Editor</SelectItem>
                            <SelectItem value="owner">Owner</SelectItem>
                          </SelectContent>
                        </Select>
                      ) : (
                        <span className="capitalize">{user.role}</span>
                      )}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {formatDate(user.added_at)}
                    </TableCell>
                    {canManage && (
                      <TableCell>
                        <ConfirmDialog
                          title="Remove User"
                          description={`Remove ${describePartitionMember(user)} from this partition? They will lose access to partition data.`}
                          onConfirm={() => removeMutation.mutate(user.user_id)}
                        >
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={removeMutation.isPending}
                            aria-label={`Remove ${describePartitionMember(user)} from partition`}
                          >
                            <Trash2 className="h-3 w-3 text-destructive" />
                          </Button>
                        </ConfirmDialog>
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground text-center py-6">
            No users assigned to this partition yet.
          </p>
        )}

        <Dialog open={dialogOpen} onOpenChange={handleDialogOpenChange}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Add Users to Partition</DialogTitle>
              <DialogDescription>
                Select users and assign the same partition role to each of them.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-4">
              <MemberPicker
                candidates={candidates}
                isLoading={candidatesQuery.isLoading || candidateSearchPending}
                isInitialError={candidatesQuery.isError && candidatesQuery.data === undefined}
                isRefreshError={
                  candidatesQuery.isRefetchError &&
                  candidatesQuery.data !== undefined &&
                  !candidatesQuery.isFetchNextPageError
                }
                isLoadMoreError={candidatesQuery.isFetchNextPageError}
                search={candidateSearch}
                searchReady={candidateSearchReady}
                onSearchChange={(search) => {
                  setCandidateSearch(search);
                  setAddFailures([]);
                }}
                onRetry={() => {
                  void candidatesQuery.refetch();
                }}
                hasMore={Boolean(candidatesQuery.hasNextPage)}
                isLoadingMore={candidatesQuery.isFetchingNextPage}
                onLoadMore={() => {
                  void candidatesQuery.fetchNextPage();
                }}
                selectedCandidates={selectedCandidates}
                onSelectionChange={(selection) => {
                  setSelectedCandidates(selection);
                  setAddFailures([]);
                }}
              />
              {addFailures.length > 0 && (
                <Alert variant="destructive">
                  <AlertTitle>Some users were not added</AlertTitle>
                  <AlertDescription>
                    <ul className="list-disc space-y-1 pl-4">
                      {addFailures.map((failure) => (
                        <li key={failure.candidate.user_id}>
                          {candidateLabel(failure.candidate)} ({candidateSecondaryLabel(failure.candidate)}):
                          {" "}
                          {failure.message}
                        </li>
                      ))}
                    </ul>
                  </AlertDescription>
                </Alert>
              )}
              <div className="space-y-2">
                <Label>Role</Label>
                <Select
                  value={role}
                  onValueChange={(value) => {
                    setRole(value);
                    setAddFailures([]);
                  }}
                >
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="Select a role" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="viewer">Viewer — read &amp; chat only</SelectItem>
                    <SelectItem value="editor">Editor — add, edit &amp; delete documents</SelectItem>
                    <SelectItem value="owner">Owner — full control (members &amp; deletion)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => handleDialogOpenChange(false)}
                disabled={addMutation.isPending}
              >
                Cancel
              </Button>
              <Button
                onClick={handleAddUsers}
                disabled={addMutation.isPending || selectedCandidates.length === 0}
              >
                {addMutation.isPending
                  ? "Adding..."
                  : selectedCandidates.length === 0
                    ? "Add Users"
                    : `Add ${selectedCandidates.length} User${selectedCandidates.length === 1 ? "" : "s"}`}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  );
}

// --- Main Detail Page ---

export default function PartitionDetailPage() {
  const { name } = useParams<{ name: string }>();

  const partitionQuery = useQuery({
    queryKey: ["partition", name],
    queryFn: () => getPartitionConfig(name!),
    enabled: !!name,
  });

  if (!name) {
    return (
      <div className="text-center py-12 text-muted-foreground">
        Invalid partition name.
      </div>
    );
  }

  if (partitionQuery.isLoading) {
    return (
      <div>
        <div className="flex items-center gap-4 mb-6">
          <Skeleton className="h-9 w-9" />
          <div>
            <Skeleton className="h-7 w-48 mb-2" />
            <Skeleton className="h-4 w-64" />
          </div>
        </div>
        <Skeleton className="h-10 w-80 mb-6" />
        <div className="space-y-4">
          <Skeleton className="h-64 w-full" />
        </div>
      </div>
    );
  }

  if (partitionQuery.isError) {
    return (
      <div className="text-center py-12">
        <p className="text-destructive mb-4">
          Failed to load partition: {partitionQuery.error.message}
        </p>
        <Button variant="outline" asChild>
          <Link to="/partitions">
            <ArrowLeft className="h-4 w-4" />
            Back to Partitions
          </Link>
        </Button>
      </div>
    );
  }

  const partition = partitionQuery.data!;

  return (
    <div>
      <PageHeader
        title={partition.name}
        description={partition.description || "No description"}
        actions={
          <Button variant="outline" asChild>
            <Link to="/partitions">
              <ArrowLeft className="h-4 w-4" />
              Back
            </Link>
          </Button>
        }
      />

      <Tabs defaultValue="general">
        <TabsList>
          <TabsTrigger value="general">General</TabsTrigger>
          <TabsTrigger value="pipeline">Pipeline Config</TabsTrigger>
          <TabsTrigger value="users">Users</TabsTrigger>
        </TabsList>

        <TabsContent value="general" className="mt-6">
          <GeneralTab partition={partition} />
        </TabsContent>

        <TabsContent value="pipeline" className="mt-6">
          <PipelineConfigTab partition={partition} />
        </TabsContent>

        <TabsContent value="users" className="mt-6">
          <UsersTab partitionName={partition.name} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
