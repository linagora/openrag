import { useState, useEffect, useRef, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Plus,
  Trash2,
  Pencil,
  Star,
  Code2,
  Eye,
  AlertTriangle,
  Circle,
  Users,
} from "lucide-react";
import {
  listPrompts,
  createPrompt,
  updatePrompt,
  deletePrompt,
  setPromptDefault,
} from "@/lib/api/prompts";
import type { PromptResponse, PromptType } from "@/lib/api/prompts";
import {
  PROMPT_GROUPS,
  PROMPT_TYPES,
  promptTypeLabel,
  PROMPT_TYPE_VARIABLES,
  validatePlaceholders,
  renderPreview,
} from "@/lib/prompt-meta";
import { PageHeader } from "@/components/shared/page-header";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Skeleton } from "@/components/ui/skeleton";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
  SheetFooter,
} from "@/components/ui/sheet";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { formatDate } from "@/lib/utils";

// Filter chips: "all" plus one per concern group.
const CONCERN_FILTERS = ["all", ...PROMPT_GROUPS.map((g) => g.name)] as const;

export default function PromptsPage() {
  const queryClient = useQueryClient();
  const [concern, setConcern] = useState<(typeof CONCERN_FILTERS)[number]>("all");
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<PromptResponse | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["prompts-library"],
    queryFn: () => listPrompts({ limit: 500 }),
  });
  const prompts = data ?? [];

  const setDefaultMut = useMutation({
    mutationFn: (id: string) => setPromptDefault(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["prompts-library"] });
      toast.success("Default prompt updated");
    },
    onError: (e) => toast.error(e.message),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => deletePrompt(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["prompts-library"] });
      toast.success("Prompt deleted");
    },
    onError: (e) => toast.error(e.message),
  });

  const openCreate = () => {
    setEditing(null);
    setEditorOpen(true);
  };
  const openEdit = (p: PromptResponse) => {
    setEditing(p);
    setEditorOpen(true);
  };

  const visibleGroups = PROMPT_GROUPS.filter((g) => concern === "all" || g.name === concern);
  const customCount = prompts.filter((p) => !p.is_default).length;

  return (
    <div>
      <PageHeader
        title="Prompt Library"
        description="Author the prompts your presets and partitions select. Edits take effect on the next request — no redeploy."
        actions={
          <Button onClick={openCreate}>
            <Plus className="mr-2 h-4 w-4" /> New Prompt
          </Button>
        }
      />

      <div className="mb-6 flex items-center gap-4">
        <div className="flex gap-1 rounded-lg border bg-muted p-1">
          {CONCERN_FILTERS.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => setConcern(c)}
              className={`rounded-md px-3 py-1 text-xs font-semibold capitalize transition-colors ${
                concern === c
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {c === "all" ? "All types" : c}
            </button>
          ))}
        </div>
        {!isLoading && (
          <span className="text-xs text-muted-foreground">
            {prompts.length} prompt{prompts.length === 1 ? "" : "s"} · {customCount} custom
          </span>
        )}
      </div>

      {isLoading ? (
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
          {[1, 2, 3, 4, 5, 6].map((i) => (
            <Skeleton key={i} className="h-40" />
          ))}
        </div>
      ) : (
        <div className="space-y-8">
          {visibleGroups.map((group) => {
            const groupTypes = new Set(group.types.map((t) => t.value));
            const groupPrompts = prompts.filter((p) => groupTypes.has(p.prompt_type));
            return (
              <section key={group.name}>
                <div className="mb-3">
                  <h2 className="text-xs font-bold uppercase tracking-wider text-muted-foreground">
                    {group.name}
                  </h2>
                  <p className="text-xs text-muted-foreground/70">{group.description}</p>
                </div>
                {groupPrompts.length === 0 ? (
                  <p className="rounded-md border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
                    No {group.name.toLowerCase()} prompts yet.
                  </p>
                ) : (
                  <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
                    {groupPrompts.map((prompt) => (
                      <PromptCard
                        key={prompt.id}
                        prompt={prompt}
                        onEdit={() => openEdit(prompt)}
                        onSetDefault={() => setDefaultMut.mutate(prompt.id)}
                        onDelete={() => deleteMut.mutate(prompt.id)}
                      />
                    ))}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      )}

      <PromptEditorSheet
        open={editorOpen}
        onOpenChange={setEditorOpen}
        editing={editing}
      />
    </div>
  );
}

/* ---------- Prompt card ---------- */

function PromptCard({
  prompt,
  onEdit,
  onSetDefault,
  onDelete,
}: {
  prompt: PromptResponse;
  onEdit: () => void;
  onSetDefault: () => void;
  onDelete: () => void;
}) {
  const used = prompt.used_by;
  return (
    <Card className="relative flex flex-col">
      <div className="absolute right-3 top-3">
        <Badge
          variant="outline"
          className={
            used > 0
              ? "text-xs bg-sky-50 text-sky-700 border-sky-200 dark:bg-sky-950/30 dark:text-sky-200 dark:border-sky-900/60"
              : "text-xs bg-muted text-muted-foreground border-transparent"
          }
        >
          <Users className="mr-1 h-3 w-3" />
          {used > 0 ? `${used} partition${used === 1 ? "" : "s"}` : "Unused"}
        </Badge>
      </div>
      <CardHeader className="pb-2">
        <p className="font-mono text-[0.7rem] text-muted-foreground">{prompt.prompt_type}</p>
        <div className="flex items-center gap-2 pr-24">
          <span className="truncate font-semibold">{prompt.name || "Untitled"}</span>
          {prompt.is_default && (
            <Badge className="shrink-0 bg-green-600 text-xs hover:bg-green-700">Default</Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col text-sm">
        <p className="mb-3 line-clamp-2 flex-1 border-l-2 border-muted pl-2.5 text-xs text-muted-foreground">
          {prompt.content.slice(0, 160) || "(empty)"}
        </p>
        <div className="text-[0.7rem] text-muted-foreground">Updated {formatDate(prompt.updated_at)}</div>
        <div className="mt-3 flex items-center gap-1.5 border-t pt-3">
          <Button size="sm" variant="outline" onClick={onEdit}>
            <Pencil className="mr-1 h-3 w-3" /> Edit
          </Button>
          {!prompt.is_default && (
            <Button size="sm" variant="ghost" className="text-muted-foreground" onClick={onSetDefault}>
              <Star className="mr-1 h-3 w-3" /> Set default
            </Button>
          )}
          <div className="flex-1" />
          {!prompt.is_default && (
            <ConfirmDialog
              title="Delete prompt?"
              description={
                used > 0
                  ? `"${prompt.name}" is selected by ${used} partition${used === 1 ? "" : "s"}. They will fall back to the default. Delete anyway?`
                  : `This will permanently delete "${prompt.name}".`
              }
              onConfirm={onDelete}
            >
              <Button size="sm" variant="ghost" className="text-destructive">
                <Trash2 className="h-3 w-3" />
              </Button>
            </ConfirmDialog>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

/* ---------- Editor drawer ---------- */

function PromptEditorSheet({
  open,
  onOpenChange,
  editing,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  editing: PromptResponse | null;
}) {
  const queryClient = useQueryClient();
  const [promptType, setPromptType] = useState<PromptType>("sys_prompt");
  const [name, setName] = useState("");
  const [content, setContent] = useState("");

  // Sync the form to the editing target each time the drawer opens (reset for
  // "create"). Controlled-drawer reset pattern.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (!open) return;
    if (editing) {
      setPromptType(editing.prompt_type);
      setName(editing.name);
      setContent(editing.content);
    } else {
      setPromptType("sys_prompt");
      setName("");
      setContent("");
    }
  }, [open, editing]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const createMut = useMutation({
    mutationFn: createPrompt,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["prompts-library"] });
      queryClient.invalidateQueries({ queryKey: ["prompts-for-presets"] });
      toast.success("Prompt created");
      onOpenChange(false);
    },
    onError: (e) => toast.error(e.message),
  });

  const updateMut = useMutation({
    mutationFn: ({ id, ...data }: { id: string; name: string; content: string }) =>
      updatePrompt(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["prompts-library"] });
      queryClient.invalidateQueries({ queryKey: ["prompts-for-presets"] });
      toast.success("Prompt updated");
      onOpenChange(false);
    },
    onError: (e) => toast.error(e.message),
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      toast.error("Name is required");
      return;
    }
    if (!content.trim()) {
      toast.error("Content is required");
      return;
    }
    if (editing) {
      updateMut.mutate({ id: editing.id, name, content });
    } else {
      createMut.mutate({ prompt_type: promptType, name, content });
    }
  };

  const loading = createMut.isPending || updateMut.isPending;
  const effectiveType = editing?.prompt_type ?? promptType;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>{editing ? "Edit prompt" : "New prompt"}</SheetTitle>
          <SheetDescription>
            Changes apply to every preset and partition that selects this prompt.
          </SheetDescription>
        </SheetHeader>
        <form onSubmit={handleSubmit} className="flex flex-1 flex-col gap-4 overflow-y-auto px-4 py-4">
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label>Name</Label>
              <Input
                placeholder="e.g. legal-assistant"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
              />
            </div>
            <div className="space-y-1.5">
              <Label>Type</Label>
              {editing ? (
                <Input value={promptTypeLabel(editing.prompt_type)} disabled />
              ) : (
                <Select value={promptType} onValueChange={(v) => setPromptType(v as PromptType)}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {PROMPT_TYPES.map((t) => (
                      <SelectItem key={t.value} value={t.value}>
                        {t.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>
          </div>

          <PromptTemplateEditor promptType={effectiveType} value={content} onChange={setContent} />

          {editing && (
            <p className="flex items-center gap-1.5 rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
              <Users className="h-3.5 w-3.5" />
              {editing.used_by > 0
                ? `Selected by ${editing.used_by} partition${editing.used_by === 1 ? "" : "s"}.`
                : "Not selected by any partition yet."}
            </p>
          )}

          <SheetFooter className="mt-auto flex-row justify-end gap-2 px-0">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={loading}>
              {loading ? "Saving..." : editing ? "Save prompt" : "Create prompt"}
            </Button>
          </SheetFooter>
        </form>
      </SheetContent>
    </Sheet>
  );
}

/* ---------- Template editor (edit / preview + {var} helpers) ---------- */

function PromptTemplateEditor({
  promptType,
  value,
  onChange,
}: {
  promptType: string;
  value: string;
  onChange: (v: string) => void;
}) {
  const [tab, setTab] = useState<"edit" | "preview">("edit");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const variables = PROMPT_TYPE_VARIABLES[promptType] ?? [];
  const hasVariables = variables.length > 0;

  const validation = useMemo(() => validatePlaceholders(value, promptType), [value, promptType]);
  const preview = useMemo(() => renderPreview(value, promptType), [value, promptType]);

  const insertVariable = (varName: string) => {
    const ta = textareaRef.current;
    if (!ta) return;
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    const placeholder = `{${varName}}`;
    onChange(value.slice(0, start) + placeholder + value.slice(end));
    requestAnimationFrame(() => {
      ta.focus();
      ta.setSelectionRange(start + placeholder.length, start + placeholder.length);
    });
  };

  return (
    <div className="flex flex-1 flex-col space-y-2">
      <div className="flex items-center justify-between">
        <Label>Content</Label>
        <div className="flex rounded-md border bg-muted p-0.5">
          <button
            type="button"
            onClick={() => setTab("edit")}
            className={`flex items-center gap-1 rounded px-2.5 py-1 text-xs font-medium transition-colors ${
              tab === "edit" ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <Code2 className="h-3 w-3" /> Edit
          </button>
          <button
            type="button"
            onClick={() => setTab("preview")}
            className={`flex items-center gap-1 rounded px-2.5 py-1 text-xs font-medium transition-colors ${
              tab === "preview" ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <Eye className="h-3 w-3" /> Preview
          </button>
        </div>
      </div>

      {hasVariables && tab === "edit" && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="mr-1 text-xs text-muted-foreground">Variables:</span>
          {variables.map((v) => (
            <button
              key={v.name}
              type="button"
              onClick={() => insertVariable(v.name)}
              title={`${v.description} — click to insert`}
              className="inline-flex items-center gap-1 rounded-md border bg-muted/50 px-2 py-0.5 font-mono text-xs transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              <span className="text-sky-600 dark:text-sky-400">{`{${v.name}}`}</span>
            </button>
          ))}
        </div>
      )}

      {tab === "edit" ? (
        <>
          <Textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            rows={14}
            className="min-h-[260px] resize-y font-mono text-sm"
            placeholder={
              hasVariables
                ? `Write your prompt template here.\nUse ${variables.map((v) => `{${v.name}}`).join(", ")} as placeholders.`
                : "Write your prompt here..."
            }
            required
          />
          {(validation.unknown.length > 0 || validation.missing.length > 0) && (
            <div className="space-y-1.5">
              {validation.unknown.map((v) => (
                <div key={v} className="flex items-start gap-2 text-xs text-amber-600 dark:text-amber-400">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
                  <span>
                    Unknown variable <code className="rounded bg-muted px-1 font-mono">{`{${v}}`}</code> — the
                    pipeline won't replace this.
                    {variables.length > 0 && (
                      <>
                        {" "}Known:{" "}
                        {variables.map((kv) => (
                          <code key={kv.name} className="rounded bg-muted px-1 font-mono">{`{${kv.name}}`}</code>
                        ))}
                      </>
                    )}
                  </span>
                </div>
              ))}
              {validation.missing.map((v) => (
                <div key={v} className="flex items-start gap-2 text-xs text-muted-foreground">
                  <Circle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0" />
                  <span>
                    <code className="rounded bg-muted px-1 font-mono">{`{${v}}`}</code> is available but not used —
                    fine if intentional.
                  </span>
                </div>
              ))}
            </div>
          )}
          {!hasVariables && (
            <p className="text-xs text-muted-foreground">
              This prompt is sent as a system message; the document, chunk, or image content is attached
              separately at runtime. No placeholders needed.
            </p>
          )}
        </>
      ) : (
        <div className="min-h-[240px] rounded-md border bg-muted/30 p-4">
          {!value.trim() ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              Nothing to preview — write some content first.
            </p>
          ) : hasVariables ? (
            <div className="space-y-3">
              <p className="text-xs text-muted-foreground">
                Preview with sample values. Highlighted spans are injected by the pipeline at runtime.
              </p>
              <div className="whitespace-pre-wrap font-mono text-sm leading-relaxed">
                {preview.map((seg, i) =>
                  seg.isVariable ? (
                    <span
                      key={i}
                      className="rounded border border-sky-300/50 bg-sky-100 px-0.5 text-sky-700 dark:border-sky-700/50 dark:bg-sky-900/40 dark:text-sky-300"
                      title={`{${seg.varName}} — sample value`}
                    >
                      {seg.value}
                    </span>
                  ) : (
                    <span key={i}>{seg.value}</span>
                  ),
                )}
              </div>
            </div>
          ) : (
            <div className="whitespace-pre-wrap font-mono text-sm leading-relaxed">{value}</div>
          )}
        </div>
      )}
    </div>
  );
}
