import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SystemPage from "./system";

const systemConfig = vi.hoisted(() => ({
  grafanaUrl: null as string | null,
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: ({ queryKey }: { queryKey: unknown[] }) => {
    const key = queryKey[0];
    if (key === "system-config") {
      return {
        data: { grafana_url: systemConfig.grafanaUrl },
        error: null,
        isLoading: false,
      };
    }
    if (key === "system-health") {
      return {
        isLoading: false,
        isSuccess: true,
        refetch: vi.fn(),
      };
    }
    if (key === "system-version") {
      return {
        data: { version: "2.0.2" },
        isLoading: false,
      };
    }
    if (key === "system-actors") {
      return {
        data: { actors: [] },
        isLoading: false,
      };
    }
    if (key === "system-metrics") {
      return {
        data: "openrag_requests_total 1",
        isLoading: false,
      };
    }
    return {
      data: undefined,
      isLoading: false,
    };
  },
  useMutation: () => ({
    isPending: false,
    mutate: vi.fn(),
  }),
  useQueryClient: () => ({
    invalidateQueries: vi.fn(),
  }),
}));

describe("SystemPage Grafana action", () => {
  beforeEach(() => {
    systemConfig.grafanaUrl = null;
  });

  it("opens the runtime-configured dashboard from the Metrics tab", async () => {
    systemConfig.grafanaUrl =
      "https://grafana.example/d/openrag-http/openrag-http-metrics";

    render(<SystemPage />);

    await userEvent.click(screen.getByRole("tab", { name: "Metrics" }));

    const link = screen.getByRole("link", {
      name: "Open metrics dashboard in Grafana (opens in a new tab)",
    });
    expect(link.getAttribute("href")).toBe(
      "https://grafana.example/d/openrag-http/openrag-http-metrics",
    );
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("hides Grafana actions when the deployment has no dashboard URL", async () => {
    render(<SystemPage />);

    expect(screen.queryByRole("link", { name: /grafana/i })).toBeNull();

    await userEvent.click(screen.getByRole("tab", { name: "Metrics" }));

    expect(screen.queryByRole("link", { name: /grafana/i })).toBeNull();
  });
});
