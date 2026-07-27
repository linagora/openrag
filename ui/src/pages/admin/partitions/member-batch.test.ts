import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/client";
import { addPartitionMembers } from "./member-batch";

const candidates = [
  { user_id: 2, display_name: "Sam Lee" },
  { user_id: 3, display_name: "Alex Morgan" },
  { user_id: 4, display_name: null },
];

describe("addPartitionMembers", () => {
  it("preserves the server reason for each failed user", async () => {
    const addMember = vi.fn()
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error("User is already a member"))
      .mockRejectedValueOnce(new Error("Account is inactive"));

    const result = await addPartitionMembers({
      partitionName: "legal",
      candidates,
      role: "viewer",
      addMember,
    });

    expect(result.addedCandidates).toEqual([candidates[0]]);
    expect(result.failures).toEqual([
      {
        candidate: candidates[1],
        message: "User is already a member",
        attempted: true,
      },
      {
        candidate: candidates[2],
        message: "Account is inactive",
        attempted: true,
      },
    ]);
  });

  it("stops sending requests after authorization is lost", async () => {
    const addMember = vi.fn().mockRejectedValueOnce(
      new ApiError(403, { detail: "Partition owner role required" }),
    );

    const result = await addPartitionMembers({
      partitionName: "legal",
      candidates,
      role: "editor",
      addMember,
    });

    expect(addMember).toHaveBeenCalledOnce();
    expect(result.failures).toHaveLength(3);
    expect(result.failures[0]).toMatchObject({
      candidate: candidates[0],
      message: "Partition owner role required",
      attempted: true,
    });
    expect(result.failures[1]).toMatchObject({
      candidate: candidates[1],
      attempted: false,
    });
  });
});
