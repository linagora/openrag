import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cancelEvalRun,
  getEvalRun,
  listEvalDatasets,
  listEvalRuns,
  startEvalRun,
  type EvalRun,
  type EvalRunSummary,
} from "@/lib/api/evaluation";
import { EvaluationTab } from "./index";

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/lib/api/evaluation", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/evaluation")>("@/lib/api/evaluation");
  return {
    ...actual,
    listEvalDatasets: vi.fn(),
    listEvalRuns: vi.fn(),
    getEvalRun: vi.fn(),
    startEvalRun: vi.fn(),
    cancelEvalRun: vi.fn(),
    deleteEvalDataset: vi.fn(),
    createEvalDataset: vi.fn(),
  };
});

const listDatasetsMock = vi.mocked(listEvalDatasets);
const listRunsMock = vi.mocked(listEvalRuns);
const getRunMock = vi.mocked(getEvalRun);
const startRunMock = vi.mocked(startEvalRun);
const cancelRunMock = vi.mocked(cancelEvalRun);

const DATASET = {
  id: "ds1",
  name: "Support docs",
  corpus_file_count: 3,
  testset_row_count: 12,
  created_at: null,
  created_by: 1,
};

const COMPLETED_RUN: EvalRunSummary = {
  id: "run-completed",
  dataset_id: "ds1",
  status: "COMPLETED",
  started_at: "2026-07-27T10:00:00Z",
  finished_at: "2026-07-27T10:05:00Z",
  hit_rate: 0.75,
  mrr: 0.5,
  answer_pass_rate: 1,
  files_per_minute: 12.5,
  error: null,
};

const RUN_DETAIL: EvalRun = {
  id: "run-completed",
  dataset_id: "ds1",
  status: "COMPLETED",
  started_at: "2026-07-27T10:00:00Z",
  finished_at: "2026-07-27T10:05:00Z",
  indexing: {
    files_total: 3,
    files_failed: 0,
    bytes_total: 3 * 1024 * 1024,
    wall_seconds: 14.4,
    files_per_minute: 12.5,
    megabytes_per_second: 0.21,
    p50_seconds: 4.5,
    p95_seconds: 6.1,
    by_extension: {},
    samples: [],
  },
  retrieval: {
    scored_cases: 8,
    skipped_cases: 4,
    hit_rate: 0.75,
    mrr: 0.5,
    recall: 0.6,
    context_relevance: 0.82,
  },
  answer: { scored_cases: 12, pass_rate: 1, factuality: 0.9, rubric_score: 0.85 },
  cases: [
    {
      query: "What is the refund window?",
      retrieved_file_ids: ["policy.pdf"],
      expected_file_ids: ["policy.pdf"],
      hit: true,
      reciprocal_rank: 1,
      answer: "30 days",
      answer_passed: true,
      grader_reason: "Matches the reference answer",
    },
  ],
  error: null,
  created_by: 1,
};

function renderTab() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <EvaluationTab />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  listDatasetsMock.mockResolvedValue([DATASET]);
  listRunsMock.mockResolvedValue([COMPLETED_RUN]);
  getRunMock.mockResolvedValue(RUN_DETAIL);
});

describe("EvaluationTab", () => {
  it("lists datasets with their corpus and question counts", async () => {
    renderTab();
    expect(await screen.findByText("Support docs")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("12")).toBeTruthy();
  });

  it("starts a run for the chosen dataset", async () => {
    startRunMock.mockResolvedValue(RUN_DETAIL);
    renderTab();

    await userEvent.click(await screen.findByRole("button", { name: /^run$/i }));

    await waitFor(() => expect(startRunMock).toHaveBeenCalledWith("ds1"));
  });

  it("disables starting a run while one is in flight", async () => {
    listRunsMock.mockResolvedValue([{ ...COMPLETED_RUN, id: "run-active", status: "INDEXING" }]);
    getRunMock.mockResolvedValue({ ...RUN_DETAIL, id: "run-active", status: "INDEXING" });
    renderTab();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^run$/i }).hasAttribute("disabled")).toBe(true),
    );
  });

  it("offers cancel only while a run is active", async () => {
    renderTab();
    await screen.findByText("Support docs");
    expect(screen.queryByRole("button", { name: /cancel run/i })).toBeNull();

    listRunsMock.mockResolvedValue([{ ...COMPLETED_RUN, id: "run-active", status: "EVALUATING" }]);
    getRunMock.mockResolvedValue({ ...RUN_DETAIL, id: "run-active", status: "EVALUATING" });
    cancelRunMock.mockResolvedValue({ ...RUN_DETAIL, status: "CANCELLED" });

    const { unmount } = renderTab();
    const cancelButton = await screen.findAllByRole("button", { name: /cancel run/i });
    await userEvent.click(cancelButton[0]);
    await waitFor(() => expect(cancelRunMock).toHaveBeenCalledWith("run-active"));
    unmount();
  });

  it("shows the three metric families for the selected run", async () => {
    renderTab();

    expect(await screen.findByText("Indexing speed")).toBeTruthy();
    expect(screen.getByText("Retrieval quality")).toBeTruthy();
    expect(screen.getByText("Answer quality")).toBeTruthy();
    // hit_rate 0.75 rendered as a percentage in the detail panel
    expect(screen.getByText("75.0%")).toBeTruthy();
    // Throughput appears twice: once in the run row, once as a detail stat.
    expect(screen.getAllByText("12.5").length).toBeGreaterThan(0);
  });

  it("reports how many questions were skipped for lacking ground-truth sources", async () => {
    renderTab();
    expect(
      await screen.findByText(/8 question\(s\) scored, 4 skipped \(no expected_file_ids\)/),
    ).toBeTruthy();
  });

  it("renders the per-question table with the grader's reasoning", async () => {
    renderTab();
    expect(await screen.findByText("What is the refund window?")).toBeTruthy();
    expect(screen.getByText("Matches the reference answer")).toBeTruthy();
  });

  it("surfaces a failed run's error message", async () => {
    listRunsMock.mockResolvedValue([
      { ...COMPLETED_RUN, id: "run-failed", status: "FAILED", error: "promptfoo timed out." },
    ]);
    getRunMock.mockResolvedValue({
      ...RUN_DETAIL,
      id: "run-failed",
      status: "FAILED",
      error: "promptfoo timed out.",
    });
    renderTab();

    expect(await screen.findByText("promptfoo timed out.")).toBeTruthy();
  });

  it("tells the admin what to do when there are no datasets yet", async () => {
    listDatasetsMock.mockResolvedValue([]);
    listRunsMock.mockResolvedValue([]);
    renderTab();

    expect(await screen.findByText(/No datasets yet/)).toBeTruthy();
    expect(screen.getByText("No runs yet.")).toBeTruthy();
  });
});
