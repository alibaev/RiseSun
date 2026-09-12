import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { tokenStorage } from "../auth/tokenStorage";
import { useAuth, canTriggerRead, canWriteParameter } from "../auth/AuthContext";
import { ConfirmModal } from "../components/ConfirmModal";
import { formatValue } from "../lib/format";
import type { Job, LoadProfileRow, LogEntry, Meter, MeterReading } from "../api/types";

const DEFAULT_OBIS = "1.1.1.8.0.ff"; // активная энергия, приём, всего (ТЗ Приложение Г.3)

// Профиль нагрузки (Этап 3, ТЗ п.4.2.3) — "Profile 1"/канал 1 счётчика
// DTZY217 (см. DECISIONS.md, разбор реальных байтовых трасс 2026-09-12):
// values_json — сырой массив в ПОРЯДКЕ capture_objects счётчика: V1,V2,
// V3, I1,I2,I3, PF1,PF2,PF3, Total Active Energy (OBIS 96.80.0), Import
// Active Energy (OBIS 1.8.0, последняя) — порядок и названия 10-й/11-й
// колонок подтверждены заводским GPRS.ini (архив Ver1.zip, раздел
// [LoadProfileParameter], предоставлен пользователем 2026-09-12).
// Форма и порядок столбцов таблицы — по образцу референсной программы
// счётчика (скриншот "screen1", по просьбе пользователя 2026-09-12).
const LOAD_PROFILE_COLUMNS: { label: string; index: number }[] = [
  { label: "Активная энергия (импорт)", index: 10 },
  { label: "Активная энергия (всего)", index: 9 },
  { label: "Нагрузка, фаза A", index: 3 },
  { label: "Нагрузка, фаза B", index: 4 },
  { label: "Нагрузка, фаза C", index: 5 },
  { label: "Напряжение, фаза A", index: 0 },
  { label: "Напряжение, фаза B", index: 1 },
  { label: "Напряжение, фаза C", index: 2 },
  { label: "Коэф. мощности, фаза A", index: 6 },
  { label: "Коэф. мощности, фаза B", index: 7 },
  { label: "Коэф. мощности, фаза C", index: 8 },
];
const LOAD_PROFILE_PAGE_SIZE = 20;

function profileCellValue(row: LoadProfileRow, index: number): string {
  const value = row.values_json[index];
  if (value == null) return "—";
  return typeof value === "number" ? String(value) : String(value);
}

function toDateInput(date: Date): string {
  const offsetMs = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offsetMs).toISOString().slice(0, 10);
}

