import { Fragment, useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { canManageAutomation, useAuth } from "../auth/AuthContext";
import { Pagination } from "../components/Pagination";
import { usePagination } from "../lib/usePagination";
import type { EudbExportItem, EudbExportRun } from "../api/types";

// Ежедневный экспорт профиля нагрузки (Profile1) во внешнюю БД ЕЭБД
// (2026-09-14, по прямому указанию пользователя: "Необходимо
// импортировать данные 1 раз в сутки"). Источник — уже читаемый по
// расписанию буфер Profile1 (напряжение/ток/коэффициент мощности по
// фазам + активная энергия — ровно то, что нужно ЕЭБД), не отдельный
// опрос. Доступ — та же роль, что и у "Расписания опроса"
// (canManageAutomation, Admin/Super-admin): административная
// автоматизация, не рядовой просмотр.
const RUN_STATUS_LABELS: Record<string, string> = {
  running: "выполняется",
  succeeded: "успешно",
  partial_failure: "частичная ошибка",
  failed: "ошибка",
};

const POLL_INTERVAL_MS = 3000;

export function EudbExportPage() {
  const { role } = useAuth();
  const canManage = canManageAutomation(role);
  const [runs, setRuns] = useState<EudbExportRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [starting, setStarting] = useState(false);
  const [expandedRunId, setExpandedRunId] = useState<number | null>(null);
  const [items, setItems] = useState<Record<number, EudbExportItem[]>>({});
  const runsPagination = usePagination(runs ?? []);
  const itemsPagination = usePagination(items[expandedRunId ?? -1] ?? [], [expandedRunId]);

  const load = useCallback(() => {
    api
      .get<EudbExportRun[]>("/api/eudb-export/runs")
      .then(setRuns)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось загрузить журнал экспорта"));
  }, []);

  useEffect(load, [load]);

  useEffect(() => {
    api
      .get<{ configured: boolean }>("/api/eudb-export/status")
      .then((r) => setConfigured(r.configured))
      .catch(() => setConfigured(null));
  }, []);

  // Поллинг, пока есть запись со статусом "выполняется" — тот же
  // принцип, что и на страницах "Работа с счётчиками".
  useEffect(() => {
    if (!runs || !runs.some((r) => r.status === "running")) return;
    const interval = window.setInterval(load, POLL_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [runs, load]);

  function handleRunNow() {
    setError(null);
    setNotice(null);
    setStarting(true);
    api
      .post<EudbExportRun>("/api/eudb-export/run-now")
      .then(() => {
        setNotice("Экспорт запущен.");
        load();
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось запустить экспорт"))
      .finally(() => setStarting(false));
  }

  function toggleExpand(run: EudbExportRun) {
    if (expandedRunId === run.id) {
      setExpandedRunId(null);
      return;
    }
    setExpandedRunId(run.id);
    if (!items[run.id]) {
      api
        .get<EudbExportItem[]>(`/api/eudb-export/runs/${run.id}/items`)
        .then((data) => setItems((prev) => ({ ...prev, [run.id]: data })))
        .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось загрузить детали запуска"));
    }
  }

  return (
    <div>
      <h1>Экспорт профиля в ЕЭБД</h1>
      <p className="hint">
        Раз в сутки (в конце суток, 23:50 по Бишкеку) данные профиля нагрузки (Profile1: напряжение, ток,
        коэффициент мощности по фазам, активная энергия — по каждому активному счётчику) выгружаются во внешнюю
        базу данных ЕЭБД.
        {!canManage && " Просмотр и запуск доступны ролям «Администратор» и «Супер-администратор»."}
      </p>
      {error && <div className="error-message">{error}</div>}
      {notice && <p className="hint">{notice}</p>}
      {configured === false && (
        <div className="error-message">
          Подключение к ЕЭБД не настроено (переменные окружения MMWS_EUDB_*) — экспорт работать не будет.
        </div>
      )}

      {canManage && (
        <section className="card">
          <button disabled={starting} onClick={handleRunNow}>
            Запустить сейчас
          </button>
        </section>
      )}

      <section className="card">
        <h2>Журнал запусков</h2>
        {runs === null && <p>Загрузка...</p>}
        {runs !== null && runs.length === 0 && <p>Запусков ещё не было.</p>}
        {runs !== null && runs.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th>Начало</th>
                <th>Окончание</th>
                <th>Тип</th>
                <th>Статус</th>
                <th>Счётчиков (успех/ошибка/всего)</th>
              </tr>
            </thead>
            <tbody>
              {runsPagination.pageRows.map((r) => (
                <Fragment key={r.id}>
                  <tr>
                    <td>{new Date(r.started_at).toLocaleString("ru-RU")}</td>
                    <td>{r.finished_at ? new Date(r.finished_at).toLocaleString("ru-RU") : "—"}</td>
                    <td>{r.triggered_manually ? "вручную" : "по расписанию"}</td>
                    <td>
                      {RUN_STATUS_LABELS[r.status] ?? r.status}
                      {r.error_message && `: ${r.error_message}`}
                    </td>
                    <td>
                      <a
                        href="#"
                        onClick={(e) => {
                          e.preventDefault();
                          toggleExpand(r);
                        }}
                      >
                        {r.meters_succeeded} / {r.meters_failed} / {r.meters_total}
                      </a>
                    </td>
                  </tr>
                  {expandedRunId === r.id && (
                    <tr>
                      <td colSpan={5}>
                        {!items[r.id] && <p>Загрузка...</p>}
                        {items[r.id] && items[r.id].length === 0 && <p>Нет данных по счётчикам.</p>}
                        {items[r.id] && items[r.id].length > 0 && (
                          <table className="data-table">
                            <thead>
                              <tr>
                                <th>Счётчик</th>
                                <th>Результат</th>
                                <th>Строк выгружено</th>
                              </tr>
                            </thead>
                            <tbody>
                              {itemsPagination.pageRows.map((it) => (
                                <tr key={it.id}>
                                  <td>{it.meter_serial}</td>
                                  <td>{it.ok ? "успешно" : it.error_message}</td>
                                  <td>{it.rows_exported}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        )}
                        {items[r.id] && items[r.id].length > 0 && (
                          <Pagination
                            page={itemsPagination.page}
                            pageCount={itemsPagination.pageCount}
                            onPageChange={itemsPagination.setPage}
                            total={itemsPagination.total}
                            start={itemsPagination.start}
                            pageSize={itemsPagination.pageSize}
                          />
                        )}
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
        {runs !== null && runs.length > 0 && (
          <Pagination
            page={runsPagination.page}
            pageCount={runsPagination.pageCount}
            onPageChange={runsPagination.setPage}
            total={runsPagination.total}
            start={runsPagination.start}
            pageSize={runsPagination.pageSize}
          />
        )}
      </section>
    </div>
  );
}
