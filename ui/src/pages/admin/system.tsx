import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Circle, ExternalLink, RotateCcw } from "lucide-react";
import {
  healthCheck,
  getVersion,
  getConfig,
  getMetrics,
  listActors,
  restartActor,
} from "@/lib/api/system";
import { PageHeader } from "@/components/shared/page-header";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

const BUILD_TIME_GRAFANA_URL = import.meta.env.VITE_GRAFANA_URL || "";

function actorStateColor(state: string): string {
  const s = state.toUpperCase();
  if (s === "ALIVE") return "text-green-500";
  if (s === "DEAD") return "text-red-500";
  return "text-gray-400";
}

export default function SystemPage() {
  const { data: config } = useQuery({
    queryKey: ["system-config"],
    queryFn: getConfig,
    staleTime: Infinity,
    refetchOnMount: "always",
  });
  const runtimeGrafanaUrl =
    typeof config?.grafana_url === "string" ? config.grafana_url.trim() : "";
  const grafanaUrl = runtimeGrafanaUrl || BUILD_TIME_GRAFANA_URL;

  return (
    <div>
      <PageHeader
        title="System"
        description="Status, Ray actors, metrics and configuration"
      />

      <Tabs defaultValue="status">
        <TabsList>
          <TabsTrigger value="status">Status</TabsTrigger>
          <TabsTrigger value="actors">Actors</TabsTrigger>
          <TabsTrigger value="metrics">Metrics</TabsTrigger>
          <TabsTrigger value="config">Config</TabsTrigger>
        </TabsList>

        <TabsContent value="status">
          <StatusTab />
        </TabsContent>
        <TabsContent value="actors">
          <ActorsTab />
        </TabsContent>
        <TabsContent value="metrics">
          <MetricsTab grafanaUrl={grafanaUrl} />
        </TabsContent>
        <TabsContent value="config">
          <ConfigTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function StatusTab() {
  const healthQuery = useQuery({
    queryKey: ["system-health"],
    queryFn: healthCheck,
    refetchInterval: 15000,
  });
  const versionQuery = useQuery({ queryKey: ["system-version"], queryFn: getVersion });

  const apiUp = healthQuery.isSuccess;

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>API Status</CardTitle>
        <Button variant="outline" size="sm" onClick={() => healthQuery.refetch()}>
          Refresh
        </Button>
      </CardHeader>
      <CardContent>
        {healthQuery.isLoading ? (
          <Skeleton className="h-16" />
        ) : (
          <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
            <div className="space-y-1">
              <dt className="text-muted-foreground">API</dt>
              <dd>
                <Badge variant={apiUp ? "default" : "destructive"}>{apiUp ? "Up" : "Down"}</Badge>
              </dd>
            </div>
            <div className="space-y-1">
              <dt className="text-muted-foreground">Version</dt>
              <dd className="font-mono font-medium">{versionQuery.data?.version ?? "—"}</dd>
            </div>
          </dl>
        )}
      </CardContent>
    </Card>
  );
}

function ActorsTab() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["system-actors"],
    queryFn: listActors,
    refetchInterval: 15000,
  });

  const restartMut = useMutation({
    mutationFn: (name: string) => restartActor(name),
    onSuccess: (res) => {
      toast.success(res.message ?? "Actor restarted");
      queryClient.invalidateQueries({ queryKey: ["system-actors"] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const actors = data?.actors ?? [];

  return (
    <Card>
      <CardHeader>
        <CardTitle>Ray Actors</CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-48" />
        ) : actors.length === 0 ? (
          <p className="text-muted-foreground text-center py-6">No actors found.</p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {actors.map((a) => (
              <div key={a.actor_id} className="flex items-center gap-3 rounded-lg border p-3">
                <Circle className={`h-3 w-3 flex-shrink-0 fill-current ${actorStateColor(a.state)}`} />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium truncate">{a.name}</p>
                  <p className="text-xs text-muted-foreground truncate">
                    {a.class_name} · {a.state.toLowerCase()}
                  </p>
                </div>
                <ConfirmDialog
                  title={`Restart ${a.name}?`}
                  description="Kills and recreates the actor. Ongoing operations on it may be interrupted."
                  onConfirm={() => restartMut.mutate(a.name)}
                >
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 flex-shrink-0"
                    disabled={restartMut.isPending}
                    aria-label={`Restart ${a.name}`}
                    title={`Restart ${a.name}`}
                  >
                    <RotateCcw className="h-3.5 w-3.5" />
                  </Button>
                </ConfirmDialog>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function MetricsTab({ grafanaUrl }: { grafanaUrl: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["system-metrics"],
    queryFn: getMetrics,
    refetchInterval: 10000,
  });

  // Parse Prometheus text format into simple metric entries.
  const lines = (data || "").split("\n").filter((l) => l && !l.startsWith("#"));
  const parsed = lines.map((line) => {
    const idx = line.lastIndexOf(" ");
    return idx === -1
      ? { name: line, value: "" }
      : { name: line.slice(0, idx), value: line.slice(idx + 1) };
  });

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>Prometheus Metrics</CardTitle>
        {grafanaUrl ? (
          <Button variant="outline" size="sm" asChild>
            <a
              href={grafanaUrl}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Open metrics dashboard in Grafana (opens in a new tab)"
            >
              <ExternalLink className="mr-2 h-3.5 w-3.5" />
              Open in Grafana
            </a>
          </Button>
        ) : (
          <Dialog>
            <DialogTrigger asChild>
              <Button variant="outline" size="sm">
                <ExternalLink className="mr-2 h-3.5 w-3.5" />
                Open in Grafana
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Grafana is not configured</DialogTitle>
                <DialogDescription>
                  Set <code className="font-mono text-foreground">GRAFANA_URL</code> to the
                  browser-reachable OpenRAG dashboard URL, then restart the API. This action will
                  open that dashboard without rebuilding the Admin UI.
                </DialogDescription>
              </DialogHeader>
              <DialogFooter showCloseButton />
            </DialogContent>
          </Dialog>
        )}
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-48" />
        ) : parsed.length === 0 ? (
          <p className="text-muted-foreground text-center py-4">No metrics available</p>
        ) : (
          <div className="overflow-auto max-h-[600px]">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b">
                  <th className="text-left py-2 pr-4 font-medium">Metric</th>
                  <th className="text-right py-2 font-medium">Value</th>
                </tr>
              </thead>
              <tbody>
                {parsed.map((m, i) => (
                  <tr key={i} className="border-b">
                    <td className="py-1.5 pr-4 font-mono text-xs break-all">{m.name}</td>
                    <td className="py-1.5 text-right font-mono text-xs">{m.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ConfigTab() {
  const { data, isLoading, error } = useQuery({ queryKey: ["system-config"], queryFn: getConfig });

  if (isLoading) return <Skeleton className="h-48" />;
  if (error)
    return (
      <Card>
        <CardContent className="py-8 text-center text-muted-foreground">
          Access denied. Only admins can view system configuration.
        </CardContent>
      </Card>
    );

  return (
    <Card>
      <CardHeader>
        <CardTitle>System Configuration</CardTitle>
      </CardHeader>
      <CardContent>
        <pre className="overflow-auto max-h-[600px] rounded-md bg-muted p-4 text-xs font-mono">
          {JSON.stringify(data, null, 2)}
        </pre>
      </CardContent>
    </Card>
  );
}
