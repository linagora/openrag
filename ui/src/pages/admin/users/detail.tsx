import { useState } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ArrowLeft, Plus, Trash2, Copy, KeyRound } from "lucide-react";
import {
  getUser,
  updateUser,
  deleteUser,
  regenerateUserToken,
  listUserPartitions,
  assignPartition,
  removePartition,
} from "@/lib/api/users";
import { PageHeader } from "@/components/shared/page-header";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
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
import { formatDate } from "@/lib/utils";

export default function UserDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data: user, isLoading } = useQuery({
    queryKey: ["user", id],
    queryFn: () => getUser(id!),
    enabled: !!id,
  });

  // Editable admin controls (identity itself is owned by the IdP — read-only).
  const [isAdmin, setIsAdmin] = useState(false);
  const [quota, setQuota] = useState("");
  const [formLoaded, setFormLoaded] = useState(false);

  if (user && !formLoaded) {
    const extra = user as unknown as { is_admin?: boolean; file_quota?: number | null };
    setIsAdmin(extra.is_admin ?? user.role === "admin");
    setQuota(extra.file_quota == null ? "" : String(extra.file_quota));
    setFormLoaded(true);
  }

  const updateMut = useMutation({
    mutationFn: (data: Record<string, unknown>) => updateUser(id!, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["user", id] });
      queryClient.invalidateQueries({ queryKey: ["users"] });
      toast.success("User updated");
    },
    onError: (e) => toast.error(e.message),
  });

  const deleteMut = useMutation({
    mutationFn: () => deleteUser(id!),
    onSuccess: () => {
      toast.success("User deleted");
      navigate("/users");
    },
    onError: (e) => toast.error(e.message),
  });

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault();
    updateMut.mutate({
      is_admin: isAdmin,
      file_quota: quota.trim() === "" ? null : Number(quota),
    });
  };

  if (isLoading) return <Skeleton className="h-96" />;
  if (!user) return <p>User not found</p>;

  return (
    <div>
      <PageHeader
        title={user.display_name || user.email}
        description={user.email}
        actions={
          <div className="flex gap-2">
            <Button variant="outline" asChild>
              <Link to="/users">
                <ArrowLeft className="mr-2 h-4 w-4" /> Back
              </Link>
            </Button>
            <ConfirmDialog
              title="Delete user?"
              description={`Permanently delete "${user.email}"?`}
              onConfirm={() => deleteMut.mutate()}
            >
              <Button variant="destructive">
                <Trash2 className="mr-2 h-4 w-4" /> Delete
              </Button>
            </ConfirmDialog>
          </div>
        }
      />

      <Tabs defaultValue="profile">
        <TabsList>
          <TabsTrigger value="profile">Profile</TabsTrigger>
          <TabsTrigger value="partitions">Partitions</TabsTrigger>
          <TabsTrigger value="token">API Token</TabsTrigger>
        </TabsList>

        <TabsContent value="profile" className="grid gap-4 md:grid-cols-2">
          {/* Identity — owned by the IdP (SSO). Read-only here. */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Identity</CardTitle>
              <CardDescription>
                Synced from your identity provider (SSO). Manage these in the IdP, not here.
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-4">
              <div className="space-y-2">
                <Label>Email</Label>
                <Input value={user.email} readOnly disabled />
              </div>
              <div className="space-y-2">
                <Label>Display Name</Label>
                <Input value={user.display_name || ""} readOnly disabled />
              </div>
            </CardContent>
          </Card>

          {/* Access & limits — the genuine admin decisions. */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Access & limits</CardTitle>
              <CardDescription>Promote to admin and set this user's file quota.</CardDescription>
            </CardHeader>
            <CardContent>
              <form onSubmit={handleSave} className="space-y-5">
                <div className="flex items-center justify-between">
                  <div>
                    <Label>Administrator</Label>
                    <p className="text-xs text-muted-foreground">Full access to every partition and admin function.</p>
                  </div>
                  <Switch checked={isAdmin} onCheckedChange={setIsAdmin} />
                </div>
                <div className="space-y-2">
                  <Label>File quota</Label>
                  <Input
                    type="number"
                    value={quota}
                    onChange={(e) => setQuota(e.target.value)}
                    placeholder="Global default"
                  />
                  <p className="text-xs text-muted-foreground">
                    Empty = global default · <span className="font-mono">-1</span> = unlimited
                  </p>
                </div>
                <Button type="submit" disabled={updateMut.isPending}>
                  {updateMut.isPending ? "Saving..." : "Save Changes"}
                </Button>
              </form>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="partitions">
          <PartitionsTab userId={id!} />
        </TabsContent>

        <TabsContent value="token">
          <ApiTokenTab userId={id!} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function PartitionsTab({ userId }: { userId: string }) {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [partition, setPartition] = useState("");
  const [role, setRole] = useState("viewer");

  const partitionsQuery = useQuery({
    queryKey: ["user-partitions", userId],
    queryFn: () => listUserPartitions(userId),
  });

  const assignments = partitionsQuery.data ?? [];

  const assignMut = useMutation({
    mutationFn: () => assignPartition(userId, partition, role),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["user-partitions", userId] });
      toast.success(`Assigned to ${partition}`);
      setDialogOpen(false);
      setPartition("");
      setRole("viewer");
    },
    onError: (e) => toast.error(e.message),
  });

  const removeMut = useMutation({
    mutationFn: (part: string) => removePartition(userId, part),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["user-partitions", userId] });
      queryClient.invalidateQueries({ queryKey: ["user", userId] });
      toast.success("Partition removed");
    },
    onError: (e) => toast.error(e.message),
  });

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle className="text-base">Partition access</CardTitle>
          <CardDescription>Grant this user a role on specific partitions.</CardDescription>
        </div>
        <Button size="sm" onClick={() => setDialogOpen(true)}>
          <Plus className="mr-1 h-3 w-3" /> Grant access
        </Button>
      </CardHeader>
      <CardContent>
        {assignments.length === 0 ? (
          <p className="text-muted-foreground text-sm py-4 text-center">
            No partition access yet. Click "Grant access" to add one.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Partition</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Granted</TableHead>
                <TableHead></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {assignments.map((a) => (
                <TableRow key={a.partition}>
                  <TableCell>{a.partition}</TableCell>
                  <TableCell>
                    <Badge variant="outline" className="capitalize">{a.role}</Badge>
                  </TableCell>
                  <TableCell>{formatDate(a.created_at)}</TableCell>
                  <TableCell>
                    <ConfirmDialog
                      title="Remove access?"
                      description={`Remove this user's access to "${a.partition}"?`}
                      onConfirm={() => removeMut.mutate(a.partition)}
                    >
                      <Button size="sm" variant="ghost" className="text-destructive">
                        <Trash2 className="h-3 w-3" />
                      </Button>
                    </ConfirmDialog>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}

        <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Grant partition access</DialogTitle>
            </DialogHeader>
            <div className="space-y-4">
              <div className="space-y-2">
                <Label>Partition name</Label>
                <Input value={partition} onChange={(e) => setPartition(e.target.value)} required />
              </div>
              <div className="space-y-2">
                <Label>Role</Label>
                <Select value={role} onValueChange={setRole}>
                  <SelectTrigger>
                    <SelectValue />
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
              <Button onClick={() => assignMut.mutate()} disabled={assignMut.isPending || !partition}>
                {assignMut.isPending ? "Granting..." : "Grant access"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  );
}

function ApiTokenTab({ userId }: { userId: string }) {
  const [token, setToken] = useState<string | null>(null);

  const regenerateMut = useMutation({
    mutationFn: () => regenerateUserToken(userId),
    onSuccess: (data) => {
      setToken(data.token);
      toast.success("API token regenerated");
    },
    onError: (e) => toast.error(e.message),
  });

  const copyToken = () => {
    if (token) {
      navigator.clipboard.writeText(token);
      toast.success("Token copied to clipboard");
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">API Token</CardTitle>
        <CardDescription>
          This user has a single bearer token for programmatic access. Regenerating it immediately
          invalidates the previous one.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {token && (
          <div className="space-y-2">
            <p className="text-sm text-muted-foreground">Copy this token now — it won't be shown again.</p>
            <div className="flex gap-2">
              <Input value={token} readOnly className="font-mono text-xs" />
              <Button size="icon" variant="outline" onClick={copyToken}>
                <Copy className="h-4 w-4" />
              </Button>
            </div>
          </div>
        )}
        <ConfirmDialog
          title="Regenerate this user's token?"
          description="Their current token stops working immediately and must be replaced wherever it's used."
          onConfirm={() => regenerateMut.mutate()}
        >
          <Button disabled={regenerateMut.isPending}>
            <KeyRound className="mr-2 h-4 w-4" />
            {regenerateMut.isPending ? "Regenerating..." : "Regenerate token"}
          </Button>
        </ConfirmDialog>
      </CardContent>
    </Card>
  );
}
