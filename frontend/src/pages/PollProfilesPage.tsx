import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { canManageAutomation, useAuth } from "../auth/AuthContext";
import { ConfirmModal } from "../components/ConfirmModal";
import type { ObisEntry, PollProfile, PollProfileItem } from "../api/types";

const OBIS_PATTERN = /^[0-9a-fA-F]{1,2}(\.[0-9a-fA-F]{1,2}){5}$/;

const EMPTY_ITEM: PollProfileItem = { obis: "", label: "", enabled: true };

function emptyForm(): { name: string; description: string; items: PollProfileItem[] } {
  return { name: "", description: "", items: [] };
}

export function PollProfilesPage() {
  const { role } = useAuth();
  const [profiles, setProfiles] = useState<PollProfile[] | null>(null);
  const [catalog, setCatalog] = useState<ObisEntry[]>([]);
  const [error, setError] = useState<string | null>(null);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(emptyForm());
  const [pendingDelete, setPendingDelete] = useState<PollProfile | null>(null);
  const [showCatalogPicker, setShowCatalogPicker] = useState(false);

  const loadAll = useCallback(async () => {
    setError(null);
    try {
      const [p, c] = await Promise.all([
        api.get<PollProfile[]>("/api/poll-profiles"),
        api.get<ObisEntry[]>("/api/obis-catalog"),
      ]);
      setProfiles(p);
      setCatalog(c);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не удалось загрузить профили опроса");
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  function openCreate() {
    setEditingId(null);
    setForm(emptyForm());
    setShowForm(true);
    setShowCatalogPicker(false);
    setError(null);
  }

  function openEdit(p: PollProfile) {
    setEditingId(p.id);
    setForm({ name: p.name, description: p.description ?? "", items: p.items.map((i) => ({ ...i })) });
    setShowForm(true);
    setShowCatalogPicker(false);
    setError(null);
  }

  function updateItem(index: number, patch: Partial<PollProfileItem>) {
    setForm((prev) => ({
      ...prev,
      items: prev.items.map((it, i) => (i === index ? { ...it, ...patch } : it)),
    }));
  }

  function addItem() {
    setForm((prev) => ({ ...prev, items: [...prev.items, { ...EMPTY_ITEM }] }));
  }

  function addFromCatalog(entry: ObisEntry) {
    setForm((prev) => {
      if (prev.items.some((it) => it.obis === entry.obis)) return prev; // уже добавлен
      return { ...prev, items: [...prev.items, { obis: entry.obis, label: entry.label, enabled: true }] };
    });
  }

  function removeItem(index: number) {
    setForm((prev) => ({ ...prev, items: prev.items.filter((_, i) => i !== index) }));
  }

  function handleSave() {
    if (!form.name.trim()) {
      setError("Укажите имя профиля");
      return;
    }
    const items = form.items.filter((it) => it.obis.trim() !== "" || it.label.trim() !== "");
    if (items.length === 0) {
      setError("Профиль должен включать хотя бы один OBIS-код");
      return;
    }
    const invalid = items.find((it) => !OBIS_PATTERN.test(it.obis.trim()));
    if (invalid) {
      setError(`Некорректный OBIS-код: «${invalid.obis}» — ожидается вид A.B.C.D.E.F`);
      return;
    }
    if (items.some((it) => !it.label.trim())) {
      setError("У каждого OBIS-кода должно быть пояснение");
      return;
    }

    setError(null);
    const body = { name: form.name, description: form.description || null, items };
    const req = editingId
      ? api.put<PollProfile>(`/api/poll-profiles/${editingId}`, body)
      : api.post<PollProfile>("/api/poll-profiles", body);
    req
      .then(() => {
        setShowForm(false);
        return loadAll();
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось сохранить профиль"));
  }

  function handleConfirmDelete() {
    if (!pendingDelete) return;
    const profile = pendingDelete;
    setPendingDelete(null);
    api
      .del(`/api/poll-profiles/${profile.id}`)
      .then(() => loadAll())
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось удалить профиль"));
  }

  return (
    <div>
      <h1>Профили опроса</h1>
      <p className="hint">
        Набор OBIS-кодов с пояснением, что каждый опрашивает, и признаком включён/выключен. Используется в
        «Расписаниях опроса» вместо одного жёстко заданного OBIS — расписание с профилем опрашивает у каждого
        счётчика группы все включённые пункты профиля.
      </p>

      {error && <div className="error-message">{error}</div>}

      {canManageAutomation(role) && (
        <section className="card">
          <div className="card-header">
            <h2>{editingId ? "Редактирование профиля" : "Новый профиль"}</h2>
            <button onClick={() => (showForm ? setShowForm(false) : openCreate())}>
              {showForm ? "Отмена" : "Новый профиль"}
            </button>
          </div>
          {showForm && (
            <div className="filters">
              <label>
                Имя профиля
                <br />
                <input value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} disabled={editingId !== null} />
              </label>
              <label>
                Описание
                <br />
                <input value={form.description} onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))} />
              </label>

              <table className="data-table">
                <thead>
                  <tr>
                    <th>Опрашивать</th>
                    <th>OBIS-код</th>
                    <th>Пояснение</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {form.items.map((item, i) => (
                    <tr key={i}>
                      <td>
                        <input
                          type="checkbox"
                          checked={item.enabled}
                          onChange={(e) => updateItem(i, { enabled: e.target.checked })}
                        />
                      </td>
                      <td>
                        <input
                          value={item.obis}
                          placeholder="1.1.1.8.0.ff"
                          onChange={(e) => updateItem(i, { obis: e.target.value })}
                          style={{ width: 130 }}
                        />
                      </td>
                      <td>
                        <input
                          value={item.label}
                          placeholder="Что опрашивает этот OBIS"
                          onChange={(e) => updateItem(i, { label: e.target.value })}
                          style={{ width: 260 }}
                        />
                      </td>
                      <td>
                        <button className="secondary" onClick={() => removeItem(i)}>
                          Убрать
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <button onClick={() => setShowCatalogPicker((v) => !v)}>
                {showCatalogPicker ? "Закрыть список кодов" : "+ добавить код из карты OBIS"}
              </button>{" "}
              <button className="secondary" onClick={addItem}>
                + строка вручную
              </button>{" "}
              <button onClick={handleSave}>Сохранить профиль</button>

              {showCatalogPicker && (
                <div className="card" style={{ marginTop: 12 }}>
                  <p className="hint">
                    Выберите код — посмотрите описание и нажмите «Добавить». Уже добавленные в этот профиль коды
                    отмечены.
                  </p>
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>№</th>
                        <th>OBIS-код</th>
                        <th>Название</th>
                        <th>Описание</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {catalog.map((entry) => {
                        const alreadyAdded = form.items.some((it) => it.obis === entry.obis);
                        return (
                          <tr key={entry.number}>
                            <td>{entry.number}</td>
                            <td>{entry.obis}</td>
                            <td>{entry.label}</td>
                            <td>{entry.description}</td>
                            <td>
                              <button disabled={alreadyAdded} onClick={() => addFromCatalog(entry)}>
                                {alreadyAdded ? "Добавлено" : "Добавить"}
                              </button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  {catalog.length === 0 && (
                    <p className="hint">
                      Карта OBIS-кодов пуста — см. раздел «Параметры → Карта OBIS-кодов».
                    </p>
                  )}
                </div>
              )}
            </div>
          )}
        </section>
      )}

      {profiles === null && <p>Загрузка...</p>}
      {profiles !== null && profiles.length === 0 && <p>Профилей опроса пока нет.</p>}

      {profiles !== null && profiles.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Имя</th>
              <th>Описание</th>
              <th>OBIS-коды (включённые)</th>
              <th>Действия</th>
            </tr>
          </thead>
          <tbody>
            {profiles.map((p) => (
              <tr key={p.id}>
                <td>{p.name}</td>
                <td>{p.description ?? "—"}</td>
                <td>{p.items.filter((i) => i.enabled).map((i) => `${i.label} (${i.obis})`).join(", ") || "—"}</td>
                <td>
                  {canManageAutomation(role) && (
                    <>
                      <button onClick={() => openEdit(p)}>Изменить</button>{" "}
                      <button className="secondary" onClick={() => setPendingDelete(p)}>
                        Удалить
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {pendingDelete && (
        <ConfirmModal
          title="Удалить профиль опроса"
          message={`Профиль «${pendingDelete.name}» будет удалён без возможности восстановления. Расписания, ссылающиеся на него, перестанут опрашивать по нему до выбора другого профиля. Подтвердите операцию.`}
          confirmLabel="Удалить"
          onConfirm={handleConfirmDelete}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </div>
  );
}
