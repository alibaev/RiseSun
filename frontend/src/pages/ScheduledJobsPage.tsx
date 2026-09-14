import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import { canManageAutomation, useAuth } from "../auth/AuthContext";
import { ConfirmModal } from "../components/ConfirmModal";
import type { Job, Meter, PollProfile, ScheduledJob, ScheduledJobRun, ScheduledJobType } from "../api/types";

const JOB_TYPE_LABELS: Record<ScheduledJobType, string> = {
  read_current: "Текущие показания",
  read_load_profile: "Профиль нагрузки",
};

const JOB_STATUS_LABELS: Record<string, string> = {
  queued: "в очереди",
  running: "выполняется",
  succeeded: "успешно",
  failed: "ошибка",
};

// Свернуть журнал в сводку "Все — N" вместо построчного списка, когда
// строк слишком много для чтения (2026-09-08, по просьбе пользователя
// — профиль опроса на весь парк даёт тысячи задач за один запуск).
const RUN_JOBS_LIST_THRESHOLD = 30;

const RUN_STATUS_LABELS: Record<string, string> = {
  running: "выполняется",
  succeeded: "успешно",
  partial_failure: "частичная ошибка",
  failed: "ошибка",
};

// 2026-09-12 (по просьбе пользователя — "Расписание автоопроса сделай
// попроще... только два пункта: чтение показаний с... до..., чтение
// профиля1 с... до..., и возможность добавить ещё автоопрос") — эти
// два расписания управляются НЕ через общую таблицу/форму ниже, а
// через отдельные упрощённые карточки (только окно времени, весь
// активный парк подразумевается всегда). Каждое представлено ПАРОЙ
// ScheduledJob (основное окно + добивка на остаток суток) с общим
// operation_params.managed_pair — так расписание не соревнуется за
// то же самое call-home соединение счётчика с другим типом опроса в
// одно и то же время суток (сначала показания, потом профиль, потом
// оба сразу добивают, кто не успел).
type ManagedPairKey = "readings" | "profile1";

const MANAGED_PAIR_TITLES: Record<ManagedPairKey, string> = {
  readings: "Чтение показаний",
  profile1: "Чтение профиля1",
};

interface ManagedPair {
  key: ManagedPairKey;
  primary: ScheduledJob | null;
  catchup: ScheduledJob | null;
}

function isManaged(job: ScheduledJob): boolean {
  return typeof job.operation_params.managed_pair === "string";
}

function groupManagedPairs(jobs: ScheduledJob[]): ManagedPair[] {
  const byKey = new Map<ManagedPairKey, ManagedPair>();
  for (const job of jobs) {
    const key = job.operation_params.managed_pair as ManagedPairKey | undefined;
    if (!key) continue;
    const role = job.operation_params.managed_role;
    const entry = byKey.get(key) ?? { key, primary: null, catchup: null };
    if (role === "primary") entry.primary = job;
    else if (role === "catchup") entry.catchup = job;
    byKey.set(key, entry);
  }
  return (["readings", "profile1"] as ManagedPairKey[])
    .map((key) => byKey.get(key))
    .filter((p): p is ManagedPair => p !== undefined);
}

function hourLabel(h: number): string {
  return `${String(h % 24).padStart(2, "0")}:00`;
}

function primaryCronForWindow(fromHour: number, toHour: number): string {
  const upper = Math.max(fromHour, toHour - 1);
  return `*/5 ${fromHour}-${upper} * * *`;
}

function catchupCronForWindow(toHour: number): string {
  return `*/5 ${Math.min(toHour, 23)}-23 * * *`;
}

function formatMeters(meters: Meter[], ids: number[]): string {
  if (meters.length > 0 && ids.length === meters.length) return `Все (${ids.length})`;
  const bySerial = ids.map((id) => meters.find((m) => m.id === id)?.serial_number ?? `#${id}`);
  return bySerial.join(", ");
}

function formatOperationParams(job: ScheduledJob, profiles: PollProfile[]): string {
  if (job.job_type === "read_current") {
    const profileId = job.operation_params.poll_profile_id;
    if (typeof profileId === "number") {
      return `профиль «${profiles.find((p) => p.id === profileId)?.name ?? `#${profileId}`}»`;
    }
    return `OBIS ${job.operation_params.obis ?? "—"}`;
  }
  if (job.job_type === "read_load_profile") {
    return `окно ${job.operation_params.window_hours ?? "—"} ч`;
  }
  return "—";
}

