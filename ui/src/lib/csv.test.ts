import { afterEach, describe, expect, it, vi } from "vitest";
import { downloadCsv, toCsv, toExcelCsv } from "./csv";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

describe("toCsv", () => {
  it("sanitizes formula-leading cells", () => {
    const csv = toCsv(
      [{ header: "filename", value: (row: { filename: string }) => row.filename }],
      [
        { filename: "=cmd|' /C calc'!A0" },
        { filename: "+sum(1,2)" },
        { filename: "-10" },
        { filename: "@admin" },
        { filename: "\t=cmd" },
        { filename: "\r=cmd" },
        { filename: "\n=cmd" },
        { filename: "safe.pdf" },
      ],
    );

    expect(csv).toBe([
      "filename",
      "'=cmd|' /C calc'!A0",
      "\"'+sum(1,2)\"",
      "'-10",
      "'@admin",
      "'\t=cmd",
      "\"'\r=cmd\"",
      "\"'\n=cmd\"",
      "safe.pdf",
    ].join("\r\n"));
  });

  it("adds Excel encoding and delimiter hints to downloads", () => {
    const csv = toExcelCsv(
      [{ header: "filename", value: (row: { filename: string }) => row.filename }],
      [{ filename: "Rapport_priv\u00e9.pdf" }],
    );

    expect(csv).toBe("\uFEFFsep=,\r\nfilename\r\nRapport_priv\u00e9.pdf");
  });

  it("keeps the object URL alive until the browser handles the click", () => {
    vi.useFakeTimers();
    const createObjectURL = vi.fn(() => "blob:csv");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    downloadCsv("report.csv", [{ header: "name", value: (row: { name: string }) => row.name }], [
      { name: "report" },
    ]);

    expect(document.querySelector('a[download="report.csv"]')).not.toBeNull();
    expect(click).toHaveBeenCalledOnce();
    expect(revokeObjectURL).not.toHaveBeenCalled();

    vi.runAllTimers();

    expect(document.querySelector('a[download="report.csv"]')).toBeNull();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:csv");
  });
});
