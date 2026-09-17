import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { Pagination } from "../components/Pagination";
import { usePagination } from "../lib/usePagination";
import type { ResStats } from "../api/types";

export function ResPage() {
  const [stats, setStats] = useState<ResStats[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pagination = usePagination(stats ?? []);

  function load() {
    setError(null);
    api
      .get<ResStats[]>("/api/meters/res-stats")
      .then(setStats)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось загрузить статистику по РЭС"));
  }

  useEffect(load, []);

  const total = stats?.reduce((sum, s) => sum + s.meters_total, 0) ?? 0;
  const totalActive = stats?.reduce((sum, s) => sum + s.meters_active, 0) ?? 0;
  // Средний процент по всем РЭС — взвешенный по числу активных счётчиков
  // (не простое среднее долей, иначе маленький РЭС искажал бы итог так
  // же сильно, как крупный).
  function weightedAveragePct(key: "pct_read_today" | "pct_read_3d"): number | null {
    if (!stats || totalActive === 0) return null;
    const readCount = stats.reduce((sum, s) => sum + Math.round((s.meters_active * s[key]) / 100), 0);
    return Math.round((1000 * readCount) / totalActive) / 10;
  }

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <h1>РЭСы и Объекты</h1>
        <button onClick={load}>Обновить</button>
      </div>
      <p className="hint">
        Счётчики физически разведены по РЭС/объектам через разные call-home порты Gateway'я — принадлежность
        определяется автоматически по тому, на какой порт звонит счётчик, и обновляется при каждом звонке.
      </p>

      {error && <div className="error-message">{error}</div>}
      {stats === null && !error && <p>Загрузка...</p>}
      {stats !== null && stats.length === 0 && (
        <p>Пока ни один счётчик не привязан к РЭС/объекту (порт ещё не опознан ни разу).</p>
      )}

      {stats !== null && stats.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>РЭС / Объект</th>
              <th>Счётчиков всего</th>
              <th>Активных</th>
              <th>Онлайн</th>
              <th>% чтения сегодня</th>
              <th>% чтения за 3 дня</th>
            </tr>
          </thead>
          <tbody>
            {pagination.pageRows.map((s) => (
              <tr key={s.res_name}>
                <td>
                  <Link to={`/meters?res_name=${encodeURIComponent(s.res_name)}`}>{s.res_name}</Link>
                </td>
                <td>{s.meters_total}</td>
                <td>{s.meters_active}</td>
                <td>{s.meters_online}</td>
                <td>{s.meters_active > 0 ? `${s.pct_read_today}%` : "—"}</td>
                <td>{s.meters_active > 0 ? `${s.pct_read_3d}%` : "—"}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td>Итого</td>
              <td>{total}</td>
              <td>{totalActive}</td>
              <td>{stats.reduce((sum, s) => sum + s.meters_online, 0)}</td>
              <td>{totalActive > 0 ? `${weightedAveragePct("pct_read_today")}%` : "—"}</td>
              <td>{totalActive > 0 ? `${weightedAveragePct("pct_read_3d")}%` : "—"}</td>
            </tr>
          </tfoot>
        </table>
      )}
      {stats !== null && stats.length > 0 && (
        <Pagination
          page={pagination.page}
          pageCount={pagination.pageCount}
          onPageChange={pagination.setPage}
          total={pagination.total}
          start={pagination.start}
          pageSize={pagination.pageSize}
        />
      )}
    </div>
  );
}
