import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { ConfirmModal } from "../components/ConfirmModal";

interface BillingApiKey {
  id: number;
  client_id: string;
  description: string | null;
  rate_limit_per_minute: number;
  status: "active" | "revoked";
  created_at: string;
  revoked_at: string | null;
  last_used_at: string | null;
}

// ТЗ п.4.2.9, API.docx п.3.4 — управление API-ключами биллинговой
// интеграции: выдача, отзыв. Секрет показывается один раз в момент выдачи.
export function BillingKeysPage() {
  const [keys, setKeys] = useState<BillingApiKey[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [issuedKey, setIssuedKey] = useState<{ clientId: string; secret: string } | null>(null);

  const [showCreate, setShowCreate] = useState(false);
  const [clientId, setClientId] = useState("");
  const [description, setDescription] = useState("");
  const [rateLimit, setRateLimit] = useState("60");

  const [pendingRevoke, setPendingRevoke] = useState<BillingApiKey | null>(null);

  const load = useCallback(() => {
    setError(null);
    api
      .get<BillingApiKey[]>("/api/billing-keys")
      .then(setKeys)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось загрузить API-ключи"));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function handleCreate() {
    if (!clientId.trim()) {
      setError("Укажите client_id");
      return;
    }
    setError(null);
    api
      .post<BillingApiKey & { api_key: string }>("/api/billing-keys", {
        client_id: clientId,
        description: description || null,
        rate_limit_per_minute: Number(rateLimit) || 60,
      })
      .then((created) => {
        setIssuedKey({ clientId: created.client_id, secret: created.api_key });
        setShowCreate(false);
        setClientId("");
        setDescription("");
        setRateLimit("60");
        load();
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось создать ключ"));
  }

  function handleConfirmRevoke() {
    if (!pendingRevoke) return;
    const key = pendingRevoke;
    setPendingRevoke(null);
    setError(null);
    api
      .post(`/api/billing-keys/${key.id}/revoke`)
      .then(() => load())
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось отозвать ключ"));
  }

  return (
    <div>
      <h1>API-ключи биллинга</h1>

      {error && <div className="error-message">{error}</div>}

      {issuedKey && (
        <div className="card" style={{ borderColor: "var(--color-accent, #4f7cff)" }}>
          <h2>Ключ выдан: {issuedKey.clientId}</h2>
          <p>
            Секрет показывается <strong>только один раз</strong> — сохраните его сейчас, повторно получить будет
            нельзя (только отозвать и выдать новый).
          </p>
          <code style={{ display: "block", padding: 8, background: "var(--color-surface)", wordBreak: "break-all" }}>
            {issuedKey.secret}
          </code>
          <button onClick={() => setIssuedKey(null)}>Понятно, скрыть</button>
        </div>
      )}

      <section className="card">
        <div className="card-header">
          <h2>Выдача ключа</h2>
          <button onClick={() => setShowCreate((v) => !v)}>{showCreate ? "Отмена" : "Новый ключ"}</button>
        </div>
        {showCreate && (
          <div className="filters">
            <label>
              Client ID
              <br />
              <input value={clientId} onChange={(e) => setClientId(e.target.value)} placeholder="1c-billing" />
            </label>
            <label>
              Описание
              <br />
              <input value={description} onChange={(e) => setDescription(e.target.value)} />
            </label>
            <label>
              Лимит запросов/мин
              <br />
              <input
                type="number"
                min={1}
                value={rateLimit}
                onChange={(e) => setRateLimit(e.target.value)}
                style={{ width: 100 }}
              />
            </label>
            <button onClick={handleCreate}>Выдать</button>
          </div>
        )}
      </section>

      {keys === null && <p>Загрузка...</p>}

      {keys !== null && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Client ID</th>
              <th>Описание</th>
              <th>Лимит/мин</th>
              <th>Статус</th>
              <th>Последнее использование</th>
              <th>Действия</th>
            </tr>
          </thead>
          <tbody>
            {keys.map((k) => (
              <tr key={k.id}>
                <td>{k.client_id}</td>
                <td>{k.description ?? "—"}</td>
                <td>{k.rate_limit_per_minute}</td>
                <td>{k.status === "active" ? "активен" : "отозван"}</td>
                <td>{k.last_used_at ? new Date(k.last_used_at).toLocaleString("ru-RU") : "—"}</td>
                <td>
                  {k.status === "active" && (
                    <button className="secondary" onClick={() => setPendingRevoke(k)}>
                      Отозвать
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {pendingRevoke && (
        <ConfirmModal
          title="Отозвать API-ключ"
          message={`Ключ «${pendingRevoke.client_id}» будет немедленно отозван — все запросы с ним начнут отклоняться с 401. Подтвердите операцию.`}
          confirmLabel="Отозвать"
          onConfirm={handleConfirmRevoke}
          onCancel={() => setPendingRevoke(null)}
        />
      )}
    </div>
  );
}
