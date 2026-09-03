import { Progress, Table, Tag } from "antd";
import type { ColumnsType } from "antd/es/table";
import type {
  ContributionRow,
  DailyRow,
  Holding,
  LedgerRow,
  MarketRow,
  Position,
  PositionHistoryRow,
  UnifiedBalance,
} from "./api";
import { ACCOUNT_LABELS } from "./api";
import {
  clockFromUnix,
  integer,
  money,
  percent,
  ratio,
  signed,
} from "./format";
import { plColors, usePlMode } from "./palette";

/* antd tables for every dataset the dashboard shows. Sorting and filtering
 * come from the column definitions — they were the point of this rewrite. */

function usePlText() {
  usePlMode();
  const colors = plColors();
  return (value: number | null | undefined) => ({
    style: {
      color:
        value == null || !Number.isFinite(value) || value === 0
          ? undefined
          : value > 0
            ? colors.profit
            : colors.loss,
    },
    children: signed(value),
  });
}

const PAGE = { pageSize: 30, hideOnSinglePage: true, showSizeChanger: false };

/** Table twin of the PnL charts. */
export function DailyTable({ rows }: { rows: DailyRow[] }) {
  const plText = usePlText();
  const columns: ColumnsType<DailyRow> = [
    {
      title: "日期",
      dataIndex: "day",
      render: (day: string, row) => (
        <>
          {day}
          {row.spans_days != null && row.spans_days > 1 && (
            <Tag style={{ marginLeft: 6 }}>{row.spans_days} 天</Tag>
          )}
          {row.unclassified_types.length > 0 && (
            <Tag color="warning" style={{ marginLeft: 6 }}>
              未分类
            </Tag>
          )}
        </>
      ),
    },
    { title: "收盘权益", dataIndex: "equity_close", align: "right", render: (v) => money(v) },
    {
      title: "权益口径盈亏",
      dataIndex: "pnl_equity",
      align: "right",
      render: (v: number | null) => <span {...plText(v)} />,
    },
    {
      title: "已实现净额",
      dataIndex: "net_realized",
      align: "right",
      render: (v: number) => <span {...plText(v)} />,
    },
    {
      title: "浮动变化",
      dataIndex: "unrealised_change",
      align: "right",
      render: (v: number | null) => <span {...plText(v)} />,
    },
    { title: "手续费", dataIndex: "fee", align: "right", render: (v: number) => signed(v) },
    { title: "资金费", dataIndex: "funding", align: "right", render: (v: number) => signed(v) },
    {
      title: "出入金",
      dataIndex: "cashflow_net",
      align: "right",
      render: (v: number) => (v === 0 ? "—" : signed(v)),
    },
    { title: "笔数", dataIndex: "trade_count", align: "right", render: (v: number) => integer(v) },
    { title: "maker", dataIndex: "maker_ratio", align: "right", render: (v: number | null) => percent(v, 0) },
    { title: "费/毛利", dataIndex: "fee_to_gross_ratio", align: "right", render: (v: number | null) => percent(v, 1) },
  ];

  return (
    <Table<DailyRow>
      size="small"
      rowKey="day"
      columns={columns}
      dataSource={[...rows].reverse()}
      pagination={PAGE}
      scroll={{ y: 480 }}
    />
  );
}

/** Open positions; the liquidation distance is a progress bar that turns red
 * as the headroom shrinks. */
export function PositionsTable({ positions }: { positions: Position[] }) {
  const plText = usePlText();
  const ordered = [...positions].sort(
    (a, b) => (a.liq_distance_pct ?? 9) - (b.liq_distance_pct ?? 9),
  );

  const columns: ColumnsType<Position> = [
    { title: "合约", dataIndex: "contract" },
    {
      title: "方向",
      dataIndex: "side",
      render: (side: string) => (
        <Tag color={side === "long" ? "red" : "green"}>{side === "long" ? "多" : "空"}</Tag>
      ),
    },
    { title: "张数", dataIndex: "size", align: "right", render: (v: number) => integer(v) },
    { title: "名义价值", dataIndex: "value", align: "right", render: (v: number) => money(v) },
    { title: "开仓均价", dataIndex: "entry_price", align: "right", render: (v: number) => money(v, 4) },
    { title: "标记价", dataIndex: "mark_price", align: "right", render: (v: number) => money(v, 4) },
    { title: "强平价", dataIndex: "liq_price", align: "right", render: (v: number) => money(v, 4) },
    {
      title: "距强平",
      dataIndex: "liq_distance_pct",
      align: "right",
      sorter: (a, b) => (a.liq_distance_pct ?? 9) - (b.liq_distance_pct ?? 9),
      render: (dist: number | null) =>
        dist == null ? (
          "—"
        ) : (
          <Progress
            percent={Math.max(0, Math.min(100, dist * 100))}
            size="small"
            style={{ width: 90, margin: 0 }}
            strokeColor={dist < 0.15 ? "#e5484d" : dist < 0.3 ? "#faad14" : "#52c41a"}
            format={(p) => `${(p ?? 0).toFixed(1)}%`}
          />
        ),
    },
    {
      title: "杠杆",
      dataIndex: "leverage",
      align: "right",
      render: (v: number) => (v === 0 ? "全仓" : `${v}x`),
    },
    { title: "保证金", dataIndex: "margin", align: "right", render: (v: number) => money(v) },
    {
      title: "未实现盈亏",
      dataIndex: "unrealised_pnl",
      align: "right",
      sorter: (a, b) => a.unrealised_pnl - b.unrealised_pnl,
      render: (v: number) => <span {...plText(v)} />,
    },
  ];

  if (positions.length === 0) {
    return <div className="chart-empty">当前没有持仓。</div>;
  }
  return <Table<Position> size="small" rowKey="contract" columns={columns} dataSource={ordered} pagination={false} />;
}

