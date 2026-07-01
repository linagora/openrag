import { useAuth } from "@/lib/auth";
import { useNavigate } from "react-router-dom";
import { LogOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { SidebarTrigger } from "@/components/ui/sidebar";

export function Header() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const handleLogout = () => {
    const wasOidcSession = logout();
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
        <div className="flex items-center gap-3">
          <span className="text-sm text-muted-foreground">
            {user.display_name || user.email || `User #${user.id}`}
          </span>
          <Badge className="bg-primary/10 text-primary border-primary/20 hover:bg-primary/15">
            {user.is_admin ? "Admin" : "User"}
          </Badge>
          <Button variant="ghost" size="icon" onClick={handleLogout} className="text-muted-foreground hover:text-destructive">
            <LogOut className="h-4 w-4" />
          </Button>
        </div>
      )}
    </header>
  );
}
