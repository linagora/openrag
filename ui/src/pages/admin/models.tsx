import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { NewBadge } from "@/components/shared/new-badge";
import {
  Plus,
  Trash2,
  Pencil,
  Star,
  Loader2,
  CheckCircle,
  XCircle,
  Eye,
  EyeOff,
  Copy,
  Info,
  ChevronsUpDown,
  RefreshCw,
} from "lucide-react";
import {
  listModelEndpoints,
  createModelEndpoint,
  updateModelEndpoint,
  deleteModelEndpoint,
  getModelEndpointIndexedUsage,
  DEFAULT_VENDOR_BY_TYPE,
  formatLlmBudget,
  llmContextSize,
  llmOutputTokens,
  mergeModelEndpointApiKeyExtra,
  mergeModelEndpointImplementation,
  mergeModelEndpointLlmContext,
  mergeModelEndpointMossSpeakerAware,
  mergeModelEndpointSttLanguage,
  prepareModelEndpointExtraForSubmit,
  refetchWhileDetecting,
  revealModelEndpointApiKey,
  REDACTED_SECRET,
  setDefaultModelEndpoint,
  splitModelEndpointApiKeyExtra,
  splitModelEndpointImplementation,
  splitModelEndpointLlmContext,
  splitModelEndpointMossSpeakerAware,
  splitModelEndpointSttLanguage,
  validateModelEndpoint,
  VENDOR_OPTIONS_BY_TYPE,
} from "@/lib/api/models";
import type {
  LlmBudget,
  ModelEndpointResponse,
  CreateModelEndpointRequest,
  UpdateModelEndpointRequest,
  ModelType,
} from "@/lib/api/models";
import { PageHeader } from "@/components/shared/page-header";
import { QueryErrorNotice } from "@/components/shared/query-error-notice";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Checkbox } from "@/components/ui/checkbox";
import {
  diffEndpointUpdate,
  hasMaterialChange,
  suggestCopyName,
  type EndpointFieldChange,
} from "./embedder-edit-guard";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { VendorIcon } from "@/components/ui/vendor-icon";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { formatDate, intOr, numOr } from "@/lib/utils";

const MODEL_TYPES = ["embedder", "reranker", "llm", "vlm", "stt"] as const;

type RevealedApiKey = {
  modelType: ModelType;
  name: string;
  value: string;
};

// Discovery waits for typing to pause, and probes with a short timeout so a
// dead URL doesn't hold the model field's hint for the full request timeout.
const DISCOVERY_DEBOUNCE_MS = 600;
const DISCOVERY_TIMEOUT_S = 10;
// That timeout bounds OpenRAG's call to the provider. The browser's own request
// to OpenRAG gets a deadline too, so a stalled one ends with a retryable error
// rather than a spinner that never stops.
const DISCOVERY_DEADLINE_S = DISCOVERY_TIMEOUT_S + 5;

const normalizeEndpointUrl = (value: string) => value.trim().replace(/\/+$/, "");
// A server message ends with a period or not ("All connection attempts failed"):
// end it with one, so the hint's next sentence doesn't run into it.
const asSentence = (text: string) => (/[.!?]$/.test(text.trim()) ? text.trim() : `${text.trim()}.`);

// What deleting this endpoint actually does to the partitions pointing at it.
// The two answers differ (#762): an embedder names the model a partition's
// vectors were built with, so the server refuses; a chat LLM is resolved per
// request and falls back to the default, so the delete just resets them.
function describeDelete(ep: ModelEndpointResponse): string {
  const n = ep.used_by_partitions ?? 0;
  if (n === 0) return `This will permanently delete "${ep.name}".`;
  const partitions = `${n} partition${n === 1 ? "" : "s"}`;
  if (ep.model_type === "embedder") {
    if (ep.is_default) {
      // The count includes partitions following the default alias, and the
      // empty ones among them simply follow the endpoint promoted in its place.
      return `"${ep.name}" is the default embedder for ${partitions}. The server refuses the delete while any of them names it or already holds indexed files — reassign those first. Partitions with no files yet follow the endpoint promoted in its place.`;
    }
    return `"${ep.name}" is the embedder for ${partitions}. Deleting it would break every query and upload on them, so the server will refuse — reassign those partitions to another embedder first.`;
  }
  if (ep.model_type === "llm") {
    return `"${ep.name}" is the chat LLM for ${partitions}. Deleting it resets them to the default LLM.`;
  }
  return `This will permanently delete "${ep.name}". ${partitions} reference it.`;
}

// Mirrors the backend's `_NAME_PATTERN` allowlist (api/schemas/admin/model_endpoint_schemas.py):
// `name` is a single path segment in every single-endpoint route. An allowlist, not a
// denylist — `/` splits across path segments, and `.`/`..` are RFC 3986 dot-segments that
// browsers normalize out of the URL before the request is even sent — so anchoring both
// ends on alphanumeric rules out all of that (and any leading/trailing separator) at once.
const NAME_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$/;
const NAME_MAX_LENGTH = 128;

// Placeholder for a blank per-endpoint budget: the value the server falls back
// to, as it reports it (the endpoint's detected max_model_len, else the
// MAX_LLM_CONTEXT_SIZE / MAX_OUTPUT_TOKENS defaults — environment-configurable,
// so never hard-coded here). Generic only on a backend too old to report it.
function budgetPlaceholder(budget: LlmBudget): string {
  if (budget.source === "detecting") return "Detecting…";
  if (budget.value === null) return "System default";
  const n = budget.value.toLocaleString("en-US");
  return budget.source === "detected" ? `Detected: ${n}` : `System default: ${n}`;
}

function LabelWithInfo({ label, tooltip }: { label: string; tooltip: string }) {
  return (
    <div className="flex items-center gap-1.5">
      <Label>{label}</Label>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              className="text-muted-foreground hover:text-foreground"
              aria-label={`${label} info`}
            >
              <Info className="h-3.5 w-3.5" />
            </button>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs">{tooltip}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    </div>
  );
}

