export type UserRole = "operator" | "engineer" | "observer" | "admin" | "super_admin";

export type ProtocolProfile = "mode_c" | "mode_e" | "hdlc_dlms";

// Этап 6 — обнаружение новых счётчиков по call-home. INSTALLED: найден
// автоматически, ещё не активирован (нет пароля/протокола). ACTIVE:
// полностью настроен, доступен для операций. INVALID (2026-09-08):
// серийный номер повреждён при обнаружении (буквы вместо цифр) —
// физический адрес не вычисляется, чтение невозможно, пока не
// исправлен на вкладке «Некорректные данные».
export type MeterStatus = "installed" | "active" | "invalid";

export type GatewayStatus = "pending" | "approved" | "disabled";

export interface Gateway {
  id: number;
  name: string;
  manufacturer: string;
  driver_version: string | null;
  grpc_target: string;
  supported_operations: Record<string, unknown>;
  status: GatewayStatus;
  last_heartbeat_at: string | null;
  call_home_port: number | null;
  call_home_ports: number[] | null;
  is_online: boolean;
  created_at: string;
}

export interface Meter {
  id: number;
  serial_number: string;
  physical_address: string | null;
  ip_address: string | null;
  port: number | null;
  protocol_profile: ProtocolProfile | null;
  location: string | null;
  model: string | null;
  is_active: boolean;
  status: MeterStatus;
  is_online: boolean;
  gateway_id: number;
  last_seen_at: string | null;
  last_read_at: string | null;
  created_at: string;
  last_reading_value: unknown | null;
  rated_current_amps: number | null;
  is_low_consumption: boolean;
  res_name: string | null;
}

// Меню "Архив" -> "РЭСы и Объекты" (2026-09-11) — определяется по
// call-home порту (счётчики физически разведены по РЭС/объектам через
// разные порты Gateway'я).
export interface ResStats {
  res_name: string;
  meters_total: number;
  meters_active: number;
  meters_online: number;
  pct_read_today: number;
  pct_read_3d: number;
}

export interface MeterReading {
  id: number;
  meter_id: number;
  obis_code: string;
  value_json: unknown;
  unit: string | null;
  read_at: string;
}

export interface LoadProfileRow {
  id: number;
  meter_id: number;
  obis_code: string;
  timestamp: string;
  values_json: unknown[];
}

export interface LogEntry {
  id: number;
  category: string;
  raw_code: string | null;
  occurred_at: string;
}

export type JobStatus = "queued" | "running" | "succeeded" | "failed";

export interface Job {
  id: number;
  job_type: string;
  meter_id: number;
  status: JobStatus;
  // read_current -> {obis, value}; write_datetime (Этап 2, ТЗ п.4.2.4) ->
  // {[parameter]: {ok, error}} по каждому записанному OBIS-объекту.
  result: Record<string, unknown> | null;
  error: { code: string; message: string; is_partial?: boolean } | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

// Этап 4 (ТЗ п.4.2.5) — именованная схема параметров.
export interface ParameterSchemeParam {
  parameter: string;
  value: number;
}

export interface ParameterScheme {
  id: number;
  name: string;
  description: string | null;
  parameters: ParameterSchemeParam[];
  created_at: string;
  updated_at: string | null;
}

// Карта OBIS-кодов (по просьбе пользователя, 2026-09-08) — справочник,
// только чтение.
export interface ObisEntry {
  number: string;
  obis: string;
  label: string;
  description: string;
  source: string;
}

// Профили опроса (по просьбе пользователя, 2026-09-08) — именованный
// набор OBIS-кодов с пояснением, что каждый опрашивает, и признаком
// включён/выключен; используется в расписаниях вместо одного OBIS.
export interface PollProfileItem {
  obis: string;
  label: string;
  enabled: boolean;
}

export interface PollProfile {
  id: number;
  name: string;
  description: string | null;
  items: PollProfileItem[];
  created_at: string;
  updated_at: string | null;
}

// Этап 4 (ТЗ п.4.2.6) — расписание автоматического опроса.
export type ScheduledJobType = "read_current" | "read_load_profile";

export interface ScheduledJob {
  id: number;
  name: string;
  cron_expression: string;
  job_type: ScheduledJobType;
  operation_params: Record<string, unknown>;
  meter_ids: number[];
  is_enabled: boolean;
  created_at: string;
  last_run_at: string | null;
  next_run_at: string | null;
}

export type ScheduledJobRunStatus = "running" | "succeeded" | "partial_failure" | "failed";

export interface ScheduledJobRun {
  id: number;
  scheduled_job_id: number;
  status: ScheduledJobRunStatus;
  meters_total: number;
  meters_succeeded: number;
  meters_failed: number;
  started_at: string;
  finished_at: string | null;
}

// Ежедневный экспорт профиля нагрузки (Profile1) во внешнюю БД ЕЭБД
// (2026-09-14, по прямому указанию пользователя).
export type EudbExportRunStatus = "running" | "succeeded" | "partial_failure" | "failed";

export interface EudbExportRun {
  id: number;
  status: EudbExportRunStatus;
  meters_total: number;
  meters_succeeded: number;
  meters_failed: number;
  triggered_manually: boolean;
  started_at: string;
  finished_at: string | null;
  error_message: string | null;
}

export interface EudbExportItem {
  id: number;
  meter_id: number;
  meter_serial: string;
  ok: boolean;
  rows_exported: number;
  error_message: string | null;
  created_at: string;
}

// Этап 4 (ТЗ п.4.2.8) — уведомления.
export type NotificationCategory = "meter_offline" | "tamper_event" | "scheduled_job_failed";

export interface Notification {
  id: number;
  category: NotificationCategory;
  message: string;
  meter_id: number | null;
  scheduled_job_id: number | null;
  details: Record<string, unknown> | null;
  is_read: boolean;
  created_at: string;
}
