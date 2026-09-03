/** Typed client for the local FastAPI backend. Gate is never contacted directly. */

export type AccountKey = "spot" | "futures_usdt" | "futures_btc" | "unified";

/** Tab labels. Lives beside AccountKey rather than in views.tsx, so that module
 *  exports only components and Fast Refresh can hot-swap it. */
export const ACCOUNT_LABELS: Record<AccountKey, string> = {
  spot: "现货",
  futures_usdt: "U 本位合约",
  futures_btc: "币本位合约",
  unified: "统一账户",
};

/** One set of API credentials — one Gate account or sub-account. */
export interface ProfileInfo {
  id: string;
  label: string;
  accounts: AccountKey[];
  has_credentials: boolean;
}

export interface AppConfig {
  profiles: ProfileInfo[];
  /** (profile, account) pairs that share the quote currency and can be summed. */
  aggregatable: [string, AccountKey][];
  excluded_from_portfolio: { profile: string; account: AccountKey }[];
  timezone: string;
  quote: string;
  has_credentials: boolean;
  /** True when the backend is serving fabricated snapshots (DASHBOARD_DEMO=1). */
  demo: boolean;
  last_sync: string | null;
  today: string;
}

export interface Position {
  contract: string;
  side: "long" | "short";
  size: number;
  value: number;
  leverage: number;
  entry_price: number;
  mark_price: number;
  liq_price: number;
  liq_distance_pct: number | null;
  margin: number;
  maintenance_rate: number;
  unrealised_pnl: number;
  realised_pnl: number;
  mode: string | null;
}

export interface Holding {
  currency: string;
  available: number;
  locked: number;
  total: number;
  price: number | null;
  value: number | null;
}

export interface UnifiedBalance {
  currency: string;
  available: number;
  freeze: number;
  borrowed: number;
  interest: number;
  equity: number;
  unrealised_pnl: number;
}

export interface FeeTier {
  maker: number;
  taker: number;
  base_maker: number;
  base_taker: number;
  gt_discount: boolean;
  point_type: string | null;
  futures_maker: number;
  futures_taker: number;
}

export interface Snapshot {
  demo?: boolean;
  account_type: string;
  currency: string;
  equity: number;
  wallet_balance: number;
  unrealised_pnl: number;
  available: number;
  position_margin: number;
  order_margin?: number;
  maintenance_margin?: number;
  margin_ratio?: number;
  maintenance_ratio?: number;
  in_dual_mode?: boolean;
  positions?: Position[];
  holdings?: Holding[];
  balances?: UnifiedBalance[];
  unpriced?: string[];
  fee_tier?: FeeTier | null;
  liabilities?: number;
  leverage?: number;
  accrued_interest?: number;
  risk_units?: unknown[];
  lifetime?: {
    realized_pnl: number;
    fee: number;
    funding: number;
    rebate: number;
    net_transfer: number;
    net_pnl: number;
  };
}

export interface DailyRow {
  day: string;
  realized: number;
  fee: number;
  funding: number;
  rebate: number;
  interest: number;
  cashflow: number;
  bonus: number;
  unclassified: number;
  unclassified_types: string[];
  gross_realized: number;
  ledger_entries: number;
  trade_count: number;
  volume: number;
  maker_count: number;
  taker_count: number;
  cashflow_net: number;
  equity_close: number | null;
  unrealised_close: number | null;
  net_realized: number;
  fee_to_gross_ratio: number | null;
  maker_ratio: number | null;
  equity_delta: number | null;
  pnl_equity: number | null;
  return_pct: number | null;
  spans_days: number | null;
  unrealised_change: number | null;
  /** How many of the summed accounts reported a snapshot that day. */
  scopes_reported: number;
  scopes_expected: number;
  /** False when an account is missing, in which case there is no PnL. */
  complete: boolean;
}

export interface Stats {
  days: number;
  /** Days skipped because not every included account reported a snapshot. */
  partial_days: number;
  total_return: number | null;
  annualized_return?: number | null;
  max_drawdown: number | null;
  sharpe: number | null;
  calmar: number | null;
  win_rate: number | null;
  profit_factor: number | null;
  best_day: { day: string; pnl: number } | null;
  worst_day: { day: string; pnl: number } | null;
  equity_index?: number[];
}

