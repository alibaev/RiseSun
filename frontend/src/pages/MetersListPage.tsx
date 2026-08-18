import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api/client";
import type { Meter, ProtocolProfile } from "../api/types";

const PROFILE_LABELS: Record<ProtocolProfile, string> = {
  mode_c: "IEC 62056-21 mode C",
  mode_e: "IEC 62056-21 mode E",
  hdlc_dlms: "HDLC-DLMS",
};

export function MetersListPage() {
  const [meters, setMeters] = useState<Meter[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [protocolFilter, setProtocolFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    setError(null);
    const params = new URLSearchParams();
    if (search) params.set("search", search);
    if (protocolFilter) params.set("protocol_profile", protocolFilter);
    if (statusFilter) params.set("is_active", statusFilter);

    api
      .get<Meter[]>(`/api/meters${params.toString() ? `?${params}` : ""}`)
      .then((data) => {
        if (!cancelled) setMeters(data);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Не удалось загрузить счётчики");
      });
    return () => {
      cancelled = true;
    };
  }, [search, protocolFilter, statusFilter]);

  const rows = useMemo(() => meters ?? [], [meters]);

  return (
    <div>
      <h1>Справочник счётчиков</h1>

      <div className="filters">
        <input
          placeholder="Поиск по серийному номеру / IP"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select value={protocolFilter} onChange={(e) => setProtocolFilter(e.target.value)}>
          <option value="">Все протоколы</option>
          <option value="mode_c">IEC 62056-21 mode C</option>
          <option value="mode_e">IEC 62056-21 mode E</option>
          <option value="hdlc_dlms">HDLC-DLMS</option>
        </select>
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">Любой статус</option>
          <option value="true">Активен</option>
          <option value="false">Неактивен</option>
        </select>
      </div>

      {error && <div className="error-message">{error}</div>}

      {meters === null && !error && <p>Загрузка...</p>}

      {meters !== null && rows.length === 0 && <p>Счётчики не найдены.</p>}

      {rows.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Серийный номер</th>
              <th>IP-адрес</th>
              <th>Протокол</th>
              <th>Местоположение</th>
              <th>Статус</th>
              <th>Последнее чтение</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => (
              <tr key={m.id}>
                <td>
                  <Link to={`/meters/${m.id}`}>{m.serial_number}</Link>
                </td>
                <td>
                  {m.ip_address}:{m.port}
                </td>
                <td>{PROFILE_LABELS[m.protocol_profile]}</td>
                <td>{m.location ?? "—"}</td>
                <td>
                  <span className={`status-dot ${m.is_online ? "online" : "offline"}`} />
                  {m.is_online ? "online" : "offline"}
                  {!m.is_active && " (неактивен)"}
                </td>
                <td>{m.last_read_at ? new Date(m.last_read_at).toLocaleString("ru-RU") : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
