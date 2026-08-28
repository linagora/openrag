import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus, Trash2, Pencil, Eye } from "lucide-react";
import {
  listPresets,
  createPreset,
  updatePreset,
  deletePreset,
  getPresetOptions,
} from "@/lib/api/presets";
import type { PresetResponse, PresetType } from "@/lib/api/presets";
import { listAllPrompts } from "@/lib/api/prompts";
import type { PromptResponse } from "@/lib/api/prompts";
import { listModelEndpoints, pickDefaultEndpoint } from "@/lib/api/models";
import { PageHeader } from "@/components/shared/page-header";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Separator } from "@/components/ui/separator";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate, intOr, numOr } from "@/lib/utils";
import {
  PROMPT_DEFAULT_OPTION,
  promptOptionToName,
  promptOptionValue,
  promptSelectValue,
} from "@/lib/prompt-meta";
import {
  type Config,
  configGet,
  configSet,
  applySttEndpointChange,
  applyParsingStrategyChange,
  PARSING_STRATEGY_INHERIT,
  STT_ENDPOINT_DEFAULT_OPTION,
} from "./preset-config";

const PRESET_TYPES = ["indexation", "retrieval"] as const;

export default function PresetsPage() {
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState("indexation");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<PresetResponse | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["presets"],
    queryFn: () => listPresets(),
  });

  const createMut = useMutation({
    mutationFn: createPreset,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["presets"] });
      toast.success("Preset created");
      setDialogOpen(false);
    },
    onError: (e) => toast.error(e.message),
  });

  const updateMut = useMutation({
    mutationFn: ({ type, originalName, name, config }: { type: string; originalName: string; name: string; config: Record<string, unknown> }) =>
      updatePreset(type as PresetType, originalName, { ...(name !== originalName ? { name } : {}), config }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["presets"] });
      toast.success("Preset updated");
      setDialogOpen(false);
      setEditing(null);
    },
    onError: (e) => toast.error(e.message),
  });

  const deleteMut = useMutation({
    mutationFn: ({ type, name }: { type: string; name: string }) => deletePreset(type as PresetType, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["presets"] });
      toast.success("Preset deleted");
    },
    onError: (e) => toast.error(e.message),
  });

  const presets = data ?? [];

  // Config keys with no editor control behind them (inert / unimplemented on the
  // backend) — kept out of the card summary so it reflects what's actually managed.
  const HIDDEN_SUMMARY_KEYS = new Set<string>([
    "enable_metadata_extraction",
    "metadata_extraction_llm",
    "contextualization_mode",
    "structured_contextualization_prompt_name",
  ]);
  const summarizeConfig = (config: Record<string, unknown>): string[] => {
    const summary: string[] = [];
    for (const [key, val] of Object.entries(config)) {
      if (HIDDEN_SUMMARY_KEYS.has(key)) continue;
      if (val === null || val === undefined || val === "") continue;
      if (typeof val === "boolean") {
        if (val) summary.push(key);
      } else if (typeof val === "object") {
        // Nested config object (e.g. chunking) — show its strategy name, not "[object Object]".
        const name = (val as Record<string, unknown>).name;
        summary.push(name ? `${key}: ${String(name)}` : key);
      } else {
        summary.push(`${key}: ${String(val)}`);
      }
    }
    return summary.slice(0, 5);
  };

  return (
    <div>
      <PageHeader
        title="Presets"
        description="Pipeline configuration presets for indexation and retrieval"
        actions={
          <Button onClick={() => { setEditing(null); setDialogOpen(true); }}>
            <Plus className="mr-2 h-4 w-4" /> Add Preset
          </Button>
        }
      />

      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <TabsList>
          {PRESET_TYPES.map((t) => (
            <TabsTrigger key={t} value={t} className="capitalize">
              {t}
            </TabsTrigger>
          ))}
        </TabsList>

        {PRESET_TYPES.map((type) => (
          <TabsContent key={type} value={type}>
            {isLoading ? (
              <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                {[1, 2, 3].map((i) => (
                  <Skeleton key={i} className="h-48" />
                ))}
              </div>
            ) : (
              <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
                {presets
                  .filter((p) => p.preset_type === type)
                  .map((preset) => (
                    <Card key={`${preset.preset_type}-${preset.name}`} className="flex flex-col">
                      <CardHeader className="pb-3">
                        <div className="flex items-center justify-between gap-2">
                          <CardTitle className="text-base truncate min-w-0">{preset.name}</CardTitle>
                          <Badge
                            variant="outline"
                            className={
                              preset.used_by_partitions > 0
                                ? "text-xs h-fit shrink-0 bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/30 dark:text-amber-100 dark:border-amber-900/60"
                                : "text-xs h-fit shrink-0 bg-muted text-muted-foreground border-transparent"
                            }
                          >
                            {preset.used_by_partitions > 0
                              ? `used by ${preset.used_by_partitions} partition${preset.used_by_partitions === 1 ? "" : "s"}`
                              : "unused"}
                          </Badge>
                        </div>
                      </CardHeader>
                      <CardContent className="flex flex-col flex-1 text-sm">
                        <div className="flex flex-wrap gap-1 flex-1">
                          {summarizeConfig(preset.config).map((s, i) => (
                            <Badge
                              key={i}
                              variant="secondary"
                              className="text-xs h-fit max-w-full whitespace-normal break-words text-left"
                            >
                              {s}
                            </Badge>
                          ))}
                          {Object.keys(preset.config).length > 5 && (
                            <Badge variant="outline" className="text-xs h-fit">
                              +{Object.keys(preset.config).length - 5} more
                            </Badge>
                          )}
                        </div>
                        <div className="text-xs text-muted-foreground mt-3">
                          Updated {formatDate(preset.updated_at)}
                        </div>
                        <div className="flex flex-wrap gap-2 pt-3">
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => { setEditing(preset); setDialogOpen(true); }}
                          >
                            <Pencil className="mr-1 h-3 w-3" /> Edit
                          </Button>
                          <ConfirmDialog
                            title="Delete preset?"
                            description={
                              preset.used_by_partitions > 0
                                ? `"${preset.name}" is used by ${preset.used_by_partitions} partition${preset.used_by_partitions === 1 ? "" : "s"}. Reassign them to a different preset before deleting.`
                                : `This will permanently delete "${preset.name}".`
                            }
                            onConfirm={() =>
                              deleteMut.mutate({ type: preset.preset_type, name: preset.name })
                            }
                          >
                            <Button size="sm" variant="outline" className="text-destructive">
                              <Trash2 className="mr-1 h-3 w-3" /> Delete
                            </Button>
                          </ConfirmDialog>
                        </div>
                      </CardContent>
                    </Card>
                  ))}
                {presets.filter((p) => p.preset_type === type).length === 0 && (
                  <p className="col-span-full text-center py-8 text-muted-foreground">
                    No {type} presets configured.
                  </p>
                )}
              </div>
            )}
          </TabsContent>
        ))}
      </Tabs>

      <PresetDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        editing={editing}
        activeTab={activeTab}
        onCreate={(data) => createMut.mutate(data)}
        onUpdate={(type, originalName, name, config) => updateMut.mutate({ type, originalName, name, config })}
        loading={createMut.isPending || updateMut.isPending}
      />
    </div>
  );
}

