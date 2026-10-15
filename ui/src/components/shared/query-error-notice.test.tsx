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
});
