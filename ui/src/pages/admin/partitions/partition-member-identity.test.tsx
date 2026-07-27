import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { PartitionMember } from "@/lib/api/partitions";
import {
  PartitionMemberEmail,
  PartitionMemberIdentity,
} from "./partition-member-identity";
import { describePartitionMember } from "./partition-member";

function member(overrides: Partial<PartitionMember> = {}): PartitionMember {
  return {
    user_id: 9,
    display_name: "Alice",
    email: "alice@example.com",
    role: "viewer",
    added_at: null,
    ...overrides,
  };
}

describe("PartitionMemberIdentity", () => {
  it("shows the display name and stable user ID", () => {
    render(<PartitionMemberIdentity member={member()} />);

    expect(screen.getByText("Alice")).not.toBeNull();
    expect(screen.getByText("User ID 9")).not.toBeNull();
  });

  it("retains a useful identity when the display name is missing", () => {
    render(<PartitionMemberIdentity member={member({ display_name: null })} />);

    expect(screen.getByText("User 9")).not.toBeNull();
    expect(screen.getByText("User ID 9")).not.toBeNull();
  });

  it("shows email explicitly with a clear missing-value state", () => {
    const { rerender } = render(<PartitionMemberEmail member={member()} />);

    expect(screen.getByText("alice@example.com")).not.toBeNull();

    rerender(<PartitionMemberEmail member={member({ email: null })} />);

    expect(screen.getByText("Not available")).not.toBeNull();
  });

  it("describes a member unambiguously in destructive actions", () => {
    expect(describePartitionMember(member())).toBe(
      "Alice, alice@example.com (user ID 9)",
    );
    expect(describePartitionMember(member({ display_name: null, email: null }))).toBe(
      "user ID 9",
    );
  });
});
