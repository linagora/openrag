import { useEffect, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
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
} from "lucide-react";
import {
  listModelEndpoints,
  createModelEndpoint,
  updateModelEndpoint,
  deleteModelEndpoint,
  DEFAULT_VENDOR_BY_TYPE,
  mergeModelEndpointApiKeyExtra,
  mergeModelEndpointImplementation,
  mergeModelEndpointLlmContext,
  prepareModelEndpointExtraForSubmit,
  revealModelEndpointApiKey,
  REDACTED_SECRET,
  setDefaultModelEndpoint,
  splitModelEndpointApiKeyExtra,
  splitModelEndpointImplementation,
  splitModelEndpointLlmContext,
  validateModelEndpoint,
  VENDOR_OPTIONS_BY_TYPE,
} from "@/lib/api/models";
import type {
  ModelEndpointResponse,
  CreateModelEndpointRequest,
  UpdateModelEndpointRequest,
  ModelType,
} from "@/lib/api/models";
import { PageHeader } from "@/components/shared/page-header";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { VendorIcon } from "@/components/ui/vendor-icon";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { formatDate, intOr, numOr } from "@/lib/utils";

const MODEL_TYPES = ["embedder", "reranker", "llm", "vlm"] as const;

type RevealedApiKey = {
  modelType: ModelType;
  name: string;
  value: string;
};

const normalizeEndpointUrl = (value: string) => value.trim().replace(/\/+$/, "");

