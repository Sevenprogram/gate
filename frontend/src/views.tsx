import type { ReactNode } from "react";
import {
  Alert,
  Card,
  Col,
  Descriptions,
  Progress,
  Row,
  Space,
  Statistic,
  Tabs,
  Typography,
} from "antd";
import type {
  AccountKey,
  ContributionRow,
  DailyPayload,
  LedgerRow,
  MarketRow,
  PortfolioSnapshot,
  PositionHistoryRow,
  RiskEvents,
  Snapshot,
} from "./api";
import { ACCOUNT_LABELS } from "./api";
import { money, percent, rate, signed, signedPercent } from "./format";
import {
  ContributionTable,
  DailyTable,
  HoldingsTable,
  LedgerTable,
  MarketTable,
  PositionHistoryTable,
  PositionsTable,
  UnifiedBalancesTable,
} from "./tables";
import { plColors, usePlMode } from "./palette";

const { Text } = Typography;

interface ViewProps {
  account: AccountKey;
  snapshot: Snapshot | null;
  daily: DailyPayload | null;
  markets: MarketRow[];
  risk: RiskEvents | null;
  quote: string;
  today: string;
  /** The fee tab, composed in App where the scope is known. */
  feePanel: ReactNode;
  /** Closed-position history; futures only, empty elsewhere. */
  positionHistory: PositionHistoryRow[];
  /** Spot/futures account book rows (used by the spot 流水 tab). */
  ledger: LedgerRow[];
}

function todayRow(daily: DailyPayload | null, today: string) {
  return daily?.series.find((row) => row.day === today) ?? null;
}

function latestClosedRow(daily: DailyPayload | null, today: string) {
  const withPnl = (daily?.series ?? []).filter((r) => r.pnl_equity != null);
  return (
    withPnl.find((r) => r.day === today) ?? withPnl[withPnl.length - 1] ?? null
  );
}


/** Equity for the headline, falling back to the newest complete local snapshot. */
function heroEquity(
  snapshot: Snapshot | null,
  daily: DailyPayload | null,
): { value: number | null; asOf: string | null } {
  if (snapshot?.equity != null) return { value: snapshot.equity, asOf: null };
  const known = (daily?.series ?? []).filter(
    (r) => r.equity_close != null && r.complete,
  );
  const last = known[known.length - 1];
  return last
    ? { value: last.equity_close!, asOf: last.day }
    : { value: null, asOf: null };
}

function Delta({ row, today }: { row: ReturnType<typeof latestClosedRow>; today: string }) {
  usePlMode();
  const colors = plColors();
  if (!row || row.pnl_equity == null) return null;
  return (
    <Text style={{ color: row.pnl_equity >= 0 ? colors.profit : colors.loss }}>
      {signed(row.pnl_equity)} ({signedPercent(row.return_pct)}){" "}
      <Text type="secondary">{row.day === today ? "今日" : row.day}</Text>
    </Text>
  );
}

/** One figure of the headline strip — no card of its own, just a cell with a
 *  hairline divider, the way a blotter header reads. */
export interface KpiItem {
  title: string;
  value: string;
  suffix?: string;
  delta?: ReactNode;
  note?: ReactNode;
  /** Pass a number to colour the value by sign. */
  tone?: number | null;
  /** Threshold breached: status colour with an icon prefix. */
  danger?: boolean;
}

function KpiCell({ item, first }: { item: KpiItem; first: boolean }) {
  usePlMode();
  const colors = plColors();
  const valueStyle: React.CSSProperties = {
    fontWeight: 600,
    fontSize: first ? 23 : 18,
    lineHeight: 1.2,
    fontVariantNumeric: "tabular-nums",
    fontFamily: "'SF Mono', ui-monospace, Menlo, monospace",
    color: "#111827",
  };
  if (item.danger) {
    valueStyle.color = "#e5484d";
  } else if (
    item.tone !== undefined &&
    item.tone != null &&
    Number.isFinite(item.tone) &&
    item.tone !== 0
  ) {
    valueStyle.color = item.tone > 0 ? colors.profit : colors.loss;
  }

  return (
    <div
      style={{
        flex: "1 1 0",
        minWidth: 0,
        padding: "10px 14px",
        borderLeft: first ? "none" : "1px solid #e5e7eb",
      }}
    >
      <div style={{ fontSize: 10.5, color: "#6b7280", letterSpacing: "0.02em" }}>
        {item.title}
      </div>
      <div style={valueStyle}>
        {item.danger ? "! " : ""}
        {item.value}
        {item.suffix && (
          <span style={{ fontSize: 11, fontWeight: 400, color: "#9ca3af", marginLeft: 5 }}>
            {item.suffix}
          </span>
        )}
      </div>
      {item.delta}
      {item.note && (
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 2 }}>
          {item.note}
        </Text>
      )}
    </div>
  );
}

