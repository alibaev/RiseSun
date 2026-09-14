import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { canManageMeters, useAuth } from "../auth/AuthContext";
import { exportToExcel } from "../lib/exportExcel";
import { formatValue } from "../lib/format";
import type { Meter, ProtocolProfile } from "../api/types";

const PROFILE_LABELS: Record<ProtocolProfile, string> = {
  mode_c: "IEC 62056-21 mode C",
  mode_e: "IEC 62056-21 mode E",
  hdlc_dlms: "HDLC-DLMS",
};

const MS_PER_DAY = 24 * 60 * 60 * 1000;

// Свежесть показания — цвет строки в "Активные" (согласовано с
// пользователем 2026-09-07): ≤2 суток зелёным, 3-5 суток жёлтым,
// >5 суток серым, показаний не было вовсе — красным.
function readingAgeClass(lastReadAt: string | null): string {
  if (!lastReadAt) return "reading-none";
  const ageDays = (Date.now() - new Date(lastReadAt).getTime()) / MS_PER_DAY;
  if (ageDays <= 2) return "reading-fresh";
  if (ageDays <= 5) return "reading-stale";
  return "reading-old";
}

export function MetersListPage() {
  const { role } = useAuth();
  const [meters, setMeters] = useState<Meter[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [protocolFilter, setProtocolFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<string>("");
  // Переход из "РЭСы и Объекты" (2026-09-11) — фильтр по res_name через
  // URL (?res_name=...), а не отдельный выпадающий список: точка входа —
  // клик по строке РЭС, не выбор из формы на этой странице.
  const [searchParams] = useSearchParams();
  const [resNameFilter, setResNameFilter] = useState(searchParams.get("res_name") ?? "");
  // Поиск по номеру прямо в строке вкладки "Активные" (2026-09-11) —
  // отдельно от общего фильтра выше: фильтрует уже загруженный список
  // на месте, без похода на сервер. Применяется по Enter/кнопке "Найти"
  // (не на каждое нажатие клавиши) — по просьбе пользователя, список
  // большой, мгновенная фильтрация на каждый символ была бы лишней.
  const [activeSearchInput, setActiveSearchInput] = useState("");
  const [activeSearch, setActiveSearch] = useState("");

  const [activatingMeter, setActivatingMeter] = useState<Meter | null>(null);
  const [activateProfile, setActivateProfile] = useState<ProtocolProfile>("hdlc_dlms");
  const [activatePassword, setActivatePassword] = useState("");
  const [activateLocation, setActivateLocation] = useState("");

  function buildParams(): URLSearchParams {
    const params = new URLSearchParams();
    if (search) params.set("search", search);
    if (protocolFilter) params.set("protocol_profile", protocolFilter);
    if (statusFilter) params.set("is_active", statusFilter);
    if (resNameFilter) params.set("res_name", resNameFilter);
    return params;
  }

  function load() {
    setError(null);
    const params = buildParams();
    api
      .get<Meter[]>(`/api/meters${params.toString() ? `?${params}` : ""}`)
      .then(setMeters)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось загрузить счётчики"));
  }

  useEffect(load, [search, protocolFilter, statusFilter, resNameFilter]);

  const rows = useMemo(() => meters ?? [], [meters]);
  // Этап 6 — обнаруженные по call-home счётчики (ещё без пароля/
  // протокола) показываются ОТДЕЛЬНОЙ группой сверху, чтобы оператор
  // сразу видел, что появилось новое оборудование, ждущее активации.
  const installedRows = useMemo(() => rows.filter((m) => m.status === "installed"), [rows]);
  // "Малое потребление" (2026-09-11) — активные счётчики с
  // пренебрежимо малым/нулевым расходом (см. Meter.is_low_consumption)
  // — отдельная секция, исключены из основного списка "Активные", чтобы
  // не дублировались.
  const lowConsumptionRows = useMemo(
    () => rows.filter((m) => m.status === "active" && m.is_low_consumption),
    [rows]
  );
  const activeRows = useMemo(
    () => rows.filter((m) => m.status === "active" && !m.is_low_consumption),
    [rows]
  );
  const filteredActiveRows = useMemo(() => {
    const query = activeSearch.trim().toLowerCase();
    if (!query) return activeRows;
    return activeRows.filter((m) => m.serial_number.toLowerCase().includes(query));
  }, [activeRows, activeSearch]);

  function handleSetLowConsumption(m: Meter, value: boolean) {
    setError(null);
    api
      .put<Meter>(`/api/meters/${m.id}`, { is_low_consumption: value })
      .then(load)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось изменить категорию счётчика"));
  }

  // "От и до скольки кВтч" (2026-09-11) — массовое добавление в «Малое
  // потребление» по диапазону последнего показания энергии, тем же
  // критерием, каким сможет пользоваться и система (см.
  // backend/app/services/low_consumption.py — эндпоинт лишь вызывает
  // общую функцию).
  const [lowConsumptionMinKwh, setLowConsumptionMinKwh] = useState("");
  const [lowConsumptionMaxKwh, setLowConsumptionMaxKwh] = useState("");
  const [applyingRange, setApplyingRange] = useState(false);
  const [rangeMessage, setRangeMessage] = useState<string | null>(null);

  function handleApplyLowConsumptionRange() {
    const min_kwh = Number(lowConsumptionMinKwh);
    const max_kwh = Number(lowConsumptionMaxKwh);
    if (lowConsumptionMinKwh === "" || lowConsumptionMaxKwh === "" || Number.isNaN(min_kwh) || Number.isNaN(max_kwh)) {
      setError("Укажите оба значения диапазона (от и до, кВт·ч)");
      return;
    }
    setError(null);
    setRangeMessage(null);
    setApplyingRange(true);
    api
      .post<{ matched_meters: Meter[] }>("/api/meters/low-consumption/apply-range", { min_kwh, max_kwh })
      .then((res) => {
        setRangeMessage(
          res.matched_meters.length > 0
            ? `Добавлено в категорию: ${res.matched_meters.length}`
            : "По этому диапазону новых счётчиков не найдено"
        );
        load();
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось применить диапазон"))
      .finally(() => setApplyingRange(false));
  }

  function handleExportActive() {
    exportToExcel(
      "meters-active",
      "Активные счётчики",
      activeRows.map((m) => ({
        "Серийный номер": m.serial_number,
        "Физический адрес": m.physical_address ?? "",
        Ампер: m.rated_current_amps ?? "",
        "IP-адрес": m.ip_address ? (m.port ? `${m.ip_address}:${m.port}` : m.ip_address) : "",
        Протокол: m.protocol_profile ? PROFILE_LABELS[m.protocol_profile] : "",
        Местоположение: m.location ?? "",
        Статус: m.is_online ? "online" : "offline",
        Показание: formatValue(m.last_reading_value),
        "Дата показания": m.last_read_at ? new Date(m.last_read_at).toLocaleString("ru-RU") : "",
      }))
    );
  }

  function openActivate(meter: Meter) {
    setActivatingMeter(meter);
    setActivateProfile("hdlc_dlms");
    setActivatePassword("");
    setActivateLocation("");
    setError(null);
  }

  function handleConfirmActivate() {
    if (!activatingMeter) return;
    if (activatePassword.length === 0) {
      setError("Укажите пароль доступа (LLS) счётчика");
      return;
    }
    setError(null);
    api
      .post<Meter>(`/api/meters/${activatingMeter.id}/activate`, {
        protocol_profile: activateProfile,
        password: activatePassword,
        location: activateLocation || null,
      })
      .then(() => {
        setActivatingMeter(null);
        load();
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Не удалось активировать счётчик"));
  }

  return (
    <div>
      <div className="filters">
        <input
          placeholder="Поиск по серийному номеру / IP"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select value={protocolFilter} onChange={(e) => setProtocolFilter(e.target.value)}>
          <option value="">Все протоколы</option>
          <option value="mode_c">IEC 62056-21 mode C</option>
          <option value="mode_e">IEC 62056-21 mode E</option>
          <option value="hdlc_dlms">HDLC-DLMS</option>
        </select>
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
          <option value="">Любой статус</option>
          <option value="true">Активен</option>
          <option value="false">Неактивен</option>
        </select>
      </div>

      {resNameFilter && (
        <p className="hint">
          Фильтр по РЭС/объекту: <strong>{resNameFilter}</strong>{" "}
          <button className="secondary" onClick={() => setResNameFilter("")}>
            Сбросить
          </button>
        </p>
      )}

      {error && <div className="error-message">{error}</div>}

      {meters === null && !error && <p>Загрузка...</p>}

      {meters !== null && installedRows.length > 0 && (
        <section className="card">
          <div className="card-header">
            <h2>Установленные (ожидают активации) — {installedRows.length}</h2>
          </div>
          <p className="hint">
            Эти счётчики сами позвонили на call-home порт Gateway и были опознаны по серийному номеру, но пароль
            доступа и протокольный профиль ещё не известны — операции с ними недоступны до активации.
          </p>
          <table className="data-table">
            <thead>
              <tr>
                <th>Серийный номер</th>
                <th>Обнаружен</th>
                {canManageMeters(role) && <th>Действия</th>}
              </tr>
            </thead>
            <tbody>
              {installedRows.map((m) => (
                <tr key={m.id}>
                  <td>
                    <Link to={`/meters/${m.id}`}>{m.serial_number}</Link>
                  </td>
                  <td>{new Date(m.created_at).toLocaleString("ru-RU")}</td>
                  {canManageMeters(role) && (
                    <td>
                      <button onClick={() => openActivate(m)}>Активировать</button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {meters !== null && (
        <section className="card">
          <div className="card-header">
            <h2>Малое потребление — {lowConsumptionRows.length}</h2>
          </div>
          <p className="hint">
            Активные счётчики с пренебрежимо малым или нулевым расходом продолжительное время —
            повод проверить (обрыв линии у абонента, незаселённый объект, неисправность счётчика).
          </p>
          {canManageMeters(role) && (
            <div className="filters">
              <label>
                От, кВт·ч
                <br />
                <input
                  type="number"
                  value={lowConsumptionMinKwh}
                  onChange={(e) => setLowConsumptionMinKwh(e.target.value)}
                  style={{ width: 100 }}
                />
              </label>
              <label>
                До, кВт·ч
                <br />
                <input
                  type="number"
                  value={lowConsumptionMaxKwh}
                  onChange={(e) => setLowConsumptionMaxKwh(e.target.value)}
                  style={{ width: 100 }}
                />
              </label>
              <button onClick={handleApplyLowConsumptionRange} disabled={applyingRange}>
                {applyingRange ? "Добавление…" : "Добавить по диапазону"}
              </button>
              {rangeMessage && <span className="hint">{rangeMessage}</span>}
            </div>
          )}
          {lowConsumptionRows.length === 0 && <p>Счётчиков в этой категории нет.</p>}
          {lowConsumptionRows.length > 0 && (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Серийный номер</th>
                  <th>Показание</th>
                  <th>Дата показания</th>
                  <th>IP-адрес</th>
                  {canManageMeters(role) && <th>Действия</th>}
                </tr>
              </thead>
              <tbody>
                {lowConsumptionRows.map((m) => (
                  <tr key={m.id}>
                    <td>
                      <Link to={`/meters/${m.id}`}>{m.serial_number}</Link>
                    </td>
                    <td>{formatValue(m.last_reading_value)}</td>
                    <td>{m.last_read_at ? new Date(m.last_read_at).toLocaleString("ru-RU") : "—"}</td>
                    <td>{m.ip_address ?? "—"}</td>
                    {canManageMeters(role) && (
                      <td>
                        <button onClick={() => handleSetLowConsumption(m, false)}>Убрать из категории</button>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      )}

      {activatingMeter && (
        <section className="card">
          <div className="card-header">
            <h2>Активировать счётчик {activatingMeter.serial_number}</h2>
          </div>
          <div className="filters">
            <label>
              Протокольный профиль
              <br />
              <select value={activateProfile} onChange={(e) => setActivateProfile(e.target.value as ProtocolProfile)}>
                <option value="hdlc_dlms">HDLC-DLMS</option>
                <option value="mode_c">IEC 62056-21 mode C</option>
                <option value="mode_e">IEC 62056-21 mode E</option>
              </select>
            </label>
            <label>
              Пароль доступа (LLS)
              <br />
              <input
                type="password"
                value={activatePassword}
                onChange={(e) => setActivatePassword(e.target.value)}
              />
            </label>
            <label>
              Местоположение
              <br />
              <input value={activateLocation} onChange={(e) => setActivateLocation(e.target.value)} />
            </label>
          </div>
          <button onClick={handleConfirmActivate}>Активировать</button>{" "}
          <button className="secondary" onClick={() => setActivatingMeter(null)}>
            Отмена
          </button>
        </section>
      )}

      <section className="card">
        <div className="card-header">
          <h2>Активные — {activeRows.length}</h2>
          <div className="btn-group">
            <input
              placeholder="Поиск по номеру"
              value={activeSearchInput}
              onChange={(e) => setActiveSearchInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") setActiveSearch(activeSearchInput);
              }}
              style={{ width: 160 }}
            />
            <button onClick={() => setActiveSearch(activeSearchInput)}>Найти</button>
            <button onClick={load}>Обновить</button>
            <button onClick={handleExportActive} disabled={activeRows.length === 0}>
              Экспорт в Excel
            </button>
          </div>
        </div>
        {meters !== null && activeRows.length === 0 && <p>Активных счётчиков нет.</p>}
        {meters !== null && activeRows.length > 0 && filteredActiveRows.length === 0 && (
          <p>По запросу «{activeSearch}» ничего не найдено.</p>
        )}

        {filteredActiveRows.length > 0 && (
          <table className="data-table">
            <thead>
              <tr>
                <th>Серийный номер</th>
                <th>Физический адрес</th>
                <th>Ампер</th>
                <th>IP-адрес</th>
                <th>Протокол</th>
                <th>Местоположение</th>
                <th>Статус</th>
                <th>Показание</th>
                <th>Дата показания</th>
              </tr>
            </thead>
            <tbody>
              {filteredActiveRows.map((m) => (
                <tr key={m.id} className={readingAgeClass(m.last_read_at)}>
                  <td>
                    <Link to={`/meters/${m.id}`}>{m.serial_number}</Link>
                  </td>
                  <td>{m.physical_address ?? "—"}</td>
                  <td>{m.rated_current_amps ?? "—"}</td>
                  <td>{m.ip_address ? (m.port ? `${m.ip_address}:${m.port}` : m.ip_address) : "—"}</td>
                  <td>{m.protocol_profile ? PROFILE_LABELS[m.protocol_profile] : "—"}</td>
                  <td>{m.location ?? "—"}</td>
                  <td>
                    <span className={`badge ${m.is_online ? "badge-online" : "badge-offline"}`}>
                      {m.is_online ? "online" : "offline"}
                    </span>
                    {!m.is_active && " · неактивен"}
                  </td>
                  <td>{formatValue(m.last_reading_value)}</td>
                  <td>{m.last_read_at ? new Date(m.last_read_at).toLocaleString("ru-RU") : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
