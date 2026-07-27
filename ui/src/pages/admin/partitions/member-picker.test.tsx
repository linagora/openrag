import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MemberPicker } from "./member-picker";

const candidates = [
  { user_id: 2, display_name: "Sam Lee" },
  { user_id: 3, display_name: "Sam Lee" },
  { user_id: 4, display_name: null },
];

describe("MemberPicker", () => {
  it("shows stable IDs so duplicate names remain distinguishable", () => {
    render(
      <MemberPicker
        candidates={candidates}
        isLoading={false}
        isError={false}
        selectedUserIds={[]}
        onSelectionChange={vi.fn()}
      />,
    );

    expect(screen.getAllByText("Sam Lee")).toHaveLength(2);
    expect(screen.getByText("User ID 2")).not.toBeNull();
    expect(screen.getByText("User ID 3")).not.toBeNull();
    expect(screen.getByText("User #4")).not.toBeNull();
  });

  it("searches by display name and user ID", async () => {
    const user = userEvent.setup();
    render(
      <MemberPicker
        candidates={candidates}
        isLoading={false}
        isError={false}
        selectedUserIds={[]}
        onSelectionChange={vi.fn()}
      />,
    );

    const search = screen.getByRole("textbox", { name: "Users" });
    await user.type(search, "3");

    expect(screen.getByText("User ID 3")).not.toBeNull();
    expect(screen.queryByText("User ID 2")).toBeNull();
  });

  it("returns all selected user IDs when a candidate is checked", async () => {
    const onSelectionChange = vi.fn();
    const user = userEvent.setup();
    render(
      <MemberPicker
        candidates={candidates}
        isLoading={false}
        isError={false}
        selectedUserIds={[2]}
        onSelectionChange={onSelectionChange}
      />,
    );

    await user.click(screen.getByRole("checkbox", { name: /select sam lee, user id 3/i }));

    expect(onSelectionChange).toHaveBeenCalledWith([2, 3]);
  });
});
