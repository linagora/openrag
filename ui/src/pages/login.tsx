import { useState } from "react";
import { useNavigate, Navigate } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { APP_NAME } from "@/lib/brand";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";

// OpenRag has no password login. Two real paths:
//  - token mode: paste the user's bearer token (`or-…`)
//  - OIDC/SSO: redirect to the backend's /auth/login (same-origin), which sends
//    the user to the IdP and sets the session cookie on return.
const SSO_LOGIN_URL = `/auth/login?next=${encodeURIComponent(import.meta.env.BASE_URL)}`;

export default function LoginPage() {
  const { loginWithToken, isAuthenticated, isLoading } = useAuth();
  const navigate = useNavigate();
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (isAuthenticated) return <Navigate to="/" replace />;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await loginWithToken(token);
      navigate("/");
    } catch {
      setError("That token was rejected. Check it and try again.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-brand-900 via-brand-800 to-brand-950">
      <Card className="w-full max-w-sm shadow-2xl border-0">
        <CardHeader className="text-center pb-2">
          <img src="/openrag-title.svg" alt={APP_NAME} className="mx-auto mb-4 h-10 w-auto object-contain" />
          <CardTitle className="text-lg font-semibold text-muted-foreground">Admin Console</CardTitle>
          <CardDescription>Sign in to manage your RAG system</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <form onSubmit={handleSubmit} className="space-y-4">
            {error && (
              <Alert variant="destructive">
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
            <div className="space-y-2">
              <Label htmlFor="token">API token</Label>
              <Input
                id="token"
                type="password"
                placeholder="or-…"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                autoComplete="off"
                required
              />
              <p className="text-xs text-muted-foreground">
                Your OpenRag bearer token. Admins can issue one from the Users page.
              </p>
            </div>
            <Button type="submit" className="w-full" disabled={submitting || isLoading || !token.trim()}>
              {submitting ? "Signing in…" : "Sign in"}
            </Button>
          </form>

          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span className="h-px flex-1 bg-border" />
            or
            <span className="h-px flex-1 bg-border" />
          </div>

          <Button variant="outline" className="w-full" asChild>
            <a href={SSO_LOGIN_URL}>Sign in with SSO</a>
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