export interface Costs {
  fee: number;
  funding: number;
  rebate: number;
  interest: number;
  gross_realized: number;
  total_cost: number;
  cost_to_gross_ratio: number | null;
  volume: number;
  effective_fee_rate: number | null;
  maker_count: number;
  taker_count: number;
  maker_ratio: number | null;
  trade_count: number;
}

export interface DailyPayload {
  scopes: [string, AccountKey][];
  quote: string;
  timezone: string;
  series: DailyRow[];
  stats: Stats;
  costs: Costs;
}

export interface MarketRow {
  market: string;
  trade_count: number;
  volume: number;
  fee: number;
  maker_ratio: number | null;
  realized_pnl: number | null;
  position_closes: number | null;
}

export interface SyncResult {
  synced_at: number;
  window_days: number;
  profiles: string[];
  steps: Record<string, unknown>;
  errors: string[];
}

/** One account's slice of the portfolio, live. */
export interface PortfolioPart {
  profile: string;
  profile_label: string;
  account: AccountKey;
  equity: number;
  unrealised_pnl: number;
  available: number;
  position_margin: number;
  maintenance_margin: number;
  liabilities: number;
  position_count: number;
  nearest_liq: number | null;
}

export interface PortfolioSnapshot {
  demo: boolean;
  currency: string;
  equity: number;
  unrealised_pnl: number;
  available: number;
  position_margin: number;
  maintenance_margin: number;
  liabilities: number;
  margin_ratio: number;
  maintenance_ratio: number;
  position_count: number;
  nearest_liq: number | null;
  parts: PortfolioPart[];
  excluded: { profile: string; account: string; reason: string }[];
  errors: string[];
}

/** Per-account comparison over the selected window. */
export interface ContributionRow {
  profile: string;
  account: AccountKey;
  equity: number | null;
  equity_day: string | null;
  period_pnl: number;
  net_realized: number;
  fee: number;
  funding: number;
  volume: number;
  trade_count: number;
  maker_ratio: number | null;
  cost_to_gross_ratio: number | null;
  total_return: number | null;
  max_drawdown: number | null;
  sharpe: number | null;
  days: number;
  pnl_share: number | null;
}

/** One period's fee/cost aggregation. Field names follow the ledger buckets. */
export interface FeePeriod {
  period: string;
  granularity: "day" | "week" | "month";
  member_days: number;
  days_with_pnl: number;
  incomplete_days: number;
  fee: number;
  funding: number;
  interest: number;
  volume: number;
  trade_count: number;
  maker_count: number;
  taker_count: number;
  net_realized: number;
  realized: number;
  pnl_equity: number;
  cashflow_net: number;
  equity_close: number | null;
  maker_ratio: number | null;
  effective_fee_rate: number | null;
  pnl_is_partial: boolean;
  /** |fee| × 70% — the main rebate-share caliber. */
  fee_70: number;
  fee_10: number;
  fee_05: number;
  /** Ledger rebate bucket (futures refr etc.) — a fee-side reduction. */
  ledger_rebate: number;
  /** Actual rebate landed in the same profile's spot book. */
  rebate: number;
  /** fee_70 − rebate: the part of the 70% expectation not yet landed. */
  expected_extra: number;
}

export interface FeeRollup {
  fee: number;
  funding: number;
  interest: number;
  total_cost: number;
  volume: number;
  trade_count: number;
  maker_count: number;
  taker_count: number;
  maker_ratio: number | null;
  effective_fee_rate: number | null;
  cost_to_gross_ratio: number | null;
  period_count: number;
  fee_70: number;
  fee_10: number;
  fee_05: number;
  ledger_rebate: number;
  rebate: number;
  expected_extra: number;
}

export interface FeeSummary {
  granularity: "day" | "week" | "month";
  /** Hour (dashboard tz) at which a period begins: 0 or 8. */
  boundary_hour: number;
  from_day: string | null;
  to_day: string | null;
  periods: FeePeriod[];
  total: FeeRollup;
  per_account: Array<{
    profile: string;
    account: AccountKey;
  } & FeeRollup>;
}

export type FeeGranularity = "day" | "week" | "month";

/** One closed position, from the per-account close history. */
export interface PositionHistoryRow {
  ts: number;
  day: string;
  contract: string | null;
  side: string | null;
  pnl: number | null;
  pnl_pnl: number | null;
  pnl_fee: number | null;
  pnl_fund: number | null;
  first_open_time: number | null;
  max_size: number | null;
  long_price: number | null;
  short_price: number | null;
  text: string | null;
}

