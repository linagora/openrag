type CsvColumn<T> = {
  header: string;
  value: (row: T) => unknown;
};

function csvCell(value: unknown): string {
  const text = value == null ? "" : String(value);
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function toCsv<T>(columns: CsvColumn<T>[], rows: T[]): string {
  return [
    columns.map((column) => csvCell(column.header)).join(","),
    ...rows.map((row) => columns.map((column) => csvCell(column.value(row))).join(",")),
  ].join("\n");
}

export function downloadCsv<T>(filename: string, columns: CsvColumn<T>[], rows: T[]) {
  const blob = new Blob([toCsv(columns, rows)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
