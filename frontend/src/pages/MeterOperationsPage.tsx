import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { canWriteParameter, useAuth } from "../auth/AuthContext";
import { formatValue } from "../lib/format";
import type { Job, JobStatus, Meter } from "../api/types";

// 2026-09-12/13, по прямому указанию пользователя — пункт меню "Работа
// с счётчиками" (выше "Архива") разбит на 4 ОТДЕЛЬНЫХ страницы вместо
// одной общей (пользователь: "реализация нового меню неудобная"):
// "Опрос счётчика" / "Опрос профиля" (по одному) и "Опрос группы
// счётчиков" / "Опрос профиля группой" (списком). Общий код (выбор
// счётчика/группы, таблица результатов, разбор состояния реле) вынесен
// в переиспользуемые куски ниже, сами 4 страницы — тонкие обёртки в
// конце файла. Права по-прежнему — не новый механизм, привязаны к уже
// существующим ролям: страницы видит любая роль (Permission.VIEW_METERS),
// но запускать операции может только тот, кто пишет параметры
// (Permission.WRITE_PARAMETER — Инженер/Администратор/Супер-администратор).
const DEFAULT_READING_OBIS = "1.1.1.8.0.ff";
const VOLTAGE_PHASE_OBIS = ["1.1.20.7.0.ff", "1.1.34.7.0.ff", "1.1.48.7.0.ff"]; // фаза A/B/C, obis_catalog.py

// "Список профилей" (по просьбе пользователя — "пока только один, но
// будут ещё", "надо показать реальное название профиля - profile1") —
// label здесь ЛИТЕРАЛЬНОЕ техническое имя буфера на счётчике, не
// перевод/описание (то уже есть в MeterDetailPage: "Профиль нагрузки
// (Profile 1)"). Дополнить список — одна новая строка, остальной код
// (эта страница и Backend) уже параметризован по obis.
const LOAD_PROFILES: { id: string; label: string; obis: string }[] = [
  { id: "profile1", label: "Profile1", obis: "1.1.63.1.0.ff" },
];

type ReadItem = "readings" | "voltage" | "amps" | "relay";

const READ_ITEM_LABELS: Record<ReadItem, string> = {
  readings: "Показания",
  voltage: "Напряжение",
  amps: "Ампер (токовый класс)",
  relay: "Состояние реле",
};

const JOB_STATUS_LABELS: Record<JobStatus, string> = {
  queued: "в очереди",
  running: "выполняется",
  succeeded: "успешно",
  failed: "ошибка",
};

// Только эти два поля нужны таблице результатов и хранилищу в
// localStorage — не весь Meter (сериализовать его целиком в
// localStorage незачем, да и объект, восстановленный из выбора
// счётчика, не всегда под рукой при перезагрузке страницы).
type MeterRef = Pick<Meter, "id" | "serial_number">;

interface ResultRow {
  meter: MeterRef;
  label: string;
  job: Job;
}

// <input type="datetime-local"> ждёт "YYYY-MM-DDTHH:mm" в локальном
// времени пользователя, без секунд/зоны (тот же хелпер, что и на
// MeterDetailPage — период показаний там задаётся так же).
function toDatetimeLocal(date: Date): string {
  const offsetMs = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offsetMs).toISOString().slice(0, 16);
}

function startOfToday(): Date {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
}

const POLL_INTERVAL_MS = 3000;

