import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  ConfigProvider,
  Layout,
  Segmented,
  Space,
  Spin,
  theme as antdTheme,
  Typography,
} from "antd";
import { DownloadOutlined, ReloadOutlined, SettingOutlined } from "@ant-design/icons";
import zhCN from "antd/locale/zh_CN";
import "dayjs/locale/zh-cn";
import dayjs from "dayjs";
import {
  ACCOUNT_LABELS,
  api,
  ApiError,
  type AccountKey,
  type AppConfig,
  type ContributionRow,
  type DailyPayload,
  type LedgerRow,
  type MarketRow,
  type PortfolioSnapshot,
  type PositionHistoryRow,
  type ProfileInfo,
  type RiskEvents,
  type Snapshot,
  type SyncResult,
} from "./api";
import { clockFromUnix } from "./format";
import { FeePanel } from "./FeePanel";
import { ReportView } from "./ReportView";
import { ProfileManager } from "./ProfileManager";
import { setPlMode, usePlMode } from "./palette";
import { FuturesView, PortfolioView, SpotView, UnifiedView } from "./views";

const { Header, Content } = Layout;
const { Title, Text } = Typography;

const RANGES = [
  { label: "7D", value: "7" },
  { label: "30D", value: "30" },
  { label: "90D", value: "90" },
  { label: "1Y", value: "365" },
];


/** What the page is showing: the portfolio total, or one account's product. */
type Selection =
  | { kind: "report" }
  | { kind: "portfolio" }
  | { kind: "account"; profile: string; account: AccountKey };

function selectionKey(selection: Selection): string {
  if (selection.kind === "report") return "report";
  return selection.kind === "portfolio"
    ? "portfolio"
    : `a:${selection.profile}/${selection.account}`;
}

function isSelectionValid(selection: Selection, config: AppConfig): boolean {
  if (selection.kind === "report") return true;
  if (selection.kind === "portfolio") return config.aggregatable.length > 1;
  const profile = config.profiles.find((p) => p.id === selection.profile);
  return profile != null && profile.accounts.includes(selection.account);
}

function defaultSelection(_config: AppConfig): Selection | null {
  // The report is the landing view: it mirrors the Excel export exactly.
  return { kind: "report" };
}

interface ViewData {
  snapshot: Snapshot | null;
  portfolio: PortfolioSnapshot | null;
  daily: DailyPayload | null;
  markets: MarketRow[];
  risk: RiskEvents | null;
  contribution: ContributionRow[];
  positionHistory: PositionHistoryRow[];
  ledger: LedgerRow[];
  errors: string[];
}

const EMPTY: ViewData = {
  snapshot: null,
  portfolio: null,
  daily: null,
  markets: [],
  risk: null,
  contribution: [],
  positionHistory: [],
  ledger: [],
  errors: [],
};

