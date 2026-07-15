import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { Copy, KeyRound, ShieldCheck } from "lucide-react";
import { regenerateMyToken } from "@/lib/api/account";
import { PageHeader } from "@/components/shared/page-header";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { copyToClipboard } from "@/lib/utils";

export default function SettingsPage() {
  const [token, setToken] = useState<string | null>(null);

  const regenerateMut = useMutation({
    mutationFn: regenerateMyToken,
    onSuccess: (data) => {
      setToken(data.token);
      toast.success("API token regenerated");
    },
    onError: (e) => toast.error(e.message),
  });

  const copyToken = async (e: React.MouseEvent<HTMLButtonElement>) => {
    if (token) {
      const ok = await copyToClipboard(token, e.currentTarget);
      toast[ok ? "success" : "error"](
        ok ? "Token copied to clipboard" : "Couldn't copy — copy it manually",
      );
    }
  };

  return (
    <div>
      <PageHeader title="Account Settings" description="Manage your API access and authentication." />

      <div className="mt-4 grid gap-4 max-w-2xl">
        {/* Sign-in is handled by the IdP — nothing to manage here. */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <ShieldCheck className="h-4 w-4 text-primary" /> Sign-in
            </CardTitle>
            <CardDescription>
              Authentication is managed through your organization's SSO. OpenRAG does not manage
              passwords.
            </CardDescription>
          </CardHeader>
        </Card>

        {/* OpenRag has one bearer token per user, rotated here (no multi-key API). */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <KeyRound className="h-4 w-4 text-primary" /> API Token
            </CardTitle>
            <CardDescription>
              Use this token to authenticate API requests from the CLI, scripts, or other tools.
              Regenerating it immediately revokes the previous token.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {token && (
              <div className="space-y-2">
                <p className="text-sm text-muted-foreground">
                  Copy this token now — it won't be shown again.
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
              title="Regenerate API token?"
              description="Your current token will stop working immediately. Any scripts using it must be updated with the new token."
              onConfirm={() => regenerateMut.mutate()}
            >
              <Button disabled={regenerateMut.isPending}>
                <KeyRound className="mr-2 h-4 w-4" />
                {regenerateMut.isPending ? "Regenerating..." : "Regenerate token"}
              </Button>
            </ConfirmDialog>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
