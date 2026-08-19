import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { canManageAutomation, canWriteParameter, useAuth } from "../auth/AuthContext";
import { ConfirmModal } from "../components/ConfirmModal";
import { WRITABLE_PARAMS, WRITABLE_PARAM_LABELS } from "../constants/writableParameters";
import type { Job, Meter, ParameterScheme, ParameterSchemeParam } from "../api/types";

function formatParameters(parameters: ParameterSchemeParam[]): string {
  return parameters.map((p) => `${WRITABLE_PARAM_LABELS[p.parameter] ?? p.parameter} = ${p.value}`).join(", ");
}

export function ParameterSchemesPage() {
  const { role } = useAuth();
  const [schemes, setSchemes] = useState<ParameterScheme[] | null>(null);
  const [meters, setMeters] = useState<Meter[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [newValues, setNewValues] = useState<Record<string, string>>({});

  const [applyingScheme, setApplyingScheme] = useState<ParameterScheme | null>(null);
  const [selectedMeterIds, setSelectedMeterIds] = useState<Set<number>>(new Set());
  const [pendingApply, setPendingApply] = useState<ParameterScheme | null>(null);
  const [pendingDelete, setPendingDelete] = useState<ParameterScheme | null>(null);

  const loadAll = useCallback(async () => {
    setError(null);
    try {
      const [s, m] = await Promise.all([
        api.get<ParameterScheme[]>("/api/parameter-schemes"),
        api.get<Meter[]>("/api/meters"),
      ]);
      setSchemes(s);
      setMeters(m);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не удалось загрузить схемы параметров");
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  function handleCreate() {
    const parameters: ParameterSchemeParam[] = Object.entries(newValues)
      .filter(([, v]) => v.trim() !== "")
      .map(([parameter, v]) => ({ parameter, value: Number(v) }));

    if (!newName.trim()) {
      setError("Укажите имя схемы");
      return;
    }
    if (parameters.length === 0) {
      setError("Схема должна включать хотя бы один параметр");
      return;
    }
    if (parameters.some((p) => !Number.isInteger(p.value) || p.value < 0 || p.value > 255)) {
      setError("Значения параметров должны быть целыми числами от 0 до 255");
      return;
    }

    setError(null);
    api
      .post<ParameterScheme>("/api/parameter-schemes", { name: newName, description: newDescription || null, parameters })
      .then(() => {
        setShowCreate(false);
        setNewName("");
        setNewDescription("");
        setNewValues({});
        return loadAll();
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось сохранить схему"));
  }

  function handleConfirmDelete() {
    if (!pendingDelete) return;
    const scheme = pendingDelete;
    setPendingDelete(null);
    api
      .del(`/api/parameter-schemes/${scheme.id}`)
      .then(() => loadAll())
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось удалить схему"));
  }

  function openApply(scheme: ParameterScheme) {
    setApplyingScheme(scheme);
    setSelectedMeterIds(new Set());
    setNotice(null);
  }

  function toggleMeter(meterId: number) {
    setSelectedMeterIds((prev) => {
      const next = new Set(prev);
      if (next.has(meterId)) next.delete(meterId);
      else next.add(meterId);
      return next;
    });
  }

  function handleConfirmApply() {
    if (!pendingApply) return;
    const scheme = pendingApply;
    const meterIds = [...selectedMeterIds];
    setPendingApply(null);
    setError(null);
    api
      .post<Job[]>(`/api/parameter-schemes/${scheme.id}/apply`, { meter_ids: meterIds })
      .then((jobs) => {
        setApplyingScheme(null);
        setNotice(
          `Схема «${scheme.name}» поставлена в очередь: ${jobs.length} задач на ${meterIds.length} счётчик(ов). ` +
            "Статус каждой — на карточке соответствующего счётчика."
        );
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось применить схему"));
  }

  return (
    <div>
      <h1>Схемы параметров</h1>

      {error && <div className="error-message">{error}</div>}
      {notice && <p className="hint">{notice}</p>}

      {canManageAutomation(role) && (
        <section className="card">
          <div className="card-header">
            <h2>Создание схемы</h2>
            <button onClick={() => setShowCreate((v) => !v)}>{showCreate ? "Отмена" : "Новая схема"}</button>
          </div>
          {showCreate && (
            <div className="filters">
              <label>
                Имя схемы
                <br />
                <input value={newName} onChange={(e) => setNewName(e.target.value)} />
              </label>
              <label>
                Описание
                <br />
                <input value={newDescription} onChange={(e) => setNewDescription(e.target.value)} />
              </label>
              {WRITABLE_PARAMS.map(({ key, label }) => (
                <label key={key}>
                  {label} (0-255, пусто = не включать)
                  <br />
                  <input
                    type="number"
                    min={0}
                    max={255}
                    value={newValues[key] ?? ""}
                    onChange={(e) => setNewValues((prev) => ({ ...prev, [key]: e.target.value }))}
                    style={{ width: 100 }}
                  />
                </label>
              ))}
              <button onClick={handleCreate}>Сохранить схему</button>
            </div>
          )}
        </section>
      )}

      {schemes === null && <p>Загрузка...</p>}
      {schemes !== null && schemes.length === 0 && <p>Схем пока нет.</p>}

      {schemes !== null && schemes.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Имя</th>
              <th>Описание</th>
              <th>Параметры</th>
              <th>Действия</th>
            </tr>
          </thead>
          <tbody>
            {schemes.map((s) => (
              <tr key={s.id}>
                <td>{s.name}</td>
                <td>{s.description ?? "—"}</td>
                <td>{formatParameters(s.parameters)}</td>
                <td>
                  {canWriteParameter(role) && <button onClick={() => openApply(s)}>Применить</button>}{" "}
                  {canManageAutomation(role) && <button className="secondary" onClick={() => setPendingDelete(s)}>Удалить</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {applyingScheme && (
        <section className="card">
          <h2>Применить «{applyingScheme.name}» к счётчикам</h2>
          <div className="filters">
            {meters.map((m) => (
              <label key={m.id} style={{ display: "block" }}>
                <input
                  type="checkbox"
                  checked={selectedMeterIds.has(m.id)}
                  onChange={() => toggleMeter(m.id)}
                />{" "}
                {m.serial_number} ({m.ip_address ?? "call-home"})
              </label>
            ))}
          </div>
          <button
            onClick={() => setPendingApply(applyingScheme)}
            disabled={selectedMeterIds.size === 0}
          >
            Применить к выбранным ({selectedMeterIds.size})
          </button>{" "}
          <button className="secondary" onClick={() => setApplyingScheme(null)}>
            Отмена
          </button>
        </section>
      )}

      {pendingApply && (
        <ConfirmModal
          title="Применить схему параметров"
          message={`Схема «${pendingApply.name}» (${formatParameters(pendingApply.parameters)}) будет записана на ${selectedMeterIds.size} счётчик(ов). Подтвердите операцию.`}
          confirmLabel="Применить"
          onConfirm={handleConfirmApply}
          onCancel={() => setPendingApply(null)}
        />
      )}

      {pendingDelete && (
        <ConfirmModal
          title="Удалить схему"
          message={`Схема «${pendingDelete.name}» будет удалена без возможности восстановления. Подтвердите операцию.`}
          confirmLabel="Удалить"
          onConfirm={handleConfirmDelete}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </div>
  );
}