// Полярность (0 = включено, 1 = отключено) подтверждена пользователем
// (2026-09-12) на реальном счётчике — прочитанные вживую биты (0 по
// всем трём фазам) совпали с фактическим состоянием "включено", о
// котором сообщил пользователь. Проверки на реальном ОТКЛЮЧЁННОМ
// счётчике (бит=1) пока не было — если когда-нибудь встретится
// расхождение, полярность стоит перепроверить. Алгоритм разбора — тот
// же, что и в референсе (IECMeterManage, Function.BackString2 — реверс
// БИТОВ внутри каждого байта целиком), см. DECISIONS.md ("Disconnect
// Control..."). Биты 43/44/45 = Relay status фаза A/B/C.
function decodeRelayState(hexValue: unknown): "on" | "off" | null {
  if (typeof hexValue !== "string") return null;
  const clean = hexValue.replace(/^0x/i, "").padStart(12, "0");
  if (!/^[0-9a-fA-F]{12}$/.test(clean)) return null;
  let bits = "";
  for (let i = 0; i < 12; i += 2) {
    const byteBin = parseInt(clean.slice(i, i + 2), 16).toString(2).padStart(8, "0");
    bits += byteBin.split("").reverse().join("");
  }
  const relayBits = [bits[43], bits[44], bits[45]];
  return relayBits.some((b) => b === "1") ? "off" : "on";
}

function RelayStateBadge({ state }: { state: "on" | "off" }) {
  const style =
    state === "on"
      ? { background: "#d4f7d4", color: "#0a5c0a", padding: "2px 8px", borderRadius: 4 }
      : { background: "#f7d4d4", color: "#7a0a0a", padding: "2px 8px", borderRadius: 4 };
  return <span style={style}>{state === "on" ? "Включён" : "Отключён"}</span>;
}

// --- Выбор счётчика(ов) — общий для "одного" и "группы": в режиме
// "одного" выбор новой строки ЗАМЕНЯЕТ текущую (массив длины ≤1), в
// режиме "группы" — добавляет. ---
function useMeterSelection(multi: boolean) {
  const [meters, setMeters] = useState<Meter[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());

  useEffect(() => {
    api
      .get<Meter[]>("/api/meters")
      .then(setMeters)
      .catch((err) => setLoadError(err instanceof ApiError ? err.message : "Не удалось загрузить список счётчиков"));
  }, []);

  const matches = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return [];
    return meters.filter((m) => m.serial_number.toLowerCase().includes(q) && !selectedIds.has(m.id)).slice(0, 20);
  }, [meters, search, selectedIds]);

  const selectedMeters = useMemo(() => meters.filter((m) => selectedIds.has(m.id)), [meters, selectedIds]);

  function addMeter(meter: Meter) {
    setSelectedIds(multi ? (prev) => new Set(prev).add(meter.id) : () => new Set([meter.id]));
    setSearch("");
  }

  function removeMeter(meterId: number) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      next.delete(meterId);
      return next;
    });
  }

  function addManyBySerial(serials: string[]): { addedCount: number; notFound: string[] } {
    const bySerial = new Map(meters.map((m) => [m.serial_number, m]));
    const found: Meter[] = [];
    const notFound: string[] = [];
    for (const serial of serials) {
      const meter = bySerial.get(serial);
      if (meter) found.push(meter);
      else notFound.push(serial);
    }
    setSelectedIds((prev) => {
      const next = new Set(prev);
      found.forEach((m) => next.add(m.id));
      return next;
    });
    return { addedCount: found.length, notFound };
  }

  function clearSelection() {
    setSelectedIds(new Set());
  }

  return { loadError, search, setSearch, matches, selectedMeters, addMeter, removeMeter, addManyBySerial, clearSelection };
}

