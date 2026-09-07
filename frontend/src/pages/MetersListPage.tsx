import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { canManageMeters, useAuth } from "../auth/AuthContext";
import { formatValue } from "../lib/format";
import type { Meter, ProtocolProfile } from "../api/types";

const PROFILE_LABELS: Record<ProtocolProfile, string> = {
  mode_c: "IEC 62056-21 mode C",
  mode_e: "IEC 62056-21 mode E",
  hdlc_dlms: "HDLC-DLMS",
};

const MS_PER_DAY = 24 * 60 * 60 * 1000;

// Свежесть показания — цвет строки в "Активные" (согласовано с
// пользователем 2026-09-07): ≤2 суток зелёным, 3-5 суток жёлтым,
// >5 суток серым, показаний не было вовсе — красным.
function readingAgeClass(lastReadAt: string | null): string {
  if (!lastReadAt) return "reading-none";
  const ageDays = (Date.now() - new Date(lastReadAt).getTime()) / MS_PER_DAY;
  if (ageDays <= 2) return "reading-fresh";
  if (ageDays <= 5) return "reading-stale";
  return "reading-old";
}

export function MetersListPage() {
  const { role } = useAuth();
  const [meters, setMeters] = useState<Meter[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [protocolFilter, setProtocolFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<string>("");

  const [activatingMeter, setActivatingMeter] = useState<Meter | null>(null);
  const [activateProfile, setActivateProfile] = useState<ProtocolProfile>("hdlc_dlms");
  const [activatePassword, setActivatePassword] = useState("");
  const [activateLocation, setActivateLocation] = useState("");

  function buildParams(): URLSearchParams {
    const params = new URLSearchParams();
    if (search) params.set("search", search);
    if (protocolFilter) params.set("protocol_profile", protocolFilter);
    if (statusFilter) params.set("is_active", statusFilter);
    return params;
  }

  function load() {
    setError(null);
    const params = buildParams();
    api
      .get<Meter[]>(`/api/meters${params.toString() ? `?${params}` : ""}`)
      .then(setMeters)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось загрузить счётчики"));
  }

  useEffect(load, [search, protocolFilter, statusFilter]);

  const rows = useMemo(() => meters ?? [], [meters]);
  // Этап 6 — обнаруженные по call-home счётчики (ещё без пароля/
  // протокола) показываются ОТДЕЛЬНОЙ группой сверху, чтобы оператор
  // сразу видел, что появилось новое оборудование, ждущее активации.
  const installedRows = useMemo(() => rows.filter((m) => m.status === "installed"), [rows]);
  const activeRows = useMemo(() => rows.filter((m) => m.status === "active"), [rows]);

  function openActivate(meter: Meter) {
    setActivatingMeter(meter);
    setActivateProfile("hdlc_dlms");
    setActivatePassword("");
    setActivateLocation("");
    setError(null);
  }

  function handleConfirmActivate() {
    if (!activatingMeter) return;
    if (activatePassword.length === 0) {
      setError("Укажите пароль доступа (LLS) счётчика");
      return;
    }
    setError(null);
    api
      .post<Meter>(`/api/meters/${activatingMeter.id}/activate`, {
        protocol_profile: activateProfile,
        password: activatePassword,
        location: activateLocation || null,
      })
      .then(() => {
        setActivatingMeter(null);
        load();
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось активировать счётчик"));
  }

  return (
    <div>
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

      {meters !== null && installedRows.length > 0 && (
        <section className="card">
          <div className="card-header">
            <h2>Установленные (ожидают активации) — {installedRows.length}</h2>
          </div>
          <p className="hint">
            Эти счётчики сами позвонили на call-home порт Gateway и были опознаны по серийному номеру, но пароль
            доступа и протокольный профиль ещё не известны — операции с ними недоступны до активации.
          </p>
          <table className="data-table">
            <thead>
              <tr>
                <th>Серийный номер</th>
                <th>Обнаружен</th>
                {canManageMeters(role) && <th>Действия</th>}
              </tr>
            </thead>
            <tbody>
              {installedRows.map((m) => (
                <tr key={m.id}>
                  <td>
                    <Link to={`/meters/${m.id}`}>{m.serial_number}</Link>
                  </td>
                  <td>{new Date(m.created_at).toLocaleString("ru-RU")}</td>
                  {canManageMeters(role) && (
                    <td>
                      <button onClick={() => openActivate(m)}>Активировать</button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {activatingMeter && (
        <section className="card">
          <div className="card-header">
            <h2>Активировать счётчик {activatingMeter.serial_number}</h2>
          </div>
          <div className="filters">
            <label>
              Протокольный профиль
              <br />
              <select value={activateProfile} onChange={(e) => setActivateProfile(e.target.value as ProtocolProfile)}>
                <option value="hdlc_dlms">HDLC-DLMS</option>
                <option value="mode_c">IEC 62056-21 mode C</option>
                <option value="mode_e">IEC 62056-21 mode E</option>
              </select>
            </label>
            <label>
              Пароль доступа (LLS)
              <br />
              <input
                type="password"
                value={activatePassword}
                onChange={(e) => setActivatePassword(e.target.value)}
              />
            </label>
            <label>
              Местоположение
              <br />
              <input value={activateLocation} onChange={(e) => setActivateLocation(e.target.value)} />
            </label>
          </div>
          <button onClick={handleConfirmActivate}>Активировать</button>{" "}
          <button className="secondary" onClick={() => setActivatingMeter(null)}>
            Отмена
          </button>
        </section>
      )}

      <section className="card">
        <div className="card-header">
          <h2>Активные — {activeRows.length}</h2>
          <div className="btn-group">
            <button onClick={load}>Обновить</button>
          </div>
        </div>
        {meters !== null && activeRows.length === 0 && <p>Активных счётчиков нет.</p>}

        {activeRows.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th>Серийный номер</th>
                <th>IP-адрес</th>
                <th>Протокол</th>
                <th>Местоположение</th>
                <th>Статус</th>
                <th>Показание</th>
                <th>Дата показания</th>
              </tr>
            </thead>
            <tbody>
              {activeRows.map((m) => (
                <tr key={m.id} className={readingAgeClass(m.last_read_at)}>
                  <td>
                    <Link to={`/meters/${m.id}`}>{m.serial_number}</Link>
                  </td>
                  <td>{m.ip_address ? `${m.ip_address}:${m.port}` : "—"}</td>
                  <td>{m.protocol_profile ? PROFILE_LABELS[m.protocol_profile] : "—"}</td>
                  <td>{m.location ?? "—"}</td>
                  <td>
                    <span className={`badge ${m.is_online ? "badge-online" : "badge-offline"}`}>
                      {m.is_online ? "online" : "offline"}
                    </span>
                    {!m.is_active && " · неактивен"}
                  </td>
                  <td>{formatValue(m.last_reading_value)}</td>
                  <td>{m.last_read_at ? new Date(m.last_read_at).toLocaleString("ru-RU") : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
