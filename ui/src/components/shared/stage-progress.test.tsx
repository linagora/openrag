import { act, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { StageElapsedTime } from "./stage-progress";

it("updates the displayed duration every five seconds", async () => {
  vi.useFakeTimers();
  try {
    render(<StageElapsedTime elapsedMs={10_000} elapsedSampledAt={null} />);

    expect(screen.getByText("10s")).not.toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(screen.getByText("15s")).not.toBeNull();
  } finally {
    vi.useRealTimers();
  }
});

it("applies a refreshed elapsed-time sample immediately", () => {
  const { rerender } = render(<StageElapsedTime elapsedMs={10_000} elapsedSampledAt={null} />);

  expect(screen.getByText("10s")).not.toBeNull();

  rerender(<StageElapsedTime elapsedMs={20_000} elapsedSampledAt={null} />);

  expect(screen.getByText("20s")).not.toBeNull();
});
