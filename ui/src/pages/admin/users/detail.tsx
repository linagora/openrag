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
  listUserMemberships,
  grantMembership,
  revokeMembership,
  effectiveQuota,
} from "@/lib/api/users";
import type { UserResponse } from "@/lib/api/users";
import { getConfig } from "@/lib/api/system";
import { listPartitions, type PartitionRole } from "@/lib/api/partitions";
import { PageHeader } from "@/components/shared/page-header";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { QuotaUsageMeter } from "@/components/shared/quota-usage-meter";
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
import { formatDate, copyToClipboard } from "@/lib/utils";

export default function UserDetailPage() {
  const { id } = useParams<{ id: string }>();
  const userId = Number(id);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data: user, isLoading } = useQuery({
    queryKey: ["user", id],
    queryFn: () => getUser(id!),
    enabled: !!id,
  });

  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [externalId, setExternalId] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [formLoaded, setFormLoaded] = useState(false);

  if (user && !formLoaded) {
    setDisplayName(user.display_name ?? "");
    setEmail(user.email ?? "");
    setExternalId(user.external_user_id ?? "");
    setIsAdmin(user.is_admin);
    setFormLoaded(true);
  }

  const updateMut = useMutation({
    mutationFn: () =>
      updateUser(id!, {
        display_name: displayName.trim() || undefined,
        email: email.trim() || undefined,
        external_user_id: externalId.trim() || undefined,
        is_admin: isAdmin,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["user", id] });
      queryClient.invalidateQueries({ queryKey: ["users"] });
      toast.success("User updated");
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const deleteMut = useMutation({
    mutationFn: () => deleteUser(id!),
    onSuccess: () => {
      toast.success("User deleted");
      navigate("/users");
    },
    onError: (e) => toast.error((e as Error).message),
  });

  if (isLoading) return <Skeleton className="h-96" />;
  if (!user) return <p>User not found</p>;

  const title = user.display_name || user.external_user_id || `User #${user.id}`;
  const isSso = !!user.external_user_id;

  return (
    <div>
      <PageHeader
        title={title}
        description={isSso ? "SSO user" : "Token-only user"}
        actions={
          <div className="flex gap-2">
            <Button variant="outline" asChild>
              <Link to="/users">
                <ArrowLeft className="mr-2 h-4 w-4" /> Back
              </Link>
            </Button>
            <ConfirmDialog
              title="Delete user?"
              description={`Permanently delete "${title}"? This removes them from all partitions and invalidates their token.`}
              onConfirm={() => deleteMut.mutate()}
            >
              <Button variant="destructive" disabled={user.id === 1}>
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

        <TabsContent value="profile">
          <div className="grid items-start gap-6 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Profile &amp; access</CardTitle>
              <CardDescription>
                {isSso
                  ? "This user signs in via SSO. If OIDC claim mapping is enabled, display name and email may be overwritten from the IdP on their next login."
                  : "Token-only user (no linked SSO identity). Set an External User ID to enable SSO login."}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  updateMut.mutate();
                }}
                className="grid gap-5"
              >
                <div className="space-y-2">
                  <Label>Display name</Label>
                  <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
                </div>
                <div className="space-y-2">
                  <Label>Email</Label>
                  <Input
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="Metadata only — not used for login"
                  />
                </div>
                <div className="space-y-2">
                  <Label>
                    External User ID (<span className="font-mono">sub</span>)
                  </Label>
                  <Input
                    value={externalId}
                    onChange={(e) => setExternalId(e.target.value)}
                    placeholder="Empty = token-only user"
                    className="font-mono text-sm"
                  />
                  <p className="text-xs text-muted-foreground">
                    Links this user to an IdP identity. Login matches by{" "}
                    <span className="font-mono">sub</span>.
                  </p>
                </div>
                <div className="flex items-center justify-between md:col-span-2">
                  <div>
                    <Label>Administrator</Label>
                    <p className="text-xs text-muted-foreground">
                      Full access to every partition and admin function.
                    </p>
                  </div>
                  <Switch checked={isAdmin} onCheckedChange={setIsAdmin} />
                </div>
                <div className="md:col-span-2">
                  <Button type="submit" disabled={updateMut.isPending}>
                    {updateMut.isPending ? "Saving…" : "Save Changes"}
                  </Button>
                </div>
              </form>
            </CardContent>
          </Card>

          <StorageQuotaCard user={user} />
          </div>
        </TabsContent>

        <TabsContent value="partitions">
          <PartitionsTab userId={userId} />
        </TabsContent>

        <TabsContent value="token">
          <ApiTokenTab userId={userId} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function PartitionsTab({ userId }: { userId: number }) {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [partition, setPartition] = useState("");
  const [role, setRole] = useState<PartitionRole>("viewer");

  const membershipsQuery = useQuery({
    queryKey: ["user-memberships", userId],
    queryFn: () => listUserMemberships(userId),
  });
  const memberships = membershipsQuery.data ?? [];

  const partitionsQuery = useQuery({ queryKey: ["partitions"], queryFn: listPartitions });
  const assigned = new Set(memberships.map((m) => m.partition));
  const available = (partitionsQuery.data?.partitions ?? []).filter((p) => !assigned.has(p.name));

  const grantMut = useMutation({
    mutationFn: () => grantMembership(partition, userId, role),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["user-memberships", userId] });
      toast.success(`Granted ${role} on ${partition}`);
      setDialogOpen(false);
      setPartition("");
      setRole("viewer");
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const revokeMut = useMutation({
    mutationFn: (part: string) => revokeMembership(part, userId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["user-memberships", userId] });
      toast.success("Access removed");
    },
    onError: (e) => toast.error((e as Error).message),
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
        {membershipsQuery.isLoading ? (
          <Skeleton className="h-24" />
        ) : memberships.length === 0 ? (
          <p className="text-muted-foreground text-sm py-4 text-center">
            No partition access yet. Click &quot;Grant access&quot; to add one.
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
              {memberships.map((m) => (
                <TableRow key={m.partition}>
                  <TableCell>{m.partition}</TableCell>
                  <TableCell>
                    <Badge variant="outline" className="capitalize">
                      {m.role}
                    </Badge>
                  </TableCell>
                  <TableCell>{formatDate(m.added_at)}</TableCell>
                  <TableCell>
                    <ConfirmDialog
                      title="Remove access?"
                      description={`Remove this user's access to "${m.partition}"?`}
                      onConfirm={() => revokeMut.mutate(m.partition)}
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
                <Label>Partition</Label>
                <Select value={partition} onValueChange={setPartition}>
                  <SelectTrigger>
                    <SelectValue placeholder="Select a partition" />
                  </SelectTrigger>
                  <SelectContent>
                    {available.length === 0 ? (
                      <div className="px-2 py-1.5 text-sm text-muted-foreground">
                        No more partitions available
                      </div>
                    ) : (
                      available.map((p) => (
                        <SelectItem key={p.name} value={p.name}>
                          {p.name}
                        </SelectItem>
                      ))
                    )}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label>Role</Label>
                <Select value={role} onValueChange={(v) => setRole(v as PartitionRole)}>
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
              <Button onClick={() => grantMut.mutate()} disabled={grantMut.isPending || !partition}>
                {grantMut.isPending ? "Granting…" : "Grant access"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </CardContent>
    </Card>
  );
}

function StorageQuotaCard({ user }: { user: UserResponse }) {
  const queryClient = useQueryClient();
  const [quota, setQuota] = useState(user.file_quota == null ? "" : String(user.file_quota));

  // Global default so a null per-user quota resolves to a real number.
  const { data: config } = useQuery({ queryKey: ["system-config"], queryFn: getConfig });
  const globalDefault =
    (config?.rdb as { default_file_quota?: number } | undefined)?.default_file_quota ?? null;

  const saveMut = useMutation({
    mutationFn: () =>
      updateUser(String(user.id), { file_quota: quota.trim() === "" ? null : Number(quota) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["user", String(user.id)] });
      queryClient.invalidateQueries({ queryKey: ["users"] });
      toast.success("Quota updated");
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const isAdmin = user.is_admin;
  const title = user.display_name || user.external_user_id || `User #${user.id}`;
  const fileCount = user.file_count ?? 0;
  const eff = effectiveQuota(user.file_quota, isAdmin, globalDefault);

  // Typed value (drives the lower-than-usage warning + confirm).
  const quotaNum = quota.trim() === "" ? null : Number(quota);
  const overLimit =
    quotaNum != null && Number.isFinite(quotaNum) && quotaNum >= 0 && fileCount > quotaNum;
  const toRemove = quotaNum != null ? fileCount - quotaNum : 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Storage &amp; quota</CardTitle>
        <CardDescription>
          Indexed-file usage and the per-user upload cap.{" "}
          {isAdmin
            ? "Admins bypass quota checks."
            : "Lowering a quota never deletes existing files — it only blocks new uploads until the user is back under the limit."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <QuotaUsageMeter fileCount={fileCount} quota={eff} />

        <form
          onSubmit={(e) => {
            e.preventDefault();
            saveMut.mutate();
          }}
          className="space-y-2"
        >
          <Label>File quota</Label>
          <Input
            type="number"
            value={quota}
            onChange={(e) => setQuota(e.target.value)}
            placeholder="Global default"
            disabled={isAdmin}
          />
          <p className="text-xs text-muted-foreground">
            {isAdmin ? (
              "Admins bypass quota checks — this setting has no effect."
            ) : (
              <>
                Empty = global default · <span className="font-mono">-1</span> = unlimited
              </>
            )}
          </p>
          {!isAdmin && overLimit && (
            <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-800">
              ⚠️ <span className="font-medium">
                {title} already has {fileCount} indexed files.
              </span>{" "}
              A quota of {quotaNum} puts them over their limit — they can&apos;t upload until they
              remove {toRemove}+ file{toRemove === 1 ? "" : "s"}. Existing files are kept.
            </div>
          )}
          <div className="pt-1">
            {!isAdmin && overLimit ? (
              <ConfirmDialog
                title="Set quota below current usage?"
                description={`${title} currently has ${fileCount} indexed files. Setting the quota to ${quotaNum} leaves them over their limit — uploads are blocked until they drop below ${quotaNum}. Existing files are not removed.`}
                destructive={false}
                onConfirm={() => saveMut.mutate()}
              >
                <Button type="button" disabled={saveMut.isPending}>
                  {saveMut.isPending ? "Saving…" : "Save quota"}
                </Button>
              </ConfirmDialog>
            ) : (
              <Button type="submit" disabled={saveMut.isPending || isAdmin}>
                {saveMut.isPending ? "Saving…" : "Save quota"}
              </Button>
            )}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function ApiTokenTab({ userId }: { userId: number }) {
  const [token, setToken] = useState<string | null>(null);

  const regenerateMut = useMutation({
    mutationFn: () => regenerateUserToken(userId),
    onSuccess: (data) => {
      setToken(data.token);
      toast.success("API token regenerated");
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const copyToken = async () => {
    if (token) {
      const ok = await copyToClipboard(token);
      toast[ok ? "success" : "error"](
        ok ? "Token copied to clipboard" : "Couldn't copy — copy it manually",
      );
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
            <p className="text-sm text-muted-foreground">
              Copy this token now — it won&apos;t be shown again.
            </p>
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
            {regenerateMut.isPending ? "Regenerating…" : "Regenerate token"}
          </Button>
        </ConfirmDialog>
      </CardContent>
    </Card>
  );
}