function ManagedPairCard({
  pair,
  runs,
  loadingRuns,
  onSave,
  onToggleEnabled,
  canEdit,
}: {
  pair: ManagedPair;
  runs: ScheduledJobRun[];
  loadingRuns: boolean;
  onSave: (fromHour: number, toHour: number) => void;
  onToggleEnabled: (enabled: boolean) => void;
  canEdit: boolean;
}) {
  const primary = pair.primary;
  const initialFrom = typeof primary?.operation_params.window_from_hour === "number" ? primary.operation_params.window_from_hour : 0;
  const initialTo = typeof primary?.operation_params.window_to_hour === "number" ? primary.operation_params.window_to_hour : 3;
  const [fromHour, setFromHour] = useState(initialFrom);
  const [toHour, setToHour] = useState(initialTo);
  const dirty = fromHour !== initialFrom || toHour !== initialTo;
  const enabled = primary?.is_enabled ?? false;

  const runningRun = runs.find((r) => r.status === "running");
  const lastRun = runs[0] ?? null;

  return (
    <section className="card">
      <div className="card-header">
        <h2>{MANAGED_PAIR_TITLES[pair.key]} (весь активный парк)</h2>
        {canEdit && (
          <label>
            <input type="checkbox" checked={enabled} onChange={(e) => onToggleEnabled(e.target.checked)} /> Включено
          </label>
        )}
      </div>
      <div className="filters">
        <label>
          С
          <br />
          <select value={fromHour} onChange={(e) => setFromHour(Number(e.target.value))} disabled={!canEdit}>
            {Array.from({ length: 24 }, (_, h) => (
              <option key={h} value={h}>
                {hourLabel(h)}
              </option>
            ))}
          </select>
        </label>
        <label>
          До
          <br />
          <select value={toHour} onChange={(e) => setToHour(Number(e.target.value))} disabled={!canEdit}>
            {Array.from({ length: 24 }, (_, h) => (
              <option key={h + 1} value={h + 1}>
                {h + 1 === 24 ? "24:00" : hourLabel(h + 1)}
              </option>
            ))}
          </select>
        </label>
        {canEdit && dirty && (
          <button onClick={() => onSave(fromHour, toHour)} disabled={toHour <= fromHour}>
            Сохранить окно
          </button>
        )}
      </div>
      <p className="hint">
        Основное окно — с {hourLabel(fromHour)} до {toHour === 24 ? "24:00" : hourLabel(toHour)}, раз в 5 минут, весь
        активный парк. С {toHour === 24 ? "24:00" : hourLabel(toHour)} до 00:00 недочитанные счётчики
        {toHour === 24 ? " не добиваются (окно занимает все сутки)." : " добиваются параллельно (та же частота)."}
      </p>

      <div>
        <strong>Активное задание: </strong>
        {loadingRuns ? (
          "загрузка..."
        ) : runningRun ? (
          <span>
            выполняется — {runningRun.meters_succeeded} успешно / {runningRun.meters_failed} ошибок из{" "}
            {runningRun.meters_total} (начато {new Date(runningRun.started_at).toLocaleTimeString("ru-RU")})
          </span>
        ) : lastRun ? (
          <span>
            нет — последний запуск {new Date(lastRun.started_at).toLocaleString("ru-RU")}:{" "}
            {RUN_STATUS_LABELS[lastRun.status] ?? lastRun.status} ({lastRun.meters_succeeded}/{lastRun.meters_total})
          </span>
        ) : (
          "запусков ещё не было"
        )}
      </div>
    </section>
  );
}