export function HoldingsTable({ holdings }: { holdings: Holding[] }) {
  const columns: ColumnsType<Holding> = [
    { title: "币种", dataIndex: "currency" },
    { title: "可用", dataIndex: "available", align: "right", render: (v: number) => money(v, 6) },
    { title: "冻结", dataIndex: "locked", align: "right", render: (v: number) => (v === 0 ? "—" : money(v, 6)) },
    { title: "合计", dataIndex: "total", align: "right", render: (v: number) => money(v, 6) },
    { title: "价格", dataIndex: "price", align: "right", render: (v: number | null) => (v == null ? "无行情" : money(v, 4)) },
    {
      title: "估值",
      dataIndex: "value",
      align: "right",
      render: (v: number | null) => (v == null ? "—" : money(v)),
    },
  ];
  if (holdings.length === 0) {
    return <div className="chart-empty">现货账户没有余额。</div>;
  }
  return <Table<Holding> size="small" rowKey="currency" columns={columns} dataSource={holdings} pagination={false} />;
}

export function UnifiedBalancesTable({ balances }: { balances: UnifiedBalance[] }) {
  const columns: ColumnsType<UnifiedBalance> = [
    { title: "币种", dataIndex: "currency" },
    { title: "权益", dataIndex: "equity", align: "right", render: (v: number) => money(v, 6) },
    { title: "可用", dataIndex: "available", align: "right", render: (v: number) => money(v, 6) },
    { title: "冻结", dataIndex: "freeze", align: "right", render: (v: number) => (v === 0 ? "—" : money(v, 6)) },
    {
      title: "已借",
      dataIndex: "borrowed",
      align: "right",
      render: (v: number) =>
        v > 0 ? <span style={{ color: "#e8833a" }}>{money(v, 6)}</span> : "—",
    },
    { title: "应计利息", dataIndex: "interest", align: "right", render: (v: number) => (v === 0 ? "—" : money(v, 8)) },
    {
      title: "未实现盈亏",
      dataIndex: "unrealised_pnl",
      align: "right",
      render: (v: number) => (v === 0 ? "—" : signed(v)),
    },
  ];
  if (balances.length === 0) {
    return <div className="chart-empty">统一账户没有余额。</div>;
  }
  return <Table<UnifiedBalance> size="small" rowKey="currency" columns={columns} dataSource={balances} pagination={false} />;
}

export function MarketTable({ rows }: { rows: MarketRow[] }) {
  const plText = usePlText();
  const columns: ColumnsType<MarketRow> = [
    { title: "市场", dataIndex: "market" },
    { title: "笔数", dataIndex: "trade_count", align: "right", sorter: (a, b) => a.trade_count - b.trade_count },
    { title: "成交额", dataIndex: "volume", align: "right", sorter: (a, b) => a.volume - b.volume, render: (v: number) => money(v, 0) },
    { title: "手续费", dataIndex: "fee", align: "right", render: (v: number) => signed(v) },
    { title: "maker 占比", dataIndex: "maker_ratio", align: "right", render: (v: number | null) => percent(v, 0) },
    {
      title: "平仓盈亏",
      dataIndex: "realized_pnl",
      align: "right",
      sorter: (a, b) => (a.realized_pnl ?? 0) - (b.realized_pnl ?? 0),
      render: (v: number | null) => (v == null ? "—" : <span {...plText(v)} />),
    },
    {
      title: "费/盈亏",
      align: "right",
      render: (_, row) => {
        const burn =
          row.realized_pnl != null && row.realized_pnl > 0
            ? Math.abs(row.fee) / row.realized_pnl
            : null;
        return burn == null ? (
          "—"
        ) : (
          <span style={burn > 0.5 ? { color: "#e5484d", fontWeight: 600 } : undefined}>
            {percent(burn, 0)}
          </span>
        );
      },
    },
  ];
  if (rows.length === 0) {
    return <div className="chart-empty">这个区间没有成交记录。</div>;
  }
  return <Table<MarketRow> size="small" rowKey="market" columns={columns} dataSource={rows} pagination={false} />;
}

