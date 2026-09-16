/** Shapes returned by the backend. Money and prices are strings by design. */

export type Role = "SUPER_ADMIN" | "ADMIN" | "MEMBER";
export type Health = "ONLINE" | "WARNING" | "OFFLINE" | "UNKNOWN";
export type CopyStatus =
  | "PENDING" | "SENT" | "EXECUTED" | "FAILED"
  | "REJECTED" | "CANCELLED" | "TIMED_OUT";

export interface User {
  id: string;
  email: string;
  full_name: string | null;
  role: Role;
  timezone: string;
  totp_enabled?: boolean;
}

export interface LoginResponse {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  user: User;
}

export interface DashboardData {
  system: { mode: "PAPER" | "LIVE"; copying_paused: boolean; emergency_stop: boolean };
  health: Record<string, Health>;
  master: {
    id: string;
    label: string;
    balance: string | null;
    equity: string | null;
    open_positions: number | null;
    last_heartbeat_at: string | null;
  } | null;
  counts: {
    members: number;
    members_connected: number;
    trades_today: number;
    copies_successful: number;
    copies_failed: number;
    copies_rejected: number;
    dead_letters: number;
  };
  recent_events: TradeEventRow[];
}

export interface TradeEventRow {
  id: string;
  event_id?: string;
  event_type: string;
  symbol: string | null;
  side: string | null;
  volume: string | null;
  price: string | null;
  stop_loss?: string | null;
  take_profit?: string | null;
  processing_status: string;
  ignore_reason?: string | null;
  occurred_at: string;
  received_at?: string;
  telegram_status?: string;
  copy_summary?: { total: number; executed: number; failed: number; rejected: number };
}

export interface TimelineEntry {
  stage: string;
  status: "OK" | "RETRY" | "FAIL";
  message: string | null;
  meta: Record<string, unknown> | null;
  at: string;
  copy_order_id: string | null;
}

export interface CopyOrderRow {
  id: string;
  trade_event_id: string;
  member_account_id: string;
  action: string;
  symbol: string;
  side: string | null;
  requested_lot: string;
  calculated_lot: string;
  final_lot: string;
  sizing_mode: string | null;
  status: CopyStatus;
  master_price: string | null;
  execution_price: string | null;
  slippage_points: string | null;
  broker_ticket: number | null;
  broker_retcode: number | null;
  reject_reason: string | null;
  reject_detail: string | null;
  latency_ms: number | null;
  is_paper: boolean;
  created_at: string;
  executed_at: string | null;
}

export interface MemberRow {
  id: string;
  label: string;
  email: string | null;
  mt5_login: number;
  broker_server: string;
  currency: string;
  mode: "PAPER" | "LIVE";
  status: string;
  balance: string | null;
  equity: string | null;
  open_positions: number | null;
  last_heartbeat_at: string | null;
  ea_online: boolean;
  copy_enabled: boolean;
  sizing_mode: string | null;
}

export interface MasterAccountRow {
  id: string;
  label: string;
  mt5_login: number;
  broker_server: string;
  currency: string;
  leverage?: number | null;
  margin_mode: string;
  balance: string | null;
  equity: string | null;
  margin: string | null;
  free_margin: string | null;
  open_positions: number | null;
  connection: Health;
  last_heartbeat_at: string | null;
  publish_enabled: boolean;
  copy_enabled: boolean;
  magic_filter: number[] | null;
  symbol_filter: string[] | null;
  ea?: {
    installation_id: string;
    status: string;
    ea_version: string | null;
    terminal_build: number | null;
    last_seen_at: string | null;
  } | null;
  last_event?: { event_type: string; symbol: string; occurred_at: string } | null;
}

export interface TelegramChannelRow {
  id: string;
  label: string;
  chat_id: string;
  is_enabled: boolean;
  publish_types: string[];
  display_timezone: string;
  edit_in_place: boolean;
  last_ok_at: string | null;
  last_error: string | null;
  has_token: boolean;
}

export interface EaInstallationRow {
  id: string;
  kind: "MASTER" | "MEMBER";
  status: string;
  connection: Health;
  master_account_id: string | null;
  member_account_id: string | null;
  ea_version: string | null;
  terminal_build: number | null;
  last_seen_at: string | null;
  last_seen_ip: string | null;
  pending_install_code: boolean;
}

export interface AuditRow {
  id: number;
  actor_user_id: string | null;
  actor_type: string;
  action: string;
  entity_type: string | null;
  entity_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  ip: string | null;
  at: string;
}

export interface RejectionSummary {
  window_hours: number;
  total: number;
  by_reason: { reason: string; count: number }[];
  by_member: { member_account_id: string; count: number }[];
  recent: {
    id: string;
    member_account_id: string;
    symbol: string;
    reason: string;
    detail: string;
    created_at: string;
  }[];
}

export interface SimulationResult {
  signal: Record<string, unknown>;
  system: { emergency_stop: boolean; copying_paused: boolean };
  members: {
    member_account_id: string;
    label: string;
    mode: string;
    symbol: string;
    calculated_lot?: string;
    final_lot?: string;
    /** "broker" when the member's terminal reported real contract specs, else "fallback". */
    spec_source?: string;
    would_copy?: boolean;
    reason?: string | null;
    detail?: string | null;
  }[];
  summary: { total: number; would_copy: number };
}

export interface WsMessage {
  type:
    | "trade.event" | "copy.update" | "ea.status"
    | "master.telemetry" | "system.alert" | "health" | "pong";
  data: Record<string, unknown>;
}
