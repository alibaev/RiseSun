import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { Pagination } from "../components/Pagination";
import { exportToExcel } from "../lib/exportExcel";
import { usePagination } from "../lib/usePagination";
import type { ObisEntry } from "../api/types";

export function ObisCatalogPage() {
  const [entries, setEntries] = useState<ObisEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pagination = usePagination(entries ?? []);

  useEffect(() => {
    api
      .get<ObisEntry[]>("/api/obis-catalog")
      .then(setEntries)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось загрузить карту OBIS-кодов"));
  }, []);

  function handleExport() {
    if (!entries) return;
    exportToExcel(
      "obis-catalog",
      "OBIS-коды",
      entries.map((e) => ({
        "№": e.number,
        "OBIS-код": e.obis,
        Название: e.label,
        Описание: e.description,
        "Источник в коде": e.source,
      }))
    );
  }

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <h1>Карта OBIS-кодов</h1>
        {entries !== null && entries.length > 0 && <button onClick={handleExport}>Экспорт в Excel</button>}
      </div>
      <p className="hint">
        Все OBIS-коды, реально используемые в системе, с пояснением, что каждый из них означает/опрашивает. Список
        не претендует на полноту словаря производителя (см. OBIS.xlsx) — только коды с подтверждённым или
        задокументированным назначением в этом проекте.
      </p>

      {error && <div className="error-message">{error}</div>}
      {entries === null && !error && <p>Загрузка...</p>}

      {entries !== null && (
        <table className="data-table">
          <thead>
            <tr>
              <th>№</th>
              <th>OBIS-код</th>
              <th>Название</th>
              <th>Описание</th>
              <th>Источник в коде</th>
            </tr>
          </thead>
          <tbody>
            {pagination.pageRows.map((e) => (
              <tr key={e.number}>
                <td>{e.number}</td>
                <td>{e.obis}</td>
                <td>{e.label}</td>
                <td>{e.description}</td>
                <td>
                  <code>{e.source}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {entries !== null && (
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
