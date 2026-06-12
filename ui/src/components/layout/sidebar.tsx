import { Link, useLocation } from "react-router-dom";
import {
  LayoutDashboard,
  Database,
  FileText,
  Clock,
  Cpu,
  Settings,
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
import { APP_NAME } from "@/lib/brand";

interface NavItem {
  title: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
}

// Admin-only navigation — this is an admin console (chat lives in Chainlit).
const adminItems: NavItem[] = [
  { title: "Overview", href: "/", icon: LayoutDashboard },
  { title: "Partitions", href: "/partitions", icon: Database },
  { title: "Documents", href: "/documents", icon: FileText },
  { title: "Jobs", href: "/jobs", icon: Clock },
  { title: "Models", href: "/models", icon: Cpu },
  { title: "Presets", href: "/presets", icon: Settings },
  { title: "Users", href: "/users", icon: Users },
  { title: "System", href: "/system", icon: Activity },
];

const settingsItem: NavItem = { title: "Settings", href: "/settings", icon: UserCog };

export function AppSidebar() {
  const location = useLocation();

  const isActive = (href: string) => {
    if (href === "/") return location.pathname === "/";
    return location.pathname.startsWith(href);
  };

  const renderItem = (item: NavItem) => {
    const active = isActive(item.href);
    return (
      <SidebarMenuItem key={item.href}>
        <Link
          to={item.href}
          title={item.title}
          className={`relative flex items-center gap-3 px-3 py-2 rounded-xl text-sm font-medium transition-colors overflow-hidden group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0 ${
            active
              ? "bg-sidebar-accent text-sidebar-accent-foreground shadow-sm"
              : "text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground"
          }`}
        >
          <item.icon className="h-4.5 w-4.5 shrink-0" />
          <span className="truncate group-data-[collapsible=icon]:hidden">{item.title}</span>
          {active && (
            <span className="absolute right-3 h-1.5 w-1.5 rounded-full bg-sidebar-primary shrink-0 group-data-[collapsible=icon]:hidden" />
          )}
        </Link>
      </SidebarMenuItem>
    );
  };

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className="px-5 py-5 border-b border-sidebar-border group-data-[collapsible=icon]:px-2 group-data-[collapsible=icon]:py-3">
        <Link to="/" className="flex flex-col gap-1.5 group-data-[collapsible=icon]:items-center">
          <img
            src="/openrag-title.svg"
            alt={APP_NAME}
            className="h-7 w-auto object-contain group-data-[collapsible=icon]:h-7 group-data-[collapsible=icon]:w-7 group-data-[collapsible=icon]:object-cover group-data-[collapsible=icon]:object-left"
          />
          <span className="text-[0.7rem] font-semibold uppercase tracking-[0.18em] text-muted-foreground group-data-[collapsible=icon]:hidden">
            Admin Console
          </span>
        </Link>
      </SidebarHeader>
      <SidebarContent className="px-3 py-3 overflow-x-hidden group-data-[collapsible=icon]:px-1.5">
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>{adminItems.map(renderItem)}</SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter className="px-3 py-3 border-t border-sidebar-border group-data-[collapsible=icon]:px-1.5">
        <SidebarMenu>{renderItem(settingsItem)}</SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  );
}