/** The headline figures as one continuous strip, not four separate cards. */
export function KpiStrip({ items }: { items: KpiItem[] }) {
  return (
    <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: 0 } }}>
      <div style={{ display: "flex", flexWrap: "wrap" }}>
        {items.map((item, i) => (
          <KpiCell key={item.title} item={item} first={i === 0} />
        ))}
      </div>
    </Card>
  );
}

/** Risk-adjusted summary over the daily returns. */


/** The workspace tabs shared by every view. */
function Workspace({
  daily,
  markets,
  quote,
  showMarkets,
  feePanel,
  extraTables,
  extraTabs,
}: {
  daily: DailyPayload | null;
  markets: MarketRow[];
  quote: string;
  showMarkets: boolean;
  feePanel: ReactNode;
  extraTables?: ReactNode;
  extraTabs?: Array<{ key: string; label: string; children: ReactNode }>;
}) {
  const items = [
    {
      key: "tables",
      label: "明细",
      children: (
        <Space orientation="vertical" size={16} style={{ width: "100%" }}>
          {extraTables}
          {showMarkets && (
            <Card
              title="分市场"
              extra={<Text type="secondary">近 30 天</Text>}
              styles={{ body: { paddingTop: 0 } }}
            >
              <MarketTable rows={markets} />
            </Card>
          )}
          <Card
            title="每日明细"
            extra={<Text type="secondary">{quote}</Text>}
            styles={{ body: { paddingTop: 0 } }}
          >
            <DailyTable rows={daily?.series ?? []} />
          </Card>
        </Space>
      ),
    },
    { key: "fee", label: "手续费", children: feePanel },
    ...(extraTabs ?? []),
  ];

  return <Tabs defaultActiveKey="tables" items={items}  />;
}

/* --------------------------------------------------------------- spot view */

export function SpotView({
  snapshot,
  daily,
  markets,
  quote,
  today,
  feePanel,
  ledger,
}: ViewProps) {
  const delta = <Delta row={latestClosedRow(daily, today)} today={today} />;
  const tRow = todayRow(daily, today);
  const fee = snapshot?.fee_tier;
  const equity = heroEquity(snapshot, daily);

  return (
    <>
      {snapshot?.unpriced && snapshot.unpriced.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message="部分币种没有行情，估值不完整"
          description={`${snapshot.unpriced.join("、")} 在 ${quote} 市场上取不到价格，这些余额没有计入总估值。`}
          style={{ marginBottom: 16 }}
        />
      )}

      <KpiStrip items={[{ title: "现货估值", value: money(equity.value), suffix: quote, delta: delta, note: equity.asOf
              ? `实时读取失败，这是 ${equity.asOf} 的本地快照`
              : snapshot?.holdings
                ? `${snapshot.holdings.length} 个币种有余额`
                : undefined }, { title: "今日手续费", value: signed(tRow?.fee ?? 0), note: tRow?.fee_to_gross_ratio != null
              ? `占当日毛利 ${percent(tRow.fee_to_gross_ratio, 1)}`
              : "现货没有平仓盈亏概念，毛利留空" }, { title: "今日成交", value: String(tRow?.trade_count ?? 0), note: tRow?.maker_ratio != null ? `maker ${percent(tRow.maker_ratio, 0)}` : "今天还没有成交" }, { title: "当前费率档", value: fee ? rate(fee.maker) : "—", note: fee ? `吃单 ${rate(fee.taker)}${fee.gt_discount ? " · GT 抵扣生效" : ""}` : "读不到费率" }]} />

      <Card title="现货余额" style={{ marginBottom: 16 }} styles={{ body: { paddingTop: 0 } }}>
        <HoldingsTable holdings={snapshot?.holdings ?? []} />
      </Card>

      <Workspace
        daily={daily}
        markets={markets}
        quote={quote}
        showMarkets
        feePanel={feePanel}
        extraTabs={[
          {
            key: "ledger",
            label: "流水",
            children: (
              <Card
                title="资金流水"
                extra={<Text type="secondary">{ledger.length} 条 · 可按类型筛选</Text>}
                styles={{ body: { paddingTop: 0 } }}
              >
                <LedgerTable rows={ledger} />
              </Card>
            ),
          },
        ]}
      />
    </>
  );
}

