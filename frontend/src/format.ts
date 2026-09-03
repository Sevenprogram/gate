/** Number formatting. Kept in one place so every panel reads consistently. */

const DASH = "—";

export function money(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** Signed, for anything that is a profit or a loss. The sign is the accessible
 *  channel: color reinforces it but never carries it alone. */
export function signed(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${money(Math.abs(value), digits)}`;
}

export function compact(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${(value / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(value / 1e6).toFixed(2)}M`;
  if (abs >= 1e4) return `${(value / 1e3).toFixed(1)}K`;
  return money(value, abs >= 100 ? 0 : 2);
}

export function percent(
  value: number | null | undefined,
  digits = 2,
): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return `${(value * 100).toFixed(digits)}%`;
}

export function signedPercent(
  value: number | null | undefined,
  digits = 2,
): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${(Math.abs(value) * 100).toFixed(digits)}%`;
}

/** Fee rates are small; show them in basis points where that reads better. */
export function rate(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return `${(value * 100).toFixed(4)}%`;
}

export function ratio(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return value.toFixed(digits);
}

export function polarity(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value === 0) return "flat";
  return value > 0 ? "profit" : "loss";
}

export function shortDay(day: string): string {
  return day.slice(5); // MM-DD
}

export function clockFromUnix(ts: number | string | null): string {
  if (ts == null) return DASH;
  const seconds = typeof ts === "string" ? Number(ts) : ts;
  if (!Number.isFinite(seconds)) return DASH;
  return new Date(seconds * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function integer(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return DASH;
  return Math.round(value).toLocaleString("en-US");
}
