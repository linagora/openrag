import { request } from "./client";

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  must_change_password: boolean;
}

export async function login(email: string, password: string): Promise<TokenResponse> {
  return request<TokenResponse>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
    noAuth: true,
  });
}
