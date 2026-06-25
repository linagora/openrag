import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { ColumnDef } from "@tanstack/react-table";

import { DataTable } from "./data-table";

type Row = { id: number };

const columns: ColumnDef<Row, unknown>[] = [
  { accessorKey: "id", header: "ID", cell: ({ row }) => `row-${row.original.id}` },
];

const makeRows = (n: number): Row[] => Array.from({ length: n }, (_, i) => ({ id: i + 1 }));

// The pager renders only a prev + next icon button (no text), so "next" is the
// last button on screen.
const clickNextPage = () => {
  const buttons = screen.getAllByRole("button");
  fireEvent.click(buttons[buttons.length - 1]);
};

describe("DataTable pagination", () => {
  it("keeps the user on the current page when a row is deleted underneath them", () => {
    const { rerender } = render(<DataTable columns={columns} data={makeRows(25)} />);
    expect(screen.getByText("Page 1 of 3")).toBeTruthy();

    clickNextPage();
    expect(screen.getByText("Page 2 of 3")).toBeTruthy();
    expect(screen.getByText("row-11")).toBeTruthy();

    // A delete refetches the list with one fewer row. The user must stay on
    // page 2, not snap back to page 1 (the bug).
    rerender(<DataTable columns={columns} data={makeRows(24)} />);
    expect(screen.getByText("Page 2 of 3")).toBeTruthy();
    expect(screen.getByText("row-11")).toBeTruthy();
  });

  it("clamps to the last valid page when the current page disappears entirely", async () => {
    const { rerender } = render(<DataTable columns={columns} data={makeRows(11)} />);
    clickNextPage();
    expect(screen.getByText("Page 2 of 2")).toBeTruthy();

    // Deleting the only row on page 2 leaves a single page; the user should be
    // pulled back to it rather than stranded on an empty page.
    rerender(<DataTable columns={columns} data={makeRows(10)} />);
    expect(await screen.findByText("row-1")).toBeTruthy();
    expect(screen.queryByText("row-11")).toBeNull();
  });
});