export function ScheduledJobsPage() {
  const { role } = useAuth();
  const canEdit = canManageAutomation(role);
  const [jobs, setJobs] = useState<ScheduledJob[] | null>(null);
  const [meters, setMeters] = useState<Meter[]>([]);
  const [pollProfiles, setPollProfiles] = useState<PollProfile[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [managedRuns, setManagedRuns] = useState<Record<number, ScheduledJobRun[]>>({});
  const [managedRunsLoading, setManagedRunsLoading] = useState(false);

  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [cron, setCron] = useState("0 * * * *");
  const [jobType, setJobType] = useState<ScheduledJobType>("read_current");
  const [obisMode, setObisMode] = useState<"manual" | "profile">("manual");
  const [obis, setObis] = useState("1.1.1.8.0.ff");
  const [selectedProfileId, setSelectedProfileId] = useState<number | null>(null);
  const [windowHours, setWindowHours] = useState("24");

  // Выбор счётчиков — не полный чекбокс-список (2026-09-08, по просьбе
  // пользователя: список из ~150 счётчиков неудобен), а режим "все" /
  // "один по серийному номеру" (поиск клиентской фильтрацией по уже
  // загруженному списку meters).
  const [meterMode, setMeterMode] = useState<"all" | "single">("all");
  const [singleMeterSearch, setSingleMeterSearch] = useState("");
  const [selectedSingleMeter, setSelectedSingleMeter] = useState<Meter | null>(null);

  const selectedMeterIds = useMemo(() => {
    if (meterMode === "all") return new Set(meters.map((m) => m.id));
    return selectedSingleMeter ? new Set([selectedSingleMeter.id]) : new Set<number>();
  }, [meterMode, meters, selectedSingleMeter]);

  const singleMeterMatches = useMemo(() => {
    const q = singleMeterSearch.trim().toLowerCase();
    if (!q) return [];
    return meters.filter((m) => m.serial_number.toLowerCase().includes(q)).slice(0, 20);
  }, [meters, singleMeterSearch]);

  const [pendingDelete, setPendingDelete] = useState<ScheduledJob | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [runs, setRuns] = useState<ScheduledJobRun[]>([]);
  const [expandedRunId, setExpandedRunId] = useState<number | null>(null);
  const [runJobs, setRunJobs] = useState<Job[]>([]);
  const [showAdvanced, setShowAdvanced] = useState(false);

  const loadAll = useCallback(async () => {
    setError(null);
    try {
      const [j, m, p] = await Promise.all([
        api.get<ScheduledJob[]>("/api/scheduled-jobs"),
        api.get<Meter[]>("/api/meters"),
        api.get<PollProfile[]>("/api/poll-profiles"),
      ]);
      setJobs(j);
      setMeters(m);
      setPollProfiles(p);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не удалось загрузить расписания");
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const managedPairs = useMemo(() => groupManagedPairs(jobs ?? []), [jobs]);
  const genericJobs = useMemo(() => (jobs ?? []).filter((j) => !isManaged(j)), [jobs]);

  const loadManagedRuns = useCallback(async (pairs: ManagedPair[]) => {
    setManagedRunsLoading(true);
    try {
      const ids = pairs.flatMap((p) => [p.primary?.id, p.catchup?.id]).filter((id): id is number => id != null);
      const results = await Promise.all(ids.map((id) => api.get<ScheduledJobRun[]>(`/api/scheduled-jobs/${id}/runs`)));
      const byId: Record<number, ScheduledJobRun[]> = {};
      ids.forEach((id, i) => {
        byId[id] = results[i];
      });
      setManagedRuns(byId);
    } catch {
      // тихо — карточка просто покажет "загрузка..." дольше обычного, не критично
    } finally {
      setManagedRunsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (managedPairs.length > 0) loadManagedRuns(managedPairs);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobs]);

  // Автообновление "активного задания" каждые 15с — то же поведение,
  // что и у дашборда, чтобы не приходилось обновлять страницу вручную,
  // пока идёт опрос всего парка.
  useEffect(() => {
    if (managedPairs.length === 0) return;
    const interval = setInterval(() => loadManagedRuns(managedPairs), 15000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [managedPairs.length]);

  function runsForPair(pair: ManagedPair): ScheduledJobRun[] {
    const primaryRuns = pair.primary ? managedRuns[pair.primary.id] ?? [] : [];
    const catchupRuns = pair.catchup ? managedRuns[pair.catchup.id] ?? [] : [];
    return [...primaryRuns, ...catchupRuns].sort(
      (a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime()
    );
  }

  function handleSaveManagedWindow(pair: ManagedPair, fromHour: number, toHour: number) {
    if (!pair.primary) return;
    setError(null);
    const updates: Promise<ScheduledJob>[] = [
      api.put<ScheduledJob>(`/api/scheduled-jobs/${pair.primary.id}`, {
        cron_expression: primaryCronForWindow(fromHour, toHour),
        operation_params: { ...pair.primary.operation_params, window_from_hour: fromHour, window_to_hour: toHour },
      }),
    ];
    if (pair.catchup) {
      updates.push(
        api.put<ScheduledJob>(`/api/scheduled-jobs/${pair.catchup.id}`, {
          cron_expression: catchupCronForWindow(toHour),
          is_enabled: toHour < 24,
          operation_params: { ...pair.catchup.operation_params, window_from_hour: fromHour, window_to_hour: toHour },
        })
      );
    }
    Promise.all(updates)
      .then(() => loadAll())
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось сохранить окно расписания"));
  }

  function handleToggleManagedEnabled(pair: ManagedPair, enabled: boolean) {
    setError(null);
    const updates: Promise<ScheduledJob>[] = [];
    if (pair.primary) updates.push(api.put<ScheduledJob>(`/api/scheduled-jobs/${pair.primary.id}`, { is_enabled: enabled }));
    if (pair.catchup) updates.push(api.put<ScheduledJob>(`/api/scheduled-jobs/${pair.catchup.id}`, { is_enabled: enabled }));
    Promise.all(updates)
      .then(() => loadAll())
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось изменить расписание"));
  }

  function handleCreate() {
    if (!name.trim() || selectedMeterIds.size === 0) {
      setError("Укажите имя расписания и хотя бы один счётчик (режим «один по номеру» — счётчик не выбран)");
      return;
    }
    if (jobType === "read_current" && obisMode === "profile" && selectedProfileId === null) {
      setError("Выберите профиль опроса");
      return;
    }
    const operation_params =
      jobType === "read_current"
        ? obisMode === "profile"
          ? { poll_profile_id: selectedProfileId }
          : { obis }
        : { window_hours: Number(windowHours) || 24 };

    setError(null);
    api
      .post<ScheduledJob>("/api/scheduled-jobs", {
        name,
        cron_expression: cron,
        job_type: jobType,
        operation_params,
        meter_ids: [...selectedMeterIds],
      })
      .then(() => {
        setShowCreate(false);
        setName("");
        setMeterMode("all");
        setSelectedSingleMeter(null);
        setSingleMeterSearch("");
        return loadAll();
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось создать расписание"));
  }

  function handleToggleEnabled(job: ScheduledJob) {
    api
      .put<ScheduledJob>(`/api/scheduled-jobs/${job.id}`, { is_enabled: !job.is_enabled })
      .then(() => loadAll())
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось изменить расписание"));
  }

  function handleConfirmDelete() {
    if (!pendingDelete) return;
    const job = pendingDelete;
    setPendingDelete(null);
    api
      .del(`/api/scheduled-jobs/${job.id}`)
      .then(() => loadAll())
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось удалить расписание — возможно, есть история запусков"));
  }

  function toggleExpand(job: ScheduledJob) {
    if (expandedId === job.id) {
      setExpandedId(null);
      setRuns([]);
      setExpandedRunId(null);
      return;
    }
    setExpandedId(job.id);
    setExpandedRunId(null);
    setRunJobs([]);
    api
      .get<ScheduledJobRun[]>(`/api/scheduled-jobs/${job.id}/runs`)
      .then(setRuns)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось загрузить историю запусков"));
  }

  function toggleRunExpand(scheduledJobId: number, run: ScheduledJobRun) {
    if (expandedRunId === run.id) {
      setExpandedRunId(null);
      setRunJobs([]);
      return;
    }
    setExpandedRunId(run.id);
    api
      .get<Job[]>(`/api/scheduled-jobs/${scheduledJobId}/runs/${run.id}/jobs`)
      .then(setRunJobs)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось загрузить задачи запуска"));
  }

  return (
    <div>
      <h1>Расписание автоопроса</h1>

      {error && <div className="error-message">{error}</div>}

      {jobs === null && <p>Загрузка...</p>}

      {jobs !== null &&
        managedPairs.map((pair) => (
          <ManagedPairCard
            key={pair.key}
            pair={pair}
            runs={runsForPair(pair)}
            loadingRuns={managedRunsLoading}
            canEdit={canEdit}
            onSave={(fromHour, toHour) => handleSaveManagedWindow(pair, fromHour, toHour)}
            onToggleEnabled={(enabled) => handleToggleManagedEnabled(pair, enabled)}
          />
        ))}

      <section className="card">
        <div className="card-header">
          <h2>Дополнительный автоопрос</h2>
          <button onClick={() => setShowAdvanced((v) => !v)}>{showAdvanced ? "Скрыть" : "Показать"}</button>
        </div>
        {showAdvanced && (
          <>
            {canEdit && (
              <div className="card-header">
                <span />
                <button onClick={() => setShowCreate((v) => !v)}>{showCreate ? "Отмена" : "Новое расписание"}</button>
              </div>
            )}
            {showCreate && canEdit && (
              <div className="filters">
                <label>
                  Имя
                  <br />
                  <input value={name} onChange={(e) => setName(e.target.value)} />
                </label>
                <label>
                  Cron-выражение
                  <br />
                  <input value={cron} onChange={(e) => setCron(e.target.value)} placeholder="0 * * * *" />
                </label>
                <label>
                  Тип операции
                  <br />
                  <select value={jobType} onChange={(e) => setJobType(e.target.value as ScheduledJobType)}>
                    <option value="read_current">Текущие показания</option>
                    <option value="read_load_profile">Профиль нагрузки</option>
                  </select>
                </label>
                {jobType === "read_current" ? (
                  <div>
                    <label>
                      <input type="radio" checked={obisMode === "manual"} onChange={() => setObisMode("manual")} /> Один
                      OBIS-код вручную
                    </label>{" "}
                    <label>
                      <input type="radio" checked={obisMode === "profile"} onChange={() => setObisMode("profile")} />{" "}
                      Профиль опроса (несколько OBIS)
                    </label>
                    <br />
                    {obisMode === "manual" ? (
                      <input value={obis} onChange={(e) => setObis(e.target.value)} />
                    ) : (
                      <select
                        value={selectedProfileId ?? ""}
                        onChange={(e) => setSelectedProfileId(e.target.value ? Number(e.target.value) : null)}
                      >
                        <option value="">— выберите профиль —</option>
                        {pollProfiles.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name} ({p.items.filter((i) => i.enabled).length} OBIS)
                          </option>
                        ))}
                      </select>
                    )}
                    {obisMode === "profile" && pollProfiles.length === 0 && (
                      <p className="hint">Профилей опроса пока нет — создайте их на странице «Профили опроса».</p>
                    )}
                  </div>
                ) : (
                  <label>
                    Окно, часов (от текущего момента при каждом запуске)
                    <br />
                    <input
                      type="number"
                      min={1}
                      value={windowHours}
                      onChange={(e) => setWindowHours(e.target.value)}
                      style={{ width: 100 }}
                    />
                  </label>
                )}
                <div>
                  Счётчики:
                  <br />
                  <label>
                    <input type="radio" checked={meterMode === "all"} onChange={() => setMeterMode("all")} /> Все (
                    {meters.length})
                  </label>{" "}
                  <label>
                    <input type="radio" checked={meterMode === "single"} onChange={() => setMeterMode("single")} /> Один по
                    номеру
                  </label>
                  {meterMode === "single" && (
                    <div>
                      <input
                        value={selectedSingleMeter ? selectedSingleMeter.serial_number : singleMeterSearch}
                        placeholder="Начните вводить серийный номер..."
                        onChange={(e) => {
                          setSelectedSingleMeter(null);
                          setSingleMeterSearch(e.target.value);
                        }}
                      />
                      {!selectedSingleMeter && singleMeterMatches.length > 0 && (
                        <ul>
                          {singleMeterMatches.map((m) => (
                            <li key={m.id}>
                              <a
                                href="#"
                                onClick={(e) => {
                                  e.preventDefault();
                                  setSelectedSingleMeter(m);
                                  setSingleMeterSearch("");
                                }}
                              >
                                {m.serial_number}
                              </a>
                            </li>
                          ))}
                        </ul>
                      )}
                      {!selectedSingleMeter && singleMeterSearch.trim() && singleMeterMatches.length === 0 && (
                        <p className="hint">Счётчики не найдены</p>
                      )}
                    </div>
                  )}
                </div>
                <button onClick={handleCreate}>Сохранить расписание</button>
              </div>
            )}

            {genericJobs.length === 0 && <p>Дополнительных расписаний пока нет.</p>}

            {genericJobs.length > 0 && (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Имя</th>
                    <th>Cron</th>
                    <th>Тип</th>
                    <th>Параметры</th>
                    <th>Счётчики</th>
                    <th>Статус</th>
                    <th>Последний запуск</th>
                    <th>Следующий запуск</th>
                    <th>Действия</th>
                  </tr>
                </thead>
                <tbody>
                  {genericJobs.map((j) => (
                    <Fragment key={j.id}>
                      <tr>
                        <td>
                          <a
                            href="#"
                            onClick={(e) => {
                              e.preventDefault();
                              toggleExpand(j);
                            }}
                          >
                            {j.name}
                          </a>
                        </td>
                        <td>{j.cron_expression}</td>
                        <td>{JOB_TYPE_LABELS[j.job_type]}</td>
                        <td>{formatOperationParams(j, pollProfiles)}</td>
                        <td>{formatMeters(meters, j.meter_ids)}</td>
                        <td>{j.is_enabled ? "включено" : "выключено"}</td>
                        <td>{j.last_run_at ? new Date(j.last_run_at).toLocaleString("ru-RU") : "—"}</td>
                        <td>{j.next_run_at ? new Date(j.next_run_at).toLocaleString("ru-RU") : "—"}</td>
                        <td>
                          {canEdit && (
                            <>
                              <button onClick={() => handleToggleEnabled(j)}>
                                {j.is_enabled ? "Выключить" : "Включить"}
                              </button>{" "}
                              <button className="secondary" onClick={() => setPendingDelete(j)}>
                                Удалить
                              </button>
                            </>
                          )}
                        </td>
                      </tr>
                      {expandedId === j.id && (
                        <tr>
                          <td colSpan={9}>
                            <strong>Журнал запусков</strong>
                            {runs.length === 0 && <p>Запусков ещё не было.</p>}
                            {runs.length > 0 && (
                              <table className="data-table">
                                <thead>
                                  <tr>
                                    <th>Начало</th>
                                    <th>Окончание</th>
                                    <th>Статус</th>
                                    <th>Успешно / Ошибка / Всего</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {runs.map((r) => (
                                    <Fragment key={r.id}>
                                      <tr>
                                        <td>{new Date(r.started_at).toLocaleString("ru-RU")}</td>
                                        <td>{r.finished_at ? new Date(r.finished_at).toLocaleString("ru-RU") : "—"}</td>
                                        <td>{RUN_STATUS_LABELS[r.status] ?? r.status}</td>
                                        <td>
                                          <a
                                            href="#"
                                            onClick={(e) => {
                                              e.preventDefault();
                                              toggleRunExpand(j.id, r);
                                            }}
                                          >
                                            {r.meters_succeeded} / {r.meters_failed} / {r.meters_total}
                                          </a>
                                        </td>
                                      </tr>
                                      {expandedRunId === r.id && (
                                        <tr>
                                          <td colSpan={4}>
                                            {runJobs.length > RUN_JOBS_LIST_THRESHOLD ? (
                                              <div>
                                                {Object.entries(
                                                  runJobs.reduce<Record<string, number>>((acc, rj) => {
                                                    acc[rj.status] = (acc[rj.status] ?? 0) + 1;
                                                    return acc;
                                                  }, {})
                                                ).map(([status, count]) => (
                                                  <div key={status}>
                                                    {JOB_STATUS_LABELS[status] ?? status} — Все ({count})
                                                  </div>
                                                ))}
                                              </div>
                                            ) : (
                                              runJobs.map((rj) => (
                                                <div key={rj.id}>
                                                  {meters.find((m) => m.id === rj.meter_id)?.serial_number ?? rj.meter_id}:{" "}
                                                  {rj.status}
                                                  {rj.error && ` — ${rj.error.code}: ${rj.error.message}`}
                                                </div>
                                              ))
                                            )}
                                          </td>
                                        </tr>
                                      )}
                                    </Fragment>
                                  ))}
                                </tbody>
                              </table>
                            )}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </section>

      {pendingDelete && (
        <ConfirmModal
          title="Удалить расписание"
          message={`Расписание «${pendingDelete.name}» будет удалено без возможности восстановления. Подтвердите операцию.`}
          confirmLabel="Удалить"
          onConfirm={handleConfirmDelete}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </div>
  );
}
