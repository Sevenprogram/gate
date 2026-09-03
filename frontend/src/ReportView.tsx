import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Spin, Table, Tabs, Typography } from "antd";
import type { ColumnsType } from "antd/es/table";
import { ReloadOutlined } from "@ant-design/icons";
import { api, type ReportSheet } from "./api";
import { usePlMode, plColors } from "./palette";

const { Text } = Typography;

/* 报告视图：网页直接展示导出报告的全部 sheet。
 *
 * 数据来自 /api/report/sheets —— 后端跑 export_report.py 生成同一份 Excel
 * 再解析回来，所以网页和导出文件永远同口径同数字。sheet 顺序由后端排好：
 * 手续费/返佣核对的四张在最前，顶部再把合计数字放大一遍。 */

type Cell = string | number | null;

/** 底部 sheet 标签用短名——11 个标签要在一行放得下。 */
const SHEET_LABELS: Record<string, string> = {
  "合约·每日核对(自然日)": "每日核对·自然日",
  "合约·每日核对(8点交易日)": "每日核对·8点",
  "合约·每周汇总": "每周汇总",
  "合约·每月汇总": "每月汇总",
  "合约·平仓记录": "平仓记录",
  "合约·按合约汇总": "按合约",
  "现货·流水明细": "现货流水",
  "现货·每日核对(自然日)": "现货·自然日",
  "现货·每日核对(8点交易日)": "现货·8点",
  "现货·按币种汇总": "按币种",
  "现货·按类型汇总": "按类型",
};

function sheetLabel(name: string): string {
  return SHEET_LABELS[name] ?? name.replace("合约·", "").replace("现货·", "现·");
}

/** 数值列右对齐；这些列名按正负着色（盈亏/返佣/核对类）。 */
const PL_HEADER = /盈亏|返佣|预计|净变动|变动/;

function fmtCell(value: Cell, header: string): string {
  if (value == null) return "";
  if (typeof value === "number") {
    // 胜率在 Excel 里是 0.0% 格式，解析回来是比值——网页还原成百分比。
    if (header === "胜率") return `${(value * 100).toFixed(1)}%`;
    if (Math.abs(value) >= 1e6) return value.toLocaleString("en-US", { maximumFractionDigits: 0 });
    return value.toLocaleString("en-US", { maximumFractionDigits: 4 });
  }
  // 日期列带 00:00:00 尾巴（openpyxl 读出的 datetime），去掉。
  if (/日期|周（周一起）|月份/.test(header)) return value.replace(" 00:00:00", "");
  return value;
}

/** 把双块 sheet（每周/每月汇总：左「自然日」右「8 点交易日」）按空列表头
 *  拆成两组列，各自渲染一张表上下排列——28 列的宽表在网页上没法读。 */
function splitBlocks(sheet: ReportSheet): Array<{ label: string; start: number; end: number }> {
  if (!sheet.block_labels || sheet.block_labels.length < 2) return [];
  const groups: Array<{ start: number; end: number }> = [];
  let start = -1;
  sheet.headers.forEach((h, i) => {
    if (h) {
      if (start < 0) start = i;
    } else if (start >= 0) {
      groups.push({ start, end: i });
      start = -1;
    }
  });
  if (start >= 0) groups.push({ start, end: sheet.headers.length });
  const usable = groups.filter((g) => g.end - g.start > 3);
  return usable.map((g, i) => ({
    label: sheet.block_labels?.[i] ?? `块 ${i + 1}`,
    ...g,
  }));
}

function SheetTable({ sheet }: { sheet: ReportSheet }) {
  usePlMode();
  const colors = plColors();

  const buildColumns = (start: number, end: number, tag = "") =>
    sheet.headers.slice(start, end).map((header, j) => {
      const i = start + j;
      return {
        title: header || `列${j + 1}`,
        key: `${tag}-${i}`,
        align: (j === 0 ? "left" : "right") as "left" | "right",
        render: (_: unknown, row: { key: string; cells: Cell[] }) => {
          const value = row.cells[j] ?? null;
          const text = fmtCell(value, header);
          const isSummary = row.cells[0] === "合计";
          const numeric = typeof value === "number";
          const colored = numeric && PL_HEADER.test(header);
          if (isSummary) {
            return <b style={{ color: colored && numeric ? (value > 0 ? colors.profit : colors.loss) : "#111827" }}>{text}</b>;
          }
          if (colored && numeric && value !== 0) {
            return <span style={{ color: value > 0 ? colors.profit : colors.loss }}>{text}</span>;
          }
          return <span style={{ color: j === 0 ? "#111827" : undefined }}>{text}</span>;
        },
      };
    });

  const columns: ColumnsType<{ key: string; cells: Cell[] }> = useMemo(
    () => buildColumns(0, sheet.headers.length) as ColumnsType<{ key: string; cells: Cell[] }>,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sheet, colors],
  );
  const blocks = useMemo(() => splitBlocks(sheet), [sheet]);

  return (
    <Card
      size="small"
      styles={{ body: { paddingTop: 0 } }}
      extra={<Text type="secondary">{sheet.rows.length} 行</Text>}
      title={sheet.block_labels ? `${sheet.name}（左：${sheet.block_labels[0]} ｜ 右：${sheet.block_labels[1] ?? ""}）` : sheet.name}
    >
      {blocks.length >= 2
        ? blocks.map((block) => {
            const keyed = sheet.rows.map((r, i) => ({
              key: `${block.label}-${i}`,
              cells: r.slice(block.start, block.end),
            }));
            return (
              <div key={block.label}>
                <div
                  style={{
                    fontSize: 11,
                    color: "#6b7280",
                    padding: "6px 12px",
                    background: "#f9fafb",
                    borderTop: "1px solid #f0f1f3",
                  }}
                >
                  {block.label}
                </div>
                <Table
                  size="small"
                  rowKey="key"
                  columns={buildColumns(block.start, block.end, block.label)}
                  dataSource={keyed}
                  pagination={{ pageSize: 50, showSizeChanger: true, pageSizeOptions: [50, 100] }}
                  scroll={{ y: 420, x: true }}
                />
              </div>
            );
          })
        : (() => {
            const keyed = sheet.rows.map((r, i) => ({ key: String(i), cells: r }));
            return (
              <Table
                size="small"
                rowKey="key"
                columns={columns}
                dataSource={keyed}
                pagination={{
                  pageSize: 50,
                  showSizeChanger: true,
                  pageSizeOptions: [50, 100, 200],
                }}
                scroll={{ y: 520, x: true }}
              />
            );
          })()}
    </Card>
  );
}

