import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";

interface DashboardData {
  meters_total: number;
  meters_online: number;
  meters_offline: number;
  jobs_active: number;
  tamper_events_24h: number;
  readings_by_hour: { hour: string; count: number }[];
}

// ТЗ п.4.2.11 — стартовый экран со сводной статистикой.
export function DashboardPage() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get<DashboardData>("/api/dashboard")
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Не удалось загрузить дашборд");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <div className="error-message">{error}</div>;
  if (!data) return <p>Загрузка...</p>;

  const maxCount = Math.max(1, ...data.readings_by_hour.map((r) => r.count));

  return (
    <div>
      <h1>Дашборд</h1>

      <div className="dashboard-stats">
        <div className="card stat-card">
          <div className="stat-value">{data.meters_total}</div>
          <div className="stat-label">Счётчиков всего</div>
        </div>
        <div className="card stat-card">
          <div className="stat-value">
            <span className="status-dot online" /> {data.meters_online}
          </div>
          <div className="stat-label">Online</div>
        </div>
        <div className="card stat-card">
          <div className="stat-value">
            <span className="status-dot offline" /> {data.meters_offline}
          </div>
          <div className="stat-label">Offline</div>
        </div>
        <div className="card stat-card">
          <div className="stat-value">{data.jobs_active}</div>
          <div className="stat-label">Активных задач</div>
        </div>
        <div className="card stat-card">
          <div className="stat-value">{data.tamper_events_24h}</div>
          <div className="stat-label">Tamper-событий за 24ч</div>
        </div>
      </div>

      <section className="card">
        <h2>Динамика опроса (показания за последние 24 часа, по часам)</h2>
        {data.readings_by_hour.length === 0 ? (
          <p>Показаний за последние сутки нет.</p>
        ) : (
          <div className="dashboard-chart">
            {data.readings_by_hour.map((r) => (
              <div key={r.hour} className="dashboard-chart-bar-wrap" title={`${r.hour}: ${r.count}`}>
                <div className="dashboard-chart-bar" style={{ height: `${(r.count / maxCount) * 100}%` }} />
                <div className="dashboard-chart-label">{new Date(r.hour).getHours()}</div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
