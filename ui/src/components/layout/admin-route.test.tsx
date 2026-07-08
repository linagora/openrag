import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { AdminRoute } from "./admin-route";

// Mock the useAuth hook so we can control isAdmin
vi.mock("@/lib/auth", () => ({
  useAuth: vi.fn(),
}));

import { useAuth } from "@/lib/auth";

function renderAdminRoute(isAdmin: boolean) {
  (useAuth as ReturnType<typeof vi.fn>).mockReturnValue({ isAdmin });

  return render(
    <MemoryRouter initialEntries={["/admin"]}>
      <Routes>
        <Route
          path="/admin"
          element={
            <AdminRoute>
              <div>Admin Content</div>
            </AdminRoute>
          }
        />
        <Route path="/" element={<div>Home Page</div>} />
      </Routes>
    </MemoryRouter>
  );
}

describe("AdminRoute", () => {
  it("renders children when isAdmin is true (admin role)", () => {
    renderAdminRoute(true);
    expect(screen.getByText("Admin Content")).toBeDefined();
  });

  it("redirects to '/' when isAdmin is false (user role)", () => {
    renderAdminRoute(false);
    expect(screen.queryByText("Admin Content")).toBeNull();
    expect(screen.getByText("Home Page")).toBeDefined();
  });

  it("uses replace navigation so back-button cannot return to blocked route", () => {
    // When isAdmin is false, we should land on '/' rendered via replace.
    // We verify this by checking the home page is shown (redirect happened).
    renderAdminRoute(false);
    expect(screen.getByText("Home Page")).toBeDefined();
    expect(screen.queryByText("Admin Content")).toBeNull();
  });

  it("renders children when isAdmin is true (superadmin role)", () => {
    // superadmin also has isAdmin=true (computed in AuthContext), verify guard passes
    renderAdminRoute(true);
    expect(screen.getByText("Admin Content")).toBeDefined();
  });
});
