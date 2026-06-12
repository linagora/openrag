import {
  createContext,
  useContext,
  useState,
  useCallback,
  useEffect,
  type ReactNode,
} from "react";
import { login as apiLogin } from "./api/auth";

interface AuthUser {
  sub: string;
  email: string;
  role: string;
  mustChangePassword: boolean;
}

interface AuthContextType {
  user: AuthUser | null;
  isAuthenticated: boolean;
  isAdmin: boolean;
  isSuperadmin: boolean;
  isUser: boolean;
  mustChangePassword: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  clearMustChangePassword: () => void;
}

const AuthContext = createContext<AuthContextType | null>(null);

function decodeJwt(token: string): AuthUser | null {
  try {
    const payload = JSON.parse(atob(token.split(".")[1]));
    return {
      sub: payload.sub,
      email: payload.email,
      role: payload.role,
      mustChangePassword: payload.must_change_password ?? false,
    };
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(() => {
    const token = localStorage.getItem("access_token");
    return token ? decodeJwt(token) : null;
  });

  useEffect(() => {
    const token = localStorage.getItem("access_token");
    if (token) {
      const decoded = decodeJwt(token);
      if (decoded) setUser(decoded);
      else {
        localStorage.removeItem("access_token");
        localStorage.removeItem("refresh_token");
      }
    }
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const resp = await apiLogin(email, password);
    localStorage.setItem("access_token", resp.access_token);
    localStorage.setItem("refresh_token", resp.refresh_token);
    const decoded = decodeJwt(resp.access_token);
    setUser(decoded);
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    setUser(null);
  }, []);

  const clearMustChangePassword = useCallback(() => {
    setUser((prev) => prev ? { ...prev, mustChangePassword: false } : null);
  }, []);

  return (
    <AuthContext.Provider
      value={{
        user,
        isAuthenticated: !!user,
        isAdmin: user?.role === "admin" || user?.role === "superadmin",
        isSuperadmin: user?.role === "superadmin",
        isUser: user?.role === "user",
        mustChangePassword: user?.mustChangePassword ?? false,
        login,
        logout,
        clearMustChangePassword,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
