import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./client";
import { listPartitionMembers, listPartitions } from "./partitions";
import { listUserMemberships } from "./users";

vi.mock("./partitions", () => ({
  listPartitions: vi.fn(),
  listPartitionMembers: vi.fn(),
}));

const listPartitionsMock = vi.mocked(listPartitions);
const listPartitionMembersMock = vi.mocked(listPartitionMembers);

describe("listUserMemberships", () => {
  beforeEach(() => vi.clearAllMocks());

  it("returns memberships and records partitions hidden by the caller's role", async () => {
    listPartitionsMock.mockResolvedValue({
      partitions: [{ name: "visible" }, { name: "restricted" }] as never,
    });
    listPartitionMembersMock.mockImplementation(async (name) => {
      if (name === "restricted") throw new ApiError(403, { detail: "Forbidden" });
      return { members: [{ user_id: 7, role: "editor", added_at: null }] as never };
    });

    await expect(listUserMemberships(7)).resolves.toEqual({
      memberships: [{ partition: "visible", role: "editor", added_at: null }],
      unavailablePartitions: ["restricted"],
    });
  });

  it("propagates network and server failures instead of silently omitting memberships", async () => {
    listPartitionsMock.mockResolvedValue({ partitions: [{ name: "visible" }] as never });
    listPartitionMembersMock.mockRejectedValue(new ApiError(503, { detail: "Unavailable" }));

    await expect(listUserMemberships(7)).rejects.toMatchObject({ status: 503 });
  });
});
