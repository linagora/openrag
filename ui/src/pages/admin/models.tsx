import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Trash2, Pencil, Circle, Star, Loader2, CheckCircle, XCircle } from "lucide-react";
import {
  listModelEndpoints,
  createModelEndpoint,
  updateModelEndpoint,
  deleteModelEndpoint,
  setDefaultModelEndpoint,
  validateModelEndpoint,
} from "@/lib/api/models";
import type {
  ModelEndpointResponse,
  CreateModelEndpointRequest,
  UpdateModelEndpointRequest,
} from "@/lib/api/models";
import { health } from "@/lib/api/system";
import { PageHeader } from "@/components/shared/page-header";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate } from "@/lib/utils";

const MODEL_TYPES = ["embedder", "reranker", "llm", "vlm"] as const;

export default function ModelsPage() {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState("embedder");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<ModelEndpointResponse | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["model-endpoints"],
    queryFn: () => listModelEndpoints(),
  });

  const { data: healthData } = useQuery({
    queryKey: ["system-health"],
    queryFn: () => health(),
    refetchInterval: 30000,
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
      updateModelEndpoint(type, name, data),
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
      deleteModelEndpoint(type, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["model-endpoints"] });
      toast.success("Model endpoint deleted");
    },
    onError: (e) => toast.error(e.message),
  });

  const setDefaultMut = useMutation({
    mutationFn: ({ type, name }: { type: string; name: string }) =>
      setDefaultModelEndpoint(type, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["model-endpoints"] });
      toast.success("Default endpoint updated");
    },
    onError: (e) => toast.error(e.message),
  });

  const endpoints = data?.endpoints || [];

  const getHealthStatus = (type: string, name: string) => {
    if (!healthData) return undefined;
    return healthData.services[`${type}:${name}`];
  };

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
                    const isHealthy = getHealthStatus(ep.model_type, ep.name);
                    const isDefault = ep.is_default;
                    return (
                      <Card key={`${ep.model_type}-${ep.name}`}>
                        <CardHeader className="pb-3">
                          <div className="flex items-center justify-between">
                            <CardTitle className="text-base truncate">
                              {ep.name}
                            </CardTitle>
                            <div className="flex items-center gap-1.5">
                              {isDefault && (
                                <Badge variant="secondary" className="text-xs">Default</Badge>
                              )}
                              {isHealthy !== undefined && (
                                <Circle
                                  className={`h-3 w-3 fill-current ${isHealthy ? "text-green-500" : "text-red-500"}`}
                                />
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
                          <div className="flex gap-2 pt-2">
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
  const [extraJson, setExtraJson] = useState("{}");
  const [validated, setValidated] = useState<boolean | null>(null);
  const [validating, setValidating] = useState(false);

  useEffect(() => {
    if (open) {
      setValidated(null);
      setValidating(false);
      if (editing) {
        setName(editing.name);
        setEndpoint(editing.endpoint);
        setModelName(editing.model_name || "");
        setBatchSize(String(editing.batch_size));
        setTimeout(String(editing.timeout));
        setExtraJson(JSON.stringify(editing.extra, null, 2));
      } else {
        setName("");
        setEndpoint("");
        setModelName("");
        setBatchSize("32");
        setTimeout("30");
        setExtraJson("{}");
      }
    }
  }, [open, editing]);

  // Reset validation when relevant fields change
  useEffect(() => {
    setValidated(null);
  }, [endpoint, modelName, extraJson]);

  const handleValidate = async () => {
    if (!endpoint) {
      toast.error("Endpoint URL is required");
      return;
    }
    let extra: Record<string, unknown> = {};
    try {
      extra = JSON.parse(extraJson);
    } catch {
      toast.error("Invalid JSON in extra field");
      return;
    }
    setValidating(true);
    try {
      const res = await validateModelEndpoint({
        endpoint,
        model_name: modelName || undefined,
        timeout: parseFloat(timeout) || 5,
        extra,
      });
      setValidated(res.reachable);
      if (res.reachable) {
        toast.success(res.detail || "Endpoint is reachable");
      } else {
        toast.error(res.detail || "Endpoint is unreachable");
      }
    } catch (e) {
      setValidated(false);
      toast.error(e instanceof Error ? e.message : "Validation failed");
    } finally {
      setValidating(false);
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    let extra: Record<string, unknown> = {};
    try {
      extra = JSON.parse(extraJson);
    } catch {
      toast.error("Invalid JSON in extra field");
      return;
    }

    if (editing) {
      const updateData: UpdateModelEndpointRequest = {
        endpoint,
        model_name: modelName || undefined,
        batch_size: parseInt(batchSize),
        timeout: parseFloat(timeout),
        extra,
      };
      if (name !== editing.name) {
        updateData.name = name;
      }
      onUpdate(editing.model_type, editing.name, updateData);
    } else {
      onCreate({
        name,
        model_type: activeTab,
        endpoint,
        model_name: modelName || undefined,
        batch_size: parseInt(batchSize),
        timeout: parseFloat(timeout),
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
          <div className="space-y-2">
            <Label>Extra (JSON)</Label>
            <Textarea
              className="font-mono text-sm"
              value={extraJson}
              onChange={(e) => setExtraJson(e.target.value)}
              rows={4}
            />
          </div>
          <DialogFooter className="gap-2 sm:gap-0">
            <Button
              type="button"
              variant="outline"
              onClick={handleValidate}
              disabled={validating || !endpoint}
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
