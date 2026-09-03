"""USDT / BTC settled perpetual futures account view and history sync."""

from __future__ import annotations

import logging
from typing import Any

from ..db import (
    UPSERT_CASHFLOW,
    UPSERT_EQUITY,
    UPSERT_LEDGER,
    UPSERT_POSITION_CLOSE,
    UPSERT_TRADE,
    Database,
)
from ..gate_client import GateAPIError, GateClient
from . import ledger_types

log = logging.getLogger(__name__)


def _f(value: Any, default: float = 0.0) -> float:
    """Gate returns every number as a string; missing fields come back as ''."""
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def snapshot(client: GateClient, settle: str) -> dict[str, Any]:
    """Live account state: equity, margin usage, open positions, risk distance."""
    account = await client.get(f"/futures/{settle}/accounts")
    raw_positions = await client.get(f"/futures/{settle}/positions", params={"holding": "true"})

    positions = [_describe_position(p) for p in (raw_positions or []) if _f(p.get("size")) != 0]

    equity = _f(account.get("total")) + _f(account.get("unrealised_pnl"))
    position_margin = _f(account.get("position_margin"))
    order_margin = _f(account.get("order_margin"))
    maintenance_margin = _f(account.get("maintenance_margin"))

    # Lifetime totals Gate keeps on the account object — a free cumulative view
    # that needs no history walk.
    history = account.get("history") or {}
    lifetime = {
        "realized_pnl": _f(history.get("pnl")),
        "fee": _f(history.get("fee")),
        "funding": _f(history.get("fund")),
        "rebate": _f(history.get("refr")),
        "net_transfer": _f(history.get("dnw")),
    }
    lifetime["net_pnl"] = (
        lifetime["realized_pnl"] + lifetime["funding"] + lifetime["rebate"] + lifetime["fee"]
    )

    return {
        "account_type": f"futures_{settle}",
        "currency": account.get("currency") or settle.upper(),
        "equity": equity,
        "wallet_balance": _f(account.get("total")),
        "unrealised_pnl": _f(account.get("unrealised_pnl")),
        "available": _f(account.get("available")),
        "position_margin": position_margin,
        "order_margin": order_margin,
        "maintenance_margin": maintenance_margin,
        # Headroom before maintenance margin eats the whole account. This is the
        # number that belongs above the PnL figures.
        "margin_ratio": (position_margin + order_margin) / equity if equity else 0.0,
        "maintenance_ratio": maintenance_margin / equity if equity else 0.0,
        "in_dual_mode": bool(account.get("in_dual_mode")),
        "positions": positions,
        "lifetime": lifetime,
    }


def _describe_position(p: dict[str, Any]) -> dict[str, Any]:
    mark = _f(p.get("mark_price"))
    liq = _f(p.get("liq_price"))
    size = _f(p.get("size"))
    entry = _f(p.get("entry_price"))

    # Percent the mark price can move against the position before liquidation.
    # Sign of `size` gives direction: long liquidates below, short above.
    distance = None
    if mark > 0 and liq > 0:
        distance = (mark - liq) / mark if size > 0 else (liq - mark) / mark

    return {
        "contract": p.get("contract"),
        "side": "long" if size > 0 else "short",
        "size": size,
        "value": _f(p.get("value")),
        "leverage": _f(p.get("leverage")),
        "entry_price": entry,
        "mark_price": mark,
        "liq_price": liq,
        "liq_distance_pct": distance,
        "margin": _f(p.get("margin")),
        "maintenance_rate": _f(p.get("maintenance_rate")),
        "unrealised_pnl": _f(p.get("unrealised_pnl")),
        "realised_pnl": _f(p.get("realised_pnl")),
        "mode": p.get("mode"),
    }


async def record_equity_snapshot(
    db: Database, profile: str, account_key: str, snap: dict[str, Any]
) -> None:
    import time

    await db.upsert_many(
        UPSERT_EQUITY,
        [
            (
                profile,
                db.today(),
                account_key,
                snap["equity"],
                snap["unrealised_pnl"],
                snap["available"],
                snap["position_margin"],
                int(time.time()),
            )
        ],
    )