function ErrorList({ title, errors }: { title: string; errors: string[] }) {
  return (
    <Alert
      type="warning"
      showIcon
      message={title}
      description={
        <ul style={{ margin: 0, paddingLeft: 17 }}>
          {errors.map((error) => (
            <li key={error}>{error}</li>
          ))}
        </ul>
      }
      style={{ marginBottom: 16 }}
    />
  );
}

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [days, setDays] = useState(30);
  const [data, setData] = useState<ViewData>(EMPTY);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [lastSync, setLastSync] = useState<SyncResult | null>(null);
  const [managerOpen, setManagerOpen] = useState(false);

  const pl = usePlMode();

  useEffect(() => {
    dayjs.locale("zh-cn");
    api
      .config()
      .then((cfg) => {
        setConfig(cfg);
        setSelection((current) => current ?? defaultSelection(cfg));
      })
      .catch((error: unknown) =>
        setConfigError(error instanceof Error ? error.message : "无法连接后端"),
      );
  }, []);

  const load = useCallback(async (target: Selection, window: number) => {
    setLoading(true);
    const errors: string[] = [];
    const settle = async <T,>(
      label: string,
      promise: Promise<T>,
      fallback: T,
    ): Promise<T> => {
      try {
        return await promise;
      } catch (error) {
        const detail =
          error instanceof ApiError
            ? error.message
            : error instanceof Error
              ? error.message
              : String(error);
        errors.push(`${label}：${detail}`);
        return fallback;
      }
    };

    if (target.kind === "report") {
      return; // the report view fetches its own data
    }

    if (target.kind === "portfolio") {
      const [portfolio, daily, contribution, markets] = await Promise.all([
        settle("组合快照", api.portfolioSnapshot(), null as PortfolioSnapshot | null),
        settle("组合盈亏", api.portfolioDaily(window), null as DailyPayload | null),
        settle("分账户对比", api.contribution(window), [] as ContributionRow[]),
        settle("分市场", api.portfolioMarkets(Math.min(window, 365)), [] as MarketRow[]),
      ]);
      setData({ ...EMPTY, portfolio, daily, contribution, markets, errors });
      setLoading(false);
      return;
    }

    const { profile, account } = target;
    const [snapshot, daily, markets, risk, positionHistory, ledger] = await Promise.all([
      settle("实时快照", api.snapshot(profile, account), null as Snapshot | null),
      settle("每日盈亏", api.daily(profile, account, window), null as DailyPayload | null),
      settle("分市场", api.markets(profile, account, Math.min(window, 365)), [] as MarketRow[]),
      account.startsWith("futures")
        ? settle("风险事件", api.risk(profile, account), null as RiskEvents | null)
        : Promise.resolve(null),
      account.startsWith("futures")
        ? settle("历史仓位", api.positionHistory(profile, account), [] as PositionHistoryRow[])
        : Promise.resolve([] as PositionHistoryRow[]),
      account === "spot"
        ? settle("账本", api.ledger(profile, account), [] as LedgerRow[])
        : Promise.resolve([] as LedgerRow[]),
    ]);

    setData({
      ...EMPTY,
      snapshot,
      daily,
      markets,
      risk,
      positionHistory,
      ledger,
      errors,
    });
    setLoading(false);
  }, []);

  useEffect(() => {
    if (selection && selection.kind !== "report") void load(selection, days);
  }, [selection, days, load]);

  const refreshConfig = useCallback(async () => {
    const cfg = await api.config();
    setConfig(cfg);
    setSelection((current) =>
      current && isSelectionValid(current, cfg) ? current : defaultSelection(cfg),
    );
  }, []);

  const refresh = useCallback(async () => {
    if (!selection) return;
    setSyncing(true);
    try {
      const result = await api.sync(Math.max(days, 30));
      setLastSync(result);
      setConfig(await api.config());
      await load(selection, days);
    } catch (error) {
      setData((current) => ({
        ...current,
        errors: [
          ...current.errors,
          `同步失败：${error instanceof Error ? error.message : String(error)}`,
        ],
      }));
    } finally {
      setSyncing(false);
    }
  }, [selection, days, load]);

  const labelFor = useCallback(
    (profileId: string) =>
      config?.profiles.find((p) => p.id === profileId)?.label ?? profileId,
    [config],
  );

  const scopeCount = config?.aggregatable.length ?? 0;
  const showPortfolio = scopeCount > 1;

  if (configError) {
    return (
      <ConfigProvider locale={zhCN}>
        <div style={{ maxWidth: 720, margin: "80px auto", padding: "0 24px" }}>
          <Alert
            type="error"
            showIcon
            message="连不上后端"
            description={
              <>
                {configError}
                <div style={{ marginTop: 8 }}>
                  确认 FastAPI 已启动：<code>./.venv/bin/uvicorn backend.app.main:app --reload</code>
                </div>
              </>
            }
          />
        </div>
      </ConfigProvider>
    );
  }

  if (!config || !selection) {
    return (
      <ConfigProvider locale={zhCN}>
        <div style={{ padding: "80px 0", textAlign: "center" }}>
          <Spin size="large" />
        </div>
      </ConfigProvider>
    );
  }

  // Top-nav items: portfolio (when it sums to something) + every account
  // product. With a single profile the product name stands alone.
  const navItems: Array<{ value: string; label: string }> = [
      ...(showPortfolio
        ? [{ value: "portfolio", label: `组合 (${scopeCount})` }]
        : []),
      ...config.profiles.flatMap((profile: ProfileInfo) =>
        profile.accounts.map((account: AccountKey) => ({
          value: `a:${profile.id}/${account}`,
          label:
            config.profiles.length === 1
              ? ACCOUNT_LABELS[account]
              : `${profile.label}·${ACCOUNT_LABELS[account]}`,
        })),
      ),
    ];

    const onNav = (key: string) => {
      if (key === "report") {
        setSelection({ kind: "report" });
        return;
      }
      if (key === "portfolio") {
        setSelection({ kind: "portfolio" });
        return;
      }
      const rest = key.slice(2);
      const slash = rest.indexOf("/");
      setSelection({
        kind: "account",
        profile: rest.slice(0, slash),
        account: rest.slice(slash + 1) as AccountKey,
      });
    };

    const feePanel = (
      <FeePanel
        config={config}
        scope={
          selection.kind === "account"
            ? { profile: selection.profile, account: selection.account }
            : null
        }
        days={days}
      />
    );

    const headerTitle =
      selection.kind === "report"
        ? "交易报告"
        : selection.kind === "portfolio"
          ? `组合总览 (${scopeCount})`
          : `${labelFor(selection.profile)} · ${ACCOUNT_LABELS[selection.account]}`;

    return (
      <ConfigProvider
        locale={zhCN}
        theme={{
          algorithm: antdTheme.defaultAlgorithm,
          token: {
            colorBgBase: "#f5f6f8",
            colorPrimary: "#2563eb",
            colorInfo: "#2563eb",
            borderRadius: 4,
            fontSize: 12.5,
          },
          components: {
            Table: {
              headerBg: "#f3f4f6",
              headerColor: "#6b7280",
              rowHoverBg: "#f2f4f7",
              borderColor: "#e5e7eb",
              colorBgContainer: "#ffffff",
            },
            Card: { colorBgContainer: "#ffffff", headerBg: "transparent" },
          },
        }}
      >
      <Layout style={{ minHeight: "100vh" }}>
          <Header
            style={{
              display: "flex",
              alignItems: "center",
              gap: 16,
              padding: "0 20px",
              height: 46,
              lineHeight: "46px",
              borderBottom: "1px solid #e5e7eb",
              background: "#ffffff",
              position: "sticky",
              top: 0,
              zIndex: 20,
            }}
          >
            <Text strong style={{ fontSize: 14, color: "#2563eb", whiteSpace: "nowrap" }}>
              GATE
            </Text>
            <Text type="secondary" style={{ fontSize: 11, whiteSpace: "nowrap" }}>
              每日查账
            </Text>
            <Segmented
              value={selectionKey(selection)}
              onChange={(v) => onNav(v as string)}
              options={navItems}
            />
            <Segmented
              value={String(days)}
              onChange={(v) => setDays(Number(v))}
              options={RANGES}
            />
            <div style={{ flex: 1 }} />
            <Space size={6}>
              <Button
                size="small"
                onClick={() => setPlMode(pl === "cn" ? "cvd" : "cn")}
                title="切换盈亏配色（红绿 / 红蓝）"
              >
                {pl === "cn" ? "红/绿" : "红/蓝"}
              </Button>
              <Button
                size="small"
                icon={<DownloadOutlined />}
                onClick={() =>
                  window.open(`/api/export/report?days=${days}`, "_blank")
                }
              >
                报告
              </Button>
              <Button size="small" icon={<SettingOutlined />} onClick={() => setManagerOpen(true)}>
                账户
              </Button>
              <Button
                size="small"
                type="primary"
                icon={<ReloadOutlined spin={syncing} />}
                loading={syncing}
                onClick={() => void refresh()}
              >
                同步
              </Button>
            </Space>
          </Header>
          <Content style={{ padding: "14px 20px 64px", minWidth: 0 }}>
            <Text
              type="secondary"
              style={{ fontSize: 11, display: "block", marginBottom: 2 }}
            >
              最后同步 {config.last_sync ? clockFromUnix(config.last_sync) : "从未"} ·{" "}
              {config.timezone} · {config.quote}
            </Text>
            <div
              style={{
                display: "flex",
                alignItems: "baseline",
                justifyContent: "space-between",
                flexWrap: "wrap",
                gap: 12,
                padding: "26px 0 16px",
              }}
            >
              <Title level={3} style={{ margin: 0 }}>
                {headerTitle}
              </Title>
              <Text type="secondary">
                {config.timezone} · {config.quote}
                {config.demo && (
                  <Text type="danger" style={{ marginLeft: 8 }}>
                    演示模式
                  </Text>
                )}
              </Text>
            </div>

            {config.demo && (
              <Alert
                type="error"
                showIcon
                message="演示模式：账户数字是编造的"
                description="后端设了 DASHBOARD_DEMO=1，持仓、余额、强平价全部是合成数据，不要据此做任何交易决定。"
                style={{ marginBottom: 16 }}
              />
            )}

            {!config.has_credentials && !config.demo && (
              <Alert
                type="warning"
                showIcon
                message="还没有配置 API 密钥"
                description="点左侧「账户」按钮直接添加，或把 profiles.example.toml 复制成 profiles.toml 手工填写。每个 key 都要只读权限（不要勾交易和提现），并绑定 IP 白名单。"
                style={{ marginBottom: 16 }}
              />
            )}

            {lastSync && lastSync.errors.length > 0 && (
              <ErrorList
                title={`同步完成，但有 ${lastSync.errors.length} 项失败`}
                errors={lastSync.errors}
              />
            )}
            {data.errors.length > 0 && (
              <ErrorList title="部分面板读取失败" errors={data.errors} />
            )}

            {managerOpen && (
              <ProfileManager
                config={config}
                onClose={() => setManagerOpen(false)}
                onChanged={refreshConfig}
              />
            )}

            <Spin spinning={loading} classNames={{ root: "view-spin", container: "view-spin-inner" }}>
              <div key={selectionKey(selection)}>
                {selection.kind === "report" ? (
                  <ReportView days={days} />
                ) : selection.kind === "portfolio" ? (
                  <PortfolioView
                    snapshot={data.portfolio}
                    daily={data.daily}
                    contribution={data.contribution}
                    markets={data.markets}
                    quote={config.quote}
                    today={config.today}
                    labelFor={labelFor}
                    feePanel={feePanel}
                  />
                ) : (
                  <AccountView
                    account={selection.account}
                    data={data}
                    quote={config.quote}
                    today={config.today}
                    feePanel={feePanel}
                  />
                )}
              </div>
            </Spin>
          </Content>
      </Layout>
    </ConfigProvider>
  );
}

function AccountView({
  account,
  data,
  quote,
  today,
  feePanel,
}: {
  account: AccountKey;
  data: ViewData;
  quote: string;
  today: string;
  feePanel: React.ReactNode;
}) {
  const props = {
    account,
    snapshot: data.snapshot,
    daily: data.daily,
    markets: data.markets,
    risk: data.risk,
    quote,
    today,
    feePanel,
    positionHistory: data.positionHistory,
    ledger: data.ledger,
  };
  if (account === "spot") return <SpotView {...props} />;
  if (account === "unified") return <UnifiedView {...props} />;
  return <FuturesView {...props} />;
}
