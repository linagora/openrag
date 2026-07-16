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
    ].join("\n"));
  });
});