function MeterPickerUI({
  multi,
  sel,
  notice,
}: {
  multi: boolean;
  sel: ReturnType<typeof useMeterSelection>;
  notice?: string | null;
}) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [fileNotice, setFileNotice] = useState<string | null>(null);

  function handleFileSelected(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result ?? "");
      const serials = text.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean);
      const { addedCount, notFound } = sel.addManyBySerial(serials);
      setFileNotice(
        `Из файла добавлено ${addedCount} счётчик(ов).` +
          (notFound.length > 0 ? ` Не найдено: ${notFound.slice(0, 20).join(", ")}${notFound.length > 20 ? "…" : ""}` : "")
      );
    };
    reader.readAsText(file);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  return (
    <section className="card">
      <div className="card-header">
        <h2>{multi ? `Счётчики (${sel.selectedMeters.length})` : "Счётчик"}</h2>
        {multi && sel.selectedMeters.length > 0 && (
          <button className="secondary" onClick={sel.clearSelection}>
            Очистить список
          </button>
        )}
      </div>
      {(notice || sel.loadError || fileNotice) && (
        <p className={sel.loadError ? "error-message" : "hint"}>{sel.loadError ?? fileNotice ?? notice}</p>
      )}
      {(multi || sel.selectedMeters.length === 0) && (
        <>
          <input
            value={sel.search}
            placeholder="Начните вводить серийный номер..."
            onChange={(e) => sel.setSearch(e.target.value)}
          />
          {multi && (
            <>
              {" "}
              <button onClick={() => fileInputRef.current?.click()}>Загрузить из файла...</button>
              <input
                ref={fileInputRef}
                type="file"
                accept=".txt,.csv"
                style={{ display: "none" }}
                onChange={handleFileSelected}
              />
              <p className="hint">Файл — список серийных номеров, по одному на строку (или через запятую).</p>
            </>
          )}
          {sel.matches.length > 0 && (
            <ul>
              {sel.matches.map((m) => (
                <li key={m.id}>
                  <a
                    href="#"
                    onClick={(e) => {
                      e.preventDefault();
                      sel.addMeter(m);
                    }}
                  >
                    {m.serial_number}
                  </a>
                </li>
              ))}
            </ul>
          )}
          {sel.search.trim() && sel.matches.length === 0 && <p className="hint">Счётчики не найдены</p>}
        </>
      )}
      {sel.selectedMeters.length > 0 && (
        <div>
          {sel.selectedMeters.map((m) => (
            <span key={m.id} className="chip">
              {m.serial_number}{" "}
              <a
                href="#"
                onClick={(e) => {
                  e.preventDefault();
                  sel.removeMeter(m.id);
                }}
              >
                ×
              </a>
            </span>
          ))}
        </div>
      )}
    </section>
  );
}

const RESULT_ROWS_STORAGE_PREFIX = "mmws:meter-ops:v1:";
const MAX_PERSISTED_ROWS = 30;

interface PersistedRow {
  meter: MeterRef;
  label: string;
  jobId: number;
}

// Персист результатов в localStorage (по просьбе пользователя,
// 2026-09-13) — сами задания выполняются на бэкенде независимо от
// открытой страницы, но список "что запускалось и с каким статусом"
// раньше жил только в памяти React-компонента и терялся при
// обновлении страницы или уходе на другую вкладку (например,
// Дашборд) и обратно. У каждой из 4 страниц — свой ключ
// (``storageKey``), чтобы результаты не путались между ними.
function useResultRows(storageKey: string) {
  const [rows, setRows] = useState<ResultRow[]>([]);
  const pollRef = useRef<number | null>(null);
  const fullStorageKey = RESULT_ROWS_STORAGE_PREFIX + storageKey;

  useEffect(() => {
    let cancelled = false;
    let raw: string | null;
    try {
      raw = localStorage.getItem(fullStorageKey);
    } catch {
      return;
    }
    if (!raw) return;
    let persisted: PersistedRow[];
    try {
      persisted = JSON.parse(raw);
      if (!Array.isArray(persisted)) return;
    } catch {
      return;
    }
    Promise.all(
      persisted.map((p) =>
        api
          .get<Job>(`/api/jobs/${p.jobId}`)
          .then((job): ResultRow => ({ meter: p.meter, label: p.label, job }))
          .catch(() => null)
      )
    ).then((restored) => {
      if (!cancelled) setRows(restored.filter((r): r is ResultRow => r !== null));
    });
    return () => {
      cancelled = true;
    };
    // Восстановление читает fullStorageKey только один раз при монтировании
    // страницы — сохранение (эффект ниже) не должно вызывать повторное чтение.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    try {
      if (rows.length === 0) {
        localStorage.removeItem(fullStorageKey);
        return;
      }
      const persisted: PersistedRow[] = rows.slice(0, MAX_PERSISTED_ROWS).map((r) => ({
        meter: r.meter,
        label: r.label,
        jobId: r.job.id,
      }));
      localStorage.setItem(fullStorageKey, JSON.stringify(persisted));
    } catch {
      // localStorage недоступен (приватный режим и т.п.) — не критично,
      // страница просто не восстановит список после перезагрузки.
    }
  }, [rows, fullStorageKey]);

  const pollRows = useCallback(() => {
    setRows((prevRows) => {
      const pending = prevRows.filter((r) => r.job.status === "queued" || r.job.status === "running");
      if (pending.length === 0) return prevRows;
      Promise.all(pending.map((r) => api.get<Job>(`/api/jobs/${r.job.id}`)))
        .then((updated) => {
          setRows((cur) =>
            cur.map((r) => {
              const idx = pending.findIndex((p) => p.job.id === r.job.id);
              return idx === -1 ? r : { ...r, job: updated[idx] };
            })
          );
        })
        .catch(() => {
          // сеть моргнула — попробуем на следующем тике
        });
      return prevRows;
    });
  }, []);

  useEffect(() => {
    pollRef.current = window.setInterval(pollRows, POLL_INTERVAL_MS);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [pollRows]);

  function addRows(newRows: ResultRow[]) {
    if (newRows.length > 0) setRows((prev) => [...newRows, ...prev]);
  }

  return { rows, addRows };
}