/* ------------------------------------------------------------ futures view */

export function FuturesView({
  snapshot,
  daily,
  markets,
  risk,
  quote,
  today,
  feePanel,
  positionHistory,
}: ViewProps) {
  const delta = <Delta row={latestClosedRow(daily, today)} today={today} />;
  const tRow = todayRow(daily, today);
  const positions = snapshot?.positions ?? [];
  const equity = heroEquity(snapshot, daily);

  const nearest = positions.reduce<number | null>((min, p) => {
    if (p.liq_distance_pct == null) return min;
    return min == null ? p.liq_distance_pct : Math.min(min, p.liq_distance_pct);
  }, null);

  const liquidations = risk?.liquidations.length ?? 0;
  const adl = risk?.auto_deleverages.length ?? 0;

  return (
    <>
      {/* Risk before profit. */}
      {(liquidations > 0 || adl > 0) && (
        <Alert
          type="error"
          showIcon
          message="出现过强平或自动减仓"
          description={`最近记录里有 ${liquidations} 条强平、${adl} 条自动减仓。这不是盈亏问题，是仓位管理问题。`}
          style={{ marginBottom: 16 }}
        />
      )}
      {nearest != null && nearest < 0.15 && (
        <Alert
          type="warning"
          showIcon
          message={`最近的仓位距强平价只有 ${percent(nearest, 1)}`}
          description="标记价再往不利方向走这么多就会被强平。下面的持仓表已按距离排序。"
          style={{ marginBottom: 16 }}
        />
      )}

      <KpiStrip items={[{ title: "账户权益", value: money(equity.value), suffix: quote, delta: delta, note: equity.asOf
              ? `实时读取失败，这是 ${equity.asOf} 的本地快照`
              : `钱包余额 ${money(snapshot?.wallet_balance)} + 未实现 ${signed(snapshot?.unrealised_pnl)}` }, { title: "距强平最近", value: nearest == null ? "无持仓" : percent(nearest, 1), danger: nearest != null && nearest < 0.15, note: positions.length > 0
              ? `${positions.length} 个持仓 · 保证金占用 ${percent(snapshot?.margin_ratio, 0)}`
              : undefined }, { title: "未实现盈亏", value: signed(snapshot?.unrealised_pnl), tone: snapshot?.unrealised_pnl ?? null }, { title: "今日已实现净额", value: signed(tRow?.net_realized ?? 0), tone: tRow?.net_realized ?? null, note: tRow
              ? `平仓 ${signed(tRow.realized)} · 费 ${signed(tRow.fee)} · 资金费 ${signed(tRow.funding)}`
              : "今天还没有账本记录" }]} />

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} xl={16}>
          <Card
            title="当前持仓"
            extra={<Text type="secondary">按距强平排序</Text>}
            styles={{ body: { paddingTop: 0 } }}
          >
            <PositionsTable positions={positions} />
          </Card>
        </Col>
        <Col xs={24} xl={8}>
          <Card title="保证金占用">
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
              <span>持仓 + 委托保证金 占权益</span>
              <b>{percent(snapshot?.margin_ratio, 1)}</b>
            </div>
            <Progress
              percent={Math.min(100, (snapshot?.margin_ratio ?? 0) * 100)}
              strokeColor={
                (snapshot?.margin_ratio ?? 0) > 0.75
                  ? "#e5484d"
                  : (snapshot?.margin_ratio ?? 0) > 0.5
                    ? "#faad14"
                    : "#52c41a"
              }
            />
            <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
              维持保证金 {money(snapshot?.maintenance_margin)}（占权益{" "}
              {percent(snapshot?.maintenance_ratio, 2)}），是强平的门槛。占用率越高，
              行情反向时能承受的空间越小。
            </Text>
          </Card>
        </Col>
      </Row>

      <Workspace
        daily={daily}
        markets={markets}
        quote={quote}
        showMarkets
        feePanel={feePanel}
        extraTables={
          <>
            <Card
              title="历史仓位"
              extra={<Text type="secondary">{positionHistory.length} 条平仓记录</Text>}
              styles={{ body: { paddingTop: 0 } }}
            >
              <PositionHistoryTable rows={positionHistory} />
            </Card>
            {snapshot?.lifetime && (
              <Card title="账户开通至今" extra={<Text type="secondary">Gate 侧终身汇总</Text>}>
                <Row gutter={[16, 8]}>
                  {(
                    [
                      ["平仓盈亏", snapshot.lifetime.realized_pnl],
                      ["手续费", snapshot.lifetime.fee],
                      ["资金费", snapshot.lifetime.funding],
                      ["返佣", snapshot.lifetime.rebate],
                      ["净盈亏", snapshot.lifetime.net_pnl],
                      ["净划转", snapshot.lifetime.net_transfer],
                    ] as Array<[string, number]>
                  ).map(([label, value]) => (
                    <Col key={label} xs={12} md={4}>
                      <Statistic title={label} value={signed(value)} />
                    </Col>
                  ))}
                </Row>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  由 Gate 直接给出，不受本地历史长度限制。
                </Text>
              </Card>
            )}
          </>
        }
      />
    </>
  );
}

