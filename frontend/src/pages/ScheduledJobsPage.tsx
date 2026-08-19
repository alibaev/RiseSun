import { Fragment, useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { canManageAutomation, useAuth } from "../auth/AuthContext";
import { ConfirmModal } from "../components/ConfirmModal";
import type { Job, Meter, ScheduledJob, ScheduledJobRun, ScheduledJobType } from "../api/types";

const JOB_TYPE_LABELS: Record<ScheduledJobType, string> = {
  read_current: "Текущие показания",
  read_load_profile: "Профиль нагрузки",
};

const RUN_STATUS_LABELS: Record<string, string> = {
  running: "выполняется",
  succeeded: "успешно",
  partial_failure: "частичная ошибка",
  failed: "ошибка",
};

function formatMeters(meters: Meter[], ids: number[]): string {
  const bySerial = ids.map((id) => meters.find((m) => m.id === id)?.serial_number ?? `#${id}`);
  return bySerial.join(", ");
}

export function ScheduledJobsPage() {
  const { role } = useAuth();
  const [jobs, setJobs] = useState<ScheduledJob[] | null>(null);
  const [meters, setMeters] = useState<Meter[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [showCreate, setShowCreate] = useState(false);
  const [name, setName] = useState("");
  const [cron, setCron] = useState("0 * * * *");
  const [jobType, setJobType] = useState<ScheduledJobType>("read_current");
  const [obis, setObis] = useState("1.1.1.8.0.ff");
  const [windowHours, setWindowHours] = useState("24");
  const [selectedMeterIds, setSelectedMeterIds] = useState<Set<number>>(new Set());

  const [pendingDelete, setPendingDelete] = useState<ScheduledJob | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [runs, setRuns] = useState<ScheduledJobRun[]>([]);
  const [expandedRunId, setExpandedRunId] = useState<number | null>(null);
  const [runJobs, setRunJobs] = useState<Job[]>([]);

  const loadAll = useCallback(async () => {
    setError(null);
    try {
      const [j, m] = await Promise.all([
        api.get<ScheduledJob[]>("/api/scheduled-jobs"),
        api.get<Meter[]>("/api/meters"),
      ]);
      setJobs(j);
      setMeters(m);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не удалось загрузить расписания");
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  function toggleMeter(meterId: number) {
    setSelectedMeterIds((prev) => {
      const next = new Set(prev);
      if (next.has(meterId)) next.delete(meterId);
      else next.add(meterId);
      return next;
    });
  }

  function handleCreate() {
    if (!name.trim() || selectedMeterIds.size === 0) {
      setError("Укажите имя расписания и хотя бы один счётчик");
      return;
    }
    const operation_params =
      jobType === "read_current" ? { obis } : { window_hours: Number(windowHours) || 24 };

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
        setSelectedMeterIds(new Set());
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
      <h1>Расписания автоопроса</h1>

      {error && <div className="error-message">{error}</div>}

      {canManageAutomation(role) && (
        <section className="card">
          <div className="card-header">
            <h2>Создание расписания</h2>
            <button onClick={() => setShowCreate((v) => !v)}>{showCreate ? "Отмена" : "Новое расписание"}</button>
          </div>
          {showCreate && (
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
                <label>
                  OBIS-код
                  <br />
                  <input value={obis} onChange={(e) => setObis(e.target.value)} />
                </label>
              ) : (
                <label>
                  Окно, часов (от текущего момента при каждом запуске)
                  <br />
                  <input type="number" min={1} value={windowHours} onChange={(e) => setWindowHours(e.target.value)} style={{ width: 100 }} />
                </label>
              )}
              <div>
                Счётчики:
                {meters.map((m) => (
                  <label key={m.id} style={{ display: "block" }}>
                    <input type="checkbox" checked={selectedMeterIds.has(m.id)} onChange={() => toggleMeter(m.id)} /> {m.serial_number}
                  </label>
                ))}
              </div>
              <button onClick={handleCreate}>Сохранить расписание</button>
            </div>
          )}
        </section>
      )}

      {jobs === null && <p>Загрузка...</p>}
      {jobs !== null && jobs.length === 0 && <p>Расписаний пока нет.</p>}

      {jobs !== null && jobs.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Имя</th>
              <th>Cron</th>
              <th>Тип</th>
              <th>Счётчики</th>
              <th>Статус</th>
              <th>Последний запуск</th>
              <th>Следующий запуск</th>
              <th>Действия</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((j) => (
              <Fragment key={j.id}>
                <tr>
                  <td>
                    <a href="#" onClick={(e) => { e.preventDefault(); toggleExpand(j); }}>
                      {j.name}
                    </a>
                  </td>
                  <td>{j.cron_expression}</td>
                  <td>{JOB_TYPE_LABELS[j.job_type]}</td>
                  <td>{formatMeters(meters, j.meter_ids)}</td>
                  <td>{j.is_enabled ? "включено" : "выключено"}</td>
                  <td>{j.last_run_at ? new Date(j.last_run_at).toLocaleString("ru-RU") : "—"}</td>
                  <td>{j.next_run_at ? new Date(j.next_run_at).toLocaleString("ru-RU") : "—"}</td>
                  <td>
                    {canManageAutomation(role) && (
                      <>
                        <button onClick={() => handleToggleEnabled(j)}>{j.is_enabled ? "Выключить" : "Включить"}</button>{" "}
                        <button className="secondary" onClick={() => setPendingDelete(j)}>
                          Удалить
                        </button>
                      </>
                    )}
                  </td>
                </tr>
                {expandedId === j.id && (
                  <tr>
                    <td colSpan={8}>
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
                                    <a href="#" onClick={(e) => { e.preventDefault(); toggleRunExpand(j.id, r); }}>
                                      {r.meters_succeeded} / {r.meters_failed} / {r.meters_total}
                                    </a>
                                  </td>
                                </tr>
                                {expandedRunId === r.id && (
                                  <tr>
                                    <td colSpan={4}>
                                      {runJobs.map((rj) => (
                                        <div key={rj.id}>
                                          {meters.find((m) => m.id === rj.meter_id)?.serial_number ?? rj.meter_id}: {rj.status}
                                          {rj.error && ` — ${rj.error.code}: ${rj.error.message}`}
                                        </div>
                                      ))}
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
