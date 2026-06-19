import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import type { ColumnDef } from "@tanstack/react-table";
import { Trash2, Eye, Plus, Copy } from "lucide-react";
import { listUsers, deleteUser, createUser, effectiveQuota } from "@/lib/api/users";
import type { UserResponse, UserWithToken } from "@/lib/api/users";
import { getConfig } from "@/lib/api/system";
import { PageHeader } from "@/components/shared/page-header";
import { DataTable, SortableHeader } from "@/components/shared/data-table";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { copyToClipboard } from "@/lib/utils";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";

// "indexed / effective-quota", e.g. "11 / 200". ∞ for unlimited; `over` flags
// users at or past their cap (shown in red) so admins spot blocked uploaders.
function formatUsage(
  fileCount: number | null | undefined,
  fileQuota: number | null | undefined,
  isAdmin: boolean,
  globalDefault: number | null | undefined,
): { text: string; over: boolean } {
  const count = fileCount ?? 0;
  const eff = effectiveQuota(fileQuota, isAdmin, globalDefault);
  if (eff === "unlimited") return { text: `${count} / ∞`, over: false };
  if (eff === "unknown") return { text: `${count} / default`, over: false };
  return { text: `${count} / ${eff}`, over: count > eff };
}

export default function UserListPage() {
  const queryClient = useQueryClient();
  const [dialogOpen, setDialogOpen] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["users"],
    queryFn: listUsers,
  });

  // Global default quota (DEFAULT_FILE_QUOTA) so `null` per-user quotas resolve
  // to a real number in the Usage column. Shared cache with the System page.
  const { data: config } = useQuery({ queryKey: ["system-config"], queryFn: getConfig });
  const globalDefault =
    (config?.rdb as { default_file_quota?: number } | undefined)?.default_file_quota ?? null;

  const deleteMut = useMutation({
    mutationFn: deleteUser,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["users"] });
      toast.success("User deleted");
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const columns: ColumnDef<UserResponse>[] = [
    {
      accessorKey: "display_name",
      header: ({ column }) => <SortableHeader column={column} title="Name" />,
      cell: ({ row }) => (
        <Link to={`/users/${row.original.id}`} className="text-primary hover:underline">
          {row.original.display_name || `User #${row.original.id}`}
        </Link>
      ),
    },
    {
      accessorKey: "external_user_id",
      header: "External ID (sub)",
      cell: ({ row }) => (
        <span className="font-mono text-xs text-muted-foreground">
          {row.original.external_user_id || "—"}
        </span>
      ),
    },
    {
      accessorKey: "email",
      header: "Email",
      cell: ({ row }) => row.original.email || <span className="text-muted-foreground">—</span>,
    },
    {
      accessorKey: "is_admin",
      header: "Admin",
      cell: ({ row }) =>
        row.original.is_admin ? (
          <Badge variant="default">Admin</Badge>
        ) : (
          <Badge variant="secondary">User</Badge>
        ),
    },
    {
      id: "usage",
      header: "Usage",
      cell: ({ row }) => {
        const u = formatUsage(
          row.original.file_count,
          row.original.file_quota,
          row.original.is_admin,
          globalDefault,
        );
        return (
          <span className={u.over ? "font-medium text-destructive" : "tabular-nums"} title="Indexed files / effective quota">
            {u.text}
          </span>
        );
      },
    },
    {
      id: "actions",
      cell: ({ row }) => (
        <div className="flex gap-1">
          <Button size="sm" variant="ghost" asChild>
            <Link to={`/users/${row.original.id}`}>
              <Eye className="h-3 w-3" />
            </Link>
          </Button>
          <ConfirmDialog
            title="Delete user?"
            description={`Permanently delete "${row.original.display_name || `User #${row.original.id}`}"? This removes them from all partitions and invalidates their token.`}
            onConfirm={() => deleteMut.mutate(row.original.id)}
          >
            <Button
              size="sm"
              variant="ghost"
              className="text-destructive"
              disabled={row.original.id === 1}
              title={row.original.id === 1 ? "The default admin cannot be deleted" : undefined}
            >
              <Trash2 className="h-3 w-3" />
            </Button>
          </ConfirmDialog>
        </div>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        title="Users"
        description="Create SSO users (linked to an IdP identity) or token-only users (programmatic/service accounts), then manage admin rights, quota and partition roles."
        actions={
          <Button onClick={() => setDialogOpen(true)}>
            <Plus className="mr-2 h-4 w-4" /> New user
          </Button>
        }
      />

      {isLoading ? (
        <Skeleton className="h-64" />
      ) : (
        <DataTable columns={columns} data={data?.users || []} />
      )}

      <PreProvisionDialog open={dialogOpen} onOpenChange={setDialogOpen} />
    </div>
  );
}

function PreProvisionDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [externalId, setExternalId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [quota, setQuota] = useState("");
  const [created, setCreated] = useState<UserWithToken | null>(null);

  const reset = () => {
    setExternalId("");
    setDisplayName("");
    setEmail("");
    setIsAdmin(false);
    setQuota("");
    setCreated(null);
  };

  const handleClose = (next: boolean) => {
    if (!next) reset();
    onOpenChange(next);
  };

  const createMut = useMutation({
    mutationFn: () =>
      createUser({
        external_user_id: externalId.trim() || undefined,
        display_name: displayName.trim() || undefined,
        email: email.trim() || undefined,
        is_admin: isAdmin,
        file_quota: quota.trim() === "" ? null : Number(quota),
      }),
    onSuccess: (user) => {
      setCreated(user);
      queryClient.invalidateQueries({ queryKey: ["users"] });
      toast.success("User created");
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const copyToken = async () => {
    if (created?.token) {
      const ok = await copyToClipboard(created.token);
      toast[ok ? "success" : "error"](
        ok ? "Token copied to clipboard" : "Couldn't copy — copy it manually",
      );
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleClose}>
      <DialogContent>
        {created ? (
          <>
            <DialogHeader>
              <DialogTitle>User created</DialogTitle>
              <DialogDescription>
                {created.external_user_id
                  ? `${created.display_name || `User #${created.id}`} can sign in via SSO. The token below is also a bearer credential for programmatic access (API/CI).`
                  : `This is a token-only user. The bearer token below is their only credential — copy it now, it is shown only once.`}
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-2">
              <Label>API token</Label>
              <div className="flex gap-2">
                <Input value={created.token} readOnly className="font-mono text-xs" />
                <Button size="icon" variant="outline" onClick={copyToken}>
                  <Copy className="h-4 w-4" />
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">Shown once — store it securely.</p>
            </div>
            <DialogFooter>
              <Button onClick={() => handleClose(false)}>Done</Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>New user</DialogTitle>
              <DialogDescription>
                Provide an External User ID to link an SSO identity, or leave it empty for a
                token-only user (service / programmatic account).
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-4">
              <div className="space-y-2">
                <Label>
                  External User ID (<span className="font-mono">sub</span>) — optional
                </Label>
                <Input
                  value={externalId}
                  onChange={(e) => setExternalId(e.target.value)}
                  placeholder="e.g. kc-alice-uuid"
                />
                <p className="text-xs text-muted-foreground">
                  The user&apos;s stable subject claim from your IdP — set it to enable SSO login
                  (match is by <span className="font-mono">sub</span>). Leave empty for a token-only
                  user.
                </p>
              </div>
              <div className="space-y-2">
                <Label>Display name</Label>
                <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
              </div>
              <div className="space-y-2">
                <Label>Email (optional)</Label>
                <Input
                  type="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="Metadata only — not used for login"
                />
              </div>
              <div className="flex items-center justify-between">
                <div>
                  <Label>Administrator</Label>
                  <p className="text-xs text-muted-foreground">Full access to every partition.</p>
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
            </div>
            <DialogFooter>
              <Button variant="outline" onClick={() => handleClose(false)}>
                Cancel
              </Button>
              <Button onClick={() => createMut.mutate()} disabled={createMut.isPending}>
                {createMut.isPending ? "Creating…" : "Create user"}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