export default function ModelsPage() {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState("embedder");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<ModelEndpointResponse | null>(null);
  // The embedder whose edit is waiting on the explanation below, and the one a
  // prefilled create is copying from. Never both.
  const [pendingEdit, setPendingEdit] = useState<ModelEndpointResponse | null>(null);
  // Carries its suggested name: computing it needs the full endpoint list, which
  // is a fresh array every render and would thrash the dialog's seeding effect.
  const [seed, setSeed] = useState<{ from: ModelEndpointResponse; name: string } | null>(null);
  // The embedder about to become the default, waiting on the dialog that says
  // which partitions stay behind.
  const [pendingDefault, setPendingDefault] = useState<ModelEndpointResponse | null>(null);

  const endpointsQuery = useQuery({
    queryKey: ["model-endpoints"],
    queryFn: () => listModelEndpoints(),
    refetchInterval: refetchWhileDetecting,
  });
  const { data, isLoading } = endpointsQuery;

  const createMut = useMutation({
    mutationFn: (req: CreateModelEndpointRequest) => createModelEndpoint(req),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["model-endpoints"] });
      toast.success("Model endpoint created");
      setDialogOpen(false);
      setSeed(null);
    },
    onError: (e) => toast.error(e.message),
  });

  const updateMut = useMutation({
    mutationFn: ({ type, name, data }: { type: string; name: string; data: UpdateModelEndpointRequest }) =>
      updateModelEndpoint(type as ModelType, name, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["model-endpoints"] });
      toast.success("Model endpoint updated");
      setDialogOpen(false);
      setEditing(null);
    },
    onError: (e) => toast.error(e.message),
  });

  const deleteMut = useMutation({
    mutationFn: ({ type, name }: { type: string; name: string }) =>
      deleteModelEndpoint(type as ModelType, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["model-endpoints"] });
      toast.success("Model endpoint deleted");
    },
    onError: (e) => toast.error(e.message),
  });

  const setDefaultMut = useMutation({
    mutationFn: ({ type, name }: { type: string; name: string }) =>
      setDefaultModelEndpoint(type as ModelType, name),
    onSuccess: (_endpoint, { type }) => {
      queryClient.invalidateQueries({ queryKey: ["model-endpoints"] });
      if (type === "embedder") {
        // Indexed partitions on the alias were just pinned to the old default by name.
        queryClient.invalidateQueries({ queryKey: ["partitions"] });
        queryClient.invalidateQueries({ queryKey: ["partition"] });
      }
      toast.success("Default endpoint updated");
    },
    onError: (e) => toast.error(e.message),
  });

  const endpoints = data ?? [];

  const handleOpenCreate = () => {
    setEditing(null);
    setSeed(null);
    setDialogOpen(true);
  };

  const handleOpenEdit = (ep: ModelEndpointResponse) => {
    // An embedder edit is the one that can silently invalidate stored data, and
    // the safe alternative (a second endpoint) is only obvious if it is offered
    // before the form is open and the change already typed (#762 C). An embedder
    // no partition uses has no stored vectors to invalidate, so it edits directly.
    if (ep.model_type === "embedder" && (ep.used_by_partitions ?? 0) > 0) {
      setPendingEdit(ep);
      return;
    }
    setEditing(ep);
    setDialogOpen(true);
  };

  const handleModifyExisting = (ep: ModelEndpointResponse) => {
    setPendingEdit(null);
    setSeed(null);
    setEditing(ep);
    setDialogOpen(true);
  };

  const handleCreateCopy = (ep: ModelEndpointResponse) => {
    setPendingEdit(null);
    setEditing(null);
    setSeed({ from: ep, name: suggestCopyName(ep.name, endpoints.map((e) => e.name)) });
    setDialogOpen(true);
  };

  return (
    <div>
      <PageHeader
        title="Model Endpoints"
        description="Manage embedder, reranker, LLM, VLM, and speech-to-text endpoints"
        actions={
          <Button onClick={handleOpenCreate}>
            <Plus className="mr-2 h-4 w-4" /> Add Endpoint
          </Button>
        }
      />

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          {MODEL_TYPES.map((t) => (
            <TabsTrigger key={t} value={t} className="capitalize" aria-label={t}>
              {t}
              <NewBadge feature={`models.${t}`} />
            </TabsTrigger>
          ))}
        </TabsList>

        {MODEL_TYPES.map((type) => (
          <TabsContent key={type} value={type}>
            {isLoading ? (
              <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                {[1, 2, 3].map((i) => (
                  <Skeleton key={i} className="h-48" />
                ))}
              </div>
            ) : endpointsQuery.isLoadingError ? (
              <QueryErrorNotice
                title="Model endpoints could not be loaded"
                message="The endpoint list is unavailable. Try again to reload it."
                isRetrying={endpointsQuery.isFetching}
                onRetry={() => void endpointsQuery.refetch()}
              />
            ) : (
              <>
                {endpointsQuery.isRefetchError && (
                  <div className="mb-4">
                    <QueryErrorNotice
                      title="Model endpoints could not be refreshed"
                      message="Showing previously loaded endpoints. They may be out of date."
                      isRetrying={endpointsQuery.isFetching}
                      onRetry={() => void endpointsQuery.refetch()}
                      stale
                    />
                  </div>
                )}
                <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                {endpoints
                  .filter((ep) => ep.model_type === type)
                  .map((ep) => {
                    const isDefault = ep.is_default;
                    const usedBy = ep.used_by_partitions ?? 0;
                    return (
                      <Card key={`${ep.model_type}-${ep.name}`}>
                        <CardHeader className="pb-3">
                          <div className="flex flex-wrap items-center justify-between gap-2 min-w-0">
                            <CardTitle className="text-base truncate min-w-0" title={ep.name}>
                              {ep.name}
                            </CardTitle>
                            <div className="flex items-center gap-1.5 shrink-0">
                              {usedBy > 0 && (
                                <Badge
                                  variant="outline"
                                  className="shrink-0 text-xs bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/30 dark:text-amber-100 dark:border-amber-900/60"
                                >
                                  {`used by ${usedBy} partition${usedBy === 1 ? "" : "s"}`}
                                </Badge>
                              )}
                              {isDefault && (
                                <Badge className="shrink-0 bg-green-600 text-xs hover:bg-green-700">Default</Badge>
                              )}
                            </div>
                          </div>
                          {ep.model_name && (
                            <CardDescription className="font-mono text-xs truncate" title={ep.model_name}>
                              {ep.model_name}
                            </CardDescription>
                          )}
                        </CardHeader>
                        <CardContent className="space-y-2 text-sm">
                          <div className="flex justify-between">
                            <span className="text-muted-foreground">Endpoint</span>
                            <span className="truncate ml-2 max-w-[200px] text-right" title={ep.endpoint}>{ep.endpoint}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-muted-foreground">
                              {ep.model_type === "stt" ? "Concurrency per worker" : "Batch Size"}
                            </span>
                            <span>{ep.batch_size}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-muted-foreground">Timeout</span>
                            <span>{ep.timeout}s</span>
                          </div>
                          {ep.model_type === "llm" && (
                            <>
                              <div className="flex justify-between">
                                <span className="text-muted-foreground">Context size</span>
                                <span>{formatLlmBudget(llmContextSize(ep))}</span>
                              </div>
                              <div className="flex justify-between">
                                <span className="text-muted-foreground">Max output tokens</span>
                                <span>{formatLlmBudget(llmOutputTokens(ep))}</span>
                              </div>
                            </>
                          )}
                          <div className="text-xs text-muted-foreground">
                            Updated {formatDate(ep.updated_at)}
                          </div>
                          <div className="flex flex-wrap gap-2 pt-2">
                            {!isDefault && (
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={() =>
                                  // Only an embedder's default decides where vectors live.
                                  ep.model_type === "embedder"
                                    ? setPendingDefault(ep)
                                    : setDefaultMut.mutate({ type: ep.model_type, name: ep.name })
                                }
                                disabled={setDefaultMut.isPending}
                              >
                                <Star className="mr-1 h-3 w-3" /> Set Default
                              </Button>
                            )}
                            <Button size="sm" variant="outline" onClick={() => handleOpenEdit(ep)}>
                              <Pencil className="mr-1 h-3 w-3" /> Edit
                            </Button>
                            <ConfirmDialog
                              title="Delete endpoint?"
                              description={describeDelete(ep)}
                              onConfirm={() => deleteMut.mutate({ type: ep.model_type, name: ep.name })}
                            >
                              <Button size="sm" variant="outline" className="text-destructive">
                                <Trash2 className="mr-1 h-3 w-3" /> Delete
                              </Button>
                            </ConfirmDialog>
                          </div>
                        </CardContent>
                      </Card>
                    );
                  })}
                {endpoints.filter((ep) => ep.model_type === type).length === 0 && (
                  <p className="col-span-full text-center py-8 text-muted-foreground">
                    No {type} endpoints configured.
                  </p>
                )}
                </div>
              </>
            )}
          </TabsContent>
        ))}
      </Tabs>

      {pendingDefault && (
        <SetDefaultEmbedderDialog
          endpoint={pendingDefault}
          current={endpoints.find((e) => e.model_type === "embedder" && e.is_default) ?? null}
          onCancel={() => setPendingDefault(null)}
          onConfirm={() => {
            setPendingDefault(null);
            setDefaultMut.mutate({ type: pendingDefault.model_type, name: pendingDefault.name });
          }}
        />
      )}

      <EmbedderEditIntroDialog
        endpoint={pendingEdit}
        onCancel={() => setPendingEdit(null)}
        onCreateNew={handleCreateCopy}
        onModify={handleModifyExisting}
      />

      <EndpointDialog
        open={dialogOpen}
        onOpenChange={(v) => {
          setDialogOpen(v);
          if (!v) setSeed(null);
        }}
        editing={editing}
        liveEditing={
          editing
            ? (endpoints.find((ep) => ep.model_type === editing.model_type && ep.name === editing.name) ?? editing)
            : null
        }
        llmDefaults={endpoints.find((ep) => ep.model_type === "llm") ?? null}
        seed={seed}
        activeTab={activeTab}
        onCreate={(data) => createMut.mutate(data)}
        onUpdate={(type, name, data) => updateMut.mutate({ type, name, data })}
        loading={createMut.isPending || updateMut.isPending}
      />
    </div>
  );
}