/* ---------- Indexation form ---------- */

function IndexationPresetForm({
  config,
  onChange,
  chunkingStrategies,
  parsingStrategies,
  vlms,
  stts,
  llms,
  prompts,
  defaultLlm,
  defaultVlm,
  defaultStt,
}: {
  config: Config;
  onChange: (c: Config) => void;
  chunkingStrategies: string[];
  parsingStrategies: string[];
  vlms: string[];
  stts: string[];
  llms: string[];
  prompts: PromptResponse[];
  defaultLlm?: string;
  defaultVlm?: string;
  defaultStt?: string;
}) {
  const set = (key: string, value: unknown) => onChange(configSet(config, key, value));

  const chunking = (config.chunking ?? {}) as Record<string, unknown>;
  const setChunking = (key: string, value: unknown) => {
    onChange({ ...config, chunking: { ...chunking, [key]: value } });
  };

  const toggleFeature = (featureKey: string, modelKey: string, on: boolean) => {
    const next = { ...config, [featureKey]: on };
    if (!on) {
      delete next[modelKey];
    } else if (!next[modelKey]) {
      // Default the model field to the default/only endpoint so enabling a
      // feature doesn't leave an empty (invalid) picker.
      const fallback = modelKey === "vlm" ? defaultVlm : defaultLlm;
      if (fallback) next[modelKey] = fallback;
    }
    onChange(next);
  };

  const promptsByType = (type: string) => prompts.filter((p) => p.prompt_type === type);

  return (
    <div className="space-y-5">
      {/* Chunking */}
      <section className="space-y-3">
        <h4 className="text-sm font-medium">Chunking</h4>
        <div className={String(configGet(chunking as Config, "name", "")) !== "markdown_section" ? "grid grid-cols-[3fr_1fr_1fr] gap-3" : "max-w-xs"}>
          <div className="space-y-1.5">
            <Label className="text-xs">Strategy</Label>
            <Select
              value={configGet(chunking as Config, "name", "")}
              onValueChange={(v) => setChunking("name", v)}
            >
              <SelectTrigger size="sm">
                <SelectValue placeholder="Select..." />
              </SelectTrigger>
              <SelectContent>
                {chunkingStrategies.map((s) => (
                  <SelectItem key={s} value={s}>
                    {s}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {String(configGet(chunking as Config, "name", "")) !== "markdown_section" && (
            <div className="space-y-1.5">
              <Label className="text-xs">Chunk size</Label>
              <Input
                type="number"
                min={1}
                value={configGet(chunking as Config, "chunk_size", 512)}
                onChange={(e) => setChunking("chunk_size", intOr(e.target.value, 512))}
              />
            </div>
          )}
          {!["markdown_section", "markdown_layout"].includes(
            String(configGet(chunking as Config, "name", ""))
          ) && (
            <div className="space-y-1.5">
              <Label className="text-xs">Overlap rate</Label>
              <Input
                type="number"
                min={0}
                max={1}
                step={0.05}
                title="Fraction of chunk size (0–1)"
                value={configGet(chunking as Config, "chunk_overlap_rate", 0.2)}
                onChange={(e) => setChunking("chunk_overlap_rate", numOr(e.target.value, 0.2))}
              />
            </div>
          )}
        </div>
      </section>

      <Separator />

      {/* Parsing */}
      <section className="space-y-3">
        <h4 className="text-sm font-medium">Parsing</h4>
        <div className="grid gap-3 md:grid-cols-3">
          <div className="space-y-1.5">
            <Label className="text-xs">Strategy</Label>
            <Select
              value={configGet(config, "parsing_strategy", PARSING_STRATEGY_INHERIT)}
              onValueChange={(v) => onChange(applyParsingStrategyChange(config, v))}
            >
              <SelectTrigger size="sm">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={PARSING_STRATEGY_INHERIT}>Default (inherit global loader)</SelectItem>
                {parsingStrategies.map((s) => (
                  <SelectItem key={s} value={s}>
                    {s}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">STT endpoint</Label>
            <Select
              value={configGet(config, "stt", STT_ENDPOINT_DEFAULT_OPTION)}
              onValueChange={(v) => onChange(applySttEndpointChange(config, v))}
            >
              <SelectTrigger size="sm">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={STT_ENDPOINT_DEFAULT_OPTION}>
                  {defaultStt ? `Use default (${defaultStt})` : "Use default"}
                </SelectItem>
                {stts.map((name) => (
                  <SelectItem key={name} value={name}>
                    {name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <PromptSelect
            label="Transcription prompt"
            prompts={promptsByType("asr_transcription")}
            value={configGet(config, "asr_transcription_prompt_name", "")}
            onChange={(v) => set("asr_transcription_prompt_name", v || null)}
          />
        </div>
      </section>

      <Separator />

      {/* Features */}
      <section className="space-y-3">
        <h4 className="text-sm font-medium">Features</h4>
        <FeatureToggle
          label="Image captioning"
          // Backend default is ON (IndexationPipelineConfig.enable_image_captioning
          // = True) and configs are stored sparse — so a preset that omits the key
          // captions by default. The toggle must show that truth, otherwise it
          // reads "off" while the backend still captions during indexing (#453).
          enabled={configGet(config, "enable_image_captioning", true)}
          onToggle={(on) => toggleFeature("enable_image_captioning", "vlm", on)}
          disabled={configGet<string>(config, "parsing_strategy", PARSING_STRATEGY_INHERIT) === "pymupdf"}
          disabledHint="pymupdf extracts text only — use marker or docling for image captioning."
          modelLabel="VLM"
          modelValue={configGet(config, "vlm", "")}
          onModelChange={(v) => set("vlm", v)}
          models={vlms}
          promptLabel="Caption prompt"
          promptValue={configGet(config, "image_captioning_prompt_name", "")}
          onPromptChange={(v) => set("image_captioning_prompt_name", v || null)}
          prompts={promptsByType("image_captioning")}
        />
        <FeatureToggle
          label="Contextualization"
          enabled={configGet(config, "enable_contextualization", false)}
          onToggle={(on) => toggleFeature("enable_contextualization", "contextualization_llm", on)}
          modelLabel="LLM"
          modelValue={configGet(config, "contextualization_llm", "")}
          onModelChange={(v) => set("contextualization_llm", v)}
          models={llms}
          promptLabel="Prompt"
          promptValue={configGet(config, "contextualization_prompt_name", "")}
          onPromptChange={(v) => set("contextualization_prompt_name", v || null)}
          prompts={promptsByType("chunk_contextualizer")}
        />
        <FeatureToggle
          label="Topic tagging"
          enabled={configGet(config, "enable_topic_tagging", false)}
          onToggle={(on) => toggleFeature("enable_topic_tagging", "topic_tagging_llm", on)}
          // Postponed: tags are generated but not yet surfaced or used in
          // retrieval, so the control is disabled until the feature ships.
          disabled
          disabledHint="Coming soon — generated tags aren't surfaced or used in retrieval yet."
          modelLabel="LLM"
          modelValue={configGet(config, "topic_tagging_llm", "")}
          onModelChange={(v) => set("topic_tagging_llm", v)}
          models={llms}
          promptLabel="Prompt"
          promptValue={configGet(config, "topic_tagging_prompt_name", "")}
          onPromptChange={(v) => set("topic_tagging_prompt_name", v || null)}
          prompts={promptsByType("topic_tagger")}
          numberLabel="Max tags"
          numberValue={configGet(config, "max_topic_tags", 7)}
          onNumberChange={(v) => set("max_topic_tags", v)}
          numberMin={1}
          numberMax={50}
        />
      </section>

    </div>
  );
}

/* ---------- Prompt view button ---------- */

function PromptViewButton({ prompts, selectedName }: { prompts: PromptResponse[]; selectedName: string }) {
  const [open, setOpen] = useState(false);

  // Resolve the prompt to display: selected by name, or the active one
  const prompt = selectedName
    ? prompts.find((p) => p.name === selectedName)
    : prompts.find((p) => p.is_default) ?? prompts[0];

  if (!prompt) return null;

  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-6 w-6 p-0"
        onClick={() => setOpen(true)}
        title="View prompt"
      >
        <Eye className="h-3.5 w-3.5 text-muted-foreground" />
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-2xl max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              {prompt.name}
              {prompt.is_default && <Badge variant="outline" className="text-xs">default</Badge>}
            </DialogTitle>
          </DialogHeader>
          <pre className="text-sm font-mono bg-muted rounded-md p-4 overflow-auto whitespace-pre-wrap break-words flex-1">
            {prompt.content}
          </pre>
        </DialogContent>
      </Dialog>
    </>
  );
}

/* ---------- Feature toggle row ---------- */

function FeatureToggle({
  label,
  enabled,
  onToggle,
  modelLabel,
  modelValue,
  onModelChange,
  models,
  promptLabel,
  promptValue,
  onPromptChange,
  prompts,
  numberLabel,
  numberValue,
  onNumberChange,
  numberMin,
  numberMax,
  disabled = false,
  disabledHint,
}: {
  label: string;
  enabled: boolean;
  onToggle: (on: boolean) => void;
  modelLabel: string;
  modelValue: string;
  onModelChange: (v: string) => void;
  models: string[];
  promptLabel?: string;
  promptValue?: string;
  onPromptChange?: (v: string) => void;
  prompts?: PromptResponse[];
  numberLabel?: string;
  numberValue?: number;
  onNumberChange?: (v: number) => void;
  numberMin?: number;
  numberMax?: number;
  disabled?: boolean;
  disabledHint?: string;
}) {
  const handleToggle = (on: boolean) => {
    onToggle(on);
    // Auto-select the sole model, but never the sole prompt: an empty prompt
    // value is a real choice ("use the type's global default"), and after
    // seeding a type usually has exactly one prompt — the default itself.
    // Auto-selecting it would pin that *name* into the preset just by opening
    // and saving, so promoting a different global default later would silently
    // stop affecting this preset. Models have no such fallback, so they keep it.
    if (on && models.length === 1 && !modelValue) onModelChange(models[0]);
  };

  // Same for a list that resolves to a single item after the query settles.
  useEffect(() => {
    if (!enabled) return;
    if (models.length === 1 && !modelValue) onModelChange(models[0]);
  }, [enabled, models, modelValue, onModelChange]);

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <Label className="text-sm">{label}</Label>
        <Switch checked={enabled && !disabled} onCheckedChange={handleToggle} size="sm" disabled={disabled} />
      </div>
      {disabled && disabledHint && (
        <p className="pl-2 text-xs text-muted-foreground">{disabledHint}</p>
      )}
      {enabled && !disabled && (
        <div className="pl-2 space-y-2">
          <div>
            <Label className="text-xs text-muted-foreground">{modelLabel}</Label>
            <Select value={modelValue} onValueChange={onModelChange}>
              <SelectTrigger size="sm">
                <SelectValue placeholder="Select..." />
              </SelectTrigger>
              <SelectContent>
                {models.map((m) => (
                  <SelectItem key={m} value={m}>
                    {m}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {numberLabel && onNumberChange && (
            <div>
              <Label className="text-xs text-muted-foreground">{numberLabel}</Label>
              <Input
                type="number"
                min={numberMin}
                max={numberMax}
                value={numberValue ?? ""}
                onChange={(e) => onNumberChange(intOr(e.target.value, numberValue ?? 0))}
              />
            </div>
          )}
          {prompts && prompts.length > 0 && onPromptChange && (
            <div>
              <div className="flex items-center gap-1">
                <Label className="text-xs text-muted-foreground">{promptLabel || "Prompt"}</Label>
                <PromptViewButton prompts={prompts} selectedName={promptValue || ""} />
              </div>
              <Select
                value={promptSelectValue(promptValue)}
                onValueChange={(v) => onPromptChange(promptOptionToName(v))}
              >
                <SelectTrigger size="sm">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={PROMPT_DEFAULT_OPTION}>Use default</SelectItem>
                  {prompts.map((p) => (
                    <SelectItem key={p.id} value={promptOptionValue(p.name)}>
                      {p.name}{p.is_default ? " (default)" : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ---------- Standalone prompt picker (retrieval hyde / multi_query) ---------- */

function PromptSelect({
  label,
  prompts,
  value,
  onChange,
}: {
  label: string;
  prompts: PromptResponse[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1">
        <Label className="text-xs">{label}</Label>
        {prompts.length > 0 && <PromptViewButton prompts={prompts} selectedName={value} />}
      </div>
      <Select
        value={promptSelectValue(value)}
        onValueChange={(v) => onChange(promptOptionToName(v))}
      >
        <SelectTrigger size="sm">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={PROMPT_DEFAULT_OPTION}>Use default</SelectItem>
          {prompts.map((p) => (
            <SelectItem key={p.id} value={promptOptionValue(p.name)}>
              {p.name}{p.is_default ? " (default)" : ""}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

/* ---------- Retrieval form ---------- */

function RetrievalPresetForm({
  config,
  onChange,
  retrievalPipelines,
  rerankers,
  llms,
  prompts,
}: {
  config: Config;
  onChange: (c: Config) => void;
  retrievalPipelines: string[];
  rerankers: string[];
  llms: string[];
  prompts: PromptResponse[];
}) {
  const set = (key: string, value: unknown) => onChange(configSet(config, key, value));
  const pipelineType: string = configGet(config, "type", "single");
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const promptsByType = (type: string) => prompts.filter((p) => p.prompt_type === type);

  return (
    <div className="space-y-5">
      {/* Search */}
      <section className="space-y-3">
        <h4 className="text-sm font-medium">Search</h4>
        <div className="space-y-1.5">
          <Label className="text-xs">Strategy</Label>
          <Select value={pipelineType} onValueChange={(v) => set("type", v)}>
            <SelectTrigger size="sm"><SelectValue /></SelectTrigger>
            <SelectContent>
              {retrievalPipelines.map((p) => (
                <SelectItem key={p} value={p}>{p}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <PromptSelect
          label="Query contextualizer prompt"
          prompts={promptsByType("query_contextualizer")}
          value={configGet(config, "query_contextualizer_prompt_name", "")}
          onChange={(v) => set("query_contextualizer_prompt_name", v || null)}
        />
        {pipelineType !== "single" && (
          <div className="space-y-1.5">
            <Label className="text-xs">Expansion LLM</Label>
            <p className="text-[0.7rem] text-muted-foreground">
              LLM used to expand the query for the {pipelineType} strategy. Default = the
              pipeline's configured LLM.
            </p>
            <Select
              value={configGet(config, "llm", "__none__")}
              onValueChange={(v) => set("llm", v === "__none__" ? null : v)}
            >
              <SelectTrigger size="sm"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__">Default</SelectItem>
                {llms.map((m) => (
                  <SelectItem key={m} value={m}>{m}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )}
        {pipelineType === "hyde" && (
          <PromptSelect
            label="HyDE prompt"
            prompts={promptsByType("hyde")}
            value={configGet(config, "hyde_prompt_name", "")}
            onChange={(v) => set("hyde_prompt_name", v || null)}
          />
        )}
        {pipelineType === "multiQuery" && (
          <PromptSelect
            label="Multi-query prompt"
            prompts={promptsByType("multi_query")}
            value={configGet(config, "multi_query_prompt_name", "")}
            onChange={(v) => set("multi_query_prompt_name", v || null)}
          />
        )}
        <div className="space-y-1.5">
          <Label className="text-xs">top_k (vector retrieval count)</Label>
          <Input
            type="number"
            min={1}
            max={1000}
            value={configGet(config, "top_k", 50)}
            onChange={(e) => set("top_k", intOr(e.target.value, 50))}
          />
        </div>
        <div className="space-y-1.5">
          <Label className="text-xs">Similarity threshold</Label>
          <p className="text-[0.7rem] text-muted-foreground">
            Minimum cosine similarity (0–1) for vector results; higher = stricter, fewer results.
          </p>
          <Input
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={configGet(config, "similarity_threshold", 0.6)}
            onChange={(e) => set("similarity_threshold", numOr(e.target.value, 0.6))}
          />
        </div>
      </section>

      <Separator />

      {/* Reranker */}
      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h4 className="text-sm font-medium">Reranker</h4>
          <Switch
            checked={configGet(config, "enable_reranker", true) as boolean}
            onCheckedChange={(on) => set("enable_reranker", on)}
            size="sm"
          />
        </div>
        <div className="space-y-1.5">
          <Label className="text-xs">Model</Label>
          <Select
            value={configGet(config, "reranker", "__none__")}
            onValueChange={(v) => set("reranker", v === "__none__" ? null : v)}
            disabled={!configGet(config, "enable_reranker", true)}
          >
            <SelectTrigger size="sm"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="__none__">Default</SelectItem>
              {rerankers.map((r) => (
                <SelectItem key={r} value={r}>{r}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label className="text-xs">top_n (post-rerank count)</Label>
          <Input
            type="number"
            min={1}
            max={1000}
            value={configGet(config, "top_n", 10)}
            onChange={(e) => set("top_n", intOr(e.target.value, 10))}
            disabled={!configGet(config, "enable_reranker", true)}
          />
        </div>
      </section>

      <Separator />

      {/* Result expansion */}
      <section className="space-y-3">
        <h4 className="text-sm font-medium">Result expansion</h4>
        <p className="text-[0.7rem] text-muted-foreground">
          After matching, also pull in chunks from linked files (capped per result).
        </p>
        <div className="flex items-center justify-between">
          <Label className="text-xs">Include related files (same relationship)</Label>
          <Switch
            checked={configGet(config, "include_related", true) as boolean}
            onCheckedChange={(on) => set("include_related", on)}
            size="sm"
          />
        </div>
        <div className="flex items-center justify-between">
          <Label className="text-xs">Include ancestor files (parent hierarchy)</Label>
          <Switch
            checked={configGet(config, "include_ancestors", true) as boolean}
            onCheckedChange={(on) => set("include_ancestors", on)}
            size="sm"
          />
        </div>
      </section>

      <Separator />

      {/* Advanced Pipeline Settings */}
      <section className="space-y-3">
        <button
          type="button"
          className="flex items-center justify-between w-full text-sm font-medium"
          onClick={() => setAdvancedOpen(!advancedOpen)}
        >
          <span>Advanced Pipeline Settings</span>
          <span className="text-xs text-muted-foreground">{advancedOpen ? "collapse" : "expand"}</span>
        </button>

        {advancedOpen && (
          <div className="space-y-3 pt-2">
            <div className="space-y-1.5 max-w-xs">
              <Label className="text-xs">RRF k</Label>
              <p className="text-[0.7rem] text-muted-foreground">
                Reciprocal Rank Fusion constant for hybrid (dense + BM25) search.
              </p>
              <Input
                type="number" min={1} max={1000}
                value={configGet(config, "rrf_k", 60)}
                onChange={(e) => set("rrf_k", intOr(e.target.value, 60))}
              />
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

/* ---------- Preset dialog ---------- */

function PresetDialog({
  open,
  onOpenChange,
  editing,
  activeTab,
  onCreate,
  onUpdate,
  loading,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  editing: PresetResponse | null;
  activeTab: string;
  onCreate: (data: { name: string; preset_type: PresetType; config: Record<string, unknown> }) => void;
  onUpdate: (type: string, originalName: string, name: string, config: Record<string, unknown>) => void;
  loading: boolean;
}) {
  const presetType = editing ? editing.preset_type : activeTab;

  const [name, setName] = useState("");
  const [config, setConfig] = useState<Config>({});

  // Intentionally sync the form to the editing target each time the dialog
  // opens (and reset it for "create"). This is the controlled-dialog reset
  // pattern, so the setState-in-effect warning doesn't apply here.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (open) {
      if (editing) {
        setName(editing.name);
        setConfig({ ...editing.config });
      } else {
        setName("");
        setConfig({});
      }
    }
  }, [open, editing]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const { data: options } = useQuery({
    queryKey: ["preset-options"],
    queryFn: getPresetOptions,
    enabled: open,
  });

  const { data: llmData } = useQuery({
    queryKey: ["model-endpoints", "llm"],
    queryFn: () => listModelEndpoints("llm"),
    enabled: open,
  });
  const { data: vlmData } = useQuery({
    queryKey: ["model-endpoints", "vlm"],
    queryFn: () => listModelEndpoints("vlm"),
    enabled: open && presetType === "indexation",
  });
  const { data: sttData } = useQuery({
    queryKey: ["model-endpoints", "stt"],
    queryFn: () => listModelEndpoints("stt"),
    enabled: open && presetType === "indexation",
  });
  const { data: rerankerData } = useQuery({
    queryKey: ["model-endpoints", "reranker"],
    queryFn: () => listModelEndpoints("reranker"),
    enabled: open && presetType === "retrieval",
  });

  const llms = (llmData ?? []).map((e) => e.name);
  const vlms = (vlmData ?? []).map((e) => e.name);
  const stts = (sttData ?? []).map((e) => e.name);
  const defaultLlm = pickDefaultEndpoint(llmData)?.name;
  const defaultVlm = pickDefaultEndpoint(vlmData)?.name;
  const defaultStt = pickDefaultEndpoint(sttData)?.name;
  // The preset's `reranker` field is a reranker *endpoint name* (resolved by the
  // backend's reranker factory), not a provider type — so list the configured
  // reranker model endpoints, like the embedder/LLM pickers.
  const rerankers = (rerankerData ?? []).map((e) => e.name);

  const { data: promptData } = useQuery({
    queryKey: ["prompts-for-presets"],
    queryFn: () => listAllPrompts(),
    enabled: open,
  });
  const allPrompts = promptData ?? [];

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (editing) {
      onUpdate(editing.preset_type, editing.name, name, config);
    } else {
      onCreate({ name, preset_type: activeTab as PresetType, config });
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            {editing ? `Edit ${editing.name}` : `Create ${activeTab} preset`}
          </DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <Label>Name</Label>
            <Input value={name} onChange={(e) => setName(e.target.value)} required />
          </div>

          {presetType === "indexation" ? (
            <IndexationPresetForm
              config={config}
              onChange={setConfig}
              chunkingStrategies={options?.chunking_strategies ?? []}
              parsingStrategies={options?.parsing_strategies ?? ["marker", "pymupdf"]}
              vlms={vlms}
              stts={stts}
              llms={llms}
              prompts={allPrompts}
              defaultLlm={defaultLlm}
              defaultVlm={defaultVlm}
              defaultStt={defaultStt}
            />
          ) : (
            <RetrievalPresetForm
              config={config}
              onChange={setConfig}
              retrievalPipelines={options?.retrieval_types ?? []}
              rerankers={rerankers}
              llms={llms}
              prompts={allPrompts}
            />
          )}

          <DialogFooter>
            <Button type="submit" disabled={loading}>
              {loading ? "Saving..." : editing ? "Update" : "Create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
