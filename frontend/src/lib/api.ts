const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";
const TOKEN_KEY = "poly_jwt";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null) {
  if (typeof window === "undefined") return;
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export async function apiFetch<T = unknown>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const token = getToken();
  const headers = new Headers(init?.headers);
  if (init?.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });

  if (res.status === 204) return undefined as T;

  const text = await res.text();
  const data = text ? JSON.parse(text) : null;

  if (!res.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : res.statusText;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

// ── Types ──────────────────────────────────────────────────────────
export interface AuthState {
  setup_required: boolean;
  unlocked: boolean;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface WalletView {
  address: string | null;
  has_credentials: boolean;
  has_api_key: boolean;
  funder_address: string | null;
  usdc_balance: number | null;
}

export interface WalletPayload {
  private_key: string;
  api_key?: string | null;
  api_secret?: string | null;
  funder_address?: string | null;
}

export interface EventNode {
  id: string;
  slug: string;
  title: string;
  start_date: string | null;
  end_date: string | null;
  market_count: number;
  volume_24h: number;
  liquidity: number;
}

export interface LeagueNode {
  id: string;
  label: string;
  events: EventNode[];
  event_count: number;
}

export interface SportNode {
  id: string;
  label: string;
  leagues: LeagueNode[];
  event_count: number;
}

export interface MarketTree {
  sports: SportNode[];
  total_events: number;
}

export type FilterLevel = "sport" | "league" | "event";

export interface FilterRef {
  level: FilterLevel;
  identifier: string;
  display_name: string;
}

export interface FiltersResponse {
  tree: MarketTree | null;
  tree_age_seconds: number | null;
  tree_event_count: number;
  selected: FilterRef[];
}

export interface WatcherStatus {
  connected: boolean;
  connected_at: string | null;
  last_disconnect_at: string | null;
  last_disconnect_reason: string | null;
  subscribed_tokens: number;
  subscribed_events: number;
  subscription_truncated: boolean;
  total_messages: number;
  updates_per_min: number;
  last_message_at: string | null;
}

export type ScraperHealth = "ok" | "degraded" | "offline";

export interface ScraperStatus {
  name: string;
  health: ScraperHealth;
  last_run_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  consecutive_failures: number;
  interval_s: number;
  total_runs: number;
  total_failures: number;
  total_snapshots_published: number;
  snapshots_last_run: number;
  last_latency_ms: number | null;
  runs_per_min: number;
}