// <input type="datetime-local"> ждёт "YYYY-MM-DDTHH:mm" в локальном
// времени пользователя, без секунд/зоны — обрезаем toISOString() (UTC)
// до минут, этого достаточно для выбора периода показаний.
function toDatetimeLocal(date: Date): string {
  const offsetMs = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offsetMs).toISOString().slice(0, 16);
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
  const [showDatetimeConfirm, setShowDatetimeConfirm] = useState(false);
  const [datetimeJob, setDatetimeJob] = useState<Job | null>(null);
  const datetimeWsRef = useRef<WebSocket | null>(null);

  // Этап 5 (ТЗ п.4.2.10): удалённое отключение/подключение — вручную, по
  // одному счётчику, с обязательным подтверждением (физические
  // последствия операции — обесточивание потребителя).
  const [pendingDisconnectOp, setPendingDisconnectOp] = useState<"disconnect" | "reconnect" | null>(null);
  const [disconnectJob, setDisconnectJob] = useState<Job | null>(null);
  const disconnectWsRef = useRef<WebSocket | null>(null);

  // Вкладка "Показания" (2026-09-11, было "Текущие показания") — период
  // по умолчанию: последние сутки, правится вручную и применяется
  // кнопкой "Показать" (перезапрашивает с сервера — см. GET .../readings
  // ?from_iso=&to_iso=).
  const [readingsFrom, setReadingsFrom] = useState(() => toDatetimeLocal(new Date(Date.now() - 86400000)));
  const [readingsTo, setReadingsTo] = useState(() => toDatetimeLocal(new Date()));

  // Вкладка "Профиль нагрузки" (2026-09-12, по просьбе пользователя —
  // форма как в референсной программе счётчика): по умолчанию — сегодня
  // целиком, правится вручную и применяется кнопкой "Найти".
  const [profileFrom, setProfileFrom] = useState(() => toDateInput(new Date()));
  const [profileTo, setProfileTo] = useState(() => toDateInput(new Date()));
  const [profileRows, setProfileRows] = useState<LoadProfileRow[]>([]);
  const [profilePage, setProfilePage] = useState(1);
  const [profileError, setProfileError] = useState<string | null>(null);
  const [profileJob, setProfileJob] = useState<Job | null>(null);
  const profileWsRef = useRef<WebSocket | null>(null);

  const loadReadings = useCallback(async () => {
    if (!id) return;
    try {
      const params = new URLSearchParams({
        from_iso: `${readingsFrom}:00`,
        to_iso: `${readingsTo}:00`,
      });
      setReadings(await api.get<MeterReading[]>(`/api/meters/${id}/readings?${params}`));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не удалось загрузить показания");
    }
  }, [id, readingsFrom, readingsTo]);

  const loadProfileRows = useCallback(async () => {
    if (!id) return;
    setProfileError(null);
    try {
      const params = new URLSearchParams({
        from_iso: `${profileFrom}T00:00:00`,
        to_iso: `${profileTo}T23:59:59`,
      });
      const rows = await api.get<LoadProfileRow[]>(`/api/meters/${id}/load-profile?${params}`);
      setProfileRows(rows);
      setProfilePage(1);
    } catch (err) {
      setProfileError(err instanceof ApiError ? err.message : "Не удалось загрузить профиль нагрузки");
    }
  }, [id, profileFrom, profileTo]);

  const loadAll = useCallback(async () => {
    if (!id) return;
    setError(null);
    try {
      const [m, e, t] = await Promise.all([
        api.get<Meter>(`/api/meters/${id}`),
        api.get<LogEntry[]>(`/api/meters/${id}/event-log`),
        api.get<LogEntry[]>(`/api/meters/${id}/tamper-log`),
      ]);
      setMeter(m);
      setEventLog(e);
      setTamperLog(t);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не удалось загрузить данные счётчика");
    }
  }, [id]);

  // Загрузка при открытии страницы — только по смене id, НЕ по смене
  // periodа (иначе каждое нажатие в date-picker'е перезапрашивало бы
  // всё заново); применение нового периода — отдельно, кнопкой
  // "Показать" (см. ниже), вызывающей loadReadings() напрямую.
  useEffect(() => {
    loadAll();
    loadReadings();
    loadProfileRows();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => () => wsRef.current?.close(), []);
  useEffect(() => () => datetimeWsRef.current?.close(), []);
  useEffect(() => () => disconnectWsRef.current?.close(), []);
  useEffect(() => () => profileWsRef.current?.close(), []);

  function watchJob(job: Job, wsRef: { current: WebSocket | null }, onUpdate: (job: Job) => void) {
    const token = tokenStorage.getAccess();
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${window.location.host}/api/jobs/${job.id}/stream?token=${token}`);
    wsRef.current = ws;
    ws.onmessage = (evt) => {
      const updated: Job = JSON.parse(evt.data);
      onUpdate(updated);
      if (updated.status === "succeeded" || updated.status === "failed") {
        ws.close();
        loadAll();
      }
    };
  }

  function handleConfirmSetDatetime() {
    if (!id) return;
    setShowDatetimeConfirm(false);
    setError(null);
    api
      .post<Job>(`/api/meters/${id}/write-datetime`)
      .then((job) => {
        setDatetimeJob(job);
        watchJob(job, datetimeWsRef, setDatetimeJob);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось запустить установку времени"));
  }

  function handleConfirmDisconnectOp() {
    if (!id || !pendingDisconnectOp) return;
    const op = pendingDisconnectOp;
    setPendingDisconnectOp(null);
    setError(null);
    api
      .post<Job>(`/api/meters/${id}/${op}`)
      .then((job) => {
        setDisconnectJob(job);
        watchJob(job, disconnectWsRef, setDisconnectJob);
      })
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : `Не удалось запустить операцию "${op}"`)
      );
  }

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
            loadReadings();
          }
        };
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось запустить чтение"));
  }

  function handleTriggerProfileRead() {
    if (!id) return;
    setProfileError(null);
    api
      .post<Job>(`/api/meters/${id}/read-load-profile`, {
        from_iso: `${profileFrom}T00:00:00`,
        to_iso: `${profileTo}T23:59:59`,
      })
      .then((job) => {
        setProfileJob(job);
        watchJob(job, profileWsRef, (updated) => {
          setProfileJob(updated);
          if (updated.status === "succeeded") loadProfileRows();
        });
      })
      .catch((err) => setProfileError(err instanceof ApiError ? err.message : "Не удалось запустить чтение профиля"));
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
        <div className="card-header">
          <h2>Общая информация</h2>
          {canWriteParameter(role) && (
            <div style={{ display: "flex", gap: 8 }}>
              <button
                onClick={() => setShowDatetimeConfirm(true)}
                disabled={datetimeJob?.status === "queued" || datetimeJob?.status === "running"}
              >
                {datetimeJob?.status === "queued" || datetimeJob?.status === "running"
                  ? "Устанавливаю..."
                  : "Установить время"}
              </button>
              <button
                className="secondary"
                onClick={() => setPendingDisconnectOp("disconnect")}
                disabled={disconnectJob?.status === "queued" || disconnectJob?.status === "running"}
              >
                Отключить
              </button>
              <button
                className="secondary"
                onClick={() => setPendingDisconnectOp("reconnect")}
                disabled={disconnectJob?.status === "queued" || disconnectJob?.status === "running"}
              >
                Подключить
              </button>
            </div>
          )}
        </div>
        <dl className="info-grid">
          <dt>IP-адрес</dt>
          <dd>{meter.ip_address ? (meter.port ? `${meter.ip_address}:${meter.port}` : meter.ip_address) : "—"}</dd>
          <dt>Протокольный профиль</dt>
          <dd>{meter.protocol_profile ?? "не указан (ожидает активации)"}</dd>
          <dt>Местоположение</dt>
          <dd>{meter.location ?? "—"}</dd>
          <dt>Модель</dt>
          <dd>{meter.model ?? "—"}</dd>
          <dt>Статус</dt>
          <dd>{meter.is_active ? "активен" : "неактивен"}</dd>
          <dt>Последнее подключение</dt>
          <dd>{meter.last_seen_at ? new Date(meter.last_seen_at).toLocaleString("ru-RU") : "—"}</dd>
        </dl>
        {datetimeJob?.status === "succeeded" && <p className="hint">Дата и время счётчика обновлены.</p>}
        {datetimeJob?.status === "failed" && (
          <div className="error-message">
            Ошибка установки времени: {datetimeJob.error?.code} — {datetimeJob.error?.message}
          </div>
        )}
        {(disconnectJob?.status === "queued" || disconnectJob?.status === "running") && (
          <p className="hint">Выполняю операцию отключения/подключения...</p>
        )}
        {disconnectJob?.status === "succeeded" && (
          <p className="hint">Операция «{String(disconnectJob.result?.operation ?? "")}» выполнена.</p>
        )}
        {disconnectJob?.status === "failed" && (
          <div className="error-message">
            Ошибка отключения/подключения: {disconnectJob.error?.code} — {disconnectJob.error?.message}
          </div>
        )}
      </section>

      {showDatetimeConfirm && (
        <ConfirmModal
          title="Установить время счётчика"
          message="Счётчику будут переданы текущие дата и время сервера. Подтвердите операцию."
          confirmLabel="Установить"
          onConfirm={handleConfirmSetDatetime}
          onCancel={() => setShowDatetimeConfirm(false)}
        />
      )}

      {pendingDisconnectOp && (
        <ConfirmModal
          title={pendingDisconnectOp === "disconnect" ? "Отключить счётчик" : "Подключить счётчик"}
          message={
            pendingDisconnectOp === "disconnect"
              ? "Счётчик будет удалённо отключён от сети (обесточен потребитель). Подтвердите операцию."
              : "Счётчик будет удалённо подключён к сети. Подтвердите операцию."
          }
          confirmLabel={pendingDisconnectOp === "disconnect" ? "Отключить" : "Подключить"}
          onConfirm={handleConfirmDisconnectOp}
          onCancel={() => setPendingDisconnectOp(null)}
        />
      )}

      <section className="card">
        <div className="card-header">
          <h2>Показания</h2>
          {canTriggerRead(role) && (
            <button onClick={handleRefreshReadings} disabled={activeJob?.status === "queued" || activeJob?.status === "running"}>
              {activeJob?.status === "queued" || activeJob?.status === "running"
                ? "Читаю..."
                : "Обновить показания"}
            </button>
          )}
        </div>
        <div className="filters">
          <label>
            С
            <br />
            <input type="datetime-local" value={readingsFrom} onChange={(e) => setReadingsFrom(e.target.value)} />
          </label>
          <label>
            По
            <br />
            <input type="datetime-local" value={readingsTo} onChange={(e) => setReadingsTo(e.target.value)} />
          </label>
          <button onClick={() => loadReadings()}>Показать</button>
        </div>
        {activeJob?.status === "failed" && (
          <div className="error-message">
            Ошибка чтения: {activeJob.error?.code} — {activeJob.error?.message}
          </div>
        )}
        {error && <div className="error-message">{error}</div>}
        {readings.length === 0 ? (
          <p>Показаний за выбранный период нет.</p>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>OBIS-код</th>
                <th>Показание</th>
                <th>Единица</th>
                <th>Дата</th>
                <th>Время</th>
              </tr>
            </thead>
            <tbody>
              {readings.map((r) => {
                const readAt = new Date(r.read_at);
                return (
                  <tr key={r.id}>
                    <td>{r.obis_code}</td>
                    <td>{formatValue(r.value_json)}</td>
                    <td>{r.unit ?? "—"}</td>
                    <td>{readAt.toLocaleDateString("ru-RU")}</td>
                    <td>{readAt.toLocaleTimeString("ru-RU")}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <section className="card">
        <div className="card-header">
          <h2>Профиль нагрузки (Profile 1)</h2>
          {canTriggerRead(role) && (
            <button
              onClick={handleTriggerProfileRead}
              disabled={profileJob?.status === "queued" || profileJob?.status === "running"}
            >
              {profileJob?.status === "queued" || profileJob?.status === "running"
                ? "Читаю..."
                : "Обновить профиль"}
            </button>
          )}
        </div>
        <div className="filters">
          <label>
            Начальная дата
            <br />
            <input type="date" value={profileFrom} onChange={(e) => setProfileFrom(e.target.value)} />
          </label>
          <label>
            Конечная дата
            <br />
            <input type="date" value={profileTo} onChange={(e) => setProfileTo(e.target.value)} />
          </label>
          <button onClick={() => loadProfileRows()}>Найти</button>
        </div>
        {profileJob?.status === "failed" && (
          <div className="error-message">
            Ошибка чтения профиля: {profileJob.error?.code} — {profileJob.error?.message}
          </div>
        )}
        {profileError && <div className="error-message">{profileError}</div>}
        {profileRows.length === 0 ? (
          <p>Данных профиля нагрузки за выбранный период нет.</p>
        ) : (
          <>
            <div style={{ overflowX: "auto" }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Дата и время</th>
                    {LOAD_PROFILE_COLUMNS.map((col) => (
                      <th key={col.label}>{col.label}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {profileRows
                    .slice((profilePage - 1) * LOAD_PROFILE_PAGE_SIZE, profilePage * LOAD_PROFILE_PAGE_SIZE)
                    .map((row) => (
                      <tr key={row.id}>
                        <td>{new Date(row.timestamp).toLocaleString("ru-RU")}</td>
                        {LOAD_PROFILE_COLUMNS.map((col) => (
                          <td key={col.label}>{profileCellValue(row, col.index)}</td>
                        ))}
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
            {profileRows.length > LOAD_PROFILE_PAGE_SIZE && (
              <div className="filters" style={{ alignItems: "center" }}>
                <button
                  className="secondary"
                  onClick={() => setProfilePage((p) => Math.max(1, p - 1))}
                  disabled={profilePage <= 1}
                >
                  ‹
                </button>
                <span>
                  Стр. {profilePage} из {Math.ceil(profileRows.length / LOAD_PROFILE_PAGE_SIZE)}
                </span>
                <button
                  className="secondary"
                  onClick={() =>
                    setProfilePage((p) => Math.min(Math.ceil(profileRows.length / LOAD_PROFILE_PAGE_SIZE), p + 1))
                  }
                  disabled={profilePage >= Math.ceil(profileRows.length / LOAD_PROFILE_PAGE_SIZE)}
                >
                  ›
                </button>
              </div>
            )}
          </>
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