/* ------------------------------------------------------------ unified view */

export function UnifiedView({
  snapshot,
  daily,
  markets,
  quote,
  today,
  feePanel,
}: ViewProps) {
  const delta = <Delta row={latestClosedRow(daily, today)} today={today} />;
  const equity = heroEquity(snapshot, daily);
  const marginRatio = snapshot?.margin_ratio ?? 0;

  return (
    <>
      {marginRatio > 0.7 && (
        <Alert
          type="error"
          showIcon
          message={`维持保证金率已到 ${percent(marginRatio, 1)}`}
          description="到 100% 就是强制平仓。统一账户是共享抵押，任何一条腿的亏损都会推高这个数。"
          style={{ marginBottom: 16 }}
        />
      )}

      <KpiStrip items={[{ title: "统一账户权益", value: money(equity.value), suffix: quote, delta: delta, note: equity.asOf
              ? `实时读取失败，这是 ${equity.asOf} 的本地快照`
              : `杠杆 ${snapshot?.leverage ?? "—"}x · 可用保证金 ${money(snapshot?.available)}` }, { title: "总负债", value: money(snapshot?.liabilities) }, { title: "应计利息", value: money(snapshot?.accrued_interest, 6) }, { title: "维持保证金率", value: percent(marginRatio, 1), danger: marginRatio > 0.7, note: `维持保证金 ${money(snapshot?.maintenance_margin)}` }]} />

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} xl={8}>
          <Card title="保证金水位">
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
              <span>维持保证金 ÷ 保证金余额</span>
              <b>{percent(marginRatio, 1)}</b>
            </div>
            <Progress
              percent={Math.min(100, marginRatio * 100)}
              strokeColor={
                marginRatio > 0.7 ? "#e5484d" : marginRatio > 0.4 ? "#faad14" : "#52c41a"
              }
            />
            <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
              绿色安全、黄色注意、红色接近强平。这是统一账户唯一的死亡线。
            </Text>
          </Card>
        </Col>
        <Col xs={24} xl={16}>
          <Card title="各币种明细" styles={{ body: { paddingTop: 0 } }}>
            <UnifiedBalancesTable balances={snapshot?.balances ?? []} />
          </Card>
        </Col>
      </Row>

      <Workspace
        daily={daily}
        markets={markets}
        quote={quote}
        showMarkets={false}
        feePanel={feePanel}
      />
    </>
  );
}

/* ---------------------------------------------------------- portfolio view */

