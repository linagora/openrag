type CsvColumn<T> = {
  header: string;
  value: (row: T) => unknown;
};

const FORMULA_PREFIX = /^[=+\-@\t\r\n]/;
const CSV_DELIMITER = ",";
const CSV_LINE_BREAK = "\r\n";
const UTF8_BOM = "\uFEFF";

function csvCell(value: unknown): string {
  const raw = value == null ? "" : String(value);
  const text = FORMULA_PREFIX.test(raw) ? `'${raw}` : raw;
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

export function toCsv<T>(columns: CsvColumn<T>[], rows: T[]): string {
  return [
    columns.map((column) => csvCell(column.header)).join(CSV_DELIMITER),
    ...rows.map((row) => columns.map((column) => csvCell(column.value(row))).join(CSV_DELIMITER)),
  ].join(CSV_LINE_BREAK);
}

export function toExcelCsv<T>(columns: CsvColumn<T>[], rows: T[]): string {
  return `${UTF8_BOM}sep=${CSV_DELIMITER}${CSV_LINE_BREAK}${toCsv(columns, rows)}`;
}

export function downloadCsv<T>(filename: string, columns: CsvColumn<T>[], rows: T[]) {
  const blob = new Blob([toExcelCsv(columns, rows)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.style.display = "none";

  try {
    document.body.appendChild(link);
    link.click();
  } finally {
    window.setTimeout(() => {
      link.remove();
      URL.revokeObjectURL(url);
    }, 0);
  }
}
