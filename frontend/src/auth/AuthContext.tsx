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

// Этап 2 (ТЗ п.4.2.4): запись параметров — «Инженер»/«Администратор»,
// плюс «Супер-администратор» как надмножество прав (тот же набор, что
// и Permission.WRITE_PARAMETER на Backend, app/core/permissions.py).
const WRITE_PARAMETER_ROLES = new Set(["engineer", "admin", "super_admin"]);

export function canWriteParameter(role: string | null): boolean {
  return role !== null && WRITE_PARAMETER_ROLES.has(role);
}

// Этап 4 (ТЗ Приложение Б TABLE 0): создание/редактирование/удаление схем
// параметров и расписаний автоопроса — только «Администратор»/«Супер-
// администратор» («Инженер» лишь ПРИМЕНЯЕТ готовые схемы, см.
// canWriteParameter/Permission.WRITE_PARAMETER на apply-эндпоинте); тот же
// набор ролей, что у Permission.MANAGE_PARAMETER_SCHEMES/MANAGE_SCHEDULED_JOBS
// на Backend.
const MANAGE_AUTOMATION_ROLES = new Set(["admin", "super_admin"]);

export function canManageAutomation(role: string | null): boolean {
  return role !== null && MANAGE_AUTOMATION_ROLES.has(role);
}

// Этап 5 (ТЗ п.4.2.11): журнал аудита, управление пользователями,
// API-ключи биллинга — тот же набор ролей, что и MANAGE_AUTOMATION_ROLES
// (Permission.VIEW_AUDIT_LOG/MANAGE_USERS/MANAGE_BILLING_KEYS на
// Backend — все только Admin/Super-admin).
export function canManageSystem(role: string | null): boolean {
  return role !== null && MANAGE_AUTOMATION_ROLES.has(role);
}
