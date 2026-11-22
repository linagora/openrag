import { beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { ApiError, TOKEN_KEY } from "@/lib/api/client";

const { loginWithToken } = vi.hoisted(() => ({ loginWithToken: vi.fn() }));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({
    loginWithToken,
    isAuthenticated: false,
    isLoading: false,
  }),
}));

import LoginPage from "./login";

function renderLoginPage() {
  return render(
    <MemoryRouter>
      <LoginPage />
    </MemoryRouter>,
  );
}

describe("LoginPage token validation", () => {
  beforeEach(() => {
    loginWithToken.mockReset();
  });

  it.each([
    { name: "a 503 response", error: new ApiError(503, { detail: "Service unavailable" }) },
    { name: "a network failure", error: new TypeError("Failed to fetch") },
  ])("shows a retryable service message after $name and preserves the form", async ({ error }) => {
    loginWithToken.mockRejectedValueOnce(error).mockResolvedValueOnce(undefined);
    const user = userEvent.setup();
    renderLoginPage();

    const tokenInput = screen.getByLabelText("API token");
    await user.type(tokenInput, "or-retryable-token");
    const submit = screen.getByRole("button", { name: "Sign in" });
    await user.click(submit);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't verify your token because the service is unavailable. Try again.",
    );
    expect(tokenInput).toHaveValue("or-retryable-token");
    expect(submit).toBeEnabled();

    await user.click(submit);
    expect(loginWithToken).toHaveBeenLastCalledWith("or-retryable-token");
  });

  it("shows rejected-token feedback for a 401 response", async () => {
    loginWithToken.mockRejectedValueOnce(new ApiError(401, { detail: "Unauthorized" }));
    const user = userEvent.setup();
    renderLoginPage();

    const tokenInput = screen.getByLabelText("API token");
    await user.type(tokenInput, "or-invalid-token");
    const submit = screen.getByRole("button", { name: "Sign in" });
    await user.click(submit);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That token was rejected. Check it and try again.",
    );
    expect(tokenInput).toHaveValue("or-invalid-token");
    expect(submit).toBeEnabled();
  });

  it("shows rejected-token feedback for the token-mode 403 response", async () => {
    loginWithToken.mockRejectedValueOnce(new ApiError(403, { detail: "Invalid token" }));
    const user = userEvent.setup();
    renderLoginPage();

    const tokenInput = screen.getByLabelText("API token");
    await user.type(tokenInput, "or-invalid-token");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That token was rejected. Check it and try again.",
    );
  });

  it("clears a retained bearer token when the user switches to SSO", async () => {
    localStorage.setItem(TOKEN_KEY, "or-retryable-token");
    const user = userEvent.setup();
    renderLoginPage();

    const ssoLink = screen.getByRole("link", { name: "Sign in with SSO" });
    ssoLink.addEventListener("click", (event) => event.preventDefault(), { once: true });
    await user.click(ssoLink);

    expect(localStorage.getItem(TOKEN_KEY)).toBeNull();
  });
});
