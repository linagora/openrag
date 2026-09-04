import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SystemPage from "./system";

const systemConfig = vi.hoisted(() => ({
  grafanaUrl: null as string | null,
  metricsLoading: false,
  refetchOnMount: undefined as unknown,
}));

vi.mock("@tanstack/react-query", () => ({
  useQuery: ({
    queryKey,
    refetchOnMount,
  }: {
    queryKey: unknown[];
    refetchOnMount?: unknown;
  }) => {
    const key = queryKey[0];
    if (key === "system-config") {
      systemConfig.refetchOnMount = refetchOnMount;
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
        isLoading: systemConfig.metricsLoading,
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
    systemConfig.metricsLoading = false;
    systemConfig.refetchOnMount = undefined;
  });

  it("refetches runtime configuration whenever the page mounts", () => {
    render(<SystemPage />);

    expect(systemConfig.refetchOnMount).toBe("always");
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

  it("keeps the Grafana action visible while metrics are loading", async () => {
    systemConfig.grafanaUrl =
      "https://grafana.example/d/openrag-http/openrag-http-metrics";
    systemConfig.metricsLoading = true;

    render(<SystemPage />);

    await userEvent.click(screen.getByRole("tab", { name: "Metrics" }));

    expect(
      screen.getByRole("link", {
        name: "Open metrics dashboard in Grafana (opens in a new tab)",
      }),
    ).not.toBeNull();
  });

  it("explains how to configure Grafana when the dashboard URL is missing", async () => {
    render(<SystemPage />);

    expect(screen.queryByRole("link", { name: /grafana/i })).toBeNull();

    await userEvent.click(screen.getByRole("tab", { name: "Metrics" }));
    await userEvent.click(screen.getByRole("button", { name: "Open in Grafana" }));

    expect(screen.getByRole("dialog")).not.toBeNull();
    expect(screen.getByRole("heading", { name: "Grafana is not configured" })).not.toBeNull();
    expect(screen.getByText("GRAFANA_URL")).not.toBeNull();
  });
});