// Показывать конечный результат (число/значение), а не сырой JSON с
// OBIS-кодом внутри (по прямому указанию пользователя, 2026-09-13: "во
// всех аналогичных случаях кроме явного запроса ОБИС, показывай
// конечный результат") — на этих страницах пользователь нигде явно не
// запрашивает сам OBIS-код, поэтому он везде лишний технический шум.
// ``formatValue`` — тот же хелпер, что уже используется для показаний
// на MeterDetailPage, для единообразия форматирования значений.
function formatResult(r: ResultRow): React.ReactNode {
  if (r.job.status === "failed") {
    return r.job.error ? `${r.job.error.code}: ${r.job.error.message}` : "";
  }
  if (r.job.status !== "succeeded" || !r.job.result) return "";
  const result = r.job.result;
  if (r.label === READ_ITEM_LABELS.relay) {
    const state = decodeRelayState(result.value);
    return state ? <RelayStateBadge state={state} /> : formatValue(result.value);
  }
  if ("value" in result) return formatValue(result.value);
  if ("rated_current_amps" in result) return `${formatValue(result.rated_current_amps)} А`;
  if ("rows_written" in result) return `Записано записей: ${formatValue(result.rows_written)}`;
  if ("operation" in result) return result.ok ? "Выполнено" : "Ошибка";
  return formatValue(result);
}

