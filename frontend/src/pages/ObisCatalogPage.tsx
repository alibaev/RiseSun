import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { ObisEntry } from "../api/types";

export function ObisCatalogPage() {
  const [entries, setEntries] = useState<ObisEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<ObisEntry[]>("/api/obis-catalog")
      .then(setEntries)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось загрузить карту OBIS-кодов"));
  }, []);

  return (
    <div>
      <h1>Карта OBIS-кодов</h1>
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
            {entries.map((e) => (
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
    </div>
  );
}
