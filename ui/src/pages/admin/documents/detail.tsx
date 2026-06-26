import { useRef, useState } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Trash2, Copy, RefreshCw, FileText, BookOpen, Hash, ChevronRight } from "lucide-react";
import { toast } from "sonner";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { formatDate, cn } from "@/lib/utils";
import { listFileChunks, type PartitionChunk } from "@/lib/api/documents";
import { replaceFile, deleteFile, copyFile, newFileId } from "@/lib/api/indexing";
import { listPartitions } from "@/lib/api/partitions";

const str = (v: unknown): string => (v == null ? "" : String(v));

// File-level metadata keys are flattened onto each chunk; hide chunk-internal ones.
const HIDDEN_META = new Set(["_id", "vector", "content", "embedding"]);

function ChunksTab({ chunks }: { chunks: PartitionChunk[] }) {
  const [selected, setSelected] = useState(0);

  if (chunks.length === 0) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-muted-foreground">
          <FileText className="mx-auto h-12 w-12 mb-4 opacity-40" />
          <p className="text-lg font-medium">No chunks</p>
          <p className="text-sm mt-1">This file has no indexed chunks.</p>
        </CardContent>
      </Card>
    );
  }

  const chunk = chunks[Math.min(selected, chunks.length - 1)];
  const page = chunk.metadata?.page;

  return (
    <div className="grid grid-cols-[320px_1fr] gap-4" style={{ height: "calc(100vh - 300px)", minHeight: "500px" }}>
      <Card className="flex flex-col overflow-hidden">
        <CardHeader className="pb-3 flex-shrink-0">
          <CardTitle className="text-sm font-medium">Chunks ({chunks.length})</CardTitle>
        </CardHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4 space-y-1">
          {chunks.map((c, i) => {
            const preview = c.content.slice(0, 80).replace(/[#*_\n]/g, " ").trim();
            return (
              <button
                key={str(c.metadata?._id) || c.link || i}
                onClick={() => setSelected(i)}
                className={cn(
                  "w-full text-left rounded-md px-3 py-2.5 transition-colors text-sm",
                  "hover:bg-accent hover:text-accent-foreground",
                  i === selected ? "bg-accent text-accent-foreground" : "text-muted-foreground",
                )}
              >
                <div className="flex items-center justify-between mb-0.5">
                  <span className="flex items-center gap-1.5">
                    <Hash className="h-3 w-3 flex-shrink-0" />
                    <span className="font-medium text-foreground">{i + 1}</span>
                  </span>
                  <ChevronRight className={cn("h-3 w-3 transition-opacity", i === selected ? "opacity-100" : "opacity-0")} />
                </div>
                <p className="text-xs truncate">{preview || "Empty chunk"}</p>
                {page != null && <p className="text-[10px] text-muted-foreground mt-1">p.{str(page)}</p>}
              </button>
            );
          })}
        </div>
      </Card>

      <Card className="flex flex-col overflow-hidden">
        <CardHeader className="flex-shrink-0 pb-3">
          <div className="flex items-center justify-between">
            <CardTitle className="text-sm font-medium">Chunk #{selected + 1}</CardTitle>
            {page != null && <Badge variant="outline" className="text-xs">Page {str(page)}</Badge>}
          </div>
        </CardHeader>
        <Separator />
        <div className="flex-1 overflow-y-auto p-6 prose prose-sm dark:prose-invert max-w-none">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{chunk.content}</ReactMarkdown>
        </div>
      </Card>
    </div>
  );
}

function DetailsCard({ metadata }: { metadata: Record<string, unknown> }) {
  const entries = Object.entries(metadata).filter(([k]) => !HIDDEN_META.has(k));
  return (
    <Card>
      <CardHeader>
        <CardTitle>File Metadata</CardTitle>
      </CardHeader>
      <CardContent>
        {entries.length === 0 ? (
          <p className="text-sm text-muted-foreground">No metadata available.</p>
        ) : (
          <dl className="space-y-2 text-sm">
            {entries.map(([key, value]) => (
              <div key={key} className="flex justify-between gap-4">
                <dt className="text-muted-foreground capitalize">{key.replace(/_/g, " ")}</dt>
                <dd className="font-medium max-w-[60%] truncate text-right" title={str(value)}>
                  {/(_at|date)$/.test(key) && value ? formatDate(str(value)) : (str(value) || "—")}
                </dd>
              </div>
            ))}
          </dl>
        )}
      </CardContent>
    </Card>
  );
}

export default function DocumentDetailPage() {
  const { partition, fileId } = useParams<{ partition: string; fileId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Return to the documents list scoped to THIS file's partition, so "Back"
  // (and post-delete) lands where the user came from, not the default partition.
  const documentsHref = `/documents?partition=${encodeURIComponent(partition ?? "")}`;

  const [replaceSel, setReplaceSel] = useState<File | null>(null);
  const [copyTarget, setCopyTarget] = useState("");
  const replaceFileRef = useRef<HTMLInputElement>(null);

  const chunksKey = ["file-chunks", partition, fileId];
  const chunksQuery = useQuery({
    queryKey: chunksKey,
    queryFn: () => listFileChunks(partition!, fileId!),
    enabled: !!partition && !!fileId,
  });

  const partitionsQuery = useQuery({ queryKey: ["partitions"], queryFn: listPartitions });

  const replaceMutation = useMutation({
    mutationFn: () => {
      if (!replaceSel) throw new Error("No file selected");
      return replaceFile(partition!, fileId!, replaceSel);
    },
    onSuccess: () => {
      toast.success("File replacement queued — track progress in Jobs.");
      setReplaceSel(null);
      if (replaceFileRef.current) replaceFileRef.current.value = "";
      queryClient.invalidateQueries({ queryKey: chunksKey });
    },
    onError: (err: Error) => toast.error(`Failed to replace: ${err.message}`),
  });

  const copyMutation = useMutation({
    mutationFn: () => {
      if (!copyTarget) throw new Error("No partition selected");
      return copyFile(copyTarget, newFileId(), partition!, fileId!);
    },
    onSuccess: () => {
      toast.success(`Copied to partition "${copyTarget}"`);
      setCopyTarget("");
    },
    onError: (err: Error) => toast.error(`Failed to copy: ${err.message}`),
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteFile(partition!, fileId!),
    onSuccess: () => {
      toast.success("File deleted");
      navigate(documentsHref);
    },
    onError: (err: Error) => toast.error(`Failed to delete: ${err.message}`),
  });

  if (!partition || !fileId) {
    return <div className="text-center py-12 text-muted-foreground">Invalid file reference.</div>;
  }

  if (chunksQuery.isLoading) {
    return <div className="flex items-center justify-center py-12 text-muted-foreground">Loading file…</div>;
  }
  if (chunksQuery.isError) {
    return (
      <div className="flex items-center justify-center py-12 text-destructive">
        Failed to load file: {(chunksQuery.error as Error).message}
      </div>
    );
  }

  const chunks = chunksQuery.data ?? [];
  const metadata = chunks[0]?.metadata ?? {};
  const filename = str(metadata.filename) || fileId;
  const mimetype = str(metadata.mimetype);
  const copyTargets = (partitionsQuery.data?.partitions ?? []).filter((p) => p.partition !== partition);

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="sm" asChild>
          <Link to={documentsHref}>
            <ArrowLeft className="h-4 w-4" />
            Back to Documents
          </Link>
        </Button>
      </div>

      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{filename}</h1>
          <div className="flex items-center gap-2 mt-1 text-muted-foreground">
            <span className="text-sm font-mono">{fileId}</span>
            <span>·</span>
            <span className="text-sm">{partition}</span>
            {mimetype && (
              <>
                <span>·</span>
                <span className="text-sm">{mimetype}</span>
              </>
            )}
          </div>
        </div>
        <ConfirmDialog
          title="Delete File"
          description={`Delete "${filename}" and all its chunks? This cannot be undone.`}
          onConfirm={() => deleteMutation.mutate()}
        >
          <Button variant="destructive" disabled={deleteMutation.isPending}>
            <Trash2 className="h-4 w-4" />
            {deleteMutation.isPending ? "Deleting..." : "Delete File"}
          </Button>
        </ConfirmDialog>
      </div>

      <Tabs defaultValue="chunks">
        <TabsList>
          <TabsTrigger value="chunks">
            <BookOpen className="h-4 w-4" />
            Chunks ({chunks.length})
          </TabsTrigger>
          <TabsTrigger value="details">
            <FileText className="h-4 w-4" />
            Details
          </TabsTrigger>
        </TabsList>

        <TabsContent value="chunks" className="mt-4">
          <ChunksTab chunks={chunks} />
        </TabsContent>

        <TabsContent value="details" className="mt-4">
          <div className="grid gap-6 md:grid-cols-2">
            <DetailsCard metadata={metadata} />

            <Card>
              <CardHeader>
                <CardTitle>Actions</CardTitle>
                <CardDescription>Replace the file or copy it into another partition.</CardDescription>
              </CardHeader>
              <CardContent className="space-y-6">
                <div className="space-y-3">
                  <p className="text-sm font-medium">Replace file</p>
                  <input
                    type="file"
                    ref={replaceFileRef}
                    className="hidden"
                    onChange={(e) => setReplaceSel(e.target.files?.[0] ?? null)}
                  />
                  <div className="flex items-center gap-3">
                    <Button variant="outline" size="sm" onClick={() => replaceFileRef.current?.click()}>
                      Choose File
                    </Button>
                    <span className="text-sm text-muted-foreground truncate">
                      {replaceSel?.name ?? "No file selected"}
                    </span>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => replaceMutation.mutate()}
                    disabled={!replaceSel || replaceMutation.isPending}
                  >
                    <RefreshCw className="h-4 w-4" />
                    {replaceMutation.isPending ? "Replacing..." : "Replace"}
                  </Button>
                </div>

                <Separator />

                <div className="space-y-3">
                  <p className="text-sm font-medium">Copy to partition</p>
                  <div className="flex gap-2">
                    <Select value={copyTarget} onValueChange={setCopyTarget}>
                      <SelectTrigger className="flex-1">
                        <SelectValue placeholder="Select partition" />
                      </SelectTrigger>
                      <SelectContent>
                        {copyTargets.map((p) => (
                          <SelectItem key={p.partition} value={p.partition}>
                            {p.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <Button
                      variant="outline"
                      onClick={() => copyMutation.mutate()}
                      disabled={!copyTarget || copyMutation.isPending}
                    >
                      <Copy className="h-4 w-4" />
                      {copyMutation.isPending ? "Copying..." : "Copy"}
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
