import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { ColumnDef, RowSelectionState } from "@tanstack/react-table";
import { useState } from "react";

import { DataTable, SortableHeader } from "./data-table";

type Row = { id: number };

const columns: ColumnDef<Row, unknown>[] = [
  { accessorKey: "id", header: "ID", cell: ({ row }) => `row-${row.original.id}` },
];

const makeRows = (n: number): Row[] => Array.from({ length: n }, (_, i) => ({ id: i + 1 }));

const clickNextPage = () => {
  fireEvent.click(screen.getByRole("button", { name: /next page/i }));
};

describe("DataTable pagination", () => {
  it("labels pagination icon buttons", () => {
    render(<DataTable columns={columns} data={makeRows(25)} />);

    expect(screen.getByRole("button", { name: /previous page/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /next page/i })).toBeTruthy();
  });

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

describe("DataTable sorting", () => {
  const sortableColumns: ColumnDef<Row, unknown>[] = [
    {
      id: "id",
      accessorFn: (r) => r.id,
      header: ({ column }) => <SortableHeader column={column} title="ID" />,
      cell: ({ row }) => `row-${row.original.id}`,
    },
  ];
  const renderedOrder = () => screen.getAllByText(/^row-\d+$/).map((e) => e.textContent);

  it("honors initialSorting and toggles direction on header click", () => {
    render(<DataTable columns={sortableColumns} data={makeRows(3)} initialSorting={[{ id: "id", desc: true }]} />);
    expect(renderedOrder()).toEqual(["row-3", "row-2", "row-1"]);

    fireEvent.click(screen.getByRole("button", { name: /ID/ }));
    expect(renderedOrder()).toEqual(["row-1", "row-2", "row-3"]);
  });
});

describe("DataTable selection", () => {
  it("selects only the currently visible page rows", () => {
    function Harness() {
      const [selection, setSelection] = useState<RowSelectionState>({});
      return (
        <>
          <DataTable
            columns={columns}
            data={makeRows(11)}
            enableSelection
            getRowId={(r) => String(r.id)}
            rowSelection={selection}
            onRowSelectionChange={setSelection}
            pageSize={10}
          />
          <span>{Object.keys(selection).length} selected</span>
          <button onClick={() => setSelection({})}>clear-sel</button>
        </>
      );
    }

    render(<Harness />);

    expect(screen.getByText("0 selected")).toBeTruthy();

    // Header checkbox is the first checkbox; it should select only page 1.
    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    expect(screen.getByText("10 selected")).toBeTruthy();

    fireEvent.click(screen.getByText("clear-sel"));
    expect(screen.getByText("0 selected")).toBeTruthy();
  });

  it("does not render a built-in bulk banner", () => {
    render(
      <DataTable
        columns={columns}
        data={makeRows(3)}
        enableSelection
        getRowId={(r) => String(r.id)}
      />,
    );

    fireEvent.click(screen.getAllByRole("checkbox")[0]);
    expect(screen.queryByText(/selected/)).toBeNull();
  });
});