export function PortfolioView({
  snapshot,
  daily,
  contribution,
  markets,
  quote,
  today,
  labelFor,
  feePanel,
}: {
  snapshot: PortfolioSnapshot | null;
  daily: DailyPayload | null;
  contribution: ContributionRow[];
  markets: MarketRow[];
  quote: string;
  today: string;
  labelFor: (profile: string) => string;
  feePanel: ReactNode;
}) {
  const delta = <Delta row={latestClosedRow(daily, today)} today={today} />;
  const parts = snapshot?.parts ?? [];
  const nearest = snapshot?.nearest_liq ?? null;
  const partialDays = daily?.stats.partial_days ?? 0;

  const periodPnl = (daily?.series ?? []).reduce(
    (sum, r) => sum + (r.pnl_equity ?? 0),
    0,
  );
  const accountSum = contribution.reduce((sum, r) => sum + r.period_pnl, 0);
  const pnlDisagrees = Math.abs(periodPnl - accountSum) > Math.abs(periodPnl) * 0.01;

  // Colour follows the account identity, never its rank.

  const equity =
    snapshot?.equity ?? latestClosedRow(daily, today)?.equity_close ?? null;

  return (
    <>
      {snapshot && snapshot.errors.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message="部分账户没读到，总额不完整"
          description={
            <>
              <ul style={{ margin: 0, paddingLeft: 17 }}>
                {snapshot.errors.map((error) => (
                  <li key={error}>{error}</li>
                ))}
              </ul>
              <div style={{ marginTop: 6 }}>
                下面的合计只包含读到的账户，缺的那些没有被当成 0——但总额确实少了它们。
              </div>
            </>
          }
          style={{ marginBottom: 16 }}
        />
      )}

      {snapshot && snapshot.excluded.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message="有账户没有计入组合"
          description={`${snapshot.excluded
            .map(
              (e) =>
                `${labelFor(e.profile)}/${
                  ACCOUNT_LABELS[e.account as AccountKey] ?? e.account
                }：${e.reason}`,
            )
            .join("；")}。换算需要逐日的历史汇率，这个程序没有存，猜一个汇率比留一个缺口更糟。`}
          style={{ marginBottom: 16 }}
        />
      )}

      {partialDays > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`有 ${partialDays} 天的数据不完整，已排除`}
          description="那些天里不是所有账户都同步过，加总会缺掉没同步的那个、看起来像一次暴跌。这些天不参与盈亏和回撤计算。每天都同步一次就不会出现。"
          style={{ marginBottom: 16 }}
        />
      )}

      {nearest != null && nearest < 0.15 && (
        <Alert
          type="warning"
          showIcon
          message={`所有账户里最近的仓位距强平价只有 ${percent(nearest, 1)}`}
          description="切到对应账户可以看到具体是哪个仓位。"
          style={{ marginBottom: 16 }}
        />
      )}

      <KpiStrip items={[{ title: `组合总权益 · ${parts.length} 个账户`, value: money(equity), suffix: quote, delta: delta, note: snapshot
              ? `可用 ${money(snapshot.available)} · 未实现 ${signed(snapshot.unrealised_pnl)}`
              : "实时读取失败，这是本地快照的合计" }, { title: "区间盈亏", value: signed(periodPnl), tone: periodPnl, note: daily ? `${daily.stats.days} 个有效交易日` : undefined }, { title: "未实现合计", value: signed(snapshot?.unrealised_pnl), tone: snapshot?.unrealised_pnl ?? null }, { title: "距强平最近", value: nearest == null ? "无持仓" : percent(nearest, 1), danger: nearest != null && nearest < 0.15, note: snapshot
              ? `${snapshot.position_count} 个持仓 · 保证金占用 ${percent(snapshot.margin_ratio, 0)}`
              : undefined }]} />

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} xl={16}>
          <Card title="各账户权益" extra={<Text type="secondary">{quote}</Text>} styles={{ body: { paddingTop: 0 } }}>
            <ContributionTable rows={contribution} labelFor={labelFor} equityOnly />
          </Card>
        </Col>
        <Col xs={24} xl={8}>
          <Card title="组合风险" styles={{ body: { paddingTop: 0 } }}>
            <Descriptions
              size="small"
              column={1}
              bordered
              items={[
                { key: "m", label: "保证金占用", children: percent(snapshot?.margin_ratio, 1) },
                {
                  key: "mm",
                  label: "维持保证金",
                  children: `${money(snapshot?.maintenance_margin, 0)}（${percent(snapshot?.maintenance_ratio, 2)}）`,
                },
                { key: "liab", label: "总负债", children: money(snapshot?.liabilities, 0) },
                { key: "avail", label: "可用", children: money(snapshot?.available, 0) },
              ]}
            />
          </Card>
        </Col>
      </Row>

      <Workspace
        daily={daily}
        markets={markets}
        quote={quote}
        showMarkets
        feePanel={feePanel}
        extraTables={
          <Card
            title="谁在赚钱"
            extra={<Text type="secondary">区间累计</Text>}
            styles={{ body: { paddingTop: 0 } }}
          >
            <ContributionTable rows={contribution} labelFor={labelFor} />
            <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
              每个账户的回撤和夏普都基于它自己的权益曲线。组合的回撤在「图表」页单独算。
              {pnlDisagrees && (
                <>
                  <br />
                  各行盈亏之和（{signed(accountSum)}）和组合（{signed(periodPnl)}）不同：
                  组合只统计所有账户都同步过的日子，各账户只排除自己缺的那几天。
                </>
              )}
            </Text>
          </Card>
        }
      />
    </>
  );
}
