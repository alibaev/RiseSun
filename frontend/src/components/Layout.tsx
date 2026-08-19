import { Link, Outlet } from "react-router-dom";
import { canManageSystem, useAuth } from "../auth/AuthContext";
import { NotificationBell } from "./NotificationBell";

const ROLE_LABELS: Record<string, string> = {
  operator: "Оператор",
  engineer: "Инженер",
  observer: "Наблюдатель",
  admin: "Администратор",
  super_admin: "Супер-администратор",
};

export function Layout() {
  const { role, logout } = useAuth();

  return (
    <div className="app-shell">
      <header className="topbar">
        <Link to="/" className="brand">
          MMWS
        </Link>
        <nav>
          <Link to="/">Дашборд</Link>
          <Link to="/meters">Счётчики</Link>
          <Link to="/schemes">Схемы параметров</Link>
          <Link to="/scheduled-jobs">Расписания</Link>
          {canManageSystem(role) && <Link to="/audit-log">Журнал аудита</Link>}
          {canManageSystem(role) && <Link to="/users">Пользователи</Link>}
          {canManageSystem(role) && <Link to="/billing-keys">API-ключи биллинга</Link>}
        </nav>
        <div className="topbar-user">
          <NotificationBell />
          <span>{role ? ROLE_LABELS[role] ?? role : ""}</span>
          <button onClick={logout}>Выйти</button>
        </div>
      </header>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
