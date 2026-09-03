#!/usr/bin/env python3
"""Gate 交易报告导出（合约平仓 + 现货流水 → 一个 Excel）。

单文件、独立运行。一个 HTTP 会话里拉齐：

  合约  /api/v4/futures/{settle}/position_close   平仓历史（分页）
        /api/v4/futures/{settle}/contracts/{c}    合约乘数（算名义交易量）
  现货  /api/v4/spot/account_book                 资金流水（30 天切片）

现货账本同时承担两个用途：现货流水明细，以及合约每日核对里
「当日返佣 / 预计额外返佣」的实际返佣数据（只拉一次）。

输出一个 Excel，九个 sheet：

  合约·平仓记录 / 每日核对(自然日) / 每日核对(8点交易日)
        每周汇总 / 每月汇总 —— 每张表内左右并排「自然日」与「8 点交易日」两个块
        按合约汇总
  现货·流水明细 / 每日核对(自然日) / 每日核对(8点交易日) / 按币种汇总 / 按类型汇总

用法：

    ./.venv/bin/python export_report.py                  # 近 90 天
    ./.venv/bin/python export_report.py --days 365
    ./.venv/bin/python export_report.py --settle btc     # 币本位合约
    ./.venv/bin/python export_report.py -o 报告.xlsx

密钥读取顺序：环境变量 GATE_API_KEY / GATE_API_SECRET，其次 profiles.toml。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

API_HOST = "https://api.gateio.ws"
EMPTY_HASH = hashlib.sha512(b"").hexdigest()

# 现货账本单次 from/to 窗口上限：30 天可用，90 天报 invalid time range。
SLICE_DAYS = 30

TZ = timezone(timedelta(hours=8))


class GateError(RuntimeError):
    pass


@dataclass
class Credentials:
    key: str
    secret: str
    source: str


def load_credentials() -> Credentials:
    key = os.getenv("GATE_API_KEY", "").strip()
    secret = os.getenv("GATE_API_SECRET", "").strip()
    if key and secret:
        return Credentials(key, secret, "环境变量")

    profiles = Path(__file__).parent / "profiles.toml"
    if profiles.is_file():
        with profiles.open("rb") as handle:
            document = tomllib.load(handle)
        for profile in document.get("profile", []):
            if profile.get("key") and profile.get("secret"):
                return Credentials(
                    str(profile["key"]),
                    str(profile["secret"]),
                    f"profiles.toml:{profile['id']}",
                )

    sys.exit("找不到密钥。设置 GATE_API_KEY / GATE_API_SECRET，或在 profiles.toml 里填一个。")


def signed_get(client: httpx.Client, creds: Credentials, path: str, params: dict) -> list | dict:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    timestamp = str(int(time.time()))
    sign_string = "\n".join(["GET", path, query, EMPTY_HASH, timestamp])
    sign = hmac.new(creds.secret.encode(), sign_string.encode(), hashlib.sha512).hexdigest()

    response = client.get(
        f"{API_HOST}{path}",
        params=params,
        headers={
            "KEY": creds.key,
            "Timestamp": timestamp,
            "SIGN": sign,
            "Accept": "application/json",
        },
    )
    if response.status_code >= 400:
        try:
            body = response.json()
            detail = f"{body.get('label')}: {body.get('message')}"
        except ValueError:
            detail = response.text[:300]
        raise GateError(f"{path} 返回 {response.status_code} — {detail}")
    return response.json()


def fetch_window(
    client: httpx.Client,
    creds: Credentials,
    path: str,
    start: int,
    end: int,
    extra_params: dict | None = None,
    row_key: Any = None,
) -> list[dict]:
    """按 30 天切片拉全窗口（现货账本的窗口限制），片内时间游标翻页。

    row_key 给出每行的唯一 id（默认取 id 字段）用于跨切片去重；条目里
    部分类型的 time 是毫秒，入口统一归一化成秒。
    """

    def key_of(row: dict) -> str:
        if row_key is not None:
            return row_key(row)
        return str(
            row.get("id")
            or f"{row.get('time')}:{row.get('currency')}:{row.get('change')}:{row.get('balance')}"
        )

    rows: list[dict] = []
    seen: set[str] = set()
    slice_start = start
    while slice_start < end:
        slice_end = min(slice_start + SLICE_DAYS * 86400, end)
        cursor = slice_end
        while True:
            batch = signed_get(
                client,
                creds,
                path,
                {"from": slice_start, "to": cursor, "limit": 1000, **(extra_params or {})},
            )
            if not isinstance(batch, list) or not batch:
                break
            for row in batch:
                raw = float(row.get("time", 0) or 0)
                if raw > 1e11:
                    row["time"] = raw / 1000
                k = key_of(row)
                if k not in seen:
                    seen.add(k)
                    rows.append(row)
            if len(batch) < 1000:
                break
            oldest = min(int(float(r.get("time", slice_start))) for r in batch)
            if oldest <= slice_start:
                break
            cursor = (
                oldest - 1
                if all(int(float(r.get("time", 0))) >= oldest for r in batch)
                else oldest
            )
        slice_start = slice_end
    return rows


# ------------------------------------------------------------------ 时间与样式

def excel_time(ts: float) -> datetime:
    """真正的 datetime 值（而非文本），Excel 里可排序、可分组、可筛选。"""
    return datetime.fromtimestamp(float(ts), tz=TZ).replace(tzinfo=None)


def session_label(dt: datetime) -> str:
    return "8 点前" if dt.hour < 8 else "8 点后"


def bucket_day(ts: float, boundary_hour: int) -> date:
    """一条记录归属哪一天：0 点界是自然日，8 点界下 8 点前归前一天。"""
    dt = excel_time(ts)
    if boundary_hour == 8 and dt.hour < 8:
        return (dt - timedelta(days=1)).date()
    return dt.date()


HEADER_FILL = PatternFill("solid", fgColor="1F2937")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
RED = Font(color="C0392B", size=10)
GREEN = Font(color="1E8449", size=10)
PLAIN = Font(size=10)
BOLD = Font(bold=True, size=10)
MUTED = Font(color="9CA3AF", size=10)


def style_header(ws, headers: list[tuple[str, int]]) -> None:
    for col, (title, width) in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"


def num(v: Any) -> float | None:
    return float(v) if v not in (None, "") else None


def week_key(day: date) -> date:
    return day - timedelta(days=day.weekday())


# ------------------------------------------------------------------ 合约部分

def fetch_multipliers(
    client: httpx.Client, creds: Credentials, settle: str, contracts: list[str]
) -> dict[str, float]:
    """每张合约的乘数。名义价值 = 价格 × 张数 × 乘数；"÷100" 只是乘数 0.01
    的合约（SNDK、ETH 等）的简写，BTC 是 0.0001，有些 meme 合约是 100。"""
    out: dict[str, float] = {}
    for contract in contracts:
        detail = signed_get(client, creds, f"/api/v4/futures/{settle}/contracts/{contract}", {})
        out[contract] = float(detail.get("quanto_multiplier", 0) or 0)
    return out


def human_duration(seconds: float) -> str:
    if seconds <= 0:
        return ""
    hours = seconds / 3600
    if hours < 1:
        return f"{seconds / 60:.0f} 分钟"
    if hours < 48:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


FUTURES_COLUMNS = [
    ("平仓时间", 19), ("日期", 11), ("时段", 8), ("合约", 13), ("方向", 6),
    ("开仓均价", 11), ("平仓均价", 11), ("平仓盈亏", 11), ("价格盈亏", 11),
    ("手续费", 11), ("资金费", 10), ("持仓时长", 10), ("开仓时间", 19),
    ("最大仓位", 10), ("成交量", 10), ("杠杆", 6), ("保证金模式", 11), ("备注", 20),
]


def write_futures_detail(wb: Workbook, rows: list[dict], quote: str) -> None:
    ws = wb.active
    ws.title = "合约·平仓记录"
    style_header(ws, FUTURES_COLUMNS)

    for i, row in enumerate(
        sorted(rows, key=lambda r: float(r.get("time", 0)), reverse=True), start=2
    ):
        ts = float(row.get("time", 0))
        first_open = row.get("first_open_time")
        side = row.get("side", "")
        # 多头：long_price 开仓、short_price 平仓；空头相反。
        entry = num(row.get("long_price" if side == "long" else "short_price"))
        exit_ = num(row.get("short_price" if side == "long" else "long_price"))
        close_dt = excel_time(ts)
        values: list[Any] = [
            close_dt, close_dt.date(), session_label(close_dt),
            row.get("contract", ""),
            {"long": "多", "short": "空"}.get(side, side),
            entry, exit_,
            num(row.get("pnl")), num(row.get("pnl_pnl")),
            num(row.get("pnl_fee")), num(row.get("pnl_fund")),
            human_duration(ts - float(first_open)) if first_open else "",
            excel_time(float(first_open)) if first_open else None,
            num(row.get("max_size")), num(row.get("accum_size")),
            num(row.get("lever")), row.get("margin_mode", ""), row.get("text", ""),
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=i, column=col, value=value)
            if col == 1:
                cell.number_format = "yyyy-mm-dd hh:mm:ss"
                cell.font = PLAIN
            elif col == 2:
                cell.number_format = "yyyy-mm-dd"
                cell.font = PLAIN
            elif col == 3:
                cell.font = GREEN if value == "8 点后" else RED
                cell.alignment = Alignment(horizontal="center")
            elif col in (6, 7) and isinstance(value, (int, float)):
                cell.number_format = "#,##0.0000"
                cell.font = PLAIN
            elif col in (8, 9) and isinstance(value, (int, float)):
                cell.number_format = f'#,##0.00 "{quote}"'
                cell.font = RED if value < 0 else GREEN
            elif col in (10, 11) and isinstance(value, (int, float)):
                cell.number_format = f'#,##0.00 "{quote}"'
                cell.font = MUTED
            elif col == 13 and isinstance(value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm:ss"
                cell.font = PLAIN
            elif col == 18:
                cell.font = MUTED
            else:
                cell.font = PLAIN


def period_key(day: date, period: str) -> date:
    """周/月分组键：周一或当月 1 号。period='day' 原样返回。"""
    if period == "week":
        return week_key(day)
    if period == "month":
        return day.replace(day=1)
    return day


def futures_period_rows(
    rows: list[dict], boundary_hour: int, multipliers: dict[str, float], period: str = "day"
) -> list[dict]:
    """合约按天/周/月聚合。交易量 = 开仓名义 + 平仓名义（各计一次成交）。"""
    groups: dict[date, list[dict]] = {}
    for row in rows:
        groups.setdefault(
            period_key(bucket_day(float(row.get("time", 0)), boundary_hour), period), []
        ).append(row)

    out = []
    for day in sorted(groups):
        group = groups[day]
        volume = 0.0
        for row in group:
            side = row.get("side", "")
            entry = float(row.get("long_price" if side == "long" else "short_price", 0) or 0)
            exit_ = float(row.get("short_price" if side == "long" else "long_price", 0) or 0)
            size = abs(float(row.get("accum_size", 0) or 0))
            multiplier = multipliers.get(row.get("contract", ""), 0.01)
            volume += (entry + exit_) * size * multiplier

        def total(field: str) -> float:
            return sum(float(r[field]) for r in group if r.get(field) not in (None, ""))

        pnls = [float(r["pnl"]) for r in group if r.get("pnl") not in (None, "")]
        out.append(
            {
                "day": day, "count": len(group), "volume": volume,
                "fee": total("pnl_fee"), "funding": total("pnl_fund"),
                "pnl_price": total("pnl_pnl"), "pnl": total("pnl"),
                "win_rate": sum(1 for p in pnls if p > 0) / len(pnls) if pnls else None,
            }
        )
    return out


def write_futures_daily(
    wb: Workbook,
    title: str,
    rows: list[dict],
    quote: str,
    rebates: dict[date, float],
) -> None:
    """合约每日核对：基础统计 + 70%/10%(周)/5% 手续费份额 + 预计额外返佣。

    预计额外返佣 = 70%手续费 − 当天现货实际返佣（按同一条日界分桶）。
    日期降序。
    """
    fee_by_week: dict[date, float] = {}
    for row in rows:
        fee_by_week[week_key(row["day"])] = (
            fee_by_week.get(week_key(row["day"]), 0.0) + abs(row["fee"])
        )

    ws = wb.create_sheet(title)
    style_header(
        ws,
        [
            ("日期", 12), ("平仓笔数", 9), ("交易量", 13), ("手续费", 11),
            ("70%手续费", 11), ("10%手续费(周)", 13), ("5%手续费", 11),
            ("当日返佣", 11), ("预计额外返佣", 13),
            ("资金费", 10), ("价格盈亏", 11), ("总盈亏", 11), ("胜率", 8),
        ],
    )

    for i, row in enumerate(reversed(rows), start=2):
        day: date = row["day"]
        fee_mag = abs(row["fee"])
        rebate = rebates.get(day, 0.0)
        values = [
            day, row["count"], row["volume"], row["fee"],
            fee_mag * 0.70,
            fee_by_week.get(week_key(day), 0.0) * 0.10,
            fee_mag * 0.05,
            rebate,
            fee_mag * 0.70 - rebate,
            row["funding"], row["pnl_price"], row["pnl"], row["win_rate"],
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=i, column=col, value=value)
            if col == 1:
                cell.number_format = "yyyy-mm-dd"
                cell.font = PLAIN
            elif col in (2, 13):
                cell.font = PLAIN
                if col == 13:
                    cell.number_format = "0.0%"
            elif col == 3:
                cell.number_format = f'#,##0 "{quote}"'
                cell.font = PLAIN
            elif col in (4, 10):
                cell.number_format = f'#,##0.00 "{quote}"'
                cell.font = MUTED
            elif col in (5, 6, 7):
                cell.number_format = f'#,##0.00 "{quote}"'
                cell.font = PLAIN
            elif col == 8:
                cell.number_format = f'#,##0.00 "{quote}"'
                cell.font = GREEN if value > 0 else PLAIN
            elif col == 9:
                cell.number_format = f'#,##0.00 "{quote}"'
                cell.font = GREEN if value > 0 else (RED if value < 0 else PLAIN)
            elif col in (11, 12):
                cell.number_format = f'#,##0.00 "{quote}"'
                cell.font = RED if value < 0 else GREEN

    total_row = len(rows) + 2
    total_fee_mag = sum(abs(r["fee"]) for r in rows)
    total_rebate = sum(rebates.get(r["day"], 0.0) for r in rows)
    ws.cell(row=total_row, column=1, value="合计").font = BOLD
    sums = [
        (2, sum(r["count"] for r in rows), "int"),
        (3, sum(r["volume"] for r in rows), "vol"),
        (4, sum(r["fee"] for r in rows), "money"),
        (5, total_fee_mag * 0.70, "money"),
        (6, total_fee_mag * 0.10, "money"),
        (7, total_fee_mag * 0.05, "money"),
        (8, total_rebate, "money"),
        (9, total_fee_mag * 0.70 - total_rebate, "money"),
        (10, sum(r["funding"] for r in rows), "money"),
        (11, sum(r["pnl_price"] for r in rows), "money"),
        (12, sum(r["pnl"] for r in rows), "money"),
    ]
    for col, value, kind in sums:
        cell = ws.cell(row=total_row, column=col, value=value)
        cell.font = BOLD
        cell.number_format = f'#,##0 "{quote}"' if kind == "vol" else f'#,##0.00 "{quote}"'


def write_futures_by_contract(wb: Workbook, rows: list[dict], quote: str) -> None:
    summary = wb.create_sheet("合约·按合约汇总")
    style_header(
        summary,
        [
            ("合约", 13), ("平仓次数", 10), ("总盈亏", 12), ("价格盈亏", 12),
            ("手续费", 11), ("资金费", 10), ("盈利次数", 10), ("亏损次数", 10),
            ("胜率", 8), ("平均盈亏", 11), ("最好一笔", 11), ("最差一笔", 11),
        ],
    )

    def component(group: list[dict], field: str) -> float:
        return sum(float(r[field]) for r in group if r.get(field) not in (None, ""))

    by_contract: dict[str, list[dict]] = {}
    for row in rows:
        by_contract.setdefault(row.get("contract", "?"), []).append(row)

    for i, (contract, group) in enumerate(
        sorted(by_contract.items(), key=lambda kv: component(kv[1], "pnl")), start=2
    ):
        pnls = [float(r["pnl"]) for r in group if r.get("pnl") not in (None, "")]
        wins = [p for p in pnls if p > 0]
        stats = [
            contract, len(group),
            component(group, "pnl"), component(group, "pnl_pnl"),
            component(group, "pnl_fee"), component(group, "pnl_fund"),
            len(wins), len(pnls) - len(wins),
            len(wins) / len(pnls) if pnls else None,
            sum(pnls) / len(pnls) if pnls else None,
            max(pnls) if pnls else None, min(pnls) if pnls else None,
        ]
        for col, value in enumerate(stats, start=1):
            cell = summary.cell(row=i, column=col, value=value)
            cell.font = PLAIN
            if col in (3, 4, 10, 11, 12) and isinstance(value, (int, float)):
                cell.number_format = f'#,##0.00 "{quote}"'
                cell.font = RED if value < 0 else GREEN
            if col in (5, 6) and isinstance(value, (int, float)):
                cell.number_format = f'#,##0.00 "{quote}"'
                cell.font = MUTED
            if col == 9 and isinstance(value, (int, float)):
                cell.number_format = "0.0%"


def write_futures_period(
    wb: Workbook,
    title: str,
    blocks: list[tuple[str, list[dict], dict[date, float]]],
    quote: str,
    period: str,
) -> None:
    """合约每周/每月汇总：手续费与返佣的周期级核对。

    一张表里左右并排两个块（如「自然日」与「8 点交易日」），方便对照同一
    周在两套口径下的差异。每个块独立排序（降序）与合计。10% 直接取该期
    手续费的 10%（每日表里的 10%(周) 是周内聚合，周期表里不再嵌套）。
    """
    first_header = "周（周一起）" if period == "week" else "月份"
    first_format = "yyyy-mm-dd" if period == "week" else "yyyy-mm"

    headers = [
        (first_header, 12), ("平仓笔数", 9), ("交易量", 13), ("手续费", 11),
        ("70%手续费", 11), ("10%手续费", 11), ("5%手续费", 10),
        ("当期返佣", 11), ("预计额外返佣", 13),
        ("资金费", 10), ("价格盈亏", 11), ("总盈亏", 11), ("胜率", 8),
    ]

    ws = wb.create_sheet(title)
    GAP = 2  # 两个块之间空两列
    base_col = 1
    for label, rows, rebates in blocks:
        # 块标题行
        title_cell = ws.cell(row=1, column=base_col, value=label)
        title_cell.font = Font(bold=True, size=11)
        # 表头行
        for offset, (h, width) in enumerate(headers):
            col = base_col + offset
            cell = ws.cell(row=2, column=col, value=h)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal="center")
            ws.column_dimensions[get_column_letter(col)].width = width

        for i, row in enumerate(reversed(rows), start=3):
            fee_mag = abs(row["fee"])
            rebate = rebates.get(row["day"], 0.0)
            values = [
                row["day"], row["count"], row["volume"], row["fee"],
                fee_mag * 0.70, fee_mag * 0.10, fee_mag * 0.05,
                rebate, fee_mag * 0.70 - rebate,
                row["funding"], row["pnl_price"], row["pnl"], row["win_rate"],
            ]
            for offset, value in enumerate(values):
                col = base_col + offset
                cell = ws.cell(row=i, column=col, value=value)
                pos = offset + 1  # 与单块布局相同的列语义
                if pos == 1:
                    cell.number_format = first_format
                    cell.font = PLAIN
                elif pos in (2, 13):
                    cell.font = PLAIN
                    if pos == 13:
                        cell.number_format = "0.0%"
                elif pos == 3:
                    cell.number_format = f'#,##0 "{quote}"'
                    cell.font = PLAIN
                elif pos in (4, 10):
                    cell.number_format = f'#,##0.00 "{quote}"'
                    cell.font = MUTED
                elif pos in (5, 6, 7):
                    cell.number_format = f'#,##0.00 "{quote}"'
                    cell.font = PLAIN
                elif pos == 8:
                    cell.number_format = f'#,##0.00 "{quote}"'
                    cell.font = GREEN if value > 0 else PLAIN
                elif pos == 9:
                    cell.number_format = f'#,##0.00 "{quote}"'
                    cell.font = GREEN if value > 0 else (RED if value < 0 else PLAIN)
                elif pos in (11, 12):
                    cell.number_format = f'#,##0.00 "{quote}"'
                    cell.font = RED if value < 0 else GREEN

        # 合计行（每个块各自的行数，互不影响）
        total_row = len(rows) + 3
        ws.cell(row=total_row, column=base_col, value="合计").font = BOLD
        total_fee_mag = sum(abs(r["fee"]) for r in rows)
        total_rebate = sum(rebates.get(r["day"], 0.0) for r in rows)
        sums = [
            (2, sum(r["count"] for r in rows), "int"),
            (3, sum(r["volume"] for r in rows), "vol"),
            (4, sum(r["fee"] for r in rows), "money"),
            (5, total_fee_mag * 0.70, "money"),
            (6, total_fee_mag * 0.10, "money"),
            (7, total_fee_mag * 0.05, "money"),
            (8, total_rebate, "money"),
            (9, total_fee_mag * 0.70 - total_rebate, "money"),
            (10, sum(r["funding"] for r in rows), "money"),
            (11, sum(r["pnl_price"] for r in rows), "money"),
            (12, sum(r["pnl"] for r in rows), "money"),
        ]
        for offset, value, kind in sums:
            cell = ws.cell(row=total_row, column=base_col + offset - 1, value=value)
            cell.font = BOLD
            cell.number_format = f'#,##0 "{quote}"' if kind == "vol" else f'#,##0.00 "{quote}"'

        base_col += len(headers) + GAP

    ws.freeze_panes = "A3"


# ------------------------------------------------------------------ 现货部分

SPOT_TYPE_LABELS = {
    "deposit": "充值", "withdraw": "提现", "trade": "成交", "fee": "手续费",
    "rebate": "返佣", "transfer": "划转", "liquidate": "强平", "settlement": "结算",
    "new_order": "下单冻结/解冻", "order_fill": "成交", "order_fee": "交易手续费",
    "perp_in": "转出至合约", "perp_out": "从合约转入", "pu_rebate": "返佣",
    "subaccount_trf": "子账户划转", "unknown": "未知",
}

REBATE_TYPES = {"rebate", "pu_rebate"}


def write_spot_detail(wb: Workbook, rows: list[dict]) -> None:
    ws = wb.create_sheet("现货·流水明细")
    style_header(
        ws,
        [
            ("时间", 19), ("日期", 11), ("时段", 8), ("币种", 8),
            ("类型", 14), ("变动", 16), ("余额", 16), ("备注", 26),
        ],
    )
    for i, row in enumerate(
        sorted(rows, key=lambda r: float(r.get("time", 0)), reverse=True), start=2
    ):
        dt = excel_time(float(row.get("time", 0)))
        values = [
            dt, dt.date(), session_label(dt),
            row.get("currency", ""),
            SPOT_TYPE_LABELS.get(row.get("type", ""), row.get("type", "")),
            num(row.get("change")), num(row.get("balance")),
            row.get("text", "") or "",
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=i, column=col, value=value)
            if col == 1:
                cell.number_format = "yyyy-mm-dd hh:mm:ss"
                cell.font = PLAIN
            elif col == 2:
                cell.number_format = "yyyy-mm-dd"
                cell.font = PLAIN
            elif col == 3:
                cell.font = GREEN if value == "8 点后" else RED
                cell.alignment = Alignment(horizontal="center")
            elif col in (6, 7):
                cell.number_format = "#,##0.00######"
                if col == 6 and isinstance(value, (int, float)):
                    cell.font = RED if value < 0 else GREEN
                else:
                    cell.font = PLAIN
            elif col == 8:
                cell.font = MUTED
            else:
                cell.font = PLAIN


def write_spot_daily(
    wb: Workbook, title: str, rows: list[dict], boundary_hour: int
) -> None:
    """现货每日核对：每天每个币种的净变动 + 返佣合计。日期降序。"""
    currencies = sorted(
        {r.get("currency", "") for r in rows},
        key=lambda c: -sum(
            abs(float(r.get("change", 0) or 0)) for r in rows if r.get("currency") == c
        ),
    )
    groups: dict[date, list[dict]] = {}
    for row in rows:
        groups.setdefault(bucket_day(float(row.get("time", 0)), boundary_hour), []).append(row)
    days = sorted(groups.items())

    def rebate_total(group: list[dict]) -> float:
        return sum(
            float(r.get("change", 0) or 0)
            for r in group
            if r.get("type") in REBATE_TYPES
        )

    ws = wb.create_sheet(title)
    style_header(
        ws,
        [("日期", 12), ("笔数", 8), ("返佣合计", 12)]
        + [(f"{c} 净变动", 14) for c in currencies],
    )
    for i, (day, group) in enumerate(reversed(days), start=2):
        ws.cell(row=i, column=1, value=day).number_format = "yyyy-mm-dd"
        ws.cell(row=i, column=2, value=len(group))
        rebate = rebate_total(group)
        cell = ws.cell(row=i, column=3, value=rebate)
        cell.number_format = "#,##0.00######"
        cell.font = GREEN if rebate > 0 else PLAIN
        for j, currency in enumerate(currencies, start=4):
            net = sum(
                float(r.get("change", 0) or 0)
                for r in group
                if r.get("currency") == currency
            )
            cell = ws.cell(row=i, column=j, value=net)
            cell.number_format = "#,##0.00######"
            if net < 0:
                cell.font = RED
            elif net > 0:
                cell.font = GREEN
        for col in (1, 2):
            ws.cell(row=i, column=col).font = PLAIN

    total_row = len(days) + 2
    ws.cell(row=total_row, column=1, value="合计").font = BOLD
    ws.cell(row=total_row, column=2, value=len(rows)).font = BOLD
    cell = ws.cell(row=total_row, column=3, value=rebate_total(rows))
    cell.number_format = "#,##0.00######"
    cell.font = BOLD
    for j, currency in enumerate(currencies, start=4):
        net = sum(
            float(r.get("change", 0) or 0)
            for r in rows
            if r.get("currency") == currency
        )
        cell = ws.cell(row=total_row, column=j, value=net)
        cell.number_format = "#,##0.00######"
        cell.font = BOLD


def write_spot_by_currency(wb: Workbook, rows: list[dict]) -> None:
    by_cur = wb.create_sheet("现货·按币种汇总")
    style_header(
        by_cur,
        [("币种", 10), ("笔数", 9), ("流入", 16), ("流出", 16), ("净变动", 16), ("最新余额", 16)],
    )
    currencies = sorted(
        {r.get("currency", "") for r in rows},
        key=lambda c: -sum(
            abs(float(r.get("change", 0) or 0)) for r in rows if r.get("currency") == c
        ),
    )
    for i, currency in enumerate(currencies, start=2):
        group = [r for r in rows if r.get("currency") == currency]
        inflow = sum(float(r.get("change", 0) or 0) for r in group if float(r.get("change", 0) or 0) > 0)
        outflow = sum(float(r.get("change", 0) or 0) for r in group if float(r.get("change", 0) or 0) < 0)
        last = max(group, key=lambda r: float(r.get("time", 0)))
        values = [currency, len(group), inflow, outflow, inflow + outflow, num(last.get("balance"))]
        for col, value in enumerate(values, start=1):
            cell = by_cur.cell(row=i, column=col, value=value)
            cell.font = PLAIN
            if col >= 3:
                cell.number_format = "#,##0.00######"
                if col == 5 and isinstance(value, (int, float)):
                    cell.font = RED if value < 0 else GREEN


def write_spot_by_type(wb: Workbook, rows: list[dict]) -> None:
    by_type = wb.create_sheet("现货·按类型汇总")
    currencies = sorted(
        {r.get("currency", "") for r in rows},
        key=lambda c: -sum(
            abs(float(r.get("change", 0) or 0)) for r in rows if r.get("currency") == c
        ),
    )
    style_header(
        by_type,
        [("类型", 16), ("笔数", 9)] + [(f"{c} 净变动", 14) for c in currencies],
    )
    types = sorted({r.get("type", "") for r in rows})
    for i, type_name in enumerate(types, start=2):
        group = [r for r in rows if r.get("type") == type_name]
        by_type.cell(row=i, column=1, value=SPOT_TYPE_LABELS.get(type_name, type_name))
        by_type.cell(row=i, column=2, value=len(group))
        for j, currency in enumerate(currencies, start=3):
            net = sum(
                float(r.get("change", 0) or 0)
                for r in group
                if r.get("currency") == currency
            )
            cell = by_type.cell(row=i, column=j, value=net)
            cell.number_format = "#,##0.00######"
            if net < 0:
                cell.font = RED
            elif net > 0:
                cell.font = GREEN
        for col in (1, 2):
            by_type.cell(row=i, column=col).font = PLAIN


def spot_rebates_by_period(
    rows: list[dict], boundary_hour: int, period: str = "day"
) -> dict[date, float]:
    """现货账本 → 每天/周/月返佣总和（供合约核对用，分桶口径一致）。"""
    out: dict[date, float] = {}
    for row in rows:
        if row.get("type") not in REBATE_TYPES:
            continue
        key = period_key(bucket_day(float(row.get("time", 0)), boundary_hour), period)
        out[key] = out.get(key, 0.0) + float(row.get("change", 0) or 0)
    return out


# ---------------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(
        description="导出 Gate 交易报告（合约平仓 + 现货流水）到一个 Excel"
    )
    parser.add_argument("--settle", default="usdt", choices=["usdt", "btc"], help="合约结算币，默认 usdt")
    parser.add_argument("--days", type=int, default=90, help="回看天数，默认 90")
    parser.add_argument("--contract", default=None, help="只导出指定合约，如 BNB_USDT")
    parser.add_argument("-o", "--output", default=None, help="输出文件名")
    args = parser.parse_args()

    creds = load_credentials()
    print(f"密钥来源：{creds.source}")

    end = int(time.time())
    start = end - args.days * 86400
    quote = "USDT" if args.settle == "usdt" else "BTC"

    with httpx.Client(timeout=30) as client:
        print(f"拉取合约平仓历史（{args.contract or '全部合约'}，近 {args.days} 天）…")
        closes = fetch_window(
            client,
            creds,
            f"/api/v4/futures/{args.settle}/position_close",
            start,
            end,
            extra_params={"contract": args.contract} if args.contract else None,
            row_key=lambda r: json.dumps(r, sort_keys=True),
        )
        print(f"拉取现货资金流水（近 {args.days} 天，按 {SLICE_DAYS} 天切片）…")
        book = fetch_window(client, creds, "/api/v4/spot/account_book", start, end)

        multipliers: dict[str, float] = {}
        if closes:
            contracts = sorted({r.get("contract", "") for r in closes})
            multipliers = fetch_multipliers(client, creds, args.settle, contracts)

    if not closes and not book:
        sys.exit("这个区间合约和现货都没有记录，不生成文件。")

    wb = Workbook()
    if closes:
        write_futures_detail(wb, closes, quote)
        write_futures_daily(
            wb, "合约·每日核对(自然日)",
            futures_period_rows(closes, 0, multipliers), quote,
            spot_rebates_by_period(book, 0),
        )
        write_futures_daily(
            wb, "合约·每日核对(8点交易日)",
            futures_period_rows(closes, 8, multipliers), quote,
            spot_rebates_by_period(book, 8),
        )
        for period, label in (("week", "每周汇总"), ("month", "每月汇总")):
            write_futures_period(
                wb, f"合约·{label}",
                [
                    ("自然日", futures_period_rows(closes, 0, multipliers, period),
                     spot_rebates_by_period(book, 0, period)),
                    ("8 点交易日", futures_period_rows(closes, 8, multipliers, period),
                     spot_rebates_by_period(book, 8, period)),
                ],
                quote,
                period,
            )
        write_futures_by_contract(wb, closes, quote)
    if book:
        write_spot_detail(wb, book)
        write_spot_daily(wb, "现货·每日核对(自然日)", book, 0)
        write_spot_daily(wb, "现货·每日核对(8点交易日)", book, 8)
        write_spot_by_currency(wb, book)
        write_spot_by_type(wb, book)

    output = (
        Path(args.output)
        if args.output
        else Path(f"交易报告_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")
    )
    wb.save(output)
    print(
        f"完成：合约 {len(closes)} 条平仓 + 现货 {len(book)} 条流水 → {output.resolve()}"
    )


if __name__ == "__main__":
    main()