/** Per-account comparison — who is actually making the money. */
export function ContributionTable({
  rows,
  labelFor,
  equityOnly = false,
}: {
  rows: ContributionRow[];
  labelFor: (profile: string) => string;
  /** Compact variant for the portfolio landing block: equity + PnL only. */
  equityOnly?: boolean;
}) {
  const plText = usePlText();
  const columns: ColumnsType<ContributionRow> = equityOnly
    ? [
        {
          title: "账户",
          render: (_, row) => (
            <>
              {labelFor(row.profile)}{" "}
              <span style={{ color: "rgba(128,128,128,0.9)" }}>
                {ACCOUNT_LABELS[row.account] ?? row.account}
              </span>
            </>
          ),
        },
        { title: "权益", dataIndex: "equity", align: "right", render: (v: number | null) => money(v) },
        {
          title: "区间盈亏",
          dataIndex: "period_pnl",
          align: "right",
          sorter: (a, b) => a.period_pnl - b.period_pnl,
          render: (v: number) => <span {...plText(v)} />,
        },
        {
          title: "收益率",
          dataIndex: "total_return",
          align: "right",
          render: (v: number | null) => <span {...plText(v)} />,
        },
      ]
    : [
        {
          title: "账户",
          render: (_, row) => (
            <>
              {labelFor(row.profile)}{" "}
              <span style={{ color: "rgba(128,128,128,0.9)" }}>
                {ACCOUNT_LABELS[row.account] ?? row.account}
              </span>
            </>
          ),
        },
        { title: "权益", dataIndex: "equity", align: "right", render: (v: number | null) => money(v) },
        {
          title: "区间盈亏",
          dataIndex: "period_pnl",
          align: "right",
          sorter: (a, b) => a.period_pnl - b.period_pnl,
          render: (v: number) => <span {...plText(v)} />,
        },
        { title: "盈亏占比", dataIndex: "pnl_share", align: "right", render: (v: number | null) => percent(v, 0) },
        {
          title: "收益率",
          dataIndex: "total_return",
          align: "right",
          render: (v: number | null) => <span {...plText(v)} />,
        },
        { title: "最大回撤", dataIndex: "max_drawdown", align: "right", render: (v: number | null) => percent(v, 1) },
        { title: "夏普", dataIndex: "sharpe", align: "right", render: (v: number | null) => ratio(v) },
        { title: "手续费", dataIndex: "fee", align: "right", render: (v: number) => signed(v) },
        { title: "maker", dataIndex: "maker_ratio", align: "right", render: (v: number | null) => percent(v, 0) },
        { title: "成本/毛利", dataIndex: "cost_to_gross_ratio", align: "right", render: (v: number | null) => percent(v, 1) },
        { title: "笔数", dataIndex: "trade_count", align: "right", render: (v: number) => integer(v) },
      ];
  if (rows.length === 0) {
    return <div className="chart-empty">还没有可比较的账户数据。</div>;
  }
  return (
    <Table<ContributionRow>
      size="small"
      rowKey={(r) => `${r.profile}/${r.account}`}
      columns={columns}
      dataSource={rows}
      pagination={false}
    />
  );
}

/** Holding duration in trader-friendly units. */
function duration(from: number | null, to: number): string {
  if (from == null || from <= 0 || to <= from) return "—";
  const hours = (to - from) / 3600;
  if (hours < 1) return `${Math.round(hours * 60)} 分钟`;
  if (hours < 48) return `${hours.toFixed(1)} 小时`;
  return `${(hours / 24).toFixed(1)} 天`;
}

