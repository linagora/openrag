import { Link, useLocation } from "react-router-dom";
import {
  LayoutDashboard,
  Database,
  FileText,
  Clock,
  Cpu,
  Settings,
  MessageSquareText,
  Users,
  Activity,
  UserCog,
} from "lucide-react";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuItem,
  SidebarHeader,
  SidebarFooter,
} from "@/components/ui/sidebar";
import { Badge } from "@/components/ui/badge";
import { APP_NAME } from "@/lib/brand";
import { useActiveJobsCount } from "@/lib/jobs-queries";
import { usePermissions, type Permissions } from "@/lib/permissions";
import { ReleaseNotes } from "./release-notes";

interface NavItem {
  title: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
  // Visible to everyone if absent; otherwise only when the capability passes.
  requires?: (p: Permissions) => boolean;
}

// Management nav. Partition members see the resource pages (server-scoped to
// their memberships); platform-admin-only sections are gated by capability.
const navItems: NavItem[] = [
  { title: "Overview", href: "/", icon: LayoutDashboard },
  { title: "Partitions", href: "/partitions", icon: Database },
  { title: "Documents", href: "/documents", icon: FileText },
  { title: "Jobs", href: "/jobs", icon: Clock },
  { title: "Models", href: "/models", icon: Cpu, requires: (p) => p.canManageModels },
  { title: "Presets", href: "/presets", icon: Settings, requires: (p) => p.canManagePresets },
  { title: "Prompts", href: "/prompts", icon: MessageSquareText, requires: (p) => p.canManagePrompts },
  { title: "Users", href: "/users", icon: Users, requires: (p) => p.canManageUsers },
  { title: "System", href: "/system", icon: Activity, requires: (p) => p.canViewSystem },
];

const settingsItem: NavItem = { title: "Settings", href: "/settings", icon: UserCog };

const sidebarItemClassName =
  "relative flex items-center gap-3 px-3 py-2 rounded-xl text-sm font-medium transition-colors overflow-hidden group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0";
const sidebarItemInactiveClassName = "text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground";
const releaseNotesItemClassName =
  "flex h-7 w-full items-center gap-1.5 rounded-md px-2 text-xs font-medium transition-colors";

export function AppSidebar() {
  const location = useLocation();
  const perms = usePermissions();
  const activeJobs = useActiveJobsCount();
  const visibleItems = navItems.filter((item) => !item.requires || item.requires(perms));

  const isActive = (href: string) => {
    if (href === "/") return location.pathname === "/";
    return location.pathname.startsWith(href);
  };

  const renderItem = (item: NavItem) => {
    const active = isActive(item.href);
    // The badge and the active dot share the right edge, so only one shows.
    const badgeCount = item.href === "/jobs" ? activeJobs : 0;
    return (
      <SidebarMenuItem key={item.href}>
        <Link
          to={item.href}
          title={item.title}
          // Collapsed, the label is display:none and drops out of the
          // accessible name, so the count is spelled out on the link itself.
          aria-label={
            badgeCount > 0
              ? `${item.title}, ${badgeCount} active job${badgeCount === 1 ? "" : "s"}`
              : undefined
          }
          className={`${sidebarItemClassName} ${
            badgeCount > 0 ? "pr-11 group-data-[collapsible=icon]:pr-0" : ""
          } ${
            active
              ? "bg-sidebar-accent text-sidebar-accent-foreground shadow-sm"
              : sidebarItemInactiveClassName
          }`}
        >
          <item.icon className="h-4.5 w-4.5 shrink-0" />
          <span className="truncate group-data-[collapsible=icon]:hidden">{item.title}</span>
          {badgeCount > 0 && (
            <Badge
              variant="destructive"
              aria-hidden="true"
              data-slot="jobs-badge"
              className="pointer-events-none absolute right-2 top-1/2 h-5 min-w-5 -translate-y-1/2 px-1.5 text-[10px] leading-none font-bold tabular-nums group-data-[collapsible=icon]:right-0 group-data-[collapsible=icon]:top-0 group-data-[collapsible=icon]:h-4 group-data-[collapsible=icon]:min-w-4 group-data-[collapsible=icon]:translate-y-0 group-data-[collapsible=icon]:px-1 group-data-[collapsible=icon]:text-[9px]"
            >
              {badgeCount > 99 ? "99+" : badgeCount}
            </Badge>
          )}
          {active && badgeCount === 0 && (
            <span className="absolute right-3 h-1.5 w-1.5 rounded-full bg-sidebar-primary shrink-0 group-data-[collapsible=icon]:hidden" />
          )}
        </Link>
      </SidebarMenuItem>
    );
  };

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="px-5 py-5 border-b border-sidebar-border group-data-[collapsible=icon]:px-2 group-data-[collapsible=icon]:py-3">
        <Link to="/" className="flex flex-col items-center gap-1.5 group-data-[collapsible=icon]:items-center">
          <img
            src={`${import.meta.env.BASE_URL}openrag-title-white.svg`}
            alt={APP_NAME}
            className="h-7 w-auto object-contain group-data-[collapsible=icon]:h-7 group-data-[collapsible=icon]:w-7 group-data-[collapsible=icon]:object-cover group-data-[collapsible=icon]:object-left"
          />
          <span className="text-[0.6rem] font-semibold uppercase tracking-[0.18em] text-center text-sidebar-foreground/60 group-data-[collapsible=icon]:hidden">
            {perms.isAdmin ? "Admin Console" : "Console"}
          </span>
        </Link>
      </SidebarHeader>
      <SidebarContent className="px-3 py-3 overflow-x-hidden group-data-[collapsible=icon]:px-1.5">
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>{visibleItems.map(renderItem)}</SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      {perms.isAdmin && (
        <div
          data-slot="sidebar-release-notes"
          className="shrink-0 px-3 pb-3 group-data-[collapsible=icon]:hidden"
        >
          <SidebarMenu>
            <ReleaseNotes className={releaseNotesItemClassName} />
          </SidebarMenu>
        </div>
      )}
      <SidebarFooter className="px-3 py-3 border-t border-sidebar-border group-data-[collapsible=icon]:px-1.5">
        <SidebarMenu>{renderItem(settingsItem)}</SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  );
}
