import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import type { ColumnDef } from "@tanstack/react-table";
import { Trash2, Eye } from "lucide-react";
import { listUsers, deleteUser } from "@/lib/api/users";
import type { UserResponse } from "@/lib/api/users";
import { PageHeader } from "@/components/shared/page-header";
import { DataTable, SortableHeader } from "@/components/shared/data-table";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate } from "@/lib/utils";

// Users are provisioned through the IdP (OIDC/SSO), not created here — this page
// manages existing users (roles, access, removal). See OIDC provisioning policy.

export default function UserListPage() {
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["users"],
    queryFn: () => listUsers(),
  });

  const deleteMut = useMutation({
    mutationFn: deleteUser,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["users"] });
      toast.success("User deleted");
    },
    onError: (e) => toast.error(e.message),
  });

  const roleBadgeVariant = (r: string) => {
    switch (r) {
      case "superadmin": return "destructive" as const;
      case "admin": return "default" as const;
      default: return "secondary" as const;
    }
  };

  const columns: ColumnDef<UserResponse>[] = [
    {
      accessorKey: "email",
      header: ({ column }) => <SortableHeader column={column} title="Email" />,
      cell: ({ row }) => (
        <Link to={`/users/${row.original.id}`} className="text-primary hover:underline">
          {row.original.email}
        </Link>
      ),
    },
    {
      accessorKey: "display_name",
      header: "Name",
    },
    {
      accessorKey: "role",
      header: "Role",
      cell: ({ row }) => (
        <Badge variant={roleBadgeVariant(row.original.role)} className="capitalize">
          {row.original.role}
        </Badge>
      ),
    },
    {
      accessorKey: "is_active",
      header: "Active",
      cell: ({ row }) => (
        <Badge variant={row.original.is_active ? "default" : "secondary"}>
          {row.original.is_active ? "Yes" : "No"}
        </Badge>
      ),
    },
    {
      accessorKey: "created_at",
      header: ({ column }) => <SortableHeader column={column} title="Created" />,
      cell: ({ row }) => formatDate(row.original.created_at),
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
            description={`Permanently delete "${row.original.email}"? This cannot be undone.`}
            onConfirm={() => deleteMut.mutate(row.original.id)}
          >
            <Button size="sm" variant="ghost" className="text-destructive">
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
        description="Manage users provisioned through SSO — roles, access and removal"
      />

      {isLoading ? (
        <Skeleton className="h-64" />
      ) : (
        <DataTable columns={columns} data={data?.users || []} />
      )}
    </div>
  );
}
