import { useEffect, useState } from "react";
import { Card, DatePicker, Segmented, Space, Table, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import dayjs from "dayjs";
import {
  ACCOUNT_LABELS,
  api,
  type AccountKey,
  type AppConfig,
  type FeeGranularity,
  type FeeSummary,
} from "./api";
import { KpiStrip } from "./views";
import { integer, money, percent, rate, signed } from "./format";

const { Text } = Typography;

/**
 * Fee totals per period — day or week, custom date window, and a day boundary
 * of midnight or 08:00 UTC+8 (a trading day that starts at 8 reassigns morning
 * rows to the previous period).
 */
export function FeePanel({
  config,
  scope,
  days,
}: {
  config: AppConfig;
  /** Null = the portfolio view; otherwise one account's own panel. */
  scope: { profile: string; account: AccountKey } | null;
  /** Global window, used as the default when no custom range is set. */
  days: number;
}) {
  const [granularity, setGranularity] = useState<FeeGranularity>("day");
  const [boundary, setBoundary] = useState<number>(0);
  const [applied, setApplied] = useState<{ from: string; to: string } | null>(null);
  const [summary, setSummary] = useState<FeeSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .feeSummary(scope, {
        period: granularity,
        from_day: applied?.from,
        to_day: applied?.to,
        days: applied ? undefined : Math.max(days, 30),
        boundary,
      })
      .then((result) => {
        if (!cancelled) setSummary(result);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [scope, granularity, applied, days, boundary]);

  const labelFor = (profileId: string) =>
    config.profiles.find((p) => p.id === profileId)?.label ?? profileId;

  const total = summary?.total;
  const periods = summary?.periods ?? [];

  const periodColumns: ColumnsType<FeeSummary["periods"][number]> = [
    {
      title: { day: "日期", week: "周（周一起）", month: "月份" }[granularity],
      dataIndex: "period",
      render: (period: string, row) => (
        <>
          {period}
          {granularity === "week" && row.member_days < 7 && (
            <Text type="secondary" style={{ marginLeft: 6 }}>
              {row.member_days} 天
            </Text>
          )}
        </>
      ),
    },
    { title: "手续费", dataIndex: "fee", align: "right", render: (v: number) => signed(v) },
    { title: "70%手续费", dataIndex: "fee_70", align: "right", render: (v: number) => money(v, 2) },
    { title: "10%手续费", dataIndex: "fee_10", align: "right", render: (v: number) => money(v, 2) },
    { title: "5%手续费", dataIndex: "fee_05", align: "right", render: (v: number) => money(v, 2) },
    {
      title: "当期返佣",
      dataIndex: "rebate",
      align: "right",
      render: (v: number) =>
        v === 0 ? "—" : <span style={{ color: v > 0 ? "#2f9e64" : undefined }}>{money(v, 2)}</span>,
    },
    {
      title: "预计额外返佣",
      dataIndex: "expected_extra",
      align: "right",
      sorter: (a, b) => a.expected_extra - b.expected_extra,
      render: (v: number) =>
        v === 0 ? (
          "—"
        ) : (
          <span style={{ color: v > 0 ? "#e8833a" : "#2f9e64", fontWeight: v > 0 ? 600 : undefined }}>
            {signed(v)}
          </span>
        ),
    },
    { title: "资金费", dataIndex: "funding", align: "right", render: (v: number) => signed(v) },
    {
      title: "成本",
      align: "right",
      render: (_, row) =>
        money(Math.abs(row.fee) + Math.abs(Math.min(row.funding, 0)) + Math.abs(row.interest), 2),
    },
    { title: "成交额", dataIndex: "volume", align: "right", render: (v: number) => money(v, 0) },
    { title: "笔数", dataIndex: "trade_count", align: "right", render: (v: number) => integer(v) },
    { title: "maker", dataIndex: "maker_ratio", align: "right", render: (v: number | null) => percent(v, 0) },
  ];

  const accountColumns: ColumnsType<FeeSummary["per_account"][number]> = [
    {
      title: "账户",
      render: (_, row) => (
        <>
          {labelFor(row.profile)}{" "}
          <Text type="secondary">{ACCOUNT_LABELS[row.account] ?? row.account}</Text>
        </>
      ),
    },
    { title: "手续费", dataIndex: "fee", align: "right", render: (v: number) => signed(v) },
    { title: "成本合计", dataIndex: "total_cost", align: "right", render: (v: number) => money(v, 2) },
    {
      title: "返佣",
      dataIndex: "rebate",
      align: "right",
      render: (v: number) => (v === 0 ? "—" : signed(v)),
    },
    { title: "成交额", dataIndex: "volume", align: "right", render: (v: number) => money(v, 0) },
    { title: "实际费率", dataIndex: "effective_fee_rate", align: "right", render: (v: number | null) => rate(v) },
    { title: "maker", dataIndex: "maker_ratio", align: "right", render: (v: number | null) => percent(v, 0) },
  ];

  return (
    <Card
      title={scope ? "手续费统计" : "手续费统计 · 含分账户"}
      extra={
        <Text type="secondary">
          {summary
            ? applied
              ? `${applied.from} ~ ${applied.to}`
              : `近 ${Math.max(days, 30)} 天`
            : ""}
        </Text>
      }
    >
      <Space wrap style={{ marginBottom: 16 }}>
        <Segmented
          value={granularity}
          onChange={(v) => setGranularity(v as FeeGranularity)}
          options={[
            { label: "按日", value: "day" },
            { label: "按周", value: "week" },
            { label: "按月", value: "month" },
          ]}
        />
        <Segmented
          value={boundary}
          onChange={(v) => setBoundary(Number(v))}
          options={[
            { label: "0 点起", value: 0 },
            { label: "8 点起", value: 8 },
          ]}
        />
        <DatePicker.RangePicker
          size="small"
          value={applied ? [dayjs(applied.from), dayjs(applied.to)] : null}
          presets={[
            { label: "本周", value: [dayjs().startOf("week"), dayjs()] },
            {
              label: "上周",
              value: [
                dayjs().subtract(1, "week").startOf("week"),
                dayjs().subtract(1, "week").endOf("week"),
              ],
            },
            { label: "本月", value: [dayjs().startOf("month"), dayjs()] },
            {
              label: "上月",
              value: [
                dayjs().subtract(1, "month").startOf("month"),
                dayjs().subtract(1, "month").endOf("month"),
              ],
            },
          ]}
          onChange={(_, dateStrings) => {
            if (dateStrings[0] && dateStrings[1]) {
              setApplied({ from: dateStrings[0], to: dateStrings[1] });
            } else {
              setApplied(null);
            }
          }}
        />
      </Space>

      {error && <Text type="danger">读取失败：{error}</Text>}

      {total && periods.length > 0 && (
        <div style={{ opacity: loading ? 0.5 : 1 }}>
          <KpiStrip
            items={[
              {
                title: "手续费合计",
                value: money(Math.abs(total.fee), 2),
                suffix: config.quote,
                note: `${integer(total.trade_count)} 笔成交`,
              },
              {
                title: "成本合计",
                value: money(total.total_cost, 2),
                note: `含资金费 ${money(Math.abs(Math.min(total.funding, 0)), 2)}${
                  total.interest ? `、利息 ${money(Math.abs(total.interest), 2)}` : ""
                }`,
              },
              {
                title: "当期返佣到账",
                value: money(total.rebate, 2),
                tone: total.rebate,
                note: `账本返佣另有 ${signed(total.ledger_rebate)}`,
              },
              {
                title: "预计额外返佣",
                value: signed(total.expected_extra),
                suffix: config.quote,
                danger: total.expected_extra > 0,
                note: `70% 应返 ${money(total.fee_70, 2)} − 到账 ${money(total.rebate, 2)}`,
              },
            ]}
          />

          <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 12 }}>
            成本 = 手续费 + 资金费支出 + 借贷利息。70%/10%/5% 是手续费绝对值的份额；
            当期返佣是同一账户的现货账本里实际到账的返佣，按同一条日界和周期分桶；
            预计额外返佣 = 70% 应返 − 到账，即还没回来的部分。日界可选 0 点或
            8 点（UTC+8），周从周一起，月按自然月，都按同一条日界线。
          </Typography.Paragraph>

          <Table
            size="small"
            rowKey="period"
            columns={periodColumns}
            dataSource={[...periods].reverse()}
            pagination={{ pageSize: 30, showSizeChanger: true, pageSizeOptions: [30, 100] }}
            scroll={{ y: 480, x: true }}
            style={{ marginTop: 16 }}
          />

          {!scope && summary && summary.per_account.length > 0 && (
            <Card
              type="inner"
              title="分账户手续费"
              extra={<Text type="secondary">各行相加等于总计</Text>}
              style={{ marginTop: 16 }}
            >
              <Table
                size="small"
                rowKey={(r) => `${r.profile}/${r.account}`}
                columns={accountColumns}
                dataSource={summary.per_account}
                pagination={false}
              />
            </Card>
          )}
        </div>
      )}
      {summary && periods.length === 0 && !error && (
        <div className="chart-empty">这个区间没有账本记录。</div>
      )}
    </Card>
  );
}
