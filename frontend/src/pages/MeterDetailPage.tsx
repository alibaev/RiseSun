import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { tokenStorage } from "../auth/tokenStorage";
import { useAuth, canTriggerRead } from "../auth/AuthContext";
import type { Job, LogEntry, Meter, MeterReading } from "../api/types";

const DEFAULT_OBIS = "1.1.1.8.0.ff"; // активная энергия, приём, всего (ТЗ Приложение Г.3)

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (Array.isArray(value)) return value.join(" / ");
  return String(value);
}

export function MeterDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { role } = useAuth();
  const [meter, setMeter] = useState<Meter | null>(null);
  const [readings, setReadings] = useState<MeterReading[]>([]);
  const [eventLog, setEventLog] = useState<LogEntry[]>([]);
  const [tamperLog, setTamperLog] = useState<LogEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [activeJob, setActiveJob] = useState<Job | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const loadAll = useCallback(async () => {
    if (!id) return;
    setError(null);
    try {
      const [m, r, e, t] = await Promise.all([
        api.get<Meter>(`/api/meters/${id}`),
        api.get<MeterReading[]>(`/api/meters/${id}/readings`),
        api.get<LogEntry[]>(`/api/meters/${id}/event-log`),
        api.get<LogEntry[]>(`/api/meters/${id}/tamper-log`),
      ]);
      setMeter(m);
      setReadings(r);
      setEventLog(e);
      setTamperLog(t);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не удалось загрузить данные счётчика");
    }
  }, [id]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  useEffect(() => () => wsRef.current?.close(), []);

  function handleRefreshReadings() {
    if (!id) return;
    setError(null);
    api
      .post<Job>(`/api/meters/${id}/read`, { obis: DEFAULT_OBIS })
      .then((job) => {
        setActiveJob(job);
        const token = tokenStorage.getAccess();
        const proto = window.location.protocol === "https:" ? "wss" : "ws";
        const ws = new WebSocket(`${proto}://${window.location.host}/api/jobs/${job.id}/stream?token=${token}`);
        wsRef.current = ws;
        ws.onmessage = (evt) => {
          const updated: Job = JSON.parse(evt.data);
          setActiveJob(updated);
          if (updated.status === "succeeded" || updated.status === "failed") {
            ws.close();
            loadAll();
          }
        };
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось запустить чтение"));
  }

  if (error && !meter) {
    return (
      <div>
        <Link to="/meters">← К списку счётчиков</Link>
        <div className="error-message">{error}</div>
      </div>
    );
  }

  if (!meter) return <p>Загрузка...</p>;

  return (
    <div>
      <Link to="/meters">← К списку счётчиков</Link>
      <h1>
        Счётчик {meter.serial_number}
        <span className={`status-dot ${meter.is_online ? "online" : "offline"}`} />
      </h1>

      <section className="card">
        <h2>Общая информация</h2>
        <dl className="info-grid">
          <dt>IP-адрес</dt>
          <dd>
            {meter.ip_address}:{meter.port}
          </dd>
          <dt>Протокольный профиль</dt>
          <dd>{meter.protocol_profile}</dd>
          <dt>Местоположение</dt>
          <dd>{meter.location ?? "—"}</dd>
          <dt>Модель</dt>
          <dd>{meter.model ?? "—"}</dd>
          <dt>Статус</dt>
          <dd>{meter.is_active ? "активен" : "неактивен"}</dd>
          <dt>Последнее подключение</dt>
          <dd>{meter.last_seen_at ? new Date(meter.last_seen_at).toLocaleString("ru-RU") : "—"}</dd>
        </dl>
      </section>

      <section className="card">
        <div className="card-header">
          <h2>Текущие показания</h2>
          {canTriggerRead(role) && (
            <button onClick={handleRefreshReadings} disabled={activeJob?.status === "queued" || activeJob?.status === "running"}>
              {activeJob?.status === "queued" || activeJob?.status === "running"
                ? "Читаю..."
                : "Обновить показания"}
            </button>
          )}
        </div>
        {activeJob?.status === "failed" && (
          <div className="error-message">
            Ошибка чтения: {activeJob.error?.code} — {activeJob.error?.message}
          </div>
        )}
        {error && <div className="error-message">{error}</div>}
        {readings.length === 0 ? (
          <p>Показаний пока нет.</p>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>OBIS-код</th>
                <th>Значение</th>
                <th>Единица</th>
                <th>Время чтения</th>
              </tr>
            </thead>
            <tbody>
              {readings.map((r) => (
                <tr key={r.id}>
                  <td>{r.obis_code}</td>
                  <td>{formatValue(r.value_json)}</td>
                  <td>{r.unit ?? "—"}</td>
                  <td>{new Date(r.read_at).toLocaleString("ru-RU")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <h2>Журнал событий</h2>
        {eventLog.length === 0 ? (
          <p>Событий не зафиксировано.</p>
        ) : (
          <ul className="log-list">
            {eventLog.map((e) => (
              <li key={e.id}>
                {new Date(e.occurred_at).toLocaleString("ru-RU")} — {e.category}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="card">
        <h2>Журнал вмешательств</h2>
        {tamperLog.length === 0 ? (
          <p>Вмешательств не зафиксировано.</p>
        ) : (
          <ul className="log-list">
            {tamperLog.map((e) => (
              <li key={e.id}>
                {new Date(e.occurred_at).toLocaleString("ru-RU")} — {e.category}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