/** Says what a new default embedder does and does not move (#762).
 *
 *  Partitions that already hold files keep the embedder that built their
 *  vectors — the server writes it down by name as the default changes — so
 *  nothing breaks and nothing needs acknowledging. What the admin needs is the
 *  scope, because "set default" reads as if it moved everything, and the list
 *  of partitions staying behind. Neither is a warning, so neither reads as one.
 */
function SetDefaultEmbedderDialog({
  endpoint,
  current,
  onCancel,
  onConfirm,
}: {
  endpoint: ModelEndpointResponse;
  current: ModelEndpointResponse | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  // Every partition with files that resolves to the current default: the ones
  // naming it and the ones on the alias, which is exactly who stays on it.
  const usageQuery = useQuery({
    queryKey: ["model-endpoint-indexed-usage", "embedder", current?.name],
    queryFn: () => getModelEndpointIndexedUsage("embedder", current!.name),
    enabled: current !== null,
  });
  const staying = usageQuery.data?.partitions ?? [];

  return (
    <AlertDialog open onOpenChange={(next) => (next ? undefined : onCancel())}>
      <AlertDialogContent className="max-h-[calc(100vh-4rem)] overflow-y-auto">
        <AlertDialogHeader>
          <AlertDialogTitle>Make {endpoint.name} the default embedder?</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-3">
              <p className="text-sm">
                Only new partitions, and empty ones that follow the default, will use {endpoint.name}. Partitions
                that already hold files keep their embedder.
              </p>
              {current && usageQuery.isLoading && (
                <p className="text-sm text-muted-foreground">Checking which partitions already hold files&hellip;</p>
              )}
              {current && usageQuery.isError && (
                <div className="flex items-center justify-between gap-3">
                  <p className="text-sm text-destructive">
                    Could not check which partitions keep {current.name}.
                  </p>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => usageQuery.refetch()}
                    disabled={usageQuery.isFetching}
                  >
                    {usageQuery.isFetching ? "Retrying..." : "Retry"}
                  </Button>
                </div>
              )}
              {current && staying.length > 0 && (
                <div className="rounded-md border p-3">
                  <p className="mb-1.5 text-sm font-medium">Staying on {current.name}</p>
                  <ul className="space-y-0.5 text-sm">
                    {staying.map((p) => (
                      <li key={p.partition}>
                        <span className="font-mono">{p.partition}</span>
                        <span className="text-muted-foreground">
                          {" "}
                          &mdash; {p.file_count} file{p.file_count === 1 ? "" : "s"}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter className="gap-2">
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <Button onClick={onConfirm}>Set default</Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** Offered before an embedder's edit form opens, not after (#762 C).
 *
 *  An embedder is not a setting — it is the function that built every vector
 *  already stored for the partitions using it. Editing it in place keeps the
 *  name, keeps the partition references, and silently changes what queries are
 *  compared against. A second endpoint has none of those consequences, so it is
 *  offered first, while it is still a choice rather than a correction.
 */
function EmbedderEditIntroDialog({
  endpoint,
  onCancel,
  onCreateNew,
  onModify,
}: {
  endpoint: ModelEndpointResponse | null;
  onCancel: () => void;
  onCreateNew: (ep: ModelEndpointResponse) => void;
  onModify: (ep: ModelEndpointResponse) => void;
}) {
  if (!endpoint) return null;
  return (
    <AlertDialog open onOpenChange={(next) => (next ? undefined : onCancel())}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>An embedder owns its vector space</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-3">
              <p>
                Every vector indexed through <span className="font-medium">{endpoint.name}</span> lives
                in the space that model defines. Queries are only comparable to those vectors while
                the same model answers them.
              </p>
              <p>
                Changing the URL or the model here repoints existing partitions in place. If the new
                model is not the same one, already-indexed files stay in the old space and retrieval
                degrades silently &mdash; nothing errors, results just stop being meaningful.
              </p>
              <p>
                If you are switching to a different model, add a second endpoint, then move each
                partition to it with <span className="font-medium">Change embedder</span> on the
                partition&apos;s page: its files are re-embedded before searches switch over. Editing is
                for correcting a URL or a typo that points at the same model.
              </p>
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter className="gap-2">
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <Button variant="outline" onClick={() => onModify(endpoint)}>
            Modify this one
          </Button>
          <Button onClick={() => onCreateNew(endpoint)}>Create a new one</Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function EndpointDialog({
  open,
  onOpenChange,
  editing,
  liveEditing,
  llmDefaults,
  seed,
  activeTab,
  onCreate,
  onUpdate,
  loading,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  editing: ModelEndpointResponse | null;
  /** `editing` as the list currently has it, so the budget placeholders follow
   *  a detection that lands while the dialog is open. */
  liveEditing: ModelEndpointResponse | null;
  /** Any LLM endpoint, for the deployment defaults a new endpoint starts on. */
  llmDefaults: ModelEndpointResponse | null;
  /** Endpoint a prefilled create is copying from, with the free name suggested
   *  for the copy; null for a blank create. */
  seed: { from: ModelEndpointResponse; name: string } | null;
  activeTab: string;
  onCreate: (data: CreateModelEndpointRequest) => void;
  onUpdate: (type: string, name: string, data: UpdateModelEndpointRequest) => void;
  loading: boolean;
}) {
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  // The URL as it stood when the admin last left the field. Discovery probes
  // this one, never a URL still being typed: the probe carries the API key,
  // and a half-typed host is someone else's.
  const [committedEndpoint, setCommittedEndpoint] = useState("");
  const [modelName, setModelName] = useState("");
  const [batchSize, setBatchSize] = useState("32");
  const [timeout, setTimeout] = useState("30");
  const [apiKey, setApiKey] = useState("");
  // The key as it stood when the admin last left its field. Discovery waits for
  // it, so a key being typed is never tried against the endpoint piece by piece.
  const [committedApiKey, setCommittedApiKey] = useState("");
  // The URL a typed key goes with: the one committed when the key was entered,
  // or the first one committed after it. null while the key is being edited.
  const [keyEndpoint, setKeyEndpoint] = useState<string | null>(null);
  const [maxContextSize, setMaxContextSize] = useState("");
  const [maxOutputTokens, setMaxOutputTokens] = useState("");
  const [languageHint, setLanguageHint] = useState("");
  const [mossSpeakerAware, setMossSpeakerAware] = useState(false);
  const [vendor, setVendor] = useState("");
  // What `vendor` was when the form opened — the baseline the save's stamped
  // `implementation` is compared against. A ref, not state: it is read at
  // submit time and must never re-render the form.
  const initialVendorRef = useRef("");
  const [extraJson, setExtraJson] = useState("{}");

  // The backend trims `name` before validating it (and before persisting it),
  // so validate — and submit — the same trimmed value here, not the raw
  // input; otherwise e.g. " gpt-4.1 " would be accepted by the backend but
  // blocked by this form. Checked client-side too because the resulting 422
  // has no readable message — FastAPI's validation `detail` is a list, and
  // ApiError only unwraps string detail.
  const trimmedName = name.trim();
  const nameError =
    trimmedName !== "" && trimmedName.length > NAME_MAX_LENGTH
      ? `Name must be at most ${NAME_MAX_LENGTH} characters.`
      : trimmedName !== "" && !NAME_PATTERN.test(trimmedName)
        ? "Name must start/end with a letter or digit, and contain only letters, digits, '.', '_', or '-' — it's used in the endpoint's URL path."
        : null;

  const modelType = (editing?.model_type ?? seed?.from.model_type ?? activeTab) as ModelType;
  // LLM token-budget fields (max context / max output) apply to LLM endpoints only.
  const isLlm = modelType === "llm";
  // What a blank budget falls back to: the endpoint's own detected window when
  // editing; a new endpoint is only probed once saved, so it starts on the
  // deployment defaults.
  const contextFallback: LlmBudget = liveEditing
    ? llmContextSize(liveEditing, { ignoreSet: true })
    : { value: llmDefaults?.default_max_llm_context_size ?? null, source: "default" };
  const outputFallback: LlmBudget = liveEditing
    ? llmOutputTokens(liveEditing, { ignoreSet: true })
    : { value: llmDefaults?.default_max_output_tokens ?? null, source: "default" };
  const isStt = modelType === "stt";
  const sttValidationTimeout = isStt ? timeout : null;
  const sttValidationLanguageHint = isStt ? languageHint.trim() : null;
  const sttValidationMossSpeakerAware = isStt ? mossSpeakerAware : null;
  const validationDraft = JSON.stringify([
    endpoint,
    modelType,
    modelName,
    apiKey,
    extraJson,
    vendor,
    sttValidationTimeout,
    sttValidationLanguageHint,
    sttValidationMossSpeakerAware,
  ]);
  const currentValidationDraft = useRef(validationDraft);
  const validationGeneration = useRef(0);
  useLayoutEffect(() => {
    currentValidationDraft.current = validationDraft;
    validationGeneration.current += 1;
  }, [validationDraft, open, editing?.name]);
  const vendorOptions = VENDOR_OPTIONS_BY_TYPE[modelType];
  const [validated, setValidated] = useState<boolean | null>(null);
  const [validating, setValidating] = useState(false);
  const [validationMsg, setValidationMsg] = useState<string | null>(null);
  // Kept apart from validationMsg: that one is reset whenever a validated field
  // changes, which a prefilled copy does while its fields settle.
  const [copyNotice, setCopyNotice] = useState<string | null>(null);
  const [revealedApiKey, setRevealedApiKey] = useState<RevealedApiKey | null>(null);
  const [apiKeyVisible, setApiKeyVisible] = useState(false);
  const [revealingApiKey, setRevealingApiKey] = useState(false);
  const [pendingUpdate, setPendingUpdate] = useState<{
    data: UpdateModelEndpointRequest;
    changes: EndpointFieldChange[];
  } | null>(null);
  // The last model listing, tagged with the run it answers (see discoveryRun).
  const [discoveryResult, setDiscoveryResult] = useState<{
    run: string;
    models: string[];
    detail?: string | null;
    reachable: boolean;
  } | null>(null);
  const [discoveryNonce, setDiscoveryNonce] = useState(0);
  const [modelPickerOpen, setModelPickerOpen] = useState(false);

  useEffect(() => {
    if (!open) {
      setRevealedApiKey(null);
      setApiKeyVisible(false);
      setRevealingApiKey(false);
      // A listing answers this sitting of the form only: reopened, the form
      // asks again, as the gateway may have recovered or its key been rotated.
      setDiscoveryResult(null);
      setModelPickerOpen(false);
      return;
    }
    if (open) {
      setValidated(null);
      setValidating(false);
      setRevealedApiKey(null);
      setApiKeyVisible(false);
      setRevealingApiKey(false);
      // A prefilled create seeds from an endpoint the same way an edit does —
      // the point of offering "create a new one" is that the new one starts
      // where the old one did, so only the model has to be retyped.
      const source = editing ?? seed?.from ?? null;
      if (source) {
        const { apiKey: displayApiKey, extra: apiExtra } = splitModelEndpointApiKeyExtra(source.extra);
        // STT uses OpenAI's audio API directly, so unlike inference endpoints
        // it has no implementation/vendor selector. Preserve any advanced
        // `implementation` value in Extra rather than silently dropping it.
        const { languageHint: storedLanguageHint, extra: sttExtra } =
          source.model_type === "stt"
            ? splitModelEndpointSttLanguage(apiExtra)
            : { languageHint: "", extra: apiExtra };
        const { mossSpeakerAware: storedMossSpeakerAware, extra: mossExtra } =
          source.model_type === "stt"
            ? splitModelEndpointMossSpeakerAware(sttExtra)
            : { mossSpeakerAware: false, extra: sttExtra };
        const { implementation, extra: implExtra } =
          source.model_type === "stt"
            ? { implementation: "", extra: mossExtra }
            : splitModelEndpointImplementation(mossExtra);
        // Only carve the LLM token-budget keys out of the editable extra for LLM
        // endpoints — the budget fields are LLM-only, and splitting for non-LLM
        // types would drop any same-named keys from their extra on save (they are
        // re-merged only when isLlm below), silently deleting them.
        const { llmContext, extra: displayExtra } =
          source.model_type === "llm"
            ? splitModelEndpointLlmContext(implExtra)
            : { llmContext: { maxContextSize: "", maxOutputTokens: "" }, extra: implExtra };
        setName(editing ? source.name : (seed?.name ?? source.name));
        setEndpoint(source.endpoint);
        setCommittedEndpoint(source.endpoint);
        setModelName(source.model_name || "");
        setBatchSize(String(source.batch_size));
        setTimeout(String(source.timeout));
        // A stored key comes back redacted, so a copy cannot inherit it.
        setApiKey(editing ? displayApiKey : "");
        setCommittedApiKey(editing ? displayApiKey : "");
        setKeyEndpoint(null);
        setMaxContextSize(llmContext.maxContextSize);
        setMaxOutputTokens(llmContext.maxOutputTokens);
        setLanguageHint(storedLanguageHint);
        setMossSpeakerAware(storedMossSpeakerAware);
        setVendor(implementation || DEFAULT_VENDOR_BY_TYPE[source.model_type]);
        initialVendorRef.current = implementation || DEFAULT_VENDOR_BY_TYPE[source.model_type];
        setExtraJson(JSON.stringify(displayExtra, null, 2));
        setValidated(editing ? true : null);
        setValidationMsg(
          editing?.has_api_key
            ? "API key is stored server-side. Leave it unchanged to keep it, or type a new key to rotate it."
            : null,
        );
        setCopyNotice(
          !editing && seed?.from.has_api_key
            ? "Copied from " + seed.from.name + ". Its API key is stored server-side and cannot be copied — enter one for the new endpoint."
            : null,
        );
      } else {
        setName("");
        setEndpoint("");
        setCommittedEndpoint("");
        setCommittedApiKey("");
        setKeyEndpoint(null);
        setModelName("");
        setBatchSize(activeTab === "stt" ? "1" : "32");
        setTimeout(activeTab === "stt" ? "3600" : "30");
        setApiKey("");
        setMaxContextSize("");
        setMaxOutputTokens("");
        setLanguageHint("");
        setMossSpeakerAware(false);
        setVendor(DEFAULT_VENDOR_BY_TYPE[activeTab as ModelType]);
        setExtraJson("{}");
        setValidated(null);
        setValidationMsg(null);
        setCopyNotice(null);
      }
    }
  }, [open, editing, seed, activeTab]);

  // Reset validation when relevant fields change. The LLM token-budget fields
  // don't affect endpoint reachability, so they're deliberately excluded — the
  // raw extra is compared with them stripped out (as the textarea shows them).
  useEffect(() => {
    setValidating(false);
    const editingExtra = editing ? splitModelEndpointApiKeyExtra(editing.extra) : null;
    const editingSttFields = editingExtra
      ? editing?.model_type === "stt"
        ? splitModelEndpointSttLanguage(editingExtra.extra)
        : { languageHint: "", extra: editingExtra.extra }
      : null;
    const editingSttExtra = editingSttFields?.extra ?? null;
    const editingMossFields = editingSttExtra
      ? editing?.model_type === "stt"
        ? splitModelEndpointMossSpeakerAware(editingSttExtra)
        : { mossSpeakerAware: false, extra: editingSttExtra }
      : null;
    const editingMossExtra = editingMossFields?.extra ?? null;
    const editingImplementation =
      editingMossExtra && editing && editing.model_type !== "stt"
        ? splitModelEndpointImplementation(editingMossExtra).implementation ||
          DEFAULT_VENDOR_BY_TYPE[editing.model_type]
        : "";
    const editingImplExtra = editingMossExtra
      ? editing?.model_type === "stt"
        ? editingMossExtra
        : splitModelEndpointImplementation(editingMossExtra).extra
      : null;
    // Match the display path above: only strip the budget keys for LLM endpoints,
    // otherwise the "unchanged" comparison would treat a non-LLM endpoint's
    // same-named extra keys as already removed.
    let editingRawExtra: Record<string, unknown> | null = null;
    if (editingImplExtra) {
      editingRawExtra =
        editing?.model_type === "llm" ? splitModelEndpointLlmContext(editingImplExtra).extra : editingImplExtra;
    }
    if (
      editing &&
      endpoint === editing.endpoint &&
      (modelName || "") === (editing.model_name || "") &&
      sttValidationTimeout ===
        (editing.model_type === "stt" ? String(editing.timeout) : null) &&
      sttValidationLanguageHint ===
        (editing.model_type === "stt" ? editingSttFields?.languageHint.trim() || "" : null) &&
      sttValidationMossSpeakerAware ===
        (editing.model_type === "stt" ? editingMossFields?.mossSpeakerAware || false : null) &&
      apiKey === editingExtra?.apiKey &&
      (editing.model_type === "stt" || vendor === editingImplementation) &&
      extraJson === JSON.stringify(editingRawExtra, null, 2)
    ) {
      setValidated(true);
      setValidationMsg(
        editing.has_api_key
          ? "API key is stored server-side. Leave it unchanged to keep it, or type a new key to rotate it."
          : null,
      );
      return;
    }
    setValidated(null);
    setValidationMsg(null);
  }, [
    endpoint,
    modelName,
    sttValidationTimeout,
    sttValidationLanguageHint,
    sttValidationMossSpeakerAware,
    apiKey,
    extraJson,
    vendor,
    editing,
  ]);

  const apiKeySubmitValue = () =>
    prepareModelEndpointExtraForSubmit({ api_key: apiKey.trim() }).api_key;

  const isTypedApiKey = (value: string) => {
    const prepared = prepareModelEndpointExtraForSubmit({ api_key: value.trim() }).api_key;
    return typeof prepared === "string" && prepared !== "" && prepared !== REDACTED_SECRET;
  };

  const commitEndpoint = () => {
    setCommittedEndpoint(endpoint);
    // A key entered before any URL goes with the first URL committed after it.
    if (keyEndpoint === null && endpoint.trim() && apiKey === committedApiKey && isTypedApiKey(apiKey)) {
      setKeyEndpoint(normalizeEndpointUrl(endpoint));
    }
  };

  const commitApiKey = () => {
    setCommittedApiKey(apiKey);
    // A key just entered goes with the URL as it stands. Leaving the field
    // without editing it (tabbing through) keeps the key with its URL.
    if (keyEndpoint === null && committedEndpoint.trim()) {
      setKeyEndpoint(normalizeEndpointUrl(committedEndpoint));
    }
  };

  const shouldReuseStoredApiKey = () => {
    const preparedApiKey = apiKeySubmitValue();
    return editing?.has_api_key === true && preparedApiKey === REDACTED_SECRET;
  };

  const revealedApiKeyForEditing =
    editing &&
    revealedApiKey?.modelType === editing.model_type &&
    revealedApiKey.name === editing.name
      ? revealedApiKey.value
      : null;

  // Model discovery: ask the validate endpoint (no model name) which models the
  // URL serves, once the admin has left the URL field and the key / vendor have
  // settled. Not available for STT, whose validate call needs a model name.
  const defaultBatchSize = isStt ? 1 : 32;
  const defaultTimeout = isStt ? 3600 : 30;
  // A key typed into this form only goes with the URL it was entered for (see
  // keyEndpoint): on another URL, discovery waits for a key entered for it, or
  // for Refresh, which sends it there on purpose. The stored key goes by
  // reference (the server fills it in), and only to the saved URL. Revealing it
  // only changes what the field shows: it is still the stored key.
  const discoverySubmittedKey = apiKeySubmitValue();
  const discoveryTypedKey =
    typeof discoverySubmittedKey === "string" && discoverySubmittedKey !== REDACTED_SECRET
      ? discoverySubmittedKey
      : "";
  const discoveryKeepsStoredKey =
    !discoveryTypedKey && editing?.has_api_key === true && discoverySubmittedKey !== "";
  const storedKeyOffSavedUrl =
    discoveryKeepsStoredKey &&
    !!committedEndpoint.trim() &&
    normalizeEndpointUrl(committedEndpoint) !== normalizeEndpointUrl(editing.endpoint);
  const fieldsCommitted =
    open &&
    !isStt &&
    !!committedEndpoint.trim() &&
    normalizeEndpointUrl(endpoint) === normalizeEndpointUrl(committedEndpoint) &&
    apiKey === committedApiKey;
  const typedKeyOffItsUrl =
    !!discoveryTypedKey && keyEndpoint !== normalizeEndpointUrl(committedEndpoint);
  const typedKeyAwaitsUrl = fieldsCommitted && typedKeyOffItsUrl && keyEndpoint !== null;
  const discoveryRequest = (() => {
    if (!fieldsCommitted) return null;
    if (storedKeyOffSavedUrl || typedKeyOffItsUrl) return null;
    const base = {
      endpoint: committedEndpoint,
      model_type: modelType,
      timeout: DISCOVERY_TIMEOUT_S,
      extra: mergeModelEndpointImplementation({}, vendor),
    };
    if (discoveryTypedKey) return { ...base, api_key: discoveryTypedKey };
    if (discoveryKeepsStoredKey && editing) {
      return {
        ...base,
        stored_api_key_model_type: editing.model_type,
        stored_api_key_name: editing.name,
      };
    }
    return base;
  })();
  const discoveryKey = discoveryRequest ? JSON.stringify(discoveryRequest) : "";
  const discoveryRun = `${discoveryNonce}:${discoveryKey}`;
  // Back on the URL / key the last probe answered (A, then B, then A again):
  // that answer stands and no new probe starts, so a list is never shown beside
  // a request still in flight. Refresh starts a new run.
  const discoveryAnswered = discoveryResult?.run === discoveryRun;
  useEffect(() => {
    if (!discoveryKey || discoveryAnswered) return;
    let cancelled = false;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      let outcome: { models: string[]; detail?: string | null; reachable: boolean };
      let timedOut = false;
      const deadline = window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, DISCOVERY_DEADLINE_S * 1000);
      try {
        const res = await validateModelEndpoint(JSON.parse(discoveryKey), { signal: controller.signal });
        // A reachable endpoint that answered with an error (wrong key, no model
        // route) comes back with a detail and no list: that's a failed listing.
        const listed = !!res?.reachable && !(res.detail && !res.models_served);
        outcome = { reachable: listed, models: res?.models_served ?? [], detail: res?.detail };
      } catch (e) {
        const detail = timedOut
          ? `OpenRAG did not answer within ${DISCOVERY_DEADLINE_S} s`
          : e instanceof Error
            ? e.message
            : null;
        outcome = { reachable: false, models: [], detail };
      } finally {
        window.clearTimeout(deadline);
      }
      if (!cancelled) setDiscoveryResult({ run: `${discoveryNonce}:${discoveryKey}`, ...outcome });
    }, DISCOVERY_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [discoveryKey, discoveryNonce, discoveryAnswered]);
  const discoveryCurrent = discoveryAnswered ? discoveryResult : null;
  const discovery = {
    status: !discoveryKey
      ? "idle"
      : !discoveryCurrent
        ? "loading"
        : discoveryCurrent.reachable
          ? "ready"
          : "unreachable",
    models: discoveryCurrent?.reachable ? discoveryCurrent.models : [],
    detail: discoveryCurrent?.detail,
  };
  const discoveryHint =
    discovery.status === "loading"
      ? "Looking up the models this endpoint serves..."
      : discovery.status === "unreachable"
        ? `Couldn't list models${discovery.detail ? `: ${asSentence(discovery.detail)}` : "."} You can still type the name.`
        : discovery.status === "ready"
          ? discovery.models.length === 0
            ? "No models listed by this endpoint. Type the model name."
            : `${discovery.models.length} model${discovery.models.length === 1 ? "" : "s"} listed. Pick one or type a name.`
          : storedKeyOffSavedUrl
            ? "The stored API key is only used with the saved URL. Enter the key for this URL to list its models, or type the name."
            : typedKeyAwaitsUrl
              ? "The API key was entered for another URL. Enter the key for this URL, or press Refresh to list its models with this one."
            : "Enter the endpoint URL and API key to list its models, or type the name.";

  const fetchStoredApiKey = async ({ cache = true }: { cache?: boolean } = {}): Promise<string | null> => {
    const target = editing?.has_api_key
      ? { modelType: editing.model_type, name: editing.name }
      : null;
    if (!target) return null;
    if (revealedApiKeyForEditing) return revealedApiKeyForEditing;
    setRevealingApiKey(true);
    try {
      const result = await revealModelEndpointApiKey(target.modelType, target.name);
      if (!result.api_key) {
        toast.error("No API key is stored for this endpoint");
        return null;
      }
      if (cache) {
        setRevealedApiKey({ ...target, value: result.api_key });
      }
      return result.api_key;
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Failed to reveal API key";
      toast.error(msg);
      return null;
    } finally {
      setRevealingApiKey(false);
    }
  };

  const handleToggleApiKeyVisibility = async () => {
    if (apiKeyVisible) {
      setApiKeyVisible(false);
      setRevealedApiKey(null);
      return;
    }
    if (shouldReuseStoredApiKey()) {
      const storedApiKey = await fetchStoredApiKey();
      if (!storedApiKey) return;
      setApiKeyVisible(true);
      return;
    }
    setApiKeyVisible(true);
  };

  const handleCopyApiKey = async () => {
    const value = shouldReuseStoredApiKey() ? await fetchStoredApiKey({ cache: false }) : apiKey.trim();
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      toast.success("API key copied");
    } catch {
      toast.error("Could not copy API key");
    }
  };

  const handleValidate = async () => {
    // Draft validation: probe the values currently in the form (before saving),
    // so typos / unreachable hosts / wrong model names are caught pre-save.
    if (!endpoint.trim()) {
      toast.error("Enter an endpoint URL first");
      return;
    }
    let validationExtra: Record<string, unknown>;
    try {
      const parsedExtra: unknown = JSON.parse(extraJson);
      if (typeof parsedExtra !== "object" || parsedExtra === null || Array.isArray(parsedExtra)) {
        throw new Error("Extra must be a JSON object");
      }
      if (isStt) {
        validationExtra = mergeModelEndpointMossSpeakerAware(
          mergeModelEndpointSttLanguage(parsedExtra as Record<string, unknown>, languageHint),
          mossSpeakerAware,
        );
      } else {
        validationExtra = mergeModelEndpointImplementation(parsedExtra as Record<string, unknown>, vendor);
      }
    } catch {
      const msg = "Invalid JSON in extra field";
      setValidated(false);
      setValidationMsg(msg);
      toast.error(msg);
      return;
    }
    let apiKey: string | undefined;
    let submittedApiKey: string | undefined;
    try {
      const preparedApiKey = apiKeySubmitValue();
      submittedApiKey = typeof preparedApiKey === "string" ? preparedApiKey : undefined;
      if (submittedApiKey && submittedApiKey !== REDACTED_SECRET) {
        apiKey = submittedApiKey;
      } else if (revealedApiKeyForEditing) {
        apiKey = revealedApiKeyForEditing;
      }
    } catch {
      // invalid extra JSON is reported on save; ignore here
    }
    setValidating(true);
    setValidationMsg(null);
    const submittedDraft = currentValidationDraft.current;
    const submittedGeneration = ++validationGeneration.current;
    const validationIsCurrent = () =>
      submittedGeneration === validationGeneration.current &&
      submittedDraft === currentValidationDraft.current;
    try {
      const isClearingStoredApiKey = editing?.has_api_key === true && submittedApiKey === "";
      if (
        editing?.has_api_key === true &&
        !isClearingStoredApiKey &&
        normalizeEndpointUrl(endpoint) !== normalizeEndpointUrl(editing.endpoint) &&
        !apiKey &&
        (!submittedApiKey || submittedApiKey === REDACTED_SECRET)
      ) {
        setValidated(false);
        const msg = "Reveal or enter the API key before validating a changed endpoint URL.";
        setValidationMsg(msg);
        toast.error(msg);
        return;
      }
      const canUseStoredSecret =
        editing?.has_api_key === true &&
        !isClearingStoredApiKey &&
        !apiKey &&
        (!submittedApiKey || submittedApiKey === REDACTED_SECRET);
      const validationTimeout = numOr(timeout, isStt ? 3600 : 30);
      const res = canUseStoredSecret
        ? await validateModelEndpoint({
            endpoint,
            model_type: modelType,
            model_name: modelName || undefined,
            timeout: validationTimeout,
            extra: validationExtra,
            stored_api_key_model_type: editing.model_type,
            stored_api_key_name: editing.name,
          })
        : await validateModelEndpoint({
            endpoint,
            model_type: modelType,
            model_name: modelName || undefined,
            timeout: validationTimeout,
            extra: validationExtra,
            api_key: apiKey,
          });
      if (!validationIsCurrent()) return;
      if (!res.reachable) {
        setValidated(false);
        const msg = res.detail || "Endpoint is unreachable.";
        setValidationMsg(msg);
        toast.error(msg);
      } else if (res.model_found === false) {
        // Reachable, but the typed model isn't actually served — the common trap
        // (a green "reachable" hides a wrong model name). Block save and show
        // what the endpoint actually advertises.
        setValidated(false);
        const served = res.models_served?.length ? res.models_served.join(", ") : "none advertised";
        const msg = `Reachable, but "${modelName}" isn't served. Available: ${served}`;
        setValidationMsg(msg);
        toast.warning(msg);
      } else if (isStt && res.transcription_supported !== true) {
        setValidated(false);
        const msg = res.detail || "Could not verify OpenAI-compatible audio transcription support.";
        setValidationMsg(msg);
        toast.warning(msg);
      } else {
        setValidated(true);
        const msg = res.model_found
          ? isStt
            ? `Reachable — "${modelName}" is served and audio transcription is supported.`
            : `Reachable — "${modelName}" is served.`
          : isStt
            ? "Reachable — audio transcription is supported (couldn't confirm the model list)."
            : res.detail || "Reachable (couldn't confirm the model list).";
        setValidationMsg(msg);
        toast.success(msg);
      }
    } catch (e) {
      if (!validationIsCurrent()) return;
      setValidated(false);
      const msg = e instanceof Error ? e.message : "Validation failed";
      setValidationMsg(msg);
      toast.error(msg);
    } finally {
      if (validationIsCurrent()) {
        setValidating(false);
      }
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (nameError) {
      toast.error(nameError);
      return;
    }
    let extra: Record<string, unknown> = {};
    try {
      extra = mergeModelEndpointApiKeyExtra(JSON.parse(extraJson), apiKey, {
        clearApiKey: editing?.has_api_key === true,
      });
    } catch {
      toast.error("Invalid JSON in extra field");
      return;
    }
    if (isLlm) {
      extra = mergeModelEndpointLlmContext(extra, { maxContextSize, maxOutputTokens });
    }
    if (isStt) {
      extra = mergeModelEndpointSttLanguage(extra, languageHint);
      // MOSS deployments can expose arbitrary served-model aliases, so this
      // behavior is an explicit endpoint setting rather than a model-name check.
      extra = mergeModelEndpointMossSpeakerAware(extra, mossSpeakerAware);
    } else {
      extra = mergeModelEndpointImplementation(extra, vendor);
    }

    if (editing) {
      const updateData: UpdateModelEndpointRequest = {
        endpoint,
        model_name: modelName || undefined,
        batch_size: intOr(batchSize, isStt ? 1 : 32),
        timeout: numOr(timeout, isStt ? 3600 : 30),
        extra,
      };
      if (trimmedName !== editing.name) {
        updateData.name = trimmedName;
      }
      // Confirm before writing, showing what actually changed. Nothing changed
      // means nothing to confirm — a dialog there would only be noise. The
      // vendor the form opened with is the baseline for `implementation`: the
      // save stamps it even when the stored endpoint never carried one.
      const changes = diffEndpointUpdate(editing, updateData, { implementation: initialVendorRef.current });
      if (changes.length > 0) {
        setPendingUpdate({ data: updateData, changes });
        return;
      }
      onUpdate(editing.model_type, editing.name, updateData);
    } else {
      onCreate({
        name: trimmedName,
        model_type: activeTab as ModelType,
        endpoint,
        model_name: modelName || undefined,
        batch_size: intOr(batchSize, isStt ? 1 : 32),
        timeout: numOr(timeout, isStt ? 3600 : 30),
        extra,
      });
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {editing ? `Edit ${editing.name}` : `Add ${activeTab} endpoint`}
          </DialogTitle>
          <DialogDescription>
            Configure the endpoint connection and stored credentials.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <LabelWithInfo
              label="Name"
              tooltip="Unique name for this endpoint within its type. It's how the endpoint is referenced elsewhere — e.g. as a partition's chat LLM preset — so choose a stable, descriptive name."
            />
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
            {nameError && <p className="text-xs text-destructive">{nameError}</p>}
          </div>
          {!isStt && (
          <div className="space-y-2">
            <Label>Vendor</Label>
            <Select value={vendor} onValueChange={setVendor}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {vendorOptions.map((option) => (
                  <SelectItem key={option} value={option} className="py-2">
                    <span className="flex items-center gap-2.5 text-base">
                      <VendorIcon vendor={option} className="h-5 w-5" />
                      {option}
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Client implementation used to talk to this endpoint.
            </p>
          </div>
          )}
          <div className="space-y-2">
            {isStt ? (
              <LabelWithInfo
                label="Endpoint URL"
                tooltip="OpenAI-compatible base URL including /v1. OpenRAG sends transcription requests to /audio/transcriptions under this URL."
              />
            ) : (
              <Label>Endpoint URL</Label>
            )}
            <Input
              value={endpoint}
              onChange={(e) => setEndpoint(e.target.value)}
              onBlur={commitEndpoint}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitEndpoint();
              }}
              required
            />
          </div>
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label>API key</Label>
              <div className="flex items-center gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  onClick={handleToggleApiKeyVisibility}
                  disabled={revealingApiKey || (!apiKey.trim() && !editing?.has_api_key)}
                  aria-label={apiKeyVisible ? "Hide API key" : "Show API key"}
                  title={apiKeyVisible ? "Hide API key" : "Show API key"}
                >
                  {revealingApiKey ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : apiKeyVisible ? (
                    <EyeOff className="h-4 w-4" />
                  ) : (
                    <Eye className="h-4 w-4" />
                  )}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  onClick={handleCopyApiKey}
                  disabled={revealingApiKey || (!apiKey.trim() && !editing?.has_api_key)}
                  aria-label="Copy API key"
                  title="Copy API key"
                >
                  <Copy className="h-4 w-4" />
                </Button>
              </div>
            </div>
            <Input
              className="font-mono"
              type={apiKeyVisible ? "text" : "password"}
              value={apiKeyVisible && revealedApiKeyForEditing ? revealedApiKeyForEditing : apiKey}
              onChange={(e) => {
                setApiKey(e.target.value);
                setRevealedApiKey(null);
                setKeyEndpoint(null);
              }}
              onBlur={commitApiKey}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitApiKey();
              }}
              placeholder={editing?.has_api_key ? "Stored API key" : "Optional API key"}
              autoComplete="off"
            />
            <p className="text-xs text-muted-foreground">
              {editing?.has_api_key
                ? "Leave unchanged to keep the stored key, or type a new key to rotate it."
                : "Stored in the endpoint extra payload when provided."}
            </p>
          </div>
          <div className="space-y-2">
            {isStt ? (
              <LabelWithInfo
                label="Model Name"
                tooltip="Required by OpenAI-compatible transcription APIs. It must match the model advertised by the endpoint, for example moss-transcribe-diarize."
              />
            ) : (
              <Label>Model Name</Label>
            )}
            <div className="flex items-center gap-1">
              <Input
                value={modelName}
                onChange={(e) => setModelName(e.target.value)}
                required={isStt}
                autoComplete="off"
              />
              {!isStt && (
                <>
                  <Popover modal open={modelPickerOpen} onOpenChange={setModelPickerOpen}>
                    <PopoverTrigger asChild>
                      <Button
                        type="button"
                        variant="outline"
                        size="icon"
                        disabled={discovery.models.length === 0}
                        aria-label="Pick a listed model"
                        title="Pick a listed model"
                      >
                        <ChevronsUpDown className="h-4 w-4" />
                      </Button>
                    </PopoverTrigger>
                    <PopoverContent align="end" className="w-80 p-0">
                      <Command>
                        <CommandInput placeholder="Filter models..." />
                        <CommandList>
                          <CommandEmpty>No matching model.</CommandEmpty>
                          {discovery.models.map((id) => (
                            <CommandItem
                              key={id}
                              value={id}
                              onSelect={() => {
                                setModelName(id);
                                setModelPickerOpen(false);
                              }}
                            >
                              {id}
                            </CommandItem>
                          ))}
                        </CommandList>
                      </Command>
                    </PopoverContent>
                  </Popover>
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    onClick={() => {
                      // Refresh on a URL the typed key wasn't entered for sends
                      // it there on purpose: the key now goes with this URL.
                      if (typedKeyAwaitsUrl) setKeyEndpoint(normalizeEndpointUrl(committedEndpoint));
                      setDiscoveryNonce((n) => n + 1);
                    }}
                    disabled={(!discoveryRequest && !typedKeyAwaitsUrl) || discovery.status === "loading"}
                    aria-label="Refresh model list"
                    title="Refresh model list"
                  >
                    <RefreshCw className={`h-4 w-4 ${discovery.status === "loading" ? "animate-spin" : ""}`} />
                  </Button>
                </>
              )}
            </div>
            {!isStt && <p className="text-xs text-muted-foreground">{discoveryHint}</p>}
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              {isStt ? (
                <LabelWithInfo
                  label="Concurrent requests per worker"
                  tooltip="Maximum parallel transcription requests from each OpenRAG worker. Multiple workers or replicas each have their own limit, so size it for the transcription server's total capacity."
                />
              ) : (
                <Label>Batch Size</Label>
              )}
              <Input
                type="number"
                min="1"
                value={batchSize}
                onChange={(e) => setBatchSize(e.target.value)}
                placeholder={String(defaultBatchSize)}
              />
              <p className="text-xs text-muted-foreground">Default: {defaultBatchSize}</p>
            </div>
            <div className="space-y-2">
              <Label>Timeout (s)</Label>
              <Input
                type="number"
                step="0.1"
                value={timeout}
                onChange={(e) => setTimeout(e.target.value)}
                placeholder={String(defaultTimeout)}
              />
              <p className="text-xs text-muted-foreground">Default: {defaultTimeout}</p>
            </div>
          </div>
          {isLlm && (
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <LabelWithInfo
                  label="Max context size"
                  tooltip="Context window of the model behind this endpoint. Leave it blank to use the max_model_len the endpoint reports on /v1/models (vLLM does; most gateways and hosted APIs don't), else the system default (MAX_LLM_CONTEXT_SIZE) — the placeholder shows which applies. Requests whose prompt + max_tokens exceed it are rejected with a 413, and the documents given to the LLM are cut to fit it, so set it when the endpoint reports none."
                />
                <Input
                  type="number"
                  min="1"
                  step="1"
                  value={maxContextSize}
                  onChange={(e) => setMaxContextSize(e.target.value)}
                  placeholder={budgetPlaceholder(contextFallback)}
                />
              </div>
              <div className="space-y-2">
                <LabelWithInfo
                  label="Max output tokens"
                  tooltip="Default output-token budget (max_tokens) applied to chat completions answered by this endpoint when the request doesn't set one explicitly."
                />
                <Input
                  type="number"
                  min="1"
                  step="1"
                  value={maxOutputTokens}
                  onChange={(e) => setMaxOutputTokens(e.target.value)}
                  placeholder={budgetPlaceholder(outputFallback)}
                />
              </div>
              <p className="col-span-2 text-xs text-muted-foreground">
                Token budgets for this endpoint. Leave blank to use the value shown. Applied whenever this
                endpoint answers a request — as the default LLM, or as a partition&apos;s chat LLM preset.
              </p>
            </div>
          )}
          {isStt && (
            <div className="space-y-2">
              <LabelWithInfo
                label="Language hint (optional)"
                tooltip="A language code such as fr, en, or ar. When set, it is sent as the transcription language and takes precedence over the optional Whisper language detector. Leave blank for automatic detection."
              />
              <Input
                value={languageHint}
                onChange={(e) => setLanguageHint(e.target.value)}
                placeholder="fr"
                autoComplete="off"
              />
            </div>
          )}
          {isStt && (
            <div className="flex items-center justify-between gap-4 rounded-md border p-3">
              <div className="space-y-1">
                <div className="flex items-center gap-1.5">
                  <LabelWithInfo
                    label="Speaker-aware MOSS transcript"
                    tooltip="Normalize complete MOSS diarized responses after transcription. This also works with a served-model alias and is never sent to the provider."
                  />
                  <NewBadge feature="models.stt.moss_speaker_aware" />
                </div>
                <p className="text-xs text-muted-foreground">
                  Remove timecodes and keep speaker labels only when MOSS identifies more than one speaker.
                </p>
              </div>
              <Switch
                checked={mossSpeakerAware}
                onCheckedChange={setMossSpeakerAware}
                aria-label="Enable speaker-aware MOSS transcript normalization"
              />
            </div>
          )}
          <div className="space-y-2">
            <Label>Extra (JSON)</Label>
            <Textarea
              className="font-mono text-sm"
              value={extraJson}
              onChange={(e) => setExtraJson(e.target.value)}
              rows={4}
            />
            <p className="text-xs text-muted-foreground">
              {isStt
                ? "Advanced non-secret request-body options sent to the transcription provider. The language hint, MOSS speaker setting, and API key are handled separately above."
                : "Non-secret endpoint options only. The API key is handled separately above."}
            </p>
          </div>
          {copyNotice && <p className="text-xs text-amber-600 dark:text-amber-500">{copyNotice}</p>}
          {validationMsg && (
            <p
              className={`text-xs ${
                validated ? "text-muted-foreground" : "text-amber-600 dark:text-amber-500"
              }`}
            >
              {validationMsg}
            </p>
          )}

          <DialogFooter className="gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={handleValidate}
              disabled={validating || !endpoint.trim()}
            >
              {validating ? (
                <Loader2 className="mr-1 h-4 w-4 animate-spin" />
              ) : validated === true ? (
                <CheckCircle className="mr-1 h-4 w-4 text-green-500" />
              ) : validated === false ? (
                <XCircle className="mr-1 h-4 w-4 text-red-500" />
              ) : null}
              Validate
            </Button>
            <Button
              type="submit"
              disabled={loading || validated !== true || Boolean(nameError)}
            >
              {loading ? "Saving..." : editing ? "Update" : "Create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>

      {editing && pendingUpdate && (
        <EndpointUpdateConfirmDialog
          editing={editing}
          changes={pendingUpdate.changes}
          loading={loading}
          onCancel={() => setPendingUpdate(null)}
          onConfirm={() => {
            const { data, changes } = pendingUpdate;
            setPendingUpdate(null);
            // The dialog only enables Update for a material change once the
            // user ticked the acknowledgement, which is what the server asks for.
            onUpdate(
              editing.model_type,
              editing.name,
              hasMaterialChange(changes) ? { ...data, acknowledge_indexed_data: true } : data,
            );
          }}
        />
      )}
    </Dialog>
  );
}

/** Last stop before an endpoint edit is written (#762 C).
 *
 *  Shows what changes from what, and — when the change can move the vector
 *  space — how much already-indexed data rides on this endpoint, then holds the
 *  confirm behind an explicit acknowledgement. The checkbox appears only for
 *  material changes: demanding one to alter a timeout would teach people to
 *  tick it unread, which is precisely the habit that makes it worthless on the
 *  edit that actually matters.
 */
function EndpointUpdateConfirmDialog({
  editing,
  changes,
  loading,
  onCancel,
  onConfirm,
}: {
  editing: ModelEndpointResponse;
  changes: EndpointFieldChange[];
  loading: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const material = hasMaterialChange(changes);
  // Drift is a file recording another model, so only a model change makes the
  // affected files show as drifted and re-embeddable on this endpoint.
  const modelChanged = changes.some((c) => c.field === "model_name");
  const [acknowledged, setAcknowledged] = useState(false);

  // Only worth asking for when it changes what the dialog says. The count is
  // read at confirm time so it reflects the partition state right now.
  const usageQuery = useQuery({
    queryKey: ["model-endpoint-indexed-usage", editing.model_type, editing.name],
    queryFn: () => getModelEndpointIndexedUsage(editing.model_type as ModelType, editing.name),
    enabled: material,
  });
  const usage = usageQuery.data;
  // The count is what the acknowledgement is about, so a material edit waits
  // for it: confirming while it loads, or after it failed, acknowledges a
  // number nobody was shown.
  const usageKnown = !material || usageQuery.isSuccess;

  return (
    <AlertDialog open onOpenChange={(next) => (next ? undefined : onCancel())}>
      {/* The change list and the indexed-usage table both grow with the
          deployment, so the dialog scrolls rather than pushing its own buttons
          off a short viewport. */}
      <AlertDialogContent className="max-h-[calc(100vh-4rem)] overflow-y-auto">
        <AlertDialogHeader>
          <AlertDialogTitle>
            {material ? "This changes the vector space" : "Confirm changes"}
          </AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-4">
              <ul className="space-y-1.5">
                {changes.map((c) => (
                  <li key={c.field} className="text-sm">
                    <span className="text-muted-foreground">{c.label}</span>{" "}
                    <span className="font-mono">{c.from}</span>
                    <span className="text-muted-foreground"> &rarr; </span>
                    <span className={c.material ? "font-mono font-medium text-amber-700 dark:text-amber-100" : "font-mono"}>
                      {c.to}
                    </span>
                  </li>
                ))}
              </ul>

              {material && (
                <>
                  {usageQuery.isLoading && (
                    <p className="text-sm text-muted-foreground">Checking what is indexed&hellip;</p>
                  )}
                  {usageQuery.isError && (
                    <div className="flex items-center justify-between gap-3">
                      <p className="text-sm text-destructive">
                        Could not check what is indexed with this endpoint. The update waits until it is
                        known.
                      </p>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => usageQuery.refetch()}
                        disabled={usageQuery.isFetching}
                      >
                        {usageQuery.isFetching ? "Retrying..." : "Retry"}
                      </Button>
                    </div>
                  )}
                  {usage && usage.total_files > 0 && (
                    <div className="rounded-md border border-amber-200 bg-amber-50 p-3 dark:border-amber-900/60 dark:bg-amber-950/30">
                      <p className="mb-1.5 text-sm font-medium">
                        {usage.total_files} indexed file{usage.total_files === 1 ? "" : "s"} were built
                        with this endpoint
                      </p>
                      <ul className="space-y-0.5 text-sm">
                        {usage.partitions.map((p) => (
                          <li key={p.partition}>
                            <span className="font-mono">{p.partition}</span>
                            <span className="text-muted-foreground">
                              {" "}
                              &mdash; {p.file_count} file{p.file_count === 1 ? "" : "s"}
                            </span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {usage && usage.total_files === 0 && (
                    <p className="text-sm text-muted-foreground">
                      Nothing is indexed against this endpoint yet, so no stored vectors are at stake.
                    </p>
                  )}

                  <ul className="list-disc space-y-1.5 pl-5 text-sm">
                    <li>
                      If the new model is not the same one &mdash; a different model, a different
                      dimensionality, even a different quantization &mdash; it produces different
                      vectors, and queries stop being comparable to what is already stored.
                    </li>
                    <li>
                      Nothing errors. Search keeps returning results, ranked in a space the files were
                      never built in, so the damage is invisible until someone notices the answers are
                      wrong.
                    </li>
                    <li>
                      This takes effect on the next query &mdash; no restart, no re-embed. Existing
                      vectors are not rebuilt.
                    </li>
                    <li>
                      If it really is a different model, add a second endpoint and move partitions to
                      it with <span className="font-medium">Change embedder</span> on each
                      partition&apos;s page instead: the files are re-embedded first, and searches keep
                      working until they are.
                    </li>
                    <li>
                      {modelChanged ? (
                        <>
                          If you go ahead anyway, each affected partition can re-embed its drifted files
                          afterwards from its page.
                        </>
                      ) : (
                        <>
                          The model name stays the same, so nothing marks these files as drifted afterwards,
                          and re-embedding onto this endpoint skips them. Rebuilding their vectors takes a
                          second endpoint and <span className="font-medium">Change embedder</span>.
                        </>
                      )}
                    </li>
                  </ul>

                  <label className="flex items-start gap-2 text-sm font-medium">
                    <Checkbox
                      checked={acknowledged}
                      onCheckedChange={(v) => setAcknowledged(v === true)}
                      aria-label="I understand the consequences"
                      className="mt-0.5"
                    />
                    <span>I understand the consequences for already-indexed data.</span>
                  </label>
                </>
              )}
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter className="gap-2">
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <Button onClick={onConfirm} disabled={loading || !usageKnown || (material && !acknowledged)}>
            {loading ? "Saving..." : "Update"}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
