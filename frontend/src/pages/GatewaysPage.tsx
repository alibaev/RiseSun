import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { Gateway } from "../api/types";

const STATUS_LABELS: Record<string, string> = {
  pending: "ожидает подтверждения",
  approved: "подтверждён",
  disabled: "отключён",
};

// ТЗ п.4.1.1 — реестр экземпляров Protocol Gateway (регистрация,
// подтверждение — только «Супер-администратор»). Этап 6 добавляет живую
// настройку call-home порта.
export function GatewaysPage() {
  const [gateways, setGateways] = useState<Gateway[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [showRegister, setShowRegister] = useState(false);
  const [name, setName] = useState("");
  const [grpcTarget, setGrpcTarget] = useState("");
  const [manufacturer, setManufacturer] = useState("Risesun");

  const [portEdits, setPortEdits] = useState<Record<number, string>>({});
  const [savingPortFor, setSavingPortFor] = useState<number | null>(null);

  function load() {
    setError(null);
    api
      .get<Gateway[]>("/api/gateways")
      .then(setGateways)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось загрузить список Gateway"));
  }

  useEffect(load, []);

  function handleRegister() {
    if (!name.trim() || !grpcTarget.trim()) {
      setError("Укажите имя и адрес gRPC (host:port)");
      return;
    }
    setError(null);
    api
      .post("/api/gateways", { name, grpc_target: grpcTarget, manufacturer })
      .then(() => {
        setShowRegister(false);
        setName("");
        setGrpcTarget("");
        load();
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось зарегистрировать Gateway"));
  }

  function handleApprove(gateway: Gateway) {
    setError(null);
    api
      .post(`/api/gateways/${gateway.id}/approve`)
      .then(() => load())
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось подтвердить Gateway"));
  }

  function handleDisable(gateway: Gateway) {
    setError(null);
    api
      .post(`/api/gateways/${gateway.id}/disable`)
      .then(() => load())
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось отключить Gateway"));
  }

  function handleSetPort(gateway: Gateway) {
    const raw = portEdits[gateway.id];
    const port = Number(raw);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      setError("Порт должен быть целым числом от 1 до 65535");
      return;
    }
    setError(null);
    setNotice(null);
    setSavingPortFor(gateway.id);
    api
      .put<Gateway>(`/api/gateways/${gateway.id}/call-home-port`, { port })
      .then((updated) => {
        setNotice(`Gateway «${gateway.name}» переслушан на порту ${updated.call_home_port} без перезапуска.`);
        load();
      })
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Не удалось применить порт call-home (Gateway недоступен?)")
      )
      .finally(() => setSavingPortFor(null));
  }

  return (
    <div>
      <h1>Реестр Protocol Gateway</h1>

      {error && <div className="error-message">{error}</div>}
      {notice && <p className="hint">{notice}</p>}

      <section className="card">
        <div className="card-header">
          <h2>Регистрация Gateway</h2>
          <button onClick={() => setShowRegister((v) => !v)}>{showRegister ? "Отмена" : "Новый Gateway"}</button>
        </div>
        {showRegister && (
          <div className="filters">
            <label>
              Имя
              <br />
              <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Risesun Gateway #1" />
            </label>
            <label>
              Производитель
              <br />
              <input value={manufacturer} onChange={(e) => setManufacturer(e.target.value)} />
            </label>
            <label>
              gRPC-адрес (host:port)
              <br />
              <input value={grpcTarget} onChange={(e) => setGrpcTarget(e.target.value)} placeholder="gateway-risesun:50051" />
            </label>
            <button onClick={handleRegister}>Зарегистрировать</button>
          </div>
        )}
      </section>

      {gateways === null && <p>Загрузка...</p>}
      {gateways !== null && gateways.length === 0 && <p>Gateway не зарегистрированы.</p>}

      {gateways !== null &&
        gateways.map((g) => (
          <section className="card" key={g.id}>
            <div className="card-header">
              <h2>
                {g.name}
                <span className={`status-dot ${g.is_online ? "online" : "offline"}`} />
              </h2>
              <div style={{ display: "flex", gap: 8 }}>
                {g.status === "pending" && <button onClick={() => handleApprove(g)}>Подтвердить</button>}
                {g.status !== "disabled" && (
                  <button className="secondary" onClick={() => handleDisable(g)}>
                    Отключить
                  </button>
                )}
              </div>
            </div>
            <dl className="info-grid">
              <dt>Производитель</dt>
              <dd>{g.manufacturer}</dd>
              <dt>Версия драйвера</dt>
              <dd>{g.driver_version ?? "—"}</dd>
              <dt>gRPC-адрес</dt>
              <dd>{g.grpc_target}</dd>
              <dt>Статус</dt>
              <dd>{STATUS_LABELS[g.status] ?? g.status}</dd>
              <dt>Последний heartbeat</dt>
              <dd>{g.last_heartbeat_at ? new Date(g.last_heartbeat_at).toLocaleString("ru-RU") : "—"}</dd>
              <dt>Основной call-home порт</dt>
              <dd>{g.call_home_port ?? "—"}</dd>
              <dt>Все слушаемые call-home порты</dt>
              <dd>
                {g.call_home_ports && g.call_home_ports.length > 0
                  ? g.call_home_ports.join(", ")
                  : g.call_home_port ?? "—"}
              </dd>
            </dl>
            <label>
              Новый ОСНОВНОЙ call-home порт (применяется вживую, без перезапуска Gateway;
              остальные порты из списка выше настраиваются переменной окружения Gateway'я и
              вживую не меняются)
              <br />
              <input
                type="number"
                min={1}
                max={65535}
                placeholder={g.call_home_port ? String(g.call_home_port) : "напр. 2009"}
                value={portEdits[g.id] ?? ""}
                onChange={(e) => setPortEdits((prev) => ({ ...prev, [g.id]: e.target.value }))}
                style={{ width: 120 }}
              />{" "}
              <button onClick={() => handleSetPort(g)} disabled={savingPortFor === g.id}>
                {savingPortFor === g.id ? "Применяю..." : "Применить"}
              </button>
            </label>
          </section>
        ))}
    </div>
  );
}
