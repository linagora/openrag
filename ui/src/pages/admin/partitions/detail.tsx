import { useState, useEffect, useCallback } from "react";
import { useParams, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Save, UserPlus, Trash2, CheckCircle, XCircle, Loader2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Skeleton } from "@/components/ui/skeleton";
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
  addPartitionMember,
  removePartitionMember,
} from "@/lib/api/partitions";
import type { PartitionConfig, PartitionRole } from "@/lib/api/partitions";
import { listPresets } from "@/lib/api/presets";
import { listModelEndpoints, validateStoredModelEndpoint, resolveEmbedderName } from "@/lib/api/models";
import { usePermissions } from "@/lib/permissions";
import { formatDate } from "@/lib/utils";

// --- General Tab ---

function GeneralTab({ partition }: { partition: PartitionConfig }) {
  const queryClient = useQueryClient();

  const [description, setDescription] = useState(partition.description ?? "");
  const [chatHistoryDepth, setChatHistoryDepth] = useState(String(partition.chat_history_depth));
  const [indexationPreset, setIndexationPreset] = useState(partition.indexation_preset);
  const [retrievalPreset, setRetrievalPreset] = useState(partition.retrieval_preset);
  const [chatLlm, setChatLlm] = useState(partition.chat_llm ?? "__default__");
  const [llmValidated, setLlmValidated] = useState<boolean | null>(
    partition.chat_llm ? null : true,
  );
  const [llmValidating, setLlmValidating] = useState(false);

  const { data: presetsData } = useQuery({
    queryKey: ["presets"],
    queryFn: () => listPresets(),
  });

  const { data: llmEndpoints } = useQuery({
    queryKey: ["model-endpoints", "llm"],
    queryFn: () => listModelEndpoints("llm"),
  });

  const { data: embedderEndpoints } = useQuery({
    queryKey: ["model-endpoints", "embedder"],
    queryFn: () => listModelEndpoints("embedder"),
  });

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
        chat_history_depth: parseInt(chatHistoryDepth),
        indexation_preset: indexationPreset,
        retrieval_preset: retrievalPreset,
        chat_llm: chatLlm === "__default__" ? null : chatLlm,
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
    mutation.mutate();
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">General Settings</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-6 max-w-3xl">
          <div className="space-y-2">
            <Label>Description</Label>
            <Textarea
              placeholder="Partition description..."
              className="min-h-[80px]"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <div className="grid grid-cols-3 gap-6">
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
          <div className="pt-2 grid grid-cols-3 gap-6">
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
          <Button type="submit" disabled={mutation.isPending || llmValidating || llmValidated === false}>
            <Save className="h-4 w-4" />
            {mutation.isPending ? "Saving..." : "Save Changes"}
          </Button>
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
  const [userId, setUserId] = useState("");
  const [role, setRole] = useState("viewer");
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
  const callerRole = partitionsQuery.data?.partitions.find((p) => p.name === partitionName)?.role;
  const canManage = canManageMembers(callerRole);

  const addMutation = useMutation({
    mutationFn: () => addPartitionMember(partitionName, Number(userId), role as PartitionRole),
    onSuccess: () => {
      toast.success("User added to partition");
      queryClient.invalidateQueries({
        queryKey: ["partition", partitionName, "users"],
      });
      setDialogOpen(false);
      setUserId("");
      setRole("viewer");
    },
    onError: (error: Error) => {
      toast.error(`Failed to add user: ${error.message}`);
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

  const handleAddUser = () => {
    if (!userId.trim() || Number.isNaN(Number(userId))) {
      toast.error("A numeric user ID is required");
      return;
    }
    addMutation.mutate();
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
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>User ID</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead>Added</TableHead>
                  {canManage && <TableHead>Actions</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {usersQuery.data.members.map((user) => (
                  <TableRow key={user.user_id}>
                    <TableCell className="font-mono text-sm">
                      {user.user_id}
                    </TableCell>
                    <TableCell className="capitalize">{user.role}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {formatDate(user.added_at)}
                    </TableCell>
                    {canManage && (
                      <TableCell>
                        <ConfirmDialog
                          title="Remove User"
                          description={`Remove user "${user.user_id}" from this partition? They will lose access to partition data.`}
                          onConfirm={() => removeMutation.mutate(user.user_id)}
                        >
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={removeMutation.isPending}
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

        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Add User to Partition</DialogTitle>
              <DialogDescription>
                Assign a user to this partition with a specific role.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-4">
              <div className="space-y-2">
                <Label>User ID</Label>
                <Input
                  placeholder="Enter user ID..."
                  value={userId}
                  onChange={(e) => setUserId(e.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label>Role</Label>
                <Select value={role} onValueChange={setRole}>
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
                onClick={() => setDialogOpen(false)}
              >
                Cancel
              </Button>
              <Button onClick={handleAddUser} disabled={addMutation.isPending}>
                {addMutation.isPending ? "Adding..." : "Add User"}
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