/** One spot/futures ledger entry. */
export interface LedgerRow {
  entry_id: string;
  ts: number;
  day: string;
  type: string;
  change: number;
  balance: number | null;
  currency: string | null;
  text: string | null;
}

export interface RiskEvents {
  liquidations: unknown[];
  auto_deleverages: unknown[];
  errors: string[];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // non-JSON error body; the status is all we have
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export interface ProfileCreatePayload {
  id: string;
  label: string;
  key: string;
  secret: string;
  accounts: AccountKey[];
}

const scoped = (profile: string, account: AccountKey) =>
  `/api/profiles/${encodeURIComponent(profile)}/accounts/${account}`;

/** One sheet of the generated Excel report, parsed for web display. */
export interface ReportSheet {
  name: string;
  block_labels: string[] | null;
  headers: string[];
  rows: Array<Array<string | number | null>>;
}

export const api = {
  config: () => call<AppConfig>("/api/config"),
  reportSheets: (days: number, refresh = false) =>
    call<{ days: number; cached: boolean; sheets: ReportSheet[] }>(
      `/api/report/sheets?days=${days}${refresh ? "&refresh=1" : ""}`,
    ),
  sync: (days: number) =>
    call<SyncResult>(`/api/sync?days=${days}`, { method: "POST" }),

  // Account management. Adding verifies the credentials against Gate first;
  // a bad key fails with Gate's own error message.
  listProfiles: () => call<ProfileInfo[]>("/api/profiles"),
  addProfile: (payload: ProfileCreatePayload) =>
    call<ProfileInfo & { note?: string }>("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  updateProfile: (profileId: string, accounts: AccountKey[], label?: string) =>
    call<ProfileInfo & { note?: string }>(
      `/api/profiles/${encodeURIComponent(profileId)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ accounts, label: label ?? "" }),
      },
    ),
  deleteProfile: (profileId: string) =>
    call<{ removed: string; note?: string }>(
      `/api/profiles/${encodeURIComponent(profileId)}`,
      { method: "DELETE" },
    ),

  // Single (profile, account) slice.
  snapshot: (profile: string, account: AccountKey) =>
    call<Snapshot>(`${scoped(profile, account)}/snapshot`),
  daily: (profile: string, account: AccountKey, days: number) =>
    call<DailyPayload>(`${scoped(profile, account)}/daily?days=${days}`),
  markets: (profile: string, account: AccountKey, days: number) =>
    call<MarketRow[]>(`${scoped(profile, account)}/markets?days=${days}`),
  risk: (profile: string, account: AccountKey) =>
    call<RiskEvents>(`${scoped(profile, account)}/risk`),

  // Fee totals per period — day, ISO week, or a custom date window.
  feeSummary: (
    scope: { profile: string; account: AccountKey } | null,
    params: {
      period: FeeGranularity;
      from_day?: string;
      to_day?: string;
      days?: number;
      boundary?: number;
    },
  ) => {
    const qs = new URLSearchParams({ period: params.period });
    if (params.from_day) qs.set("from_day", params.from_day);
    if (params.to_day) qs.set("to_day", params.to_day);
    if (params.days) qs.set("days", String(params.days));
    if (params.boundary) qs.set("boundary", String(params.boundary));
    const base = scope
      ? `${scoped(scope.profile, scope.account)}/fee_summary`
      : "/api/portfolio/fee_summary";
    return call<FeeSummary>(`${base}?${qs.toString()}`);
  },

  ledger: (profile: string, account: AccountKey, limit = 2000) =>
    call<LedgerRow[]>(
      `${scoped(profile, account)}/ledger?limit=${limit}`,
    ),

  positionHistory: (
    profile: string,
    account: AccountKey,
    market?: string,
  ) => {
    const qs = market ? `?market=${encodeURIComponent(market)}` : "";
    return call<PositionHistoryRow[]>(
      `${scoped(profile, account)}/position_history${qs}`,
    );
  },

  // Everything summed across accounts that share the quote currency.
  portfolioSnapshot: () =>
    call<PortfolioSnapshot>("/api/portfolio/snapshot"),
  portfolioDaily: (days: number) =>
    call<DailyPayload>(`/api/portfolio/daily?days=${days}`),
  contribution: (days: number) =>
    call<ContributionRow[]>(`/api/portfolio/contribution?days=${days}`),
  portfolioMarkets: (days: number) =>
    call<MarketRow[]>(`/api/portfolio/markets?days=${days}`),
};