async def sync_ledger(
    client: GateClient,
    db: Database,
    profile: str,
    account_key: str,
    settle: str,
    start: int,
    end: int,
) -> dict[str, int]:
    """Pull the futures account book and split it into ledger + cashflow rows."""
    rows = await client.paginate_by_time(
        f"/futures/{settle}/account_book", start=start, end=end, time_field="time"
    )

    ledger_batch: list[tuple[Any, ...]] = []
    cashflow_batch: list[tuple[Any, ...]] = []
    unknown_types: set[str] = set()

    for row in rows:
        ts = int(float(row.get("time", 0)))
        day = db.day_of(ts)
        raw_type = str(row.get("type", ""))
        bucket = ledger_types.classify(account_key, raw_type)
        if bucket == ledger_types.UNCLASSIFIED:
            unknown_types.add(raw_type)

        entry_id = _ledger_entry_id(row, ts)
        change = _f(row.get("change"))

        ledger_batch.append(
            (
                profile,
                account_key,
                entry_id,
                ts,
                day,
                raw_type,
                change,
                _f(row.get("balance")),
                settle.upper(),
                row.get("contract"),
                row.get("text"),
            )
        )
        if bucket == ledger_types.CASHFLOW:
            cashflow_batch.append(
                (
                    profile,
                    account_key,
                    raw_type,
                    entry_id,
                    ts,
                    day,
                    settle.upper(),
                    change,
                    "done",
                )
            )

    written = await db.upsert_many(UPSERT_LEDGER, ledger_batch)
    await db.upsert_many(UPSERT_CASHFLOW, cashflow_batch)

    if unknown_types:
        log.warning(
            "unclassified %s ledger types: %s — daily net PnL may not reconcile",
            account_key,
            sorted(unknown_types),
        )

    return {"ledger_rows": written, "cashflow_rows": len(cashflow_batch)}


def _ledger_entry_id(row: dict[str, Any], ts: int) -> str:
    """Stable primary key for a ledger row.

    Gate's futures account book has no id on older records, so fall back to a
    composite that is unique in practice: same instant + type + delta + running
    balance cannot repeat, because the balance moves with every entry.
    """
    for field in ("id", "trade_id"):
        value = row.get(field)
        if value not in (None, ""):
            return f"{row.get('type', '')}:{value}"
    return f"{row.get('type', '')}:{ts}:{row.get('change', '')}:{row.get('balance', '')}"


async def sync_trades(
    client: GateClient,
    db: Database,
    profile: str,
    account_key: str,
    settle: str,
    start: int,
    contracts: list[str] | None = None,
) -> dict[str, int]:
    """Pull fills so maker/taker role and per-contract volume are available.

    Fees also appear in the account book, but only the trade records carry
    `role`, and maker ratio is the metric that decides whether a grid strategy
    is viable at all.
    """
    targets: list[str | None] = list(contracts) if contracts else [None]
    batch: list[tuple[Any, ...]] = []

    for contract in targets:
        params = {"contract": contract} if contract else {}
        try:
            rows = await client.paginate_by_offset(
                f"/futures/{settle}/my_trades",
                since=start,
                params=params,
                time_field="create_time",
            )
        except GateAPIError as exc:
            log.warning("futures my_trades failed for %s: %s", contract or "all", exc)
            continue

        for row in rows:
            ts = int(float(row.get("create_time", 0)))
            size = _f(row.get("size"))
            price = _f(row.get("price"))
            batch.append(
                (
                    profile,
                    account_key,
                    str(row.get("id")),
                    ts,
                    db.day_of(ts),
                    row.get("contract"),
                    "buy" if size > 0 else "sell",
                    row.get("role"),
                    price,
                    abs(size),
                    abs(size) * price,
                    _f(row.get("fee")),
                    settle.upper(),
                    str(row.get("order_id") or ""),
                )
            )

    return {"trade_rows": await db.upsert_many(UPSERT_TRADE, batch)}


async def sync_position_close(
    client: GateClient,
    db: Database,
    profile: str,
    account_key: str,
    settle: str,
    start: int,
    end: int,
) -> dict[str, int]:
    """Per-position lifecycle PnL, which is the view that matches the web UI."""
    try:
        rows = await client.paginate_by_time(
            f"/futures/{settle}/position_close", start=start, end=end, time_field="time"
        )
    except GateAPIError as exc:
        log.warning("position_close sync failed: %s", exc)
        return {"position_close_rows": 0}

    batch = []
    for row in rows:
        ts = int(float(row.get("time", 0)))
        first_open = row.get("first_open_time")
        batch.append(
            (
                profile,
                account_key,
                f"{row.get('contract')}:{ts}:{row.get('pnl')}",
                ts,
                db.day_of(ts),
                row.get("contract"),
                row.get("side"),
                _f(row.get("pnl")),
                row.get("text"),
                int(float(first_open)) if first_open not in (None, "", 0) else None,
                _f(row.get("max_size")) or None,
                _f(row.get("long_price")),
                _f(row.get("short_price")),
                _f(row.get("pnl_pnl")),
                _f(row.get("pnl_fund")),
                _f(row.get("pnl_fee")),
            )
        )
    return {"position_close_rows": await db.upsert_many(UPSERT_POSITION_CLOSE, batch)}


async def risk_events(client: GateClient, settle: str) -> dict[str, Any]:
    """Liquidations and auto-deleveraging. A single row here deserves an alert."""
    out: dict[str, Any] = {"liquidations": [], "auto_deleverages": [], "errors": []}
    for key, endpoint in (
        ("liquidations", f"/futures/{settle}/liquidates"),
        ("auto_deleverages", f"/futures/{settle}/auto_deleverages"),
    ):
        try:
            out[key] = await client.get(endpoint, params={"limit": 50}) or []
        except GateAPIError as exc:
            out["errors"].append(f"{key}: {exc.message}")
    return out
