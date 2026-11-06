import { beforeAll, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
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

  it("scrolls the trigger up before opening when the list would not fit below it", async () => {
    render(<PartitionCombobox partitions={partitions} value="zeta" onValueChange={vi.fn()} aria-label="Partition" />);
    const trigger = screen.getByRole("combobox", { name: "Partition" });
    trigger.getBoundingClientRect = () => ({ bottom: window.innerHeight - 50 }) as DOMRect;
    trigger.scrollIntoView = vi.fn();

    await userEvent.click(trigger);

    expect(trigger.scrollIntoView).toHaveBeenCalledWith({ block: "center" });
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