// System-wide fallback values (MAX_LLM_CONTEXT_SIZE / MAX_OUTPUT_TOKENS env
// vars — see core/config/endpoints.py:LLMContextConfig) shown as placeholders
// so admins can see what an unset override falls back to.
const DEFAULT_MAX_CONTEXT_SIZE = "8192";
const DEFAULT_MAX_OUTPUT_TOKENS = "1024";

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

  const { data, isLoading } = useQuery({
    queryKey: ["model-endpoints"],
    queryFn: () => listModelEndpoints(),
  });

  const createMut = useMutation({
    mutationFn: (req: CreateModelEndpointRequest) => createModelEndpoint(req),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["model-endpoints"] });
      toast.success("Model endpoint created");
      setDialogOpen(false);
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
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["model-endpoints"] });
      toast.success("Default endpoint updated");
    },
    onError: (e) => toast.error(e.message),
  });

  const endpoints = data ?? [];

  const handleOpenCreate = () => {
    setEditing(null);
    setDialogOpen(true);
  };

  const handleOpenEdit = (ep: ModelEndpointResponse) => {
    setEditing(ep);
    setDialogOpen(true);
  };

  return (
    <div>
      <PageHeader
        title="Model Endpoints"
        description="Manage embedder, reranker, LLM, and VLM endpoints"
        actions={
          <Button onClick={handleOpenCreate}>
            <Plus className="mr-2 h-4 w-4" /> Add Endpoint
          </Button>
        }
      />

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          {MODEL_TYPES.map((t) => (
            <TabsTrigger key={t} value={t} className="capitalize">
              {t}
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
            ) : (
              <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                {endpoints
                  .filter((ep) => ep.model_type === type)
                  .map((ep) => {
                    const isDefault = ep.is_default;
                    return (
                      <Card key={`${ep.model_type}-${ep.name}`}>
                        <CardHeader className="pb-3">
                          <div className="flex items-center justify-between gap-2">
                            <CardTitle className="text-base truncate min-w-0">
                              {ep.name}
                            </CardTitle>
                            <div className="flex items-center gap-1.5 shrink-0">
                              {isDefault && (
                                <Badge variant="secondary" className="text-xs">Default</Badge>
                              )}
                            </div>
                          </div>
                          {ep.model_name && (
                            <CardDescription className="font-mono text-xs truncate">
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
                            <span className="text-muted-foreground">Batch Size</span>
                            <span>{ep.batch_size}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-muted-foreground">Timeout</span>
                            <span>{ep.timeout}s</span>
                          </div>
                          <div className="text-xs text-muted-foreground">
                            Updated {formatDate(ep.updated_at)}
                          </div>
                          <div className="flex flex-wrap gap-2 pt-2">
                            {!isDefault && (
                              <Button
                                size="sm"
                                variant="outline"
                                onClick={() => setDefaultMut.mutate({ type: ep.model_type, name: ep.name })}
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
                              description={`This will delete "${ep.name}". Partitions referencing it will break.`}
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
            )}
          </TabsContent>
        ))}
      </Tabs>

      <EndpointDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        editing={editing}
        activeTab={activeTab}
        onCreate={(data) => createMut.mutate(data)}
        onUpdate={(type, name, data) => updateMut.mutate({ type, name, data })}
        loading={createMut.isPending || updateMut.isPending}
      />
    </div>
  );
}

function EndpointDialog({
  open,
  onOpenChange,
  editing,
  activeTab,
  onCreate,
  onUpdate,
  loading,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  editing: ModelEndpointResponse | null;
  activeTab: string;
  onCreate: (data: CreateModelEndpointRequest) => void;
  onUpdate: (type: string, name: string, data: UpdateModelEndpointRequest) => void;
  loading: boolean;
}) {
  const [name, setName] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [modelName, setModelName] = useState("");
  const [batchSize, setBatchSize] = useState("32");
  const [timeout, setTimeout] = useState("30");
  const [apiKey, setApiKey] = useState("");
  const [maxContextSize, setMaxContextSize] = useState("");
  const [maxOutputTokens, setMaxOutputTokens] = useState("");
  const [vendor, setVendor] = useState("");
  const [extraJson, setExtraJson] = useState("{}");

  const modelType = (editing ? editing.model_type : activeTab) as ModelType;
  // LLM token-budget fields (max context / max output) apply to LLM endpoints only.
  const isLlm = modelType === "llm";
  const vendorOptions = VENDOR_OPTIONS_BY_TYPE[modelType];
  const [validated, setValidated] = useState<boolean | null>(null);
  const [validating, setValidating] = useState(false);
  const [validationMsg, setValidationMsg] = useState<string | null>(null);
  const [revealedApiKey, setRevealedApiKey] = useState<RevealedApiKey | null>(null);
  const [apiKeyVisible, setApiKeyVisible] = useState(false);
  const [revealingApiKey, setRevealingApiKey] = useState(false);

  useEffect(() => {
    if (!open) {
      setRevealedApiKey(null);
      setApiKeyVisible(false);
      setRevealingApiKey(false);
      return;
    }
    if (open) {
      setValidated(null);
      setValidating(false);
      setRevealedApiKey(null);
      setApiKeyVisible(false);
      setRevealingApiKey(false);
      if (editing) {
        const { apiKey: displayApiKey, extra: apiExtra } = splitModelEndpointApiKeyExtra(editing.extra);
        const { implementation, extra: implExtra } = splitModelEndpointImplementation(apiExtra);
        const { llmContext, extra: displayExtra } = splitModelEndpointLlmContext(implExtra);
        setName(editing.name);
        setEndpoint(editing.endpoint);
        setModelName(editing.model_name || "");
        setBatchSize(String(editing.batch_size));
        setTimeout(String(editing.timeout));
        setApiKey(displayApiKey);
        setMaxContextSize(llmContext.maxContextSize);
        setMaxOutputTokens(llmContext.maxOutputTokens);
        setVendor(implementation || DEFAULT_VENDOR_BY_TYPE[editing.model_type]);
        setExtraJson(JSON.stringify(displayExtra, null, 2));
        setValidated(true);
        setValidationMsg(
          editing.has_api_key
            ? "API key is stored server-side. Leave it unchanged to keep it, or type a new key to rotate it."
            : null,
        );
      } else {
        setName("");
        setEndpoint("");
        setModelName("");
        setBatchSize("32");
        setTimeout("30");
        setApiKey("");
        setMaxContextSize("");
        setMaxOutputTokens("");
        setVendor(DEFAULT_VENDOR_BY_TYPE[activeTab as ModelType]);
        setExtraJson("{}");
        setValidated(null);
        setValidationMsg(null);
      }
    }
  }, [open, editing]);

  // Reset validation when relevant fields change. The LLM token-budget fields
  // don't affect endpoint reachability, so they're deliberately excluded — the
  // raw extra is compared with them stripped out (as the textarea shows them).
  useEffect(() => {
    const editingExtra = editing ? splitModelEndpointApiKeyExtra(editing.extra) : null;
    const editingImplExtra = editingExtra ? splitModelEndpointImplementation(editingExtra.extra).extra : null;
    const editingRawExtra = editingImplExtra ? splitModelEndpointLlmContext(editingImplExtra).extra : null;
    if (
      editing &&
      endpoint === editing.endpoint &&
      (modelName || "") === (editing.model_name || "") &&
      apiKey === editingExtra?.apiKey &&
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
  }, [endpoint, modelName, apiKey, extraJson, editing]);

  const apiKeySubmitValue = () =>
    prepareModelEndpointExtraForSubmit({ api_key: apiKey.trim() }).api_key;

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
      const res = canUseStoredSecret
        ? await validateModelEndpoint({
            endpoint,
            model_name: modelName || undefined,
            stored_api_key_model_type: editing.model_type,
            stored_api_key_name: editing.name,
          })
        : await validateModelEndpoint({
            endpoint,
            model_name: modelName || undefined,
            api_key: apiKey,
          });
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
      } else {
        setValidated(true);
        const msg = res.model_found
          ? `Reachable — "${modelName}" is served.`
          : res.detail || "Reachable (couldn't confirm the model list).";
        setValidationMsg(msg);
        toast.success(msg);
      }
    } catch (e) {
      setValidated(false);
      const msg = e instanceof Error ? e.message : "Validation failed";
      setValidationMsg(msg);
      toast.error(msg);
    } finally {
      setValidating(false);
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
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
    extra = mergeModelEndpointImplementation(extra, vendor);

    if (editing) {
      const updateData: UpdateModelEndpointRequest = {
        endpoint,
        model_name: modelName || undefined,
        batch_size: intOr(batchSize, 32),
        timeout: numOr(timeout, 30),
        extra,
      };
      if (name !== editing.name) {
        updateData.name = name;
      }
      onUpdate(editing.model_type, editing.name, updateData);
    } else {
      onCreate({
        name,
        model_type: activeTab as ModelType,
        endpoint,
        model_name: modelName || undefined,
        batch_size: intOr(batchSize, 32),
        timeout: numOr(timeout, 30),
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
            <Label>Name</Label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </div>
          <div className="space-y-2">
            <Label>Endpoint URL</Label>
            <Input value={endpoint} onChange={(e) => setEndpoint(e.target.value)} required />
          </div>
          <div className="space-y-2">
            <Label>Model Name</Label>
            <Input value={modelName} onChange={(e) => setModelName(e.target.value)} />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div className="space-y-2">
              <Label>Batch Size</Label>
              <Input type="number" value={batchSize} onChange={(e) => setBatchSize(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label>Timeout (s)</Label>
              <Input type="number" step="0.1" value={timeout} onChange={(e) => setTimeout(e.target.value)} />
            </div>
          </div>
          {isLlm && (
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <LabelWithInfo
                  label="Max context size"
                  tooltip="Fallback maximum token limit for chat/completion requests answered by this endpoint. At startup, the /v1/models endpoint is queried for the model's max_model_len; if that query fails this value is used instead. Requests whose total token count (prompt + max_tokens) exceeds the limit are rejected with a 413 error."
                />
                <Input
                  type="number"
                  min="1"
                  step="1"
                  value={maxContextSize}
                  onChange={(e) => setMaxContextSize(e.target.value)}
                  placeholder={DEFAULT_MAX_CONTEXT_SIZE}
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
                  placeholder={DEFAULT_MAX_OUTPUT_TOKENS}
                />
              </div>
              <p className="col-span-2 text-xs text-muted-foreground">
                Token budgets for this endpoint. Leave blank to use the system default. Applied whenever this
                endpoint answers a request — as the default LLM, or as a partition&apos;s chat LLM preset.
              </p>
            </div>
          )}
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
          <div className="space-y-2">
            <Label>Extra (JSON)</Label>
            <Textarea
              className="font-mono text-sm"
              value={extraJson}
              onChange={(e) => setExtraJson(e.target.value)}
              rows={4}
            />
            <p className="text-xs text-muted-foreground">
              Non-secret endpoint options only. The API key is handled separately above.
            </p>
          </div>
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
              disabled={loading || validated !== true}
            >
              {loading ? "Saving..." : editing ? "Update" : "Create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
