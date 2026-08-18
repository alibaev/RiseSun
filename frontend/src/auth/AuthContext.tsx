import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { login as apiLogin } from "../api/client";
import { roleFromAccessToken, tokenStorage } from "./tokenStorage";

interface AuthState {
  isAuthenticated: boolean;
  role: string | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const initialToken = tokenStorage.getAccess();
  const [role, setRole] = useState<string | null>(initialToken ? roleFromAccessToken(initialToken) : null);

  const login = useCallback(async (username: string, password: string) => {
    await apiLogin(username, password);
    const token = tokenStorage.getAccess();
    setRole(token ? roleFromAccessToken(token) : null);
  }, []);

  const logout = useCallback(() => {
    tokenStorage.clear();
    setRole(null);
  }, []);

  const value = useMemo<AuthState>(
    () => ({ isAuthenticated: role !== null, role, login, logout }),
    [role, login, logout]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth должен использоваться внутри AuthProvider");
  return ctx;
}

const READ_TRIGGER_ROLES = new Set(["operator", "engineer", "admin", "super_admin"]);

export function canTriggerRead(role: string | null): boolean {
  return role !== null && READ_TRIGGER_ROLES.has(role);
}
