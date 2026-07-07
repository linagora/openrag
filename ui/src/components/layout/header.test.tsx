import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { Header } from "./header";

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({
    user: {
      id: 1,
      display_name: "Admin User",
      email: "admin@example.com",
      is_admin: true,
    },
    logout: vi.fn(),
  }),
}));

vi.mock("@/components/ui/sidebar", () => ({
  SidebarTrigger: () => <button type="button">Toggle sidebar</button>,
}));

describe("Header", () => {
  it("links to the Chainlit chat", () => {
    render(
      <MemoryRouter>
        <Header />
      </MemoryRouter>,
    );

    const chatLink = screen.getByRole("link", { name: /chat/i });
    expect(chatLink.getAttribute("href")).toBe("/chainlit/");
    expect(chatLink.getAttribute("target")).toBe("_blank");
  });
});
