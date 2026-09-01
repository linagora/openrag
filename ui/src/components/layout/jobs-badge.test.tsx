import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { JobsBadge } from "./jobs-badge";

describe("JobsBadge", () => {
  it.each([
    [1, "1"],
    [9, "9"],
    [10, "10"],
    [99, "99"],
    [100, "99+"],
  ])("renders %i active jobs as %s", (count, expected) => {
    render(
      <JobsBadge
        result={{
          count,
          isInitialLoading: false,
          hasResolvedOnce: true,
          isError: false,
        }}
      />,
    );

    expect(screen.getByText(expected)).not.toBeNull();
  });

  it("stays hidden until a positive count has resolved", () => {
    const { rerender } = render(
      <JobsBadge
        result={{
          count: 0,
          isInitialLoading: true,
          hasResolvedOnce: false,
          isError: false,
        }}
      />,
    );

    expect(screen.queryByText(/\d/)).toBeNull();

    rerender(
      <JobsBadge
        result={{
          count: 0,
          isInitialLoading: false,
          hasResolvedOnce: true,
          isError: false,
        }}
      />,
    );

    expect(screen.queryByText(/\d/)).toBeNull();
  });

  it("keeps a resolved count visible during a transient refresh error", () => {
    render(
      <JobsBadge
        result={{
          count: 4,
          isInitialLoading: false,
          hasResolvedOnce: true,
          isError: true,
        }}
      />,
    );

    expect(screen.getByText("4")).not.toBeNull();
  });

  it("caps the visible count at 99+ while announcing the exact total", () => {
    render(
      <a href="/jobs">
        Jobs
        <JobsBadge
          result={{
            count: 100,
            isInitialLoading: false,
            hasResolvedOnce: true,
            isError: false,
          }}
        />
      </a>,
    );

    expect(screen.getByText("99+").getAttribute("aria-hidden")).toBe("true");
    expect(screen.getByRole("link", { name: "Jobs, 100 active jobs" })).not.toBeNull();
  });

  it("uses the compact design-system notification styling without intercepting clicks", () => {
    render(
      <JobsBadge
        result={{
          count: 9,
          isInitialLoading: false,
          hasResolvedOnce: true,
          isError: false,
        }}
      />,
    );

    const visibleBadge = screen.getByText("9");
    const wrapper = visibleBadge.parentElement;

    expect(visibleBadge.className).toContain("bg-destructive");
    expect(visibleBadge.className).toContain("text-white");
    expect(visibleBadge.className).toContain("font-bold");
    expect(visibleBadge.className).toContain("tabular-nums");
    expect(visibleBadge.className).toContain("rounded-full");
    expect(wrapper?.className).toContain("pointer-events-none");
    expect(wrapper?.className).not.toContain("group-data-[collapsible=icon]:hidden");
  });
});
