import { useState, useRef, useEffect } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Trash2, Copy, RefreshCw, FileText, Hash, BookOpen, ChevronRight, Tag, Settings2, ChevronsUpDown, Check } from "lucide-react";
import { toast } from "sonner";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { StatusBadge } from "@/components/shared/status-badge";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

import { Skeleton } from "@/components/ui/skeleton";
import { formatDate, formatBytes, cn } from "@/lib/utils";
import {
  getDocument,
  getDocumentChunks,
  replaceDocument,
  copyDocument,
  deleteDocument,
} from "@/lib/api/documents";
import type { ChunkResponse, DocumentResponse } from "@/lib/api/documents";
import { getPartition, searchPartitions } from "@/lib/api/partitions";

const ENTITY_TYPE_COLORS: Record<string, string> = {
  person: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300",
  organization: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300",
  location: "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300",
  date: "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300",
  event: "bg-rose-100 text-rose-800 dark:bg-rose-900/30 dark:text-rose-300",
};
const TOPIC_BADGE_STYLE = "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300";

function getEntityColor(type: string): string {
  return ENTITY_TYPE_COLORS[type.toLowerCase()] ?? "bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300";
}

function ChunksTab({ documentId, chunkCount }: { documentId: string; chunkCount: number }) {
  const [selectedChunkId, setSelectedChunkId] = useState<string | null>(null);

  const chunksQuery = useQuery({
    queryKey: ["document-chunks", documentId],
    queryFn: () => getDocumentChunks(documentId),
    enabled: chunkCount > 0,
  });

  const chunks = chunksQuery.data?.chunks ?? [];
  const selectedChunk = chunks.find((c) => c.id === selectedChunkId) ?? chunks[0] ?? null;

  if (chunkCount === 0) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-muted-foreground">
          <FileText className="mx-auto h-12 w-12 mb-4 opacity-40" />
          <p className="text-lg font-medium">No chunks available</p>
          <p className="text-sm mt-1">This document has not been indexed yet.</p>
        </CardContent>
      </Card>
    );
  }

  if (chunksQuery.isLoading) {
    return (
      <div className="grid grid-cols-[320px_1fr] gap-4 h-[600px]">
        <Card>
          <CardHeader className="pb-3">
            <Skeleton className="h-5 w-24" />
          </CardHeader>
          <CardContent className="space-y-2">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-14 w-full" />
            ))}
          </CardContent>
        </Card>
        <Card>
          <CardContent className="py-12">
            <Skeleton className="h-6 w-48 mb-4" />
            <Skeleton className="h-4 w-full mb-2" />
            <Skeleton className="h-4 w-3/4 mb-2" />
            <Skeleton className="h-4 w-5/6" />
          </CardContent>
        </Card>
      </div>
    );
  }

  if (chunksQuery.isError) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-destructive">
          Failed to load chunks: {(chunksQuery.error as Error).message}
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="grid grid-cols-[320px_1fr] gap-4" style={{ height: "calc(100vh - 300px)", minHeight: "500px" }}>
      {/* Chunk list sidebar */}
      <Card className="flex flex-col overflow-hidden">
        <CardHeader className="pb-3 flex-shrink-0">
          <CardTitle className="text-sm font-medium">
            Chunks ({chunks.length})
          </CardTitle>
        </CardHeader>
        <div className="flex-1 overflow-y-auto px-4 pb-4 space-y-1">
          {chunks.map((chunk) => (
            <ChunkListItem
              key={chunk.id}
              chunk={chunk}
              isSelected={chunk.id === (selectedChunkId ?? chunks[0]?.id)}
              onSelect={() => setSelectedChunkId(chunk.id)}
            />
          ))}
        </div>
      </Card>

      {/* Chunk preview */}
      <Card className="flex flex-col overflow-hidden">
        {selectedChunk ? (
          <>
            <CardHeader className="flex-shrink-0 pb-3">
              <div className="flex items-center justify-between">
                <CardTitle className="text-sm font-medium">
                  Chunk #{selectedChunk.chunk_index}
                  {selectedChunk.header && (
                    <span className="ml-2 font-normal text-muted-foreground">
                      — {selectedChunk.header}
                    </span>
                  )}
                </CardTitle>
                <div className="flex items-center gap-2">
                  {selectedChunk.page_number !== null && (
                    <Badge variant="outline" className="text-xs">
                      Page {selectedChunk.page_number}
                    </Badge>
                  )}
                  <Badge variant="secondary" className="text-xs">
                    {selectedChunk.token_count} tokens
                  </Badge>
                  <Badge variant="outline" className="text-xs">
                    {selectedChunk.chunk_type}
                  </Badge>
                </div>
              </div>
              {selectedChunk.context && (
                <p className="text-xs text-muted-foreground mt-1 italic">
                  {selectedChunk.context}
                </p>
              )}
            </CardHeader>
            <Separator />
            <div className="flex-1 overflow-y-auto p-6 prose prose-sm dark:prose-invert max-w-none">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {selectedChunk.content || selectedChunk.text}
              </ReactMarkdown>
            </div>
            <ChunkMetadataPanel chunk={selectedChunk} />
          </>
        ) : (
          <CardContent className="py-12 text-center text-muted-foreground">
            Select a chunk from the list to view its content.
          </CardContent>
        )}
      </Card>
    </div>
  );
}

