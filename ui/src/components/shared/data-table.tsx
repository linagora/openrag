import {
  type ColumnDef,
  type ColumnFiltersState,
  type OnChangeFn,
  type RowSelectionState,
  type SortingState,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight, ArrowUpDown, ArrowUp, ArrowDown } from "lucide-react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";

interface BaseDataTableProps<TData, TValue> {
  columns: ColumnDef<TData, TValue>[];
  data: TData[];
  pageSize?: number;
  emptyMessage?: string;
  initialSorting?: SortingState;
  /** Render a leading checkbox column. */
  enableSelection?: boolean;
  /** Optional row-level selection guard for pages with state-dependent bulk actions. */
  canSelectRow?: (row: TData) => boolean;
  /** Stable row id (e.g. file_id) so selection survives a data refetch. */
  getRowId?: (row: TData) => string;
}

type DataTableSelectionProps =
  | {
      /** Controlled selection state for pages that render bulk controls in their own toolbar. */
      rowSelection: RowSelectionState;
      onRowSelectionChange: OnChangeFn<RowSelectionState>;
    }
  | {
      rowSelection?: never;
      onRowSelectionChange?: never;
    };

type DataTableProps<TData, TValue> = BaseDataTableProps<TData, TValue> & DataTableSelectionProps;

export function DataTable<TData, TValue>({
  columns,
  data,
  pageSize = 10,
  emptyMessage = "No results.",
  initialSorting = [],
  enableSelection = false,
  canSelectRow,
  getRowId,
  rowSelection: controlledRowSelection,
  onRowSelectionChange,
}: DataTableProps<TData, TValue>) {
  const [sorting, setSorting] = useState<SortingState>(initialSorting);
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([]);
  const [pagination, setPagination] = useState({ pageIndex: 0, pageSize });
  const [internalRowSelection, setInternalRowSelection] = useState<RowSelectionState>({});
  const rowSelection = controlledRowSelection ?? internalRowSelection;
  const setRowSelection = onRowSelectionChange ?? setInternalRowSelection;

  // Prepend a checkbox column when selection is enabled.
  const tableColumns = useMemo<ColumnDef<TData, TValue>[]>(() => {
    if (!enableSelection) return columns;
    const selectColumn: ColumnDef<TData, TValue> = {
      id: "__select",
      enableSorting: false,
      header: ({ table }) => {
        const hasSelectablePageRows = table.getRowModel().rows.some((row) => row.getCanSelect());
        return (
          <Checkbox
            checked={
              table.getIsAllPageRowsSelected() ||
              (table.getIsSomePageRowsSelected() && "indeterminate")
            }
            onCheckedChange={(value) => table.toggleAllPageRowsSelected(!!value)}
            disabled={!hasSelectablePageRows}
            aria-label="Select visible rows"
          />
        );
      },
      cell: ({ row }) => (
        <Checkbox
          checked={row.getIsSelected()}
          onCheckedChange={(value) => row.toggleSelected(!!value)}
          disabled={!row.getCanSelect()}
          aria-label="Select row"
        />
      ),
    };
    return [selectColumn, ...columns];
  }, [columns, enableSelection]);

  const table = useReactTable({
    data,
    columns: tableColumns,
    getCoreRowModel: getCoreRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    onPaginationChange: setPagination,
    onRowSelectionChange: setRowSelection,
    enableRowSelection: enableSelection
      ? (row) => (canSelectRow ? canSelectRow(row.original) : true)
      : false,
    getRowId,
    // Keep the user on their current page when the rows change underneath them
    // (e.g. deleting a row refetches the list). TanStack resets pageIndex to 0
    // on every data change by default, which bounced the user back to page 1.
    autoResetPageIndex: false,
    state: { sorting, columnFilters, pagination, rowSelection },
  });

  // If the current page no longer exists after the data shrank — e.g. the last
  // remaining row on the last page was deleted — fall back to the last valid
  // page instead of stranding the user on an empty "No results" page.
  const pageCount = table.getPageCount();
  useEffect(() => {
    if (pagination.pageIndex > 0 && pagination.pageIndex > pageCount - 1) {
      setPagination((prev) => ({ ...prev, pageIndex: Math.max(0, pageCount - 1) }));
    }
  }, [pageCount, pagination.pageIndex]);

  const columnCount = table.getAllLeafColumns().length;

  return (
    <div>
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            {table.getHeaderGroups().map((headerGroup) => (
              <TableRow key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <TableHead key={header.id}>
                    {header.isPlaceholder
                      ? null
                      : flexRender(
                          header.column.columnDef.header,
                          header.getContext(),
                        )}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {table.getRowModel().rows.length ? (
              table.getRowModel().rows.map((row) => (
                <TableRow
                  key={row.id}
                  data-state={row.getIsSelected() ? "selected" : undefined}
                >
                  {row.getVisibleCells().map((cell) => (
                    <TableCell key={cell.id}>
                      {flexRender(
                        cell.column.columnDef.cell,
                        cell.getContext(),
                      )}
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : (
              <TableRow>
                <TableCell
                  colSpan={columnCount}
                  className="h-24 text-center text-muted-foreground"
                >
                  {emptyMessage}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>
      {table.getPageCount() > 1 && (
        <div className="flex items-center justify-between py-4">
          <p className="text-sm text-muted-foreground">
            Page {table.getState().pagination.pageIndex + 1} of{" "}
            {table.getPageCount()}
          </p>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => table.previousPage()}
              disabled={!table.getCanPreviousPage()}
              aria-label="Previous page"
              title="Previous page"
            >
              <ChevronLeft className="h-4 w-4" />
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => table.nextPage()}
              disabled={!table.getCanNextPage()}
              aria-label="Next page"
              title="Next page"
            >
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

// Helper to create a sortable, direction-aware column header.
export function SortableHeader({
  column,
  title,
}: {
  column: { toggleSorting: (desc: boolean) => void; getIsSorted: () => false | "asc" | "desc" };
  title: string;
}) {
  const sorted = column.getIsSorted();
  const Icon = sorted === "asc" ? ArrowUp : sorted === "desc" ? ArrowDown : ArrowUpDown;
  return (
    <Button
      variant="ghost"
      size="sm"
      className="-ml-3 h-8"
      onClick={() => column.toggleSorting(sorted === "asc")}
    >
      {title}
      <Icon className="ml-2 h-3 w-3" />
    </Button>
  );
}
