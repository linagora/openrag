import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Header } from "./header";

const authState = vi.hoisted(() => ({
  chainlitEnabled: true,
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({
    user: {
      id: 1,
      display_name: "Admin User",
      email: "admin@example.com",
      is_admin: true,
      chainlit_enabled: authState.chainlitEnabled,
    },
    logout: vi.fn(),
  }),
}));

vi.mock("@/components/ui/sidebar", () => ({
  SidebarTrigger: () => <button type="button">Toggle sidebar</button>,
}));

describe("Header", () => {
  beforeEach(() => {
    authState.chainlitEnabled = true;
  });

  it("links to the Chainlit chat", () => {
    render(
      <MemoryRouter>
        <Header />
      </MemoryRouter>,
    );

    const chatLink = screen.getByRole("link", { name: /chat/i });
    expect(chatLink.getAttribute("href")).toBe("/chainlit/");
    expect(chatLink.getAttribute("target")).toBe("_blank");
    expect(chatLink.getAttribute("rel")).toBe("noopener noreferrer");
  });

  it("hides the Chainlit chat link when Chainlit is disabled", () => {
    authState.chainlitEnabled = false;

    render(
      <MemoryRouter>
        <Header />
      </MemoryRouter>,
    );

    expect(screen.queryByRole("link", { name: /chat/i })).toBeNull();
  });
});