function ChunkListItem({
  chunk,
  isSelected,
  onSelect,
}: {
  chunk: ChunkResponse;
  isSelected: boolean;
  onSelect: () => void;
}) {
  const preview = (chunk.header || chunk.text || "").slice(0, 80).replace(/[#*_\n]/g, " ").trim();

  return (
    <button
      onClick={onSelect}
      className={cn(
        "w-full text-left rounded-md px-3 py-2.5 transition-colors text-sm",
        "hover:bg-accent hover:text-accent-foreground",
        isSelected
          ? "bg-accent text-accent-foreground"
          : "text-muted-foreground"
      )}
    >
      <div className="flex items-center justify-between mb-0.5">
        <span className="flex items-center gap-1.5">
          <Hash className="h-3 w-3 flex-shrink-0" />
          <span className="font-medium text-foreground">{chunk.chunk_index}</span>
          {chunk.chunk_type !== "text" && (
            <Badge variant="outline" className="text-[10px] px-1 py-0">
              {chunk.chunk_type}
            </Badge>
          )}
        </span>
        <ChevronRight className={cn("h-3 w-3 transition-opacity", isSelected ? "opacity-100" : "opacity-0")} />
      </div>
      <p className="text-xs truncate">{preview || "Empty chunk"}</p>
      <div className="flex items-center gap-2 mt-1 text-[10px] text-muted-foreground">
        {chunk.page_number !== null && <span>p.{chunk.page_number}</span>}
        <span>{chunk.token_count} tok</span>
      </div>
    </button>
  );
}

function ChunkMetadataPanel({ chunk }: { chunk: ChunkResponse }) {
  const entities = chunk.entities ?? [];
  const topics = chunk.topics ?? [];

  return (
    <div className="space-y-4 p-4 border-t">
      {/* Entities section */}
      <div>
        <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide mb-2">Entities</p>
        {entities.length === 0 ? (
          <p className="text-xs text-muted-foreground/60">No entities</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {entities.map((e, i) => (
              <Badge key={`${e.name}-${e.type}-${i}`} variant="outline" className={`text-xs ${getEntityColor(e.type)}`}>
                {e.name} <span className="ml-1 opacity-60">{e.type}</span>
              </Badge>
            ))}
          </div>
        )}
      </div>
      {/* Topics section */}
      <div>
        <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide mb-2">Topics</p>
        {topics.length === 0 ? (
          <p className="text-xs text-muted-foreground/60">No topics</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {topics.map((t) => (
              <Badge key={t.tag} variant="outline" className={`text-xs ${TOPIC_BADGE_STYLE}`}>
                {t.tag}
              </Badge>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/** Document Information card with merged metadata section. */
function DocumentInfoCard({ doc, documentId }: { doc: DocumentResponse; documentId: string }) {
  const chunksQuery = useQuery({
    queryKey: ["document-chunks", documentId],
    queryFn: () => getDocumentChunks(documentId),
    enabled: doc.chunk_count > 0,
  });

  const firstChunk = chunksQuery.data?.chunks?.[0];
  const metadata = firstChunk?.metadata ?? {};
  const entries = Object.entries(metadata);

  const docFields = ["doc_type", "filename", "category", "year", "quarter", "month", "language"];
  const tags = Array.isArray(metadata.auto_tags) ? metadata.auto_tags as string[] : [];
  const knownEntries = entries.filter(([k]) => docFields.includes(k));
  const chunkInternalFields = ["has_image_caption", "has_table", "block_types", "chunk_index", "contextualized"];
  const otherEntries = entries.filter(([k]) => !docFields.includes(k) && k !== "auto_tags" && !chunkInternalFields.includes(k));
  const hasMetadata = entries.length > 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Document Information</CardTitle>
      </CardHeader>
      <CardContent>
        <dl className="space-y-3 text-sm">
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Filename</dt>
            <dd className="font-medium">{doc.filename}</dd>
          </div>
          <Separator />
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Content Type</dt>
            <dd className="font-medium">{doc.content_type}</dd>
          </div>
          <Separator />
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Partition</dt>
            <dd className="font-medium">{doc.partition}</dd>
          </div>
          <Separator />
          <div className="flex justify-between">
            <dt className="text-muted-foreground">File Size</dt>
            <dd className="font-medium">{formatBytes(doc.file_size_bytes)}</dd>
          </div>
          <Separator />
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Chunks</dt>
            <dd className="font-medium">{doc.chunk_count}</dd>
          </div>
          <Separator />
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Created</dt>
            <dd className="font-medium">{formatDate(doc.created_at)}</dd>
          </div>
          <Separator />
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Indexed At</dt>
            <dd className="font-medium">{formatDate(doc.indexed_at)}</dd>
          </div>
          {doc.job_id && (
            <>
              <Separator />
              <div className="flex justify-between">
                <dt className="text-muted-foreground">Job</dt>
                <dd>
                  <Link
                    to={`/jobs/${doc.job_id}`}
                    className="text-primary hover:underline font-medium"
                  >
                    {doc.job_id.slice(0, 8)}...
                  </Link>
                </dd>
              </div>
            </>
          )}
          {(doc.error_message || doc.retry_count > 0) && (
            <>
              <Separator />
              <div className="space-y-1">
                <dt className="text-muted-foreground">Error</dt>
                {doc.retry_count > 0 && (
                  <dd className="text-sm text-amber-600 dark:text-amber-400 font-medium">
                    Attempt {Math.min(doc.retry_count + 1, 3)}/3
                    {doc.status === "FAILED" ? " (final)" : " (retrying...)"}
                  </dd>
                )}
                {doc.error_message && (
                  <dd className="text-destructive text-sm bg-destructive/10 rounded p-2">
                    {doc.error_message}
                  </dd>
                )}
              </div>
            </>
          )}
        </dl>

        {/* Metadata section */}
        <Separator className="my-4" />
        <div className="space-y-1.5 mb-3">
          <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Metadata</p>
        </div>
        {chunksQuery.isLoading && (
          <div className="space-y-2">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        )}
        {!chunksQuery.isLoading && !hasMetadata && (
          <p className="text-sm text-muted-foreground">No metadata available</p>
        )}
        {!chunksQuery.isLoading && hasMetadata && (
          <div className="space-y-4">
            {tags.length > 0 && (
              <div className="space-y-1.5">
                <dt className="text-sm text-muted-foreground flex items-center gap-1.5">
                  <Tag className="h-3.5 w-3.5" />
                  Tags
                </dt>
                <dd className="flex flex-wrap gap-1.5">
                  {tags.map((tag) => (
                    <Badge key={tag} variant="secondary">{tag}</Badge>
                  ))}
                </dd>
              </div>
            )}
            {knownEntries.length > 0 && (
              <dl className="space-y-2 text-sm">
                {knownEntries.map(([key, value]) => (
                  <div key={key} className="flex justify-between">
                    <dt className="text-muted-foreground capitalize">{key.replace(/_/g, " ")}</dt>
                    <dd className="font-medium">{value != null ? String(value) : "—"}</dd>
                  </div>
                ))}
              </dl>
            )}
            {otherEntries.length > 0 && (
              <>
                {knownEntries.length > 0 && <Separator />}
                <dl className="space-y-2 text-sm">
                  {otherEntries.map(([key, value]) => (
                    <div key={key} className="flex justify-between">
                      <dt className="text-muted-foreground">{key}</dt>
                      <dd className="font-medium max-w-[60%] truncate text-right" title={String(value)}>
                        {typeof value === "object" ? JSON.stringify(value) : String(value ?? "—")}
                      </dd>
                    </div>
                  ))}
                </dl>
              </>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function FeatureRow({ label, enabled, detail }: { label: string; enabled: boolean; detail?: string | null }) {
  return (
    <div className="flex items-center justify-between">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="flex items-center gap-2">
        <Badge variant={enabled ? "default" : "outline"} className="text-xs">
          {enabled ? "on" : "off"}
        </Badge>
        {enabled && detail && (
          <span className="font-medium text-xs">{detail}</span>
        )}
      </dd>
    </div>
  );
}

function IndexationPipelineCard({
  partition,
  indexationConfig,
}: {
  partition: string;
  indexationConfig: Record<string, unknown> | null;
}) {
  // Fall back to fetching current partition config for older documents
  const partitionQuery = useQuery({
    queryKey: ["partition", partition],
    queryFn: () => getPartition(partition),
    enabled: !indexationConfig,
  });

  if (!indexationConfig) {
    // Fallback path for documents indexed before this feature
    if (partitionQuery.isLoading) {
      return (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Settings2 className="h-4 w-4" />
              Indexation Pipeline
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="h-4 w-2/3" />
          </CardContent>
        </Card>
      );
    }

    if (partitionQuery.isError) {
      return (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Settings2 className="h-4 w-4" />
              Indexation Pipeline
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-destructive">
              Failed to load partition config: {(partitionQuery.error as Error).message}
            </p>
          </CardContent>
        </Card>
      );
    }

    const data = partitionQuery.data;
    if (!data) return null;

    const pipeline = data.indexation_pipeline;
    const chunking = (pipeline.chunking ?? {}) as Record<string, unknown>;

    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Settings2 className="h-4 w-4" />
            Indexation Pipeline
          </CardTitle>
          <CardDescription>
            Current partition settings — may differ from what was used at indexation time
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <IndexationPipelineContent
            chunking={chunking}
            pipeline={pipeline}
            preset={data.indexation_preset}
          />
        </CardContent>
      </Card>
    );
  }

  // Primary path: use snapshotted config from the document
  const chunking = (indexationConfig.chunking ?? {}) as Record<string, unknown>;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Settings2 className="h-4 w-4" />
          Indexation Pipeline
        </CardTitle>
        <CardDescription>
          Settings used when this document was indexed
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <IndexationPipelineContent
          chunking={chunking}
          pipeline={indexationConfig}
        />
      </CardContent>
    </Card>
  );
}

function IndexationPipelineContent({
  chunking,
  pipeline,
  preset,
}: {
  chunking: Record<string, unknown>;
  pipeline: Record<string, unknown>;
  preset?: string;
}) {
  return (
    <>
      {/* Preset (only shown for fallback path) */}
      {preset !== undefined && (
        <>
          <dl className="space-y-2 text-sm">
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Preset</dt>
              <dd className="font-medium">{preset || "—"}</dd>
            </div>
          </dl>
          <Separator />
        </>
      )}

      {/* Chunking */}
      <div className="space-y-1.5">
        <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Chunking</p>
        <dl className="space-y-2 text-sm">
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Strategy</dt>
            <dd className="font-medium">{String(chunking.strategy ?? "—")}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Chunk size</dt>
            <dd className="font-medium">{String(chunking.chunk_size ?? "—")}</dd>
          </div>
          {!["markdown_section", "markdown_layout"].includes(String(chunking.strategy ?? "")) && (
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Chunk overlap</dt>
              <dd className="font-medium">{String(chunking.chunk_overlap ?? "—")}</dd>
            </div>
          )}
        </dl>
      </div>

      <Separator />

      {/* Embedder */}
      <dl className="space-y-2 text-sm">
        <div className="flex justify-between">
          <dt className="text-muted-foreground">Embedder</dt>
          <dd className="font-medium">{String(pipeline.embedder ?? "—")}</dd>
        </div>
      </dl>

      <Separator />

      {/* Features */}
      <div className="space-y-1.5">
        <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">Features</p>
        <dl className="space-y-2 text-sm">
          <FeatureRow
            label="Image captioning"
            enabled={!!pipeline.enable_image_captioning}
            detail={pipeline.vlm as string | null}
          />
          <FeatureRow
            label="Contextualization"
            enabled={!!pipeline.enable_contextualization}
            detail={pipeline.contextualization_llm as string | null}
          />
          <FeatureRow
            label="Metadata extraction"
            enabled={!!pipeline.enable_metadata_extraction}
            detail={pipeline.metadata_extraction_llm as string | null}
          />
        </dl>
      </div>
    </>
  );
}

export default function DocumentDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [replaceFile, setReplaceFile] = useState<File | null>(null);
  const [copyPartition, setCopyPartition] = useState("");
  const [copyPopoverOpen, setCopyPopoverOpen] = useState(false);
  const [partitionSearch, setPartitionSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const replaceFileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(partitionSearch), 300);
    return () => clearTimeout(timer);
  }, [partitionSearch]);

  const documentQuery = useQuery({
    queryKey: ["document", id],
    queryFn: () => getDocument(id!),
    enabled: !!id,
  });

  const partitionsQuery = useQuery({
    queryKey: ["partitions-search", debouncedSearch],
    queryFn: () => searchPartitions(debouncedSearch, 20),
  });

  const replaceMutation = useMutation({
    mutationFn: () => {
      if (!id || !replaceFile) throw new Error("No file selected");
      return replaceDocument(id, replaceFile);
    },
    onSuccess: () => {
      toast.success("Document file replaced successfully");
      setReplaceFile(null);
      if (replaceFileRef.current) replaceFileRef.current.value = "";
      queryClient.invalidateQueries({ queryKey: ["document", id] });
    },
    onError: (err: Error) => {
      toast.error(`Failed to replace file: ${err.message}`);
    },
  });

  const copyMutation = useMutation({
    mutationFn: () => {
      if (!id || !copyPartition.trim()) throw new Error("No partition specified");
      return copyDocument(id, copyPartition.trim());
    },
    onSuccess: (data) => {
      toast.success(`Document copied to partition "${data.partition}"`);
      setCopyPartition("");
    },
    onError: (err: Error) => {
      toast.error(`Failed to copy document: ${err.message}`);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => {
      if (!id) throw new Error("No document ID");
      return deleteDocument(id);
    },
    onSuccess: () => {
      toast.success("Document deleted");
      navigate("/documents");
    },
    onError: (err: Error) => {
      toast.error(`Failed to delete document: ${err.message}`);
    },
  });

  if (documentQuery.isLoading) {
    return (
      <div className="flex items-center justify-center py-12 text-muted-foreground">
        Loading document...
      </div>
    );
  }

  if (documentQuery.isError) {
    return (
      <div className="flex items-center justify-center py-12 text-destructive">
        Failed to load document: {(documentQuery.error as Error).message}
      </div>
    );
  }

  const doc = documentQuery.data;
  if (!doc) return null;

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="sm" asChild>
          <Link to="/documents">
            <ArrowLeft className="h-4 w-4" />
            Back to Documents
          </Link>
        </Button>
      </div>

      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">{doc.filename}</h1>
          <div className="flex items-center gap-2 mt-1 text-muted-foreground">
            <span className="text-sm">{doc.id}</span>
            <span>·</span>
            <StatusBadge status={doc.status} />
            <span>·</span>
            <span className="text-sm">{doc.content_type}</span>
          </div>
        </div>
        <ConfirmDialog
          title="Delete Document"
          description={`Are you sure you want to delete "${doc.filename}"? This action cannot be undone and all associated chunks will be removed.`}
          onConfirm={() => deleteMutation.mutate()}
        >
          <Button variant="destructive" disabled={deleteMutation.isPending}>
            <Trash2 className="h-4 w-4" />
            {deleteMutation.isPending ? "Deleting..." : "Delete Document"}
          </Button>
        </ConfirmDialog>
      </div>

      <Tabs defaultValue="details">
        <TabsList>
          <TabsTrigger value="details">
            <FileText className="h-4 w-4" />
            Details
          </TabsTrigger>
          <TabsTrigger value="chunks">
            <BookOpen className="h-4 w-4" />
            Chunks ({doc.chunk_count})
          </TabsTrigger>
          <TabsTrigger value="actions">
            <Settings2 className="h-4 w-4" />
            Actions
          </TabsTrigger>
        </TabsList>

        <TabsContent value="details" className="mt-4">
          <div className="grid gap-6 md:grid-cols-2">
            <DocumentInfoCard doc={doc} documentId={id!} />
            <IndexationPipelineCard partition={doc.partition} indexationConfig={doc.indexation_config} />
          </div>
        </TabsContent>

        <TabsContent value="chunks" className="mt-4">
          <ChunksTab documentId={id!} chunkCount={doc.chunk_count} />
        </TabsContent>

        <TabsContent value="actions" className="mt-4">
          <div className="grid gap-6 md:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Replace Document</CardTitle>
                <CardDescription>
                  Upload a new file to replace the current document content
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                <input
                  type="file"
                  ref={replaceFileRef}
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0] ?? null;
                    setReplaceFile(file);
                  }}
                />
                <div className="flex items-center gap-3">
                  <Button variant="outline" onClick={() => replaceFileRef.current?.click()}>
                    Choose File
                  </Button>
                  <span className="text-sm text-muted-foreground truncate">
                    {replaceFile?.name ?? "No file selected"}
                  </span>
                </div>
                <Button
                  onClick={() => replaceMutation.mutate()}
                  disabled={!replaceFile || replaceMutation.isPending}
                  variant="outline"
                >
                  <RefreshCw className="h-4 w-4" />
                  {replaceMutation.isPending ? "Replacing..." : "Replace Document"}
                </Button>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Copy to Partition</CardTitle>
                <CardDescription>
                  Create a copy of this document in another partition
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="flex gap-2">
                  <Popover open={copyPopoverOpen} onOpenChange={setCopyPopoverOpen}>
                    <PopoverTrigger asChild>
                      <Button variant="outline" role="combobox" aria-expanded={copyPopoverOpen} className="flex-1 justify-between font-normal">
                        {copyPartition || "Select partition..."}
                        <ChevronsUpDown className="h-4 w-4 shrink-0 opacity-50" />
                      </Button>
                    </PopoverTrigger>
                    <PopoverContent className="w-[--radix-popover-trigger-width] p-0" align="start">
                      <Command shouldFilter={false}>
                        <CommandInput
                          placeholder="Search partitions..."
                          value={partitionSearch}
                          onValueChange={setPartitionSearch}
                        />
                        <CommandList>
                          <CommandEmpty>
                            {partitionsQuery.isFetching ? "Searching..." : "No other partitions found."}
                          </CommandEmpty>
                          <CommandGroup>
                            {(partitionsQuery.data?.partitions ?? [])
                              .filter((p) => p.name !== doc.partition)
                              .map((p) => (
                                <CommandItem
                                  key={p.name}
                                  value={p.name}
                                  onSelect={(value) => {
                                    setCopyPartition(value);
                                    setCopyPopoverOpen(false);
                                  }}
                                >
                                  <Check className={cn("h-4 w-4", copyPartition === p.name ? "opacity-100" : "opacity-0")} />
                                  {p.name}
                                </CommandItem>
                              ))}
                          </CommandGroup>
                        </CommandList>
                      </Command>
                    </PopoverContent>
                  </Popover>
                  <Button
                    onClick={() => copyMutation.mutate()}
                    disabled={!copyPartition.trim() || copyMutation.isPending}
                    variant="outline"
                  >
                    <Copy className="h-4 w-4" />
                    {copyMutation.isPending ? "Copying..." : "Copy"}
                  </Button>
                </div>
              </CardContent>
            </Card>
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
