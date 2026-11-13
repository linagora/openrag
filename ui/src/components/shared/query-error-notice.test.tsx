import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { QueryErrorNotice } from "./query-error-notice";

describe("QueryErrorNotice", () => {
  it("offers a retry action and disables it while retrying", async () => {
    const onRetry = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <QueryErrorNotice
        title="Partitions could not be loaded"
        message="The service is unavailable."
        isRetrying={false}
        onRetry={onRetry}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledOnce();

    rerender(
      <QueryErrorNotice
        title="Partitions could not be loaded"
        message="The service is unavailable."
        isRetrying
        onRetry={onRetry}
      />,
    );
    expect(screen.getByRole("button", { name: "Retrying…" }).hasAttribute("disabled")).toBe(true);
  });

  it("can show an informational notice without a retry action", () => {
    render(
      <QueryErrorNotice
        title="Some partition access could not be checked"
        message="Your access does not allow checking every partition."
        variant="default"
      />,
    );

    expect(screen.getByRole("alert").textContent).toContain("Some partition access could not be checked");
    expect(screen.queryByRole("button")).toBeNull();
  });
});
