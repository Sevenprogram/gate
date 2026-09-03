#!/usr/bin/env python3
"""导出 Gate 现货资金流水到 Excel。

单文件、独立运行。调 APIv4 的 /api/v4/spot/account_book，按 30 天切片
分页拉全窗口（接口限制单次窗口最长 30 天），导出为带格式的 Excel：

  - 流水明细：全部字段 + 日期/时段（UTC+8，8 点界）派生列
  - 每日核对（自然日 / 8 点交易日）：每天每个币种的净变动
  - 按币种汇总、按类型汇总

用法：

    ./.venv/bin/python export_spot_book.py                # 近 90 天
    ./.venv/bin/python export_spot_book.py --days 180
    ./.venv/bin/python export_spot_book.py -o 现货流水.xlsx

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

# 接口单次 from/to 窗口的上限：30 天可用，90 天报 invalid time range。
SLICE_DAYS = 30


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


def fetch_account_book(
    client: httpx.Client, creds: Credentials, start: int, end: int
) -> list[dict]:
    """按 30 天切片拉全窗口的现货账本，片内按时间游标翻页。"""
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
                "/api/v4/spot/account_book",
                {"from": slice_start, "to": cursor, "limit": 1000},
            )
            if not isinstance(batch, list) or not batch:
                break
            for row in batch:
                # 部分类型的 time 字段是毫秒（其余是秒）——按秒解析会得到
                # 公元 58 万年，统一在入口归一化。
                raw_time = float(row.get("time", 0) or 0)
                if raw_time > 1e11:
                    row["time"] = raw_time / 1000
                row_id = str(
                    row.get("id")
                    or f"{row.get('time')}:{row.get('currency')}:{row.get('change')}:{row.get('balance')}"
                )
                if row_id not in seen:
                    seen.add(row_id)
                    rows.append(row)
            if len(batch) < 1000:
                break
            oldest = min(int(float(r.get("time", slice_start))) for r in batch)
            if oldest <= slice_start:
                break
            cursor = oldest - 1 if all(int(float(r.get("time", 0))) >= oldest for r in batch) else oldest
        slice_start = slice_end

    return rows


# --------------------------------------------------------------------- 导出

TZ = timezone(timedelta(hours=8))

TYPE_LABELS = {
    "deposit": "充值",
    "withdraw": "提现",
    "trade": "成交",
    "fee": "手续费",
    "rebate": "返佣",
    "transfer": "划转",
    "liquidate": "强平",
    "settlement": "结算",
    "new_order": "下单冻结/解冻",
    "order_fill": "成交",
    "order_fee": "交易手续费",
    "perp_in": "转出至合约",
    "perp_out": "从合约转入",
    "pu_rebate": "返佣",
    "subaccount_trf": "子账户划转",
    "unknown": "未知",
}

HEADER_FILL = PatternFill("solid", fgColor="1F2937")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
RED = Font(color="C0392B", size=10)
GREEN = Font(color="1E8449", size=10)
PLAIN = Font(size=10)
BOLD = Font(bold=True, size=10)
MUTED = Font(color="9CA3AF", size=10)


def excel_time(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=TZ).replace(tzinfo=None)


def session_label(dt: datetime) -> str:
    return "8 点前" if dt.hour < 8 else "8 点后"


def style_header(ws, headers: list[tuple[str, int]]) -> None:
    for col, (title, width) in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"


def build_workbook(rows: list[dict]) -> Workbook:
    wb = Workbook()

    def num(v: Any) -> float | None:
        return float(v) if v not in (None, "") else None

    # ---- 明细 ----
    ws = wb.active
    ws.title = "流水明细"
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
        change = num(row.get("change"))
        values = [
            dt,
            dt.date(),
            session_label(dt),
            row.get("currency", ""),
            TYPE_LABELS.get(row.get("type", ""), row.get("type", "")),
            change,
            num(row.get("balance")),
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

    # ---- 每日核对（两套日界） ----
    currencies = sorted(
        {r.get("currency", "") for r in rows},
        key=lambda c: -sum(
            abs(float(r.get("change", 0) or 0))
            for r in rows
            if r.get("currency") == c
        ),
    )

    def daily_rows(boundary_hour: int) -> list[tuple[date, list[dict]]]:
        groups: dict[date, list[dict]] = {}
        for row in rows:
            dt = excel_time(float(row.get("time", 0)))
            if boundary_hour == 8 and dt.hour < 8:
                day = (dt - timedelta(days=1)).date()
            else:
                day = dt.date()
            groups.setdefault(day, []).append(row)
        return sorted(groups.items())

    REBATE_TYPES = {"rebate", "pu_rebate"}

    def rebate_total(group: list[dict]) -> float:
        """返佣类型的变动总和（收入项，取值为正）。"""
        return sum(
            float(r.get("change", 0) or 0)
            for r in group
            if r.get("type") in REBATE_TYPES
        )

    def write_daily(wb: Workbook, title: str, boundary_hour: int) -> None:
        ws = wb.create_sheet(title)
        headers = (
            [("日期", 12), ("笔数", 8), ("返佣合计", 12)]
            + [(f"{c} 净变动", 14) for c in currencies]
        )
        style_header(ws, headers)
        # 日期降序
        for i, (day, group) in enumerate(reversed(daily_rows(boundary_hour)), start=2):
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
        # 合计行（保持在底部）
        total_row = len(daily_rows(boundary_hour)) + 2
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

    write_daily(wb, "每日核对(自然日)", 0)
    write_daily(wb, "每日核对(8点交易日)", 8)

    # ---- 按币种汇总 ----
    by_cur = wb.create_sheet("按币种汇总")
    style_header(
        by_cur,
        [("币种", 10), ("笔数", 9), ("流入", 16), ("流出", 16), ("净变动", 16), ("最新余额", 16)],
    )
    for i, currency in enumerate(currencies, start=2):
        group = [r for r in rows if r.get("currency") == currency]
        inflow = sum(float(r.get("change", 0) or 0) for r in group if float(r.get("change", 0) or 0) > 0)
        outflow = sum(float(r.get("change", 0) or 0) for r in group if float(r.get("change", 0) or 0) < 0)
        # 最新余额 = 按时间正序最后一条的 balance
        last = max(group, key=lambda r: float(r.get("time", 0)))
        values = [currency, len(group), inflow, outflow, inflow + outflow, num(last.get("balance"))]
        for col, value in enumerate(values, start=1):
            cell = by_cur.cell(row=i, column=col, value=value)
            cell.font = PLAIN
            if col >= 3:
                cell.number_format = "#,##0.00######"
                if col == 5 and isinstance(value, (int, float)):
                    cell.font = RED if value < 0 else GREEN

    # ---- 按类型汇总 ----
    by_type = wb.create_sheet("按类型汇总")
    style_header(
        by_type,
        [("类型", 16), ("笔数", 9)] + [(f"{c} 净变动", 14) for c in currencies],
    )
    types = sorted({r.get("type", "") for r in rows})
    for i, type_name in enumerate(types, start=2):
        group = [r for r in rows if r.get("type") == type_name]
        by_type.cell(row=i, column=1, value=TYPE_LABELS.get(type_name, type_name))
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

    return wb


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 Gate 现货资金流水到 Excel")
    parser.add_argument("--days", type=int, default=90, help="回看天数，默认 90")
    parser.add_argument("-o", "--output", default=None, help="输出文件名")
    args = parser.parse_args()

    creds = load_credentials()
    print(f"密钥来源：{creds.source}")

    end = int(time.time())
    start = end - args.days * 86400
    print(f"拉取现货资金流水：近 {args.days} 天（按 {SLICE_DAYS} 天切片）…")

    with httpx.Client(timeout=30) as client:
        rows = fetch_account_book(client, creds, start, end)

    if not rows:
        sys.exit("这个区间没有流水记录，不生成文件。")

    wb = build_workbook(rows)
    output = (
        Path(args.output)
        if args.output
        else Path(f"现货流水_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")
    )
    wb.save(output)
    print(f"完成：{len(rows)} 条流水 → {output.resolve()}")


if __name__ == "__main__":
    main()
