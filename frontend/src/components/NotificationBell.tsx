import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import { tokenStorage } from "../auth/tokenStorage";
import type { Notification, NotificationCategory } from "../api/types";

const CATEGORY_LABELS: Record<NotificationCategory, string> = {
  meter_offline: "Счётчик offline",
  tamper_event: "Вмешательство",
  scheduled_job_failed: "Ошибка расписания",
};

// ТЗ п.4.2.8 — уведомления в реальном времени по WebSocket, тот же
// принцип потоковой доставки, что и у /api/jobs/{id}/stream
// (MeterDetailPage.watchJob), но на весь сеанс пользователя, а не на
// одну конкретную задачу.
export function NotificationBell() {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [open, setOpen] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const loadUnread = useCallback(() => {
    api
      .get<Notification[]>("/api/notifications?unread_only=true&limit=100")
      .then(setNotifications)
      .catch(() => {
        /* виджет не критичен для остального интерфейса — молча игнорируем сбой загрузки */
      });
  }, []);

  useEffect(() => {
    loadUnread();

    const token = tokenStorage.getAccess();
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${window.location.host}/api/notifications/stream?token=${token}`);
    wsRef.current = ws;
    ws.onmessage = (evt) => {
      const notification: Notification = JSON.parse(evt.data);
      setNotifications((prev) => [notification, ...prev]);
    };
    return () => ws.close();
  }, [loadUnread]);

  function markRead(id: number) {
    api
      .post(`/api/notifications/${id}/read`)
      .then(() => setNotifications((prev) => prev.filter((n) => n.id !== id)))
      .catch(() => {
        /* при сбое просто оставляем уведомление непрочитанным — пользователь может повторить клик */
      });
  }

  return (
    <div className="notification-bell">
      <button className="secondary" onClick={() => setOpen((v) => !v)}>
        Уведомления{notifications.length > 0 ? ` (${notifications.length})` : ""}
      </button>
      {open && (
        <div className="notification-dropdown">
          {notifications.length === 0 && <p>Непрочитанных уведомлений нет.</p>}
          {notifications.map((n) => (
            <div key={n.id} className="notification-item">
              <div>
                <strong>{CATEGORY_LABELS[n.category]}</strong>
                <span className="notification-time">{new Date(n.created_at).toLocaleString("ru-RU")}</span>
              </div>
              <div>{n.message}</div>
              <div className="notification-actions">
                {n.meter_id !== null && <Link to={`/meters/${n.meter_id}`}>Открыть счётчик</Link>}
                <button className="secondary" onClick={() => markRead(n.id)}>
                  Прочитано
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
