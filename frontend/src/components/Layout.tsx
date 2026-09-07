import { useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { canManageGateways, canManageSystem, useAuth } from "../auth/AuthContext";
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
};

function isVisible(to: string, role: string | null): boolean {
  const check = RESTRICTED_PATHS[to];
  return check ? check(role) : true;
}

export function Layout() {
  const { role, logout } = useAuth();
  const [openCategory, setOpenCategory] = useState<string | null>(null);
  const navRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    function onClickOutside(e: MouseEvent) {
      if (navRef.current && !navRef.current.contains(e.target as Node)) {
        setOpenCategory(null);
      }
    }
    document.addEventListener("mousedown", onClickOutside);
    return () => document.removeEventListener("mousedown", onClickOutside);
  }, []);

  return (
    <div className="app-shell">
      <header className="topbar">
        <NavLink to="/" end className="brand">
          MMWS
        </NavLink>
        <nav ref={navRef}>
          <NavLink to="/" end>
            Дашборд
          </NavLink>
          {MENU.map((category) => {
            const visibleItems = category.items.filter((item) => isVisible(item.to, role));
            if (visibleItems.length === 0) return null;
            const open = openCategory === category.label;
            return (
              <div key={category.label} className={`nav-group${open ? " open" : ""}`}>
                <button type="button" onClick={() => setOpenCategory(open ? null : category.label)}>
                  {category.label} <span className="caret">▾</span>
                </button>
                {open && (
                  <div className="nav-dropdown" onClick={() => setOpenCategory(null)}>
                    {visibleItems.map((item) => (
                      <Link key={item.label} to={item.to}>
                        <span>{item.label}</span>
                        {!item.implemented && <span className="stub-tag">нет backend</span>}
                      </Link>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
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
