"""Spot account view and history sync."""

from __future__ import annotations

import logging
import time
from typing import Any

from ..db import UPSERT_CASHFLOW, UPSERT_EQUITY, UPSERT_LEDGER, UPSERT_TRADE, Database
from ..gate_client import GateAPIError, GateClient
from . import ledger_types
from .futures import _f

log = logging.getLogger(__name__)

ACCOUNT_KEY = "spot"


async def snapshot(client: GateClient, quote: str) -> dict[str, Any]:
    """Balances valued in `quote`, plus the account's current fee tier."""
    balances = await client.get("/spot/accounts") or []
    prices = await _ticker_prices(client, quote)

    holdings = []
    equity = 0.0
    for row in balances:
        currency = row.get("currency", "")
        available = _f(row.get("available"))
        locked = _f(row.get("locked"))
        total = available + locked
        if total == 0:
            continue
        price = 1.0 if currency == quote else prices.get(f"{currency}_{quote}")
        value = total * price if price else None
        if value:
            equity += value
        holdings.append(
            {
                "currency": currency,
                "available": available,
                "locked": locked,
                "total": total,
                "price": price,
                "value": value,
            }
        )

    holdings.sort(key=lambda h: h["value"] or 0, reverse=True)

    return {
        "account_type": ACCOUNT_KEY,
        "currency": quote,
        "equity": equity,
        "wallet_balance": equity,
        "unrealised_pnl": 0.0,  # spot has no mark-to-market ledger concept
        "available": sum(h["value"] or 0 for h in holdings if h["locked"] == 0),
        "position_margin": 0.0,
        "holdings": holdings,
        "unpriced": [h["currency"] for h in holdings if h["value"] is None],
        "fee_tier": await fee_tier(client),
    }


async def _ticker_prices(client: GateClient, quote: str) -> dict[str, float]:
    """Last price for every `*_{quote}` market, from the public ticker feed."""
    try:
        tickers = await client.public_get("/spot/tickers") or []
    except GateAPIError as exc:
        log.warning("spot tickers unavailable, balances will show unpriced: %s", exc)
        return {}
    suffix = f"_{quote}"
    return {
        t["currency_pair"]: _f(t.get("last"))
        for t in tickers
        if str(t.get("currency_pair", "")).endswith(suffix) and _f(t.get("last")) > 0
    }


async def fee_tier(client: GateClient) -> dict[str, Any] | None:
    """Current maker/taker rates, including whether the GT discount is active.

    Worth showing on screen: the effective rate silently changes with VIP level
    and GT balance, and it is the multiplier on every cost figure below.
    """
    try:
        fee = await client.get("/wallet/fee")
    except GateAPIError as exc:
        log.warning("fee tier unavailable: %s", exc)
        return None
    gt_discount = bool(fee.get("gt_discount"))
    return {
        "maker": _f(fee.get("gt_maker_fee") if gt_discount else fee.get("maker_fee")),
        "taker": _f(fee.get("gt_taker_fee") if gt_discount else fee.get("taker_fee")),
        "base_maker": _f(fee.get("maker_fee")),
        "base_taker": _f(fee.get("taker_fee")),
        "gt_discount": gt_discount,
        "point_type": fee.get("point_type"),
        "futures_maker": _f(fee.get("futures_maker_fee")),
        "futures_taker": _f(fee.get("futures_taker_fee")),
    }


async def record_equity_snapshot(
    db: Database, profile: str, snap: dict[str, Any]
) -> None:
    await db.upsert_many(
        UPSERT_EQUITY,
        [
            (
                profile,
                db.today(),
                ACCOUNT_KEY,
                snap["equity"],
                0.0,
                snap["available"],
                0.0,
                int(time.time()),
            )
        ],
    )


async def sync_ledger(
    client: GateClient, db: Database, profile: str, start: int, end: int
) -> dict[str, int]:
    rows = await client.paginate_by_time(
        "/spot/account_book", start=start, end=end, time_field="time"
    )

    ledger_batch: list[tuple[Any, ...]] = []
    cashflow_batch: list[tuple[Any, ...]] = []
    unknown: set[str] = set()

    for row in rows:
        ts = int(float(row.get("time", 0)))
        # Some entry types (pu_rebate among them) report time in milliseconds.
        if ts > 10**11:
            ts //= 1000
        day = db.day_of(ts)
        raw_type = str(row.get("type", ""))
        bucket = ledger_types.classify(ACCOUNT_KEY, raw_type)
        if bucket == ledger_types.UNCLASSIFIED:
            unknown.add(raw_type)

        entry_id = str(row.get("id") or f"{raw_type}:{ts}:{row.get('change')}")
        change = _f(row.get("change"))
        currency = row.get("currency")

        ledger_batch.append(
            (
                profile,
                ACCOUNT_KEY,
                entry_id,
                ts,
                day,
                raw_type,
                change,
                _f(row.get("balance")),
                currency,
                None,
                row.get("text"),
            )
        )
        if bucket == ledger_types.CASHFLOW:
            cashflow_batch.append(
                (profile, ACCOUNT_KEY, raw_type, entry_id, ts, day, currency, change, "done")
            )

    written = await db.upsert_many(UPSERT_LEDGER, ledger_batch)
    await db.upsert_many(UPSERT_CASHFLOW, cashflow_batch)

    if unknown:
        log.warning("unclassified spot ledger types: %s", sorted(unknown))
    return {"ledger_rows": written, "cashflow_rows": len(cashflow_batch)}


async def sync_trades(
    client: GateClient,
    db: Database,
    profile: str,
    start: int,
    end: int,
    pairs: list[str] | None,
) -> dict[str, int]:
    """Pull spot fills.

    Gate normally wants an explicit `currency_pair`; the unfiltered form only
    covers a short recent window and is not available on every account. Try it
    once, and if it is rejected fall back to querying each configured pair.
    """
    rows: list[dict[str, Any]] = []
    try:
        rows = await client.get(
            "/spot/my_trades", params={"from": start, "to": end, "limit": 1000}
        ) or []
    except GateAPIError as exc:
        if not pairs:
            log.warning(
                "spot my_trades needs currency_pair (%s) and GATE_SPOT_PAIRS is empty; "
                "no spot trades synced",
                exc.label,
            )
            return {"trade_rows": 0}
        for pair in pairs:
            try:
                rows.extend(
                    await client.paginate_by_offset(
                        "/spot/my_trades",
                        since=start,
                        params={"currency_pair": pair, "from": start, "to": end},
                        time_field="create_time",
                    )
                )
            except GateAPIError as inner:
                log.warning("spot my_trades failed for %s: %s", pair, inner)

    batch = []
    for row in rows:
        ts = int(float(row.get("create_time", 0)))
        amount = _f(row.get("amount"))
        price = _f(row.get("price"))
        batch.append(
            (
                profile,
                ACCOUNT_KEY,
                str(row.get("id")),
                ts,
                db.day_of(ts),
                row.get("currency_pair"),
                row.get("side"),
                row.get("role"),
                price,
                amount,
                amount * price,
                # A GT- or point-paid fee is still a cost; keeping them apart
                # from the quote-currency fee would understate total cost.
                -(_f(row.get("fee")) + _f(row.get("point_fee")) + _f(row.get("gt_fee"))),
                row.get("fee_currency"),
                str(row.get("order_id") or ""),
            )
        )

    return {"trade_rows": await db.upsert_many(UPSERT_TRADE, batch)}