function ResultsTable({ rows }: { rows: ResultRow[] }) {
  return (
    <section className="card">
      <h2>Результаты</h2>
      {rows.length === 0 && <p>Пока не запускались операции в этой сессии.</p>}
      {rows.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Счётчик</th>
              <th>Операция</th>
              <th>Статус</th>
              <th>Результат</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.label}-${r.job.id}`}>
                <td>{r.meter.serial_number}</td>
                <td>{r.label}</td>
                <td>{JOB_STATUS_LABELS[r.job.status] ?? r.job.status}</td>
                <td>{formatResult(r)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function runForEachMeter(
  meters: Meter[],
  label: string,
  request: (meter: Meter) => Promise<Job>
): Promise<{ newRows: ResultRow[]; failures: string[] }> {
  return Promise.allSettled(meters.map((meter) => request(meter))).then((results) => {
    const newRows: ResultRow[] = [];
    const failures: string[] = [];
    results.forEach((result, i) => {
      const meter = meters[i];
      if (result.status === "fulfilled") newRows.push({ meter, label, job: result.value });
      else failures.push(`${meter.serial_number} (${label}): ${result.reason instanceof ApiError ? result.reason.message : "ошибка запроса"}`);
    });
    return { newRows, failures };
  });
}

// --- Страница 1/3: опрос показаний/напряжения/ампер/реле + откл/вкл,
// одного счётчика или группой (multi управляет этим). ---
function MeterPollPageBase({ multi }: { multi: boolean }) {
  const { role } = useAuth();
  const canAct = canWriteParameter(role);
  const sel = useMeterSelection(multi);
  const { rows, addRows } = useResultRows(multi ? "poll-group" : "poll");
  const [error, setError] = useState<string | null>(null);
  const [readItems, setReadItems] = useState<Record<ReadItem, boolean>>({
    readings: true,
    voltage: false,
    amps: false,
    relay: false,
  });

  function handleRunReadItems() {
    if (!canAct || sel.selectedMeters.length === 0) return;
    setError(null);
    const tasks: Promise<{ newRows: ResultRow[]; failures: string[] }>[] = [];
    if (readItems.readings) {
      tasks.push(
        runForEachMeter(sel.selectedMeters, READ_ITEM_LABELS.readings, (m) =>
          api.post<Job>(`/api/meters/${m.id}/read`, { obis: DEFAULT_READING_OBIS })
        )
      );
    }
    if (readItems.voltage) {
      VOLTAGE_PHASE_OBIS.forEach((obis, i) => {
        tasks.push(
          runForEachMeter(sel.selectedMeters, `${READ_ITEM_LABELS.voltage}, фаза ${"ABC"[i]}`, (m) =>
            api.post<Job>(`/api/meters/${m.id}/read`, { obis })
          )
        );
      });
    }
    if (readItems.amps) {
      tasks.push(runForEachMeter(sel.selectedMeters, READ_ITEM_LABELS.amps, (m) => api.post<Job>(`/api/meters/${m.id}/read-rated-current`)));
    }
    if (readItems.relay) {
      tasks.push(runForEachMeter(sel.selectedMeters, READ_ITEM_LABELS.relay, (m) => api.post<Job>(`/api/meters/${m.id}/read-relay-state`)));
    }
    if (tasks.length === 0) {
      setError("Выберите хотя бы один пункт опроса");
      return;
    }
    Promise.all(tasks).then((results) => {
      addRows(results.flatMap((r) => r.newRows));
      const failures = results.flatMap((r) => r.failures);
      if (failures.length > 0) setError(`Не удалось поставить часть задач: ${failures.join("; ")}`);
    });
  }

  function handleRunOperation(operation: "disconnect" | "reconnect") {
    if (!canAct || sel.selectedMeters.length === 0) return;
    setError(null);
    const label = operation === "disconnect" ? "Отключение" : "Подключение";
    runForEachMeter(sel.selectedMeters, label, (m) => api.post<Job>(`/api/meters/${m.id}/${operation}`)).then(
      ({ newRows, failures }) => {
        addRows(newRows);
        if (failures.length > 0) setError(`Не удалось поставить часть задач: ${failures.join("; ")}`);
      }
    );
  }

  return (
    <div>
      <h1>{multi ? "Опрос группы счётчиков" : "Опрос счётчика"}</h1>
      <p className="hint">
        {multi ? "Опрос сразу нескольких счётчиков" : "Опрос одного счётчика"} — показания/напряжение/ампер/состояние
        реле, отключение/подключение.
        {!canAct && " Запуск операций доступен ролям «Инженер», «Администратор» и «Супер-администратор»."}
      </p>
      {error && <div className="error-message">{error}</div>}

      <MeterPickerUI multi={multi} sel={sel} />

      {canAct && (
        <>
          <section className="card">
            <h2>Опрос</h2>
            {(Object.keys(READ_ITEM_LABELS) as ReadItem[]).map((item) => (
              <label key={item} style={{ marginRight: 16 }}>
                <input
                  type="checkbox"
                  checked={readItems[item]}
                  onChange={(e) => setReadItems((prev) => ({ ...prev, [item]: e.target.checked }))}
                />{" "}
                {READ_ITEM_LABELS[item]}
              </label>
            ))}
            <br />
            <button disabled={sel.selectedMeters.length === 0} onClick={handleRunReadItems}>
              Опросить выбранное
            </button>
          </section>

          <section className="card">
            <h2>Отключение / подключение</h2>
            <button disabled={sel.selectedMeters.length === 0} onClick={() => handleRunOperation("disconnect")}>
              Отключить
            </button>{" "}
            <button disabled={sel.selectedMeters.length === 0} onClick={() => handleRunOperation("reconnect")}>
              Подключить
            </button>
          </section>
        </>
      )}

      <ResultsTable rows={rows} />
    </div>
  );
}

export function MeterPollPage() {
  return <MeterPollPageBase multi={false} />;
}

export function MeterPollGroupPage() {
  return <MeterPollPageBase multi />;
}

// --- Страница 2/4: запрос профиля нагрузки, одного счётчика или
// группой. ---
function ProfilePollPageBase({ multi }: { multi: boolean }) {
  const { role } = useAuth();
  const canAct = canWriteParameter(role);
  const sel = useMeterSelection(multi);
  const { rows, addRows } = useResultRows(multi ? "profile-group" : "profile");
  const [error, setError] = useState<string | null>(null);
  const [profileId, setProfileId] = useState(LOAD_PROFILES[0].id);
  const [profileFrom, setProfileFrom] = useState(() => toDatetimeLocal(startOfToday()));
  const [profileTo, setProfileTo] = useState(() => toDatetimeLocal(new Date()));

  function handleRunProfile() {
    if (!canAct || sel.selectedMeters.length === 0) return;
    const profile = LOAD_PROFILES.find((p) => p.id === profileId);
    if (!profile) return;
    setError(null);
    runForEachMeter(sel.selectedMeters, profile.label, (m) =>
      api.post<Job>(`/api/meters/${m.id}/read-load-profile`, {
        from_iso: `${profileFrom}:00`,
        to_iso: `${profileTo}:00`,
        obis: profile.obis,
      })
    ).then(({ newRows, failures }) => {
      addRows(newRows);
      if (failures.length > 0) setError(`Не удалось поставить часть задач: ${failures.join("; ")}`);
    });
  }

  return (
    <div>
      <h1>{multi ? "Опрос профиля группой" : "Опрос профиля"}</h1>
      <p className="hint">
        Запрос профиля нагрузки {multi ? "сразу у нескольких счётчиков" : "у одного счётчика"} за выбранный период.
        {!canAct && " Запуск операций доступен ролям «Инженер», «Администратор» и «Супер-администратор»."}
      </p>
      {error && <div className="error-message">{error}</div>}

      <MeterPickerUI multi={multi} sel={sel} />

      {canAct && (
        <section className="card">
          <h2>Профиль нагрузки</h2>
          <label>
            Профиль
            <br />
            <select value={profileId} onChange={(e) => setProfileId(e.target.value)}>
              {LOAD_PROFILES.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>{" "}
          <label>
            С (дата и время)
            <br />
            <input type="datetime-local" value={profileFrom} onChange={(e) => setProfileFrom(e.target.value)} />
          </label>{" "}
          <label>
            По (дата и время)
            <br />
            <input type="datetime-local" value={profileTo} onChange={(e) => setProfileTo(e.target.value)} />
          </label>{" "}
          <button disabled={sel.selectedMeters.length === 0} onClick={handleRunProfile}>
            Запросить профиль
          </button>
        </section>
      )}

      <ResultsTable rows={rows} />
    </div>
  );
}

export function ProfilePollPage() {
  return <ProfilePollPageBase multi={false} />;
}

export function ProfilePollGroupPage() {
  return <ProfilePollPageBase multi />;
}
