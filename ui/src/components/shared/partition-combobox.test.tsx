import { beforeAll, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { PartitionCombobox } from "./partition-combobox";

beforeAll(() => {
  // cmdk measures and scrolls its list; jsdom implements neither.
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  Element.prototype.scrollIntoView ??= () => {};
});

const partitions = ["zeta", "alpha", "ptranvan.twake.linagora.com"].map((name) => ({ partition: name, name }));

describe("PartitionCombobox", () => {
  it("filters partitions as the user types and selects one", async () => {
    const onValueChange = vi.fn();
    render(<PartitionCombobox partitions={partitions} value="zeta" onValueChange={onValueChange} aria-label="Partition" />);

    await userEvent.click(screen.getByRole("combobox", { name: "Partition" }));
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
      "alpha",
      "ptranvan.twake.linagora.com",
      "zeta",
    ]);

    await userEvent.type(screen.getByPlaceholderText("Search partitions..."), "tranv");
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["ptranvan.twake.linagora.com"]);

    await userEvent.click(screen.getByRole("option", { name: "ptranvan.twake.linagora.com" }));
    expect(onValueChange).toHaveBeenCalledWith("ptranvan.twake.linagora.com");
    expect(screen.queryByRole("option")).toBeNull();
  });

  it("does not match a query whose letters only line up across a repeated name", async () => {
    render(
      <PartitionCombobox
        partitions={[{ partition: "hr.twake.linagora.com", name: "hr.twake.linagora.com" }]}
        value=""
        onValueChange={vi.fn()}
        aria-label="Partition"
      />,
    );

    await userEvent.click(screen.getByRole("combobox", { name: "Partition" }));
    await userEvent.type(screen.getByPlaceholderText("Search partitions..."), "nah");

    expect(screen.queryByRole("option")).toBeNull();
  });

  it.each(["ArrowDown", "ArrowUp"])("opens with %s and focuses the search input", async (key) => {
    render(<PartitionCombobox partitions={partitions} value="zeta" onValueChange={vi.fn()} aria-label="Partition" />);

    screen.getByRole("combobox", { name: "Partition" }).focus();
    await userEvent.keyboard(`{${key}}`);

    expect(screen.getAllByRole("option")).toHaveLength(3);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByPlaceholderText("Search partitions...")));
  });

  it("leaves room for the no-match message with very few partitions", async () => {
    render(<PartitionCombobox partitions={partitions.slice(0, 1)} value="zeta" onValueChange={vi.fn()} aria-label="Partition" />);

    await userEvent.click(screen.getByRole("combobox", { name: "Partition" }));

    expect(screen.getByRole("listbox").getAttribute("style")).toContain("76px");
  });

  it("does not report a change when the current partition is picked again", async () => {
    const onValueChange = vi.fn();
    render(<PartitionCombobox partitions={partitions} value="zeta" onValueChange={onValueChange} aria-label="Partition" />);

    await userEvent.click(screen.getByRole("combobox", { name: "Partition" }));
    await userEvent.click(screen.getByRole("option", { name: "zeta" }));

    expect(onValueChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("option")).toBeNull();
  });
});
