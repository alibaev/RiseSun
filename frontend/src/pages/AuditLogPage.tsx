import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";

interface AuditRow {
  id: number;
  user_id: number | null;
  source: string;
  action: string;
  object_type: string | null;
  object_id: string | null;
  result: string;
  ip_address: string | null;
  details: Record<string, unknown> | null;
  created_at: string;
}

interface UserOut {
  id: number;
  username: string;
}

function toCsvValue(value: unknown): string {
  const s = value === null || value === undefined ? "" : String(value);
  return `"${s.replace(/"/g, '""')}"`;
}

// ТЗ п.4.2.11 — журнал аудита: фильтрация + экспорт отфильтрованного в CSV.
export function AuditLogPage() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [users, setUsers] = useState<UserOut[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [userId, setUserId] = useState("");
  const [action, setAction] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  const load = useCallback(() => {
    setError(null);
    const params = new URLSearchParams();
    if (userId) params.set("user_id", userId);
    if (action) params.set("action", action);
    if (dateFrom) params.set("date_from", `${dateFrom}:00`);
    if (dateTo) params.set("date_to", `${dateTo}:00`);
    params.set("limit", "500");

    api
      .get<AuditRow[]>(`/api/audit-log?${params}`)
      .then(setRows)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось загрузить журнал аудита"));
  }, [userId, action, dateFrom, dateTo]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    api
      .get<UserOut[]>("/api/users")
      .then(setUsers)
      .catch(() => {
        /* список пользователей нужен только для подписи в фильтре — сбой не критичен */
      });
  }, []);

  const userLabel = (id: number | null) => (id === null ? "—" : users.find((u) => u.id === id)?.username ?? `#${id}`);

  function handleExportCsv() {
    const header = ["id", "время", "пользователь", "источник", "действие", "объект", "id объекта", "результат", "IP"];
    const lines = [header.map(toCsvValue).join(",")];
    for (const r of rows) {
      lines.push(
        [
          r.id,
          r.created_at,
          userLabel(r.user_id),
          r.source,
          r.action,
          r.object_type ?? "",
          r.object_id ?? "",
          r.result,
          r.ip_address ?? "",
        ]
          .map(toCsvValue)
          .join(",")
      );
    }
    const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `audit-log-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div>
      <h1>Журнал аудита</h1>

      <div className="filters">
        <label>
          Пользователь
          <br />
          <select value={userId} onChange={(e) => setUserId(e.target.value)}>
            <option value="">Все</option>
            {users.map((u) => (
              <option key={u.id} value={u.id}>
                {u.username}
              </option>
            ))}
          </select>
        </label>
        <label>
          Действие
          <br />
          <input value={action} onChange={(e) => setAction(e.target.value)} placeholder="напр. meter.disconnect" />
        </label>
        <label>
          С
          <br />
          <input type="datetime-local" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} />
        </label>
        <label>
          По
          <br />
          <input type="datetime-local" value={dateTo} onChange={(e) => setDateTo(e.target.value)} />
        </label>
        <button onClick={handleExportCsv} disabled={rows.length === 0}>
          Экспорт в CSV
        </button>
      </div>

      {error && <div className="error-message">{error}</div>}

      {rows.length === 0 ? (
        <p>Записей не найдено.</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Время</th>
              <th>Пользователь</th>
              <th>Источник</th>
              <th>Действие</th>
              <th>Объект</th>
              <th>Результат</th>
              <th>IP</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{new Date(r.created_at).toLocaleString("ru-RU")}</td>
                <td>{userLabel(r.user_id)}</td>
                <td>{r.source}</td>
                <td>{r.action}</td>
                <td>
                  {r.object_type ?? "—"} {r.object_id ?? ""}
                </td>
                <td>{r.result}</td>
                <td>{r.ip_address ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