/** Closed-position history with a per-market column filter. */
export function PositionHistoryTable({ rows }: { rows: PositionHistoryRow[] }) {
  const plText = usePlText();
  const markets = [...new Set(rows.map((r) => r.contract).filter(Boolean))] as string[];

  const columns: ColumnsType<PositionHistoryRow> = [
    { title: "平仓时间", dataIndex: "ts", render: (v: number) => clockFromUnix(v) },
    {
      title: "合约",
      dataIndex: "contract",
      filters: markets.map((m) => ({ text: m, value: m })),
      onFilter: (value, row) => row.contract === value,
    },
    {
      title: "方向",
      dataIndex: "side",
      render: (side: string | null) =>
        side == null ? (
          "—"
        ) : (
          <Tag color={side === "long" ? "red" : "green"}>{side === "long" ? "多" : "空"}</Tag>
        ),
    },
    {
      title: "开仓均价",
      align: "right",
      render: (_, row) => {
        const v = row.side === "long" ? row.long_price : row.short_price;
        return v == null ? "—" : money(v, 4);
      },
    },
    {
      title: "平仓均价",
      align: "right",
      render: (_, row) => {
        const v = row.side === "long" ? row.short_price : row.long_price;
        return v == null ? "—" : money(v, 4);
      },
    },
    {
      title: "价格盈亏",
      dataIndex: "pnl_pnl",
      align: "right",
      sorter: (a, b) => (a.pnl_pnl ?? 0) - (b.pnl_pnl ?? 0),
      render: (v: number | null) => (v == null ? "—" : <span {...plText(v)} />),
    },
    {
      title: "手续费",
      dataIndex: "pnl_fee",
      align: "right",
      render: (v: number | null) => (v == null ? "—" : signed(v)),
    },
    {
      title: "资金费",
      dataIndex: "pnl_fund",
      align: "right",
      render: (v: number | null) => (v == null || v === 0 ? "—" : signed(v)),
    },
    {
      title: "最大仓位",
      dataIndex: "max_size",
      align: "right",
      render: (v: number | null) => (v == null ? "—" : integer(Math.abs(v))),
    },
    {
      title: "持仓时长",
      align: "right",
      sorter: (a, b) => (a.first_open_time ?? 0) - (b.first_open_time ?? 0),
      render: (_, row) => duration(row.first_open_time, row.ts),
    },
    {
      title: "平仓盈亏",
      dataIndex: "pnl",
      align: "right",
      sorter: (a, b) => (a.pnl ?? 0) - (b.pnl ?? 0),
      render: (v: number | null) => (v == null ? "—" : <span {...plText(v)} />),
    },
  ];

  if (rows.length === 0) {
    return <div className="chart-empty">还没有平仓记录。合约平仓后这里会出现历史。</div>;
  }
  return (
    <Table<PositionHistoryRow>
      size="small"
      rowKey={(r) => `${r.ts}-${r.contract}-${r.pnl}`}
      columns={columns}
      dataSource={rows}
      pagination={{ pageSize: 50, showSizeChanger: true, pageSizeOptions: [50, 100, 200] }}
      scroll={{ y: 560, x: true }}
    />
  );
}

const LEDGER_TYPE_LABELS: Record<string, string> = {
  deposit: "充值", withdraw: "提现", trade: "成交", fee: "手续费",
  rebate: "返佣", transfer: "划转", liquidate: "强平", settlement: "结算",
  new_order: "下单冻结/解冻", order_fill: "成交", order_fee: "交易手续费",
  referral_fee: "返佣", pu_rebate: "返佣", perp_in: "转出至合约",
  perp_out: "从合约转入", subaccount_trf: "子账户划转", sub_account_transfer: "子账户划转",
  dnw: "出入金", pnl: "平仓盈亏", fund: "资金费", refr: "返佣",
};

/** Spot / futures account book, with a per-type column filter. */
export function LedgerTable({ rows }: { rows: LedgerRow[] }) {
  const plText = usePlText();
  const types = [...new Set(rows.map((r) => r.type))].sort();

  const columns: ColumnsType<LedgerRow> = [
    { title: "时间", dataIndex: "ts", render: (v: number) => clockFromUnix(v), defaultSortOrder: "descend" },
    {
      title: "类型",
      dataIndex: "type",
      filters: types.map((t) => ({ text: LEDGER_TYPE_LABELS[t] ?? t, value: t })),
      onFilter: (value, row) => row.type === value,
      render: (t: string) => LEDGER_TYPE_LABELS[t] ?? t,
    },
    { title: "币种", dataIndex: "currency" },
    {
      title: "变动",
      dataIndex: "change",
      align: "right",
      sorter: (a, b) => a.change - b.change,
      render: (v: number) => <span {...plText(v)} />,
    },
    {
      title: "余额",
      dataIndex: "balance",
      align: "right",
      render: (v: number | null) => (v == null ? "—" : money(v, 6)),
    },
    {
      title: "备注",
      dataIndex: "text",
      render: (v: string | null) => <span style={{ color: "rgba(128,128,128,0.9)" }}>{v ?? ""}</span>,
    },
  ];

  if (rows.length === 0) {
    return <div className="chart-empty">还没有账本记录。点一次同步就会拉取。</div>;
  }
  return (
    <Table<LedgerRow>
      size="small"
      rowKey="entry_id"
      columns={columns}
      dataSource={rows}
      pagination={{ pageSize: 50, showSizeChanger: true, pageSizeOptions: [50, 100, 200] }}
      scroll={{ y: 560, x: true }}
    />
  );
}
