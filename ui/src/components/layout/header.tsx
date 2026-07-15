import { useAuth } from "@/lib/auth";
import { apiUrl, request, TOKEN_KEY } from "@/lib/api/client";
import { useNavigate } from "react-router-dom";
import { LogOut, MessageSquare } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { toast } from "sonner";

export function Header() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const chatHref = apiUrl("/chainlit/");

  const handleOpenChat = async () => {
    const chatWindow = window.open("about:blank", "_blank");
    if (chatWindow) {
      chatWindow.opener = null;
    }

    try {
      await request("/auth/chainlit-session", { method: "POST" });
    } catch {
      // Keep the previous fallback behavior: if the handoff cannot be prepared,
      // still let Chainlit handle its own login flow.
      toast.error("Could not prepare Chat session. Opening Chat login instead.");
    }

    if (chatWindow) {
      chatWindow.location.href = chatHref;
    } else {
      window.location.assign(chatHref);
    }
  };

  const handleLogout = async () => {
    const token = localStorage.getItem(TOKEN_KEY);
    const clearChainlitSession =
      token !== null
        ? request("/auth/chainlit-session", {
            method: "DELETE",
            headers: { Authorization: `Bearer ${token}` },
          }).catch(() => {
            // Local token logout should still complete even if the optional
            // chat cookie cleanup request fails.
          })
        : undefined;

    const wasOidcSession = logout();
    void clearChainlitSession;
    if (wasOidcSession) {
      // Full-page navigation to the backend's RP-initiated logout: it revokes
      // the server session, clears the cookie, and redirects on to the IdP.
      window.location.assign("/auth/logout");
    } else {
      navigate("/login");
    }
  };

  return (
    <header className="flex h-14 items-center gap-4 border-b bg-card px-4 shadow-sm">
      <SidebarTrigger className="text-muted-foreground hover:text-foreground" />
      <div className="h-5 w-px bg-border" />
      <div className="flex-1" />
      {user && (
        <div className="flex items-center gap-5">
          {user.chainlit_enabled === true && (
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button variant="outline" size="sm" onClick={handleOpenChat} aria-label="Open Chat in a new tab">
                    <MessageSquare className="h-4 w-4" />
                    Chat
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Open Chat in a new tab</TooltipContent>
              </Tooltip>
            </TooltipProvider>
          )}
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">
              {user.display_name || user.email || `User #${user.id}`}
            </span>
            <Badge className="bg-primary/10 text-primary border-primary/20 hover:bg-primary/15">
              {user.is_admin ? "Admin" : "User"}
            </Badge>
            <Button
              variant="ghost"
              size="icon"
              onClick={handleLogout}
              className="text-muted-foreground hover:text-destructive"
              aria-label="Log out"
            >
              <LogOut className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}
    </header>
  );
}