/** 从首张核对 sheet 的「合计」行取手续费/返佣四个数，放大展示。 */
function FeeRebateHero({ sheet }: { sheet: ReportSheet | undefined }) {
  usePlMode();
  const colors = plColors();
  if (!sheet) return null;

  const totals = sheet.rows.find((r) => r[0] === "合计");
  if (!totals) return null;
  const byHeader = new Map<string, Cell>();
  sheet.headers.forEach((h, i) => byHeader.set(h, totals[i] ?? null));

  const fee = Number(byHeader.get("手续费") ?? 0);
  const fee70 = Number(byHeader.get("70%手续费") ?? 0);
  const rebate = Number(byHeader.get("当日返佣") ?? 0);
  const extra = Number(byHeader.get("预计额外返佣") ?? 0);

  const cells: Array<{
    label: string;
    value: string;
    color: string;
    note: string;
    tint: string;
    edge?: string;
  }> = [
    {
      label: "手续费合计",
      value: fmtCell(Math.abs(fee), "手续费"),
      color: "#111827",
      note: `${sheet.rows.length - 1} 个交易日累计`,
      tint: "#f8f9fc",
    },
    {
      label: "70% 应返",
      value: fmtCell(fee70, "70%手续费"),
      color: "#2563eb",
      note: "手续费 × 70% 返佣口径",
      tint: "rgba(37, 99, 235, 0.05)",
    },
    {
      label: "实际返佣到账",
      value: fmtCell(rebate, "返佣"),
      color: colors.profit,
      note: "现货账本实际到账",
      tint: "rgba(21, 128, 61, 0.05)",
    },
    {
      label: "预计额外返佣",
      value: `${extra > 0 ? "+" : ""}${fmtCell(extra, "预计额外返佣")}`,
      color: extra > 0 ? "#ea580c" : colors.loss,
      note: "70% 应返 − 到账，还没回来的部分",
      tint: "rgba(234, 88, 12, 0.06)",
      edge: extra > 0 ? "#ea580c" : undefined,
    },
  ];

  return (
    <Card size="small" style={{ marginBottom: 12 }} styles={{ body: { padding: 0 } }}>
      <div style={{ display: "flex", flexWrap: "wrap" }}>
        {cells.map((cell, i) => (
          <div
            key={cell.label}
            style={{
              flex: "1 1 0",
              minWidth: 0,
              padding: "14px 18px",
              borderLeft: i === 0 ? "none" : "1px solid #eef0f4",
              background: cell.tint,
              borderTop: cell.edge ? `2.5px solid ${cell.edge}` : undefined,
            }}
          >
            <div style={{ fontSize: 11, color: "#6b7280", letterSpacing: "0.02em" }}>
              {cell.label}
            </div>
            <div
              style={{
                fontFamily: "'SF Mono', ui-monospace, Menlo, monospace",
                fontWeight: 700,
                fontSize: cell.edge ? 26 : 22,
                lineHeight: 1.2,
                color: cell.color,
                fontVariantNumeric: "tabular-nums",
              }}
            >
              {cell.value}
            </div>
            <div style={{ fontSize: 11, color: "#9ca3af", marginTop: 2 }}>{cell.note}</div>
          </div>
        ))}
      </div>
    </Card>
  );
}

export function ReportView({ days }: { days: number }) {
  const [sheets, setSheets] = useState<ReportSheet[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async (refresh: boolean) => {
    setLoading(true);
    setError(null);
    try {
      const payload = await api.reportSheets(days, refresh);
      setSheets(payload.sheets);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setSheets(null);
    void load(false);
  }, [days]);

  const firstFeeSheet = sheets?.[0];

  return (
    <>
      {error && (
        <Alert type="error" showIcon message="报告生成失败" description={error} style={{ marginBottom: 12 }} />
      )}

      {sheets && sheets.length > 0 && <FeeRebateHero sheet={firstFeeSheet} />}

      {sheets && sheets.length > 0 && (
        <div style={{ opacity: loading ? 0.5 : 1 }}>
          <Tabs
            defaultActiveKey="0"
            
            items={sheets.map((sheet, i) => ({
              key: String(i),
              label: sheetLabel(sheet.name),
              children: <SheetTable sheet={sheet} />,
            }))}
          />
        </div>
      )}

      {!sheets && !error && (
        <div style={{ padding: "60px 0", textAlign: "center" }}>
          <Spin size="large" />
          <div style={{ marginTop: 12, color: "#6b7280", fontSize: 12.5 }}>
            正在生成报告：拉取合约平仓 + 现货流水，约需几秒……
          </div>
        </div>
      )}

      {sheets && (
        <div style={{ textAlign: "right", marginTop: 4 }}>
          <Button size="small" icon={<ReloadOutlined spin={loading} />} onClick={() => void load(true)}>
            重新生成
          </Button>
        </div>
      )}
    </>
  );
}
