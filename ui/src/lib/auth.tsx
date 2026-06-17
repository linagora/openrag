import {
  createContext,
  useContext,
  useState,
  useCallback,
  useEffect,
  type ReactNode,
} from "react";
import { getMyInfo, type MyInfo } from "./api/account";
import { TOKEN_KEY } from "./api/client";

// Identity comes from the backend, not a decoded token: /users/info resolves the
// current principal from either a stored bearer token (AUTH_MODE=token) or the
// same-origin OIDC session cookie. The capability layer reads `isAdmin` from here.
interface AuthContextType {
  user: MyInfo | null;
  isAuthenticated: boolean;
  isAdmin: boolean;
  isLoading: boolean;
  /** Store a bearer token and resolve identity; throws if the token is invalid. */
  loginWithToken: (token: string) => Promise<void>;
  logout: () => void;
  reload: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<MyInfo | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      setUser(await getMyInfo());
    } catch {
      setUser(null);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const loginWithToken = useCallback(async (token: string) => {
    localStorage.setItem(TOKEN_KEY, token.trim());
    try {
      setUser(await getMyInfo());
    } catch (e) {
      localStorage.removeItem(TOKEN_KEY);
      setUser(null);
      throw e;
    }
  }, []);

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider
      value={{
        user,
        isAuthenticated: !!user,
        isAdmin: !!user?.is_admin,
        isLoading,
        loginWithToken,
        logout,
        reload: load,
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
