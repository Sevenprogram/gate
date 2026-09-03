import ReactECharts from "echarts-for-react";
import type { DailyRow } from "./api";
import { money, signed } from "./format";
import { plColors, usePlMode } from "./palette";

/* ECharts replacements for the hand-drawn SVG charts.
 *
 * Tooltip, axis pointers, and zoom are the library's — a large part of why
 * this layer moved off hand-rolled SVG. */

export interface EquityPoint {
  day: string;
  equity: number;
  cashflow: number;
  pnl: number | null;
}

const SERIES_COLORS = {
  blue: "#1677ff",
  orange: "#fa8c16",
  green: "#52c41a",
  gold: "#faad14",
};

function axisLabelCompact(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(value / 1e3).toFixed(0)}K`;
  return String(value);
}

const GRID = { left: 10, right: 18, top: 32, bottom: 8, containLabel: true };
const SPLIT_LINE = { lineStyle: { color: "rgba(0, 0, 0, 0.08)" } };
const AXIS_LABEL = { color: "#6b7280", fontSize: 10 };

/** Equity over time; orange dots mark days money moved in or out. */
export function EquityChart({
  points,
  height = 240,
}: {
  points: EquityPoint[];
  height?: number;
}) {
  if (points.length === 0) {
    return <div className="chart-empty">还没有权益快照。点一次同步就会记录今天的。</div>;
  }

  const option = {
    grid: GRID,
    tooltip: {
      trigger: "axis" as const,
      valueFormatter: (v: number) => money(v),
    },
    xAxis: {
      type: "category" as const,
      data: points.map((p) => p.day),
      boundaryGap: false,
      axisLabel: AXIS_LABEL,
    },
    yAxis: {
      type: "value" as const,
      scale: true,
      axisLabel: { formatter: axisLabelCompact, ...AXIS_LABEL },
      splitLine: SPLIT_LINE,
    },
    series: [
      {
        name: "权益",
        type: "line" as const,
        data: points.map((p) => p.equity),
        showSymbol: false,
        lineStyle: { width: 2 },
        itemStyle: { color: SERIES_COLORS.blue },
        areaStyle: { opacity: 0.07 },
      },
      {
        name: "出入金日",
        type: "scatter" as const,
        data: points
          .filter((p) => p.cashflow !== 0)
          .map((p) => [p.day, p.equity, p.cashflow]),
        symbolSize: 10,
        itemStyle: { color: SERIES_COLORS.orange },
        tooltip: {
          formatter: (p: { value: [string, number, number] }) =>
            `${p.value[0]} 出入金 ${signed(p.value[2])}`,
        },
      },
    ],
  };

  return <ReactECharts option={option} style={{ height }} notMerge lazyUpdate />;
}

/**
 * Daily PnL as signed columns with the ledger's realized net overlaid.
 * Position relative to the zero axis is the primary sign channel; colour
 * reinforces it.
 */
export function PnlChart({
  rows,
  height = 240,
}: {
  rows: DailyRow[];
  height?: number;
}) {
  usePlMode();
  const colors = plColors();
  const usable = rows.filter(
    (r) => r.pnl_equity != null || Number.isFinite(r.net_realized),
  );

  if (usable.length === 0) {
    return (
      <div className="chart-empty">
        还没有可算盈亏的日子。需要至少两天的权益快照才能算出日间变化。
      </div>
    );
  }

  const option = {
    grid: GRID,
    legend: { top: 0 },
    tooltip: {
      trigger: "axis" as const,
      valueFormatter: (v: number) => signed(v),
    },
    xAxis: {
      type: "category" as const,
      data: usable.map((r) => r.day),
      axisLabel: AXIS_LABEL,
    },
    yAxis: {
      type: "value" as const,
      axisLabel: { formatter: axisLabelCompact, ...AXIS_LABEL },
      splitLine: SPLIT_LINE,
    },
    series: [
      {
        name: "权益口径盈亏",
        type: "bar" as const,
        barMaxWidth: 24,
        data: usable.map((r) => ({
          value: r.pnl_equity ?? 0,
          itemStyle: {
            color: (r.pnl_equity ?? 0) >= 0 ? colors.profit : colors.loss,
          },
        })),
      },
      {
        name: "账本已实现净额",
        type: "line" as const,
        data: usable.map((r) => r.net_realized),
        showSymbol: false,
        lineStyle: { width: 2, color: "rgba(128, 128, 128, 0.9)" },
        itemStyle: { color: "rgba(128, 128, 128, 0.9)" },
      },
    ],
  };

  return <ReactECharts option={option} style={{ height }} notMerge lazyUpdate />;
}

export interface BarItem {
  label: string;
  value: number;
  color: string;
}

/** Horizontal bars — cost composition, per-account equity. */
export function HBarChart({
  items,
  height,
  signedValues = true,
}: {
  items: BarItem[];
  height?: number;
  signedValues?: boolean;
}) {
  if (items.length === 0) {
    return <div className="chart-empty">这个区间没有记录。</div>;
  }
  const rows = height ?? Math.max(120, items.length * 44 + 40);

  const option = {
    grid: { left: 8, right: 64, top: 8, bottom: 8, containLabel: true },
    tooltip: { trigger: "item" as const },
    xAxis: { type: "value" as const, splitLine: SPLIT_LINE, axisLabel: AXIS_LABEL },
    yAxis: {
      type: "category" as const,
      data: items.map((i) => i.label),
      inverse: true,
      axisLabel: { color: "#4b5563", fontSize: 11 },
    },
    series: [
      {
        type: "bar" as const,
        barMaxWidth: 20,
        data: items.map((i) => ({
          value: i.value,
          itemStyle: { color: i.color },
        })),
        label: {
          show: true,
          position: "right" as const,
          formatter: (p: { value: number }) =>
            signedValues ? signed(p.value) : money(p.value, 0),
        },
      },
    ],
  };

  return <ReactECharts option={option} style={{ height: rows }} notMerge lazyUpdate />;
}

/** Cost per period — the fee panel's main chart. */
export function PeriodBarChart({
  labels,
  values,
  height = 220,
}: {
  labels: string[];
  values: number[];
  height?: number;
}) {
  if (values.length === 0) {
    return <div className="chart-empty">这个区间没有费用记录。</div>;
  }

  const option = {
    grid: GRID,
    tooltip: {
      trigger: "axis" as const,
      valueFormatter: (v: number) => money(Math.abs(v)),
    },
    xAxis: { type: "category" as const, data: labels, axisLabel: AXIS_LABEL },
    yAxis: {
      type: "value" as const,
      axisLabel: { formatter: axisLabelCompact, ...AXIS_LABEL },
      splitLine: SPLIT_LINE,
    },
    series: [
      {
        name: "成本",
        type: "bar" as const,
        barMaxWidth: 24,
        data: values,
        itemStyle: { color: SERIES_COLORS.blue },
      },
    ],
  };

  return <ReactECharts option={option} style={{ height }} notMerge lazyUpdate />;
}
