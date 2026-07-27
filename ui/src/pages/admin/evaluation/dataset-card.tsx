import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Play, Trash2, Upload } from "lucide-react";
import {
  createEvalDataset,
  deleteEvalDataset,
  listEvalDatasets,
  startEvalRun,
} from "@/lib/api/evaluation";
import { ConfirmDialog } from "@/components/shared/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

const CSV_HINT = "question,expected_answer,expected_file_ids";

export function DatasetCard({ runActive }: { runActive: boolean }) {
  const queryClient = useQueryClient();
  const [uploadOpen, setUploadOpen] = useState(false);

  const { data, isLoading } = useQuery({ queryKey: ["eval-datasets"], queryFn: listEvalDatasets });

  const startMut = useMutation({
    mutationFn: (datasetId: string) => startEvalRun(datasetId),
    onSuccess: () => {
      toast.success("Evaluation run queued");
      queryClient.invalidateQueries({ queryKey: ["eval-runs"] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => deleteEvalDataset(id),
    onSuccess: () => {
      toast.success("Dataset deleted");
      queryClient.invalidateQueries({ queryKey: ["eval-datasets"] });
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const datasets = data ?? [];

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between">
        <div>
          <CardTitle>Datasets</CardTitle>
          <CardDescription>
            A corpus to index plus a CSV test set (<code>{CSV_HINT}</code>).
          </CardDescription>
        </div>
        <Button size="sm" onClick={() => setUploadOpen(true)}>
          <Upload className="mr-2 h-4 w-4" />
          New dataset
        </Button>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <Skeleton className="h-24" />
        ) : datasets.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            No datasets yet. Upload a corpus and a test set to run an evaluation.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead className="text-right">Files</TableHead>
                <TableHead className="text-right">Questions</TableHead>
                <TableHead className="w-32" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {datasets.map((dataset) => (
                <TableRow key={dataset.id}>
                  <TableCell className="font-medium">{dataset.name}</TableCell>
                  <TableCell className="text-right tabular-nums">{dataset.corpus_file_count}</TableCell>
                  <TableCell className="text-right tabular-nums">{dataset.testset_row_count}</TableCell>
                  <TableCell className="text-right">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={runActive || startMut.isPending}
                      title={runActive ? "Another run is already in progress" : undefined}
                      onClick={() => startMut.mutate(dataset.id)}
                    >
                      <Play className="mr-1 h-3 w-3" />
                      Run
                    </Button>
                    <ConfirmDialog
                      title="Delete dataset?"
                      description={`"${dataset.name}" and its stored files will be removed. Past runs keep their results.`}
                      onConfirm={() => deleteMut.mutate(dataset.id)}
                    >
                      <Button size="sm" variant="ghost" aria-label={`Delete ${dataset.name}`}>
                        <Trash2 className="h-4 w-4 text-destructive" />
                      </Button>
                    </ConfirmDialog>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>

      <UploadDialog open={uploadOpen} onOpenChange={setUploadOpen} />
    </Card>
  );
}

function UploadDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [testset, setTestset] = useState<File | null>(null);
  const [corpus, setCorpus] = useState<File[]>([]);

  const reset = () => {
    setName("");
    setTestset(null);
    setCorpus([]);
  };

  const createMut = useMutation({
    mutationFn: () => createEvalDataset(name, testset as File, corpus),
    onSuccess: (dataset) => {
      toast.success(`Dataset "${dataset.name}" created (${dataset.testset_row_count} questions)`);
      queryClient.invalidateQueries({ queryKey: ["eval-datasets"] });
      reset();
      onOpenChange(false);
    },
    // The API validates the CSV row by row; surface its message verbatim.
    onError: (e) => toast.error((e as Error).message),
  });

  const canSubmit = name.trim() !== "" && testset !== null && corpus.length > 0;

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? onOpenChange(true) : (reset(), onOpenChange(false)))}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New evaluation dataset</DialogTitle>
          <DialogDescription>
            The corpus is re-indexed on every run, which is what the indexing-speed numbers measure.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="eval-dataset-name">Name</Label>
            <Input
              id="eval-dataset-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Support docs — July"
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="eval-testset">Test set (CSV)</Label>
            <Input
              id="eval-testset"
              type="file"
              accept=".csv,text/csv"
              onChange={(e) => setTestset(e.target.files?.[0] ?? null)}
            />
            <p className="text-xs text-muted-foreground">
              Header: <code>{CSV_HINT}</code>. <code>expected_file_ids</code> is optional and
              semicolon-separated; rows without it are excluded from hit rate, MRR and recall.
            </p>
          </div>
          <div className="space-y-2">
            <Label htmlFor="eval-corpus">Corpus files</Label>
            <Input
              id="eval-corpus"
              type="file"
              multiple
              onChange={(e) => setCorpus(Array.from(e.target.files ?? []))}
            />
            {corpus.length > 0 && (
              <p className="text-xs text-muted-foreground">{corpus.length} file(s) selected</p>
            )}
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={!canSubmit || createMut.isPending} onClick={() => createMut.mutate()}>
            {createMut.isPending ? "Uploading…" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
