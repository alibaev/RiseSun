import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { canManageAutomation, canManageGateways, canManageSystem, useAuth } from "../auth/AuthContext";
import { NotificationBell } from "./NotificationBell";
import { MENU } from "../constants/menu";

const ROLE_LABELS: Record<string, string> = {
  operator: "Оператор",
  engineer: "Инженер",
  observer: "Наблюдатель",
  admin: "Администратор",
  super_admin: "Супер-администратор",
};

// Реальные страницы MMWS внутри пунктов меню (см. constants/menu.ts) —
// те же права, что были у плоского списка ссылок раньше. Пункты-
// заглушки (без backend) видны всем ролям — они не открывают реальных
// данных.
const RESTRICTED_PATHS: Record<string, (role: string | null) => boolean> = {
  "/audit-log": canManageSystem,
  "/users": canManageSystem,
  "/billing-keys": canManageSystem,
  "/gateways": canManageGateways,
  // 2026-09-12, по прямому указанию пользователя — "доступ к
  // расписанию только Суперадминистратор и Администратор" (тот же
  // набор ролей, что и Permission.MANAGE_SCHEDULED_JOBS на Backend,
  // который теперь требуется даже для чтения — см. api/scheduled_jobs.py).
  "/scheduled-jobs": canManageAutomation,
  "/eudb-export": canManageAutomation,
};

function isVisible(to: string, role: string | null): boolean {
  const check = RESTRICTED_PATHS[to];
  return check ? check(role) : true;
}

export function Layout() {
  const { role, logout } = useAuth();
  const location = useLocation();
  const [openCategory, setOpenCategory] = useState<string | null>(null);
  const [mobileOpen, setMobileOpen] = useState(false);

  // Раскрыть категорию, в которой лежит текущая страница, чтобы при
  // прямом переходе по ссылке (не через клик по меню) пункт был виден.
  useEffect(() => {
    const owner = MENU.find((category) => category.items.some((item) => item.to === location.pathname));
    if (owner) setOpenCategory(owner.label);
  }, [location.pathname]);

  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  const roleLabel = role ? ROLE_LABELS[role] ?? role : "";
  const initials = roleLabel.slice(0, 2).toUpperCase();

  return (
    <div className="app-shell">
      <aside className={`sidebar${mobileOpen ? " open" : ""}`}>
        <div className="brand-block">
          <div className="brand-mark">M</div>
          <div className="brand-text">
            <NavLink to="/" end className="brand">
              MMWS
            </NavLink>
            <div className="brand-sub">Protocol Gateway</div>
          </div>
        </div>
        <nav className="sidebar-nav">
          <NavLink to="/" end className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}>
            Дашборд
          </NavLink>
          {MENU.map((category) => {
            const visibleItems = category.items.filter((item) => isVisible(item.to, role));
            if (visibleItems.length === 0) return null;
            const open = openCategory === category.label;
            return (
              <div key={category.label} className={`nav-category${open ? " open" : ""}`}>
                <button
                  type="button"
                  className="nav-category-toggle"
                  onClick={() => setOpenCategory(open ? null : category.label)}
                >
                  <span>{category.label}</span>
                  <span className="caret">▾</span>
                </button>
                {open && (
                  <div className="nav-subitems">
                    {visibleItems.map((item) => (
                      <NavLink
                        key={item.label}
                        to={item.to}
                        className={({ isActive }) => `nav-subitem${isActive ? " active" : ""}`}
                      >
                        <span>{item.label}</span>
                        {!item.implemented && <span className="stub-tag">нет backend</span>}
                      </NavLink>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </nav>
        <div className="sidebar-foot">© 2026 MMWS</div>
      </aside>

      {mobileOpen && <div className="sidebar-overlay show" onClick={() => setMobileOpen(false)} />}

      <div className="main">
        <div className="topbar">
          <button className="burger" aria-label="Меню" onClick={() => setMobileOpen((v) => !v)}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
              <path d="M3 6h18M3 12h18M3 18h18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            </svg>
          </button>
          <div className="topbar-user">
            <NotificationBell />
            <div className="avatar">{initials}</div>
            <span className="role-label">{roleLabel}</span>
            <button className="secondary" onClick={logout}>
              Выйти
            </button>
          </div>
        </div>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
