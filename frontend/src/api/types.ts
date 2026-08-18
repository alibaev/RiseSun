export type UserRole = "operator" | "engineer" | "observer" | "admin" | "super_admin";

export type ProtocolProfile = "mode_c" | "mode_e" | "hdlc_dlms";

export interface Meter {
  id: number;
  serial_number: string;
  ip_address: string;
  port: number;
  protocol_profile: ProtocolProfile;
  location: string | null;
  model: string | null;
  is_active: boolean;
  is_online: boolean;
  gateway_id: number;
  last_seen_at: string | null;
  last_read_at: string | null;
  created_at: string;
}

export interface MeterReading {
  id: number;
  meter_id: number;
  obis_code: string;
  value_json: unknown;
  unit: string | null;
  read_at: string;
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
