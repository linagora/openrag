import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Circle, ExternalLink, RotateCcw } from "lucide-react";
import { health, config, metrics, restartMarkerWorker, restartMarkerPool, type RayDetail } from "@/lib/api/system";
import { PageHeader } from "@/components/shared/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";

const GRAFANA_URL = import.meta.env.VITE_GRAFANA_URL || "";

export default function SystemPage() {
  return (
    <div>
      <PageHeader
        title="System"
        description="Health checks, metrics, configuration, and tools"
        actions={
          GRAFANA_URL ? (
            <Button variant="outline" asChild>
              <a href={GRAFANA_URL} target="_blank" rel="noopener noreferrer">
                <ExternalLink className="mr-2 h-4 w-4" />
                View in Grafana
              </a>
            </Button>
          ) : undefined
        }
      />

      <Tabs defaultValue="health">
        <TabsList>
          <TabsTrigger value="health">Health</TabsTrigger>
          <TabsTrigger value="metrics">Metrics</TabsTrigger>
          <TabsTrigger value="config">Config</TabsTrigger>
        </TabsList>

        <TabsContent value="health">
          <HealthTab />
        </TabsContent>
        <TabsContent value="metrics">
          <MetricsTab />
        </TabsContent>
        <TabsContent value="config">
          <ConfigTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function HealthTab() {
  const { data, isLoading, refetch } = useQuery({
    queryKey: ["system-health"],
    queryFn: health,
    refetchInterval: 15000,
  });

  if (isLoading) return <Skeleton className="h-48" />;
  if (!data) return <p>Failed to load health data</p>;

  const serviceEntries = Object.entries(data.services);

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            System Status
            <Badge variant={data.status === "ok" ? "default" : "destructive"}>
              {data.status.toUpperCase()}
            </Badge>
          </CardTitle>
          <Button variant="outline" size="sm" onClick={() => refetch()}>
            Refresh
          </Button>
        </CardHeader>
        <CardContent>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {serviceEntries.map(([name, healthy]) => (
              <div
                key={name}
                className="flex items-center gap-3 rounded-lg border p-3"
              >
                <Circle
                  className={`h-3 w-3 flex-shrink-0 fill-current ${
                    healthy ? "text-green-500" : "text-red-500"
                  }`}
                />
                <div className="min-w-0">
                  <p className="text-sm font-medium truncate">{name}</p>
                  <p className="text-xs text-muted-foreground">
                    {healthy ? "Healthy" : "Unhealthy"}
                  </p>
                </div>
              </div>
            ))}
            {serviceEntries.length === 0 && (
              <p className="col-span-full text-center text-muted-foreground py-4">
                No endpoints configured
              </p>
            )}
          </div>
        </CardContent>
      </Card>

      {data.ray_detail && <RayDetailSection detail={data.ray_detail} />}
    </div>
  );
}

function RayDetailSection({ detail }: { detail: RayDetail }) {
  const { nodes, serve_deployments, marker_pool } = detail;
  const queryClient = useQueryClient();
  const [restartingIndex, setRestartingIndex] = useState<number | null>(null);
  const [restartingAll, setRestartingAll] = useState(false);

  const handleRestartAll = async () => {
    setRestartingAll(true);
    try {
      await restartMarkerPool();
    } finally {
      setRestartingAll(false);
      queryClient.invalidateQueries({ queryKey: ["system-health"] });
    }
  };

  const handleRestart = async (index: number) => {
    setRestartingIndex(index);
    try {
      await restartMarkerWorker(index);
    } finally {
      setRestartingIndex(null);
      queryClient.invalidateQueries({ queryKey: ["system-health"] });
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Ray Cluster</CardTitle>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Nodes */}
        <div>
          <h4 className="text-sm font-medium mb-2">Nodes</h4>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {nodes.map((node) => (
              <div
                key={node.id}
                className="flex items-center gap-3 rounded-lg border p-3"
              >
                <Circle
                  className={`h-3 w-3 flex-shrink-0 fill-current ${
                    node.alive ? "text-green-500" : "text-red-500"
                  }`}
                />
                <div className="min-w-0">
                  <p className="text-sm font-medium truncate font-mono">
                    {node.address}
                    <Badge
                      variant={node.is_head ? "default" : "outline"}
                      className="ml-2 text-[10px] px-1.5 py-0"
                    >
                      {node.is_head ? "head" : "worker"}
                    </Badge>
                  </p>
                  <p className="text-xs text-muted-foreground truncate">
                    {node.id.slice(0, 12)}
                    {" — "}
                    {Object.entries(node.resources)
                      .filter(([k]) => k !== "memory" && k !== "object_store_memory")
                      .map(([k, v]) => `${k}: ${v}`)
                      .join(", ")}
                  </p>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Serve Deployments */}
        {serve_deployments.length > 0 && (
          <div>
            <h4 className="text-sm font-medium mb-2">Serve Deployments</h4>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              {serve_deployments.map((dep) => (
                <div
                  key={dep.name}
                  className="flex items-center gap-3 rounded-lg border p-3"
                >
                  <Circle
                    className={`h-3 w-3 flex-shrink-0 fill-current ${
                      dep.status === "HEALTHY"
                        ? "text-green-500"
                        : dep.status === "UPDATING"
                          ? "text-yellow-500"
                          : "text-red-500"
                    }`}
                  />
                  <div className="min-w-0">
                    <p className="text-sm font-medium truncate">{dep.name}</p>
                    <p className="text-xs text-muted-foreground">
                      {dep.status.toLowerCase()}
                      {dep.message && ` — ${dep.message}`}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Marker Pool */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-sm font-medium">
              Marker Worker Pool
              <span className="ml-2 text-xs font-normal text-muted-foreground">
                {detail.marker_pool_summary.alive}/{detail.marker_pool_summary.max_actors} alive
                {detail.marker_pool_summary.spawned === 0 &&
                  " — actors spawn on first ingestion"}
              </span>
            </h4>
            <Button
              variant="outline"
              size="sm"
              disabled={restartingAll}
              onClick={handleRestartAll}
            >
              <RotateCcw
                className={`mr-2 h-3.5 w-3.5 ${restartingAll ? "animate-spin" : ""}`}
              />
              {restartingAll ? "Restarting…" : "Restart All"}
            </Button>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {marker_pool.map((w, i) => {
              const isRestarting = restartingIndex === i;
              return (
                <div
                  key={w.name}
                  className="flex items-center gap-3 rounded-lg border p-3"
                >
                  <Circle
                    className={`h-3 w-3 flex-shrink-0 fill-current ${
                      w.status === "alive"
                        ? "text-green-500"
                        : w.status === "dead"
                          ? "text-red-500"
                          : "text-gray-400"
                    }`}
                  />
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium truncate">{w.name}</p>
                    <p className="text-xs text-muted-foreground">
                      {isRestarting
                        ? "restarting…"
                        : w.status === "idle"
                          ? "not spawned"
                          : `${w.status} · ping: ${w.ping}`}
                    </p>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7 flex-shrink-0"
                    disabled={isRestarting}
                    onClick={() => handleRestart(i)}
                  >
                    <RotateCcw
                      className={`h-3.5 w-3.5 ${isRestarting ? "animate-spin" : ""}`}
                    />
                  </Button>
                </div>
              );
            })}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function MetricsTab() {
  const { data, isLoading } = useQuery({
    queryKey: ["system-metrics"],
    queryFn: metrics,
    refetchInterval: 10000,
  });

  if (isLoading) return <Skeleton className="h-48" />;

  // Parse Prometheus text format into simple metric entries
  const lines = (data || "").split("\n").filter((l) => l && !l.startsWith("#"));
  const parsed = lines.map((line) => {
    const parts = line.split(" ");
    return { name: parts[0], value: parts[1] || "0" };
  });

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <CardTitle>Prometheus Metrics</CardTitle>
        {GRAFANA_URL && (
          <Button variant="outline" size="sm" asChild>
            <a href={GRAFANA_URL} target="_blank" rel="noopener noreferrer">
              <ExternalLink className="mr-2 h-3.5 w-3.5" />
              Grafana
            </a>
          </Button>
        )}
      </CardHeader>
      <CardContent>
        {parsed.length === 0 ? (
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
                    <td className="py-1.5 pr-4 font-mono text-xs break-all">
                      {m.name}
                    </td>
                    <td className="py-1.5 text-right font-mono text-xs">
                      {m.value}
                    </td>
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
  const { data, isLoading, error } = useQuery({
    queryKey: ["system-config"],
    queryFn: config,
  });

  if (isLoading) return <Skeleton className="h-48" />;
  if (error)
    return (
      <Card>
        <CardContent className="py-8 text-center text-muted-foreground">
          Access denied. Only superadmins can view system configuration.
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

