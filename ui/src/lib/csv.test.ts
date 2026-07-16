import { describe, expect, it } from "vitest";
import { toCsv } from "./csv";

describe("toCsv", () => {
  it("sanitizes formula-leading cells", () => {
    const csv = toCsv(
      [{ header: "filename", value: (row: { filename: string }) => row.filename }],
      [
        { filename: "=cmd|' /C calc'!A0" },
        { filename: "+sum(1,2)" },
        { filename: "-10" },
        { filename: "@admin" },
        { filename: "safe.pdf" },
      ],
    );

    expect(csv.split("\n")).toEqual([
      "filename",
      "'=cmd|' /C calc'!A0",
      "\"'+sum(1,2)\"",
      "'-10",
      "'@admin",
      "safe.pdf",
    ]);
  });
});
