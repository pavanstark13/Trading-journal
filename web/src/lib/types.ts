/** Shapes the backend returns. Money and prices are strings so nothing rounds. */

export type Role = "ADMIN" | "MEMBER";

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

export interface AccountRow {
  id: string;
  label: string;
  broker_name: string | null;
  mt5_login: number;
  broker_server: string;
  currency: string;
  margin_mode: "hedging" | "netting";
  starting_balance: string | null;
  balance: string | null;
  equity: string | null;
  open_positions: number | null;
  last_heartbeat_at: string | null;
  connected: boolean;
  sync_status: string;
  sync_error: string | null;
  deal_count: number;
  trade_count: number;
  is_archived: boolean;
  ea: {
    installation_id: string;
    status: string;
    ea_version: string | null;
    terminal_build: number | null;
    last_seen_at: string | null;
    awaiting_setup: boolean;
  } | null;
}

export interface TradeRow {
  id: string;
  account_id: string;
  trade_key: string;
  symbol: string;
  direction: "long" | "short";
  status: "open" | "closed";
  opened_at: string;
  closed_at: string | null;
  volume_opened: string;
  volume_closed: string;
  avg_entry_price: string | null;
  avg_exit_price: string | null;
  initial_sl: string | null;
  initial_tp: string | null;
  gross_profit: string;
  commission: string;
  swap: string;
  net_profit: string;
  risk_amount: string | null;
  r_multiple: string | null;
  pips: string | null;
  duration_seconds: number | null;
  exit_reason: string | null;
  session: string | null;
  hour_of_day: number | null;
  day_of_week: number | null;
  trade_date: string | null;
  pl_provisional: boolean;
  has_journal?: boolean;
  tags?: string[];
}

export interface JournalData {
  thesis: string | null;
  execution_notes: string | null;
  lesson: string | null;
  emotion: string | null;
  confidence: number | null;
  followed_plan: boolean | null;
  mistakes: string[];
  grade: string | null;
  setup_id: string | null;
  setup_name: string | null;
  updated_at: string;
}

export interface TradeDetail extends TradeRow {
  account_label: string;
  currency: string;
  legs: {
    seq: number;
    type: "entry" | "exit";
    deal_ticket: number;
    volume: string;
    price: string;
    time_msc: number;
  }[];
  journal: JournalData | null;
  screenshots: { id: string; kind: string; timeframe: string | null; caption: string | null }[];
}

export interface Summary {
  trades: number;
  wins: number;
  losses: number;
  scratches: number;
  win_rate: string | null;
  net_profit: string;
  gross_win: string;
  gross_loss: string;
  profit_factor: string | null;
  expectancy_r: string | null;
  avg_r: string | null;
  avg_win: string | null;
  avg_loss: string | null;
  largest_win: string | null;
  largest_loss: string | null;
  max_drawdown: string;
  max_drawdown_pct: string | null;
  longest_win_streak: number;
  longest_loss_streak: number;
  trades_without_stop: number;
  avg_duration_seconds: number | null;
  sqn: string | null;
  /** True when there are too few trades for any of this to mean much. */
  low_confidence: boolean;
  sample_size: number;
  min_meaningful_sample: number;
}

export interface Bucket {
  key: string;
  trades: number;
  net_profit: string;
  win_rate: string | null;
  expectancy_r: string | null;
  low_confidence: boolean;
}

export interface BucketStats {
  trades: number;
  net_profit: string;
  win_rate: string | null;
  expectancy_r: string | null;
  low_confidence: boolean;
}

export interface Behaviour {
  after_a_loss: { after_loss: BucketStats; otherwise: BucketStats };
  plan_adherence: {
    followed: BucketStats;
    deviated: BucketStats;
    unjournalled: number;
  };
}

export interface Overview {
  summary: Summary;
  by_symbol: Bucket[];
  by_hour: Bucket[];
  by_weekday: Bucket[];
  curves: {
    equity: { at: string; equity: string; drawdown: string; net_profit: string; trade_key: string }[];
    daily: { date: string; trades: number; net_profit: string; wins: number; losses: number }[];
  };
  behaviour: Behaviour;
}

export interface Setup {
  id: string;
  name: string;
  description: string | null;
  checklist: string[];
  color: string | null;
}

export interface TagRow {
  id: string;
  name: string;
  color: string | null;
}
