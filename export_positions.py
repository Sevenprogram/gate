#!/usr/bin/env python3
"""导出 Gate 合约历史仓位到 Excel。

单文件、独立运行，不依赖看板后端。用只读 API key 直接调 Gate APIv4 的
逐仓平仓历史接口（/futures/{settle}/position_close），分页拉全窗口，
导出为带格式的 Excel：明细一个 sheet（含持仓时长等派生列），汇总一个
sheet（按合约统计）。

用法：

    ./.venv/bin/python export_positions.py                    # 近 90 天，U 本位
    ./.venv/bin/python export_positions.py --days 365         # 近一年
    ./.venv/bin/python export_positions.py --contract BNB_USDT
    ./.venv/bin/python export_positions.py --settle btc       # 币本位
    ./.venv/bin/python export_positions.py -o 我的仓位.xlsx

密钥读取顺序：环境变量 GATE_API_KEY / GATE_API_SECRET，其次项目里的
profiles.toml（第一个有密钥的 profile）。
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
from typing import Any
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

API_HOST = "https://api.gateio.ws"
EMPTY_HASH = hashlib.sha512(b"").hexdigest()


class GateError(RuntimeError):
    pass


@dataclass
class Credentials:
    key: str
    secret: str
    source: str


def load_credentials() -> Credentials:
    """环境变量优先，其次 profiles.toml 里第一个有密钥的 profile。"""
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
                    str(profile["key"]), str(profile["secret"]), f"profiles.toml:{profile['id']}"
                )

    sys.exit(
        "找不到密钥。设置 GATE_API_KEY / GATE_API_SECRET，"
        "或在 profiles.toml 里填一个有密钥的 [[profile]]。"
    )


def signed_get(client: httpx.Client, creds: Credentials, path: str, params: dict) -> list | dict:
    """APIv4 签名 GET。签名串 = METHOD\\npath\\nquery\\nbody_hash\\ntimestamp。"""
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


def fetch_position_close(
    client: httpx.Client, creds: Credentials, settle: str, start: int, end: int, contract: str | None
) -> list[dict]:
    """分页拉平仓历史。

    接口只有 from/to/limit（无 offset），往回缩 to 游标翻页。返回值按 Gate
    原样保留全部字段，一条不少。
    """
    rows: list[dict] = []
    seen: set[str] = set()
    cursor = end
    limit = 1000

    while True:
        params = {"from": start, "to": cursor, "limit": limit}
        if contract:
            params["contract"] = contract
        batch = signed_get(client, creds, f"/api/v4/futures/{settle}/position_close", params)
        if not isinstance(batch, list) or not batch:
            break

        for row in batch:
            key = json.dumps(row, sort_keys=True)
            if key not in seen:
                seen.add(key)
                rows.append(row)

        if len(batch) < limit:
            break
        oldest = min(float(r.get("time", cursor)) for r in batch)
        if oldest <= start:
            break
        cursor = int(oldest)
        # 同一秒超过 1000 条时回退一秒，保证游标能前进。
        if all(float(r.get("time", 0)) >= oldest for r in batch) and len(batch) == limit:
            cursor = int(oldest) - 1

    return rows


# --------------------------------------------------------------------- 导出

def human_duration(seconds: float) -> str:
    if seconds <= 0:
        return ""
    hours = seconds / 3600
    if hours < 1:
        return f"{seconds / 60:.0f} 分钟"
    if hours < 48:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


# 所有时间显式用 UTC+8，不依赖运行机器的时区设置。
TZ = timezone(timedelta(hours=8))


def excel_time(ts: float) -> datetime:
    """真正的 datetime 值（而非文本），Excel 里可排序、可分组、可筛选。"""
    return datetime.fromtimestamp(float(ts), tz=TZ).replace(tzinfo=None)


def trading_session(dt: datetime) -> str:
    """按 08:00（UTC+8）的交易日界划分时段。"""
    return "8 点前" if dt.hour < 8 else "8 点后"


HEADER_FILL = PatternFill("solid", fgColor="1F2937")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
RED = Font(color="C0392B", size=10)
GREEN = Font(color="1E8449", size=10)
PLAIN = Font(size=10)
BOLD = Font(bold=True, size=10)
MUTED = Font(color="9CA3AF", size=10)

# Gate 的 position_close 把盈亏分解为 pnl = pnl_pnl + pnl_fee + pnl_fund，
# 开平均价按方向取值：多头 long_price 开仓、short_price 平仓，空头反之。
COLUMNS = [
    ("平仓时间", 19),
    ("日期", 11),
    ("时段", 8),
    ("合约", 13),
    ("方向", 6),
    ("开仓均价", 11),
    ("平仓均价", 11),
    ("平仓盈亏", 11),
    ("价格盈亏", 11),
    ("手续费", 11),
    ("资金费", 10),
    ("持仓时长", 10),
    ("开仓时间", 19),
    ("最大仓位", 10),
    ("成交量", 10),
    ("杠杆", 6),
    ("保证金模式", 11),
    ("备注", 20),
]


def fetch_spot_rebates(
    client: httpx.Client, creds: Credentials, start: int, end: int, boundary_hour: int
) -> dict[date, float]:
    """现货每天实际到账的返佣（账本里 pu_rebate/rebate 条目的变动总和）。

    账本单次窗口最长 30 天，按片拉取；部分条目的 time 是毫秒，入口归一化。
    boundary_hour 与每日核对的分桶一致：8 点界下，8 点前的返佣归前一交易日。
    """
    entries: list[dict] = []
    seen: set[str] = set()
    slice_start = start
    while slice_start < end:
        slice_end = min(slice_start + 30 * 86400, end)
        cursor = slice_end
        while True:
            batch = signed_get(
                client,
                creds,
                "/api/v4/spot/account_book",
                {"from": slice_start, "to": cursor, "limit": 1000},
            )
            if not isinstance(batch, list) or not batch:
                break
            for row in batch:
                raw = float(row.get("time", 0) or 0)
                if raw > 1e11:
                    row["time"] = raw / 1000
                rid = str(row.get("id") or f"{row.get('time')}:{row.get('currency')}:{row.get('change')}")
                if rid not in seen:
                    seen.add(rid)
                    entries.append(row)
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

    out: dict[date, float] = {}
    for row in entries:
        if row.get("type") not in ("pu_rebate", "rebate"):
            continue
        dt = excel_time(float(row.get("time", 0)))
        if boundary_hour == 8 and dt.hour < 8:
            day = (dt - timedelta(days=1)).date()
        else:
            day = dt.date()
        out[day] = out.get(day, 0.0) + float(row.get("change", 0) or 0)
    return out


# ------------------------------------------------------------------ 每日核对

def daily_summary_rows(
    rows: list[dict], boundary_hour: int, multipliers: dict[str, float]
) -> list[dict]:
    """按天聚合平仓记录，用于核实每日交易量与手续费。

    boundary_hour=0 是自然日（0 点切）；boundary_hour=8 是交易日（当天 8 点
    到次日 8 点，标签写起始日）。

    交易量（名义价值）= 开仓名义 + 平仓名义，即（开仓均价 + 平仓均价）×
    张数 × 合约乘数——一次开仓和一次平仓各计一次成交。position_close 只有
    开仓和平仓的均价，分批开平的仓位这是按两侧均价的近似。乘数逐合约查询
    （"÷100"只是乘数 0.01 的合约的简写）。
    """
    groups: dict[date, list[dict]] = {}
    for row in rows:
        close_dt = excel_time(float(row.get("time", 0)))
        # 8 点界：8 点前的时刻属于前一个交易日的分组。
        if boundary_hour == 8 and close_dt.hour < 8:
            day = (close_dt - timedelta(days=1)).date()
        else:
            day = close_dt.date()
        groups.setdefault(day, []).append(row)

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
            return sum(
                float(r[field]) for r in group if r.get(field) not in (None, "")
            )

        pnls = [float(r["pnl"]) for r in group if r.get("pnl") not in (None, "")]
        out.append(
            {
                "day": day,
                "count": len(group),
                "volume": volume,
                "fee": total("pnl_fee"),
                "funding": total("pnl_fund"),
                "pnl_price": total("pnl_pnl"),
                "pnl": total("pnl"),
                "win_rate": (
                    sum(1 for p in pnls if p > 0) / len(pnls) if pnls else None
                ),
            }
        )
    return out


def write_daily_sheet(
    wb: Workbook,
    title: str,
    rows: list[dict],
    quote: str,
    rebates: dict[date, float],
) -> None:
    """每日核对：基础统计 + 手续费返佣口径三列 + 预计额外返佣。

    70%/5% 是当天手续费绝对值的份额；10% 按周汇总（同一周的每一天都显示
    该周 0.1×手续费 的总和）；预计额外返佣 = 70%手续费 − 当天现货实际返佣，
    即按 70% 返佣口径估算、还没到账的部分。日期降序。
    """
    def week_key(day: date) -> date:
        return day - timedelta(days=day.weekday())

    fee_by_week: dict[date, float] = {}
    for row in rows:
        fee_by_week[week_key(row["day"])] = fee_by_week.get(week_key(row["day"]), 0.0) + abs(row["fee"])

    ws = wb.create_sheet(title)
    ws.freeze_panes = "A2"
    headers = [
        ("日期", 12), ("平仓笔数", 9), ("交易量", 13), ("手续费", 11),
        ("70%手续费", 11), ("10%手续费(周)", 13), ("5%手续费", 11),
        ("当日返佣", 11), ("预计额外返佣", 13),
        ("资金费", 10), ("价格盈亏", 11), ("总盈亏", 11), ("胜率", 8),
    ]
    for col, (h, width) in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(col)].width = width

    # 日期降序
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
            elif col == 4 or col == 10:
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

    # 合计行
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


def fetch_multipliers(
    client: httpx.Client, creds: Credentials, settle: str, contracts: list[str]
) -> dict[str, float]:
    """每个合约的张数乘数（1 张 = multiplier 个币）。

    名义价值 = 价格 × 张数 × 乘数。"÷100" 只是乘数 0.01 的合约（SNDK、ETH
    等）的简写——BTC 是 0.0001，有些 meme 合约是 100，差着几个数量级。
    """
    out: dict[str, float] = {}
    for contract in contracts:
        detail = signed_get(client, creds, f"/api/v4/futures/{settle}/contracts/{contract}", {})
        out[contract] = float(detail.get("quanto_multiplier", 0) or 0)
    return out


def build_workbook(
    rows: list[dict], settle: str, quote: str, multipliers: dict[str, float],
    rebates_natural: dict[date, float], rebates_trading: dict[date, float],
) -> Workbook:
    wb = Workbook()

    # ---- 明细 sheet ----
    ws = wb.active
    ws.title = "平仓记录"
    ws.freeze_panes = "A2"

    for col, (title, width) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(col)].width = width

    def num(value: Any) -> float | None:
        return float(value) if value not in (None, "") else None

    for i, row in enumerate(sorted(rows, key=lambda r: float(r.get("time", 0)), reverse=True)):
        excel_row = i + 2
        ts = float(row.get("time", 0))
        first_open = row.get("first_open_time")
        side = row.get("side", "")
        # 多头：long_price 开仓、short_price 平仓；空头相反。
        entry = num(row.get("long_price" if side == "long" else "short_price"))
        exit_ = num(row.get("short_price" if side == "long" else "long_price"))

        close_dt = excel_time(ts)
        values: list[Any] = [
            close_dt,
            close_dt.date(),
            trading_session(close_dt),
            row.get("contract", ""),
            {"long": "多", "short": "空"}.get(side, side),
            entry,
            exit_,
            num(row.get("pnl")),
            num(row.get("pnl_pnl")),
            num(row.get("pnl_fee")),
            num(row.get("pnl_fund")),
            human_duration(ts - float(first_open)) if first_open else "",
            excel_time(float(first_open)) if first_open else None,
            num(row.get("max_size")),
            num(row.get("accum_size")),
            num(row.get("lever")),
            row.get("margin_mode", ""),
            row.get("text", ""),
        ]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=excel_row, column=col, value=value)
            if col == 1 and isinstance(value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm:ss"
                cell.font = PLAIN
            elif col == 2 and isinstance(value, date):
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
            elif col == 18:
                cell.font = MUTED
            else:
                cell.font = PLAIN

    # ---- 每日核对 sheet：自然日（0 点）与交易日（8 点）两套口径 ----
    write_daily_sheet(
        wb, "每日核对(自然日)", daily_summary_rows(rows, 0, multipliers), quote,
        rebates_natural,
    )
    write_daily_sheet(
        wb, "每日核对(8点交易日)", daily_summary_rows(rows, 8, multipliers), quote,
        rebates_trading,
    )

    # ---- 汇总 sheet ----
    summary = wb.create_sheet("按合约汇总")
    summary.freeze_panes = "A2"
    headers = ["合约", "平仓次数", "总盈亏", "价格盈亏", "手续费", "资金费", "盈利次数", "亏损次数", "胜率", "平均盈亏", "最好一笔", "最差一笔"]
    for col, title in enumerate(headers, start=1):
        cell = summary.cell(row=1, column=col, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
    for col, width in enumerate([13, 10, 12, 12, 11, 10, 10, 10, 8, 11, 11, 11], start=1):
        summary.column_dimensions[get_column_letter(col)].width = width

    def component(rows: list[dict], field: str) -> float:
        return sum(float(r[field]) for r in rows if r.get(field) not in (None, ""))

    by_contract: dict[str, list[dict]] = {}
    for row in rows:
        by_contract.setdefault(row.get("contract", "?"), []).append(row)

    for i, (contract, group) in enumerate(
        sorted(by_contract.items(), key=lambda kv: component(kv[1], "pnl")), start=2
    ):
        pnls = [float(r["pnl"]) for r in group if r.get("pnl") not in (None, "")]
        wins = [p for p in pnls if p > 0]
        stats = [
            contract,
            len(group),
            component(group, "pnl"),
            component(group, "pnl_pnl"),
            component(group, "pnl_fee"),
            component(group, "pnl_fund"),
            len(wins),
            len(pnls) - len(wins),
            len(wins) / len(pnls) if pnls else None,
            sum(pnls) / len(pnls) if pnls else None,
            max(pnls) if pnls else None,
            min(pnls) if pnls else None,
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

    return wb


def main() -> None:
    parser = argparse.ArgumentParser(
        description="导出 Gate 合约历史仓位到 Excel（单文件，独立于看板运行）"
    )
    parser.add_argument("--settle", default="usdt", choices=["usdt", "btc"], help="结算币，默认 usdt")
    parser.add_argument("--days", type=int, default=90, help="回看天数，默认 90")
    parser.add_argument("--contract", default=None, help="只导出指定合约，如 BNB_USDT")
    parser.add_argument("-o", "--output", default=None, help="输出文件名，默认自动命名")
    args = parser.parse_args()

    creds = load_credentials()
    print(f"密钥来源：{creds.source}")

    end = int(time.time())
    start = end - args.days * 86400
    scope = f"合约 {args.contract or '全部'} · 近 {args.days} 天 · {args.settle.upper()} 本位"
    print(f"拉取平仓历史：{scope} …")

    with httpx.Client(timeout=30) as client:
        rows = fetch_position_close(client, creds, args.settle, start, end, args.contract)
        contracts = sorted({r.get("contract", "") for r in rows})
        multipliers = fetch_multipliers(client, creds, args.settle, contracts)
        print("拉取现货返佣（用于预计额外返佣）…")
        try:
            rebates_natural = fetch_spot_rebates(client, creds, start, end, 0)
            rebates_trading = fetch_spot_rebates(client, creds, start, end, 8)
        except GateError as exc:
            print(f"警告：现货返佣拉取失败（{exc}），预计额外返佣列将按 0 处理")
            rebates_natural, rebates_trading = {}, {}

    if not rows:
        sys.exit("这个区间没有平仓记录，不生成文件。")

    quote = "USDT" if args.settle == "usdt" else "BTC"
    wb = build_workbook(rows, args.settle, quote, multipliers, rebates_natural, rebates_trading)

    output = Path(args.output) if args.output else Path(
        f"仓位历史_{args.settle.upper()}"
        f"_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    )
    wb.save(output)

    total_pnl = sum(float(r["pnl"]) for r in rows if r.get("pnl") not in (None, ""))
    wins = sum(1 for r in rows if r.get("pnl") not in (None, "") and float(r["pnl"]) > 0)
    print(
        f"完成：{len(rows)} 条平仓记录，总盈亏 {total_pnl:+,.2f} {quote}，"
        f"胜率 {wins / len(rows):.1%} → {output.resolve()}"
    )


if __name__ == "__main__":
    main()
