"""Wallet-level data: cross-account valuation and real money in/out.

Deposits and withdrawals are what separate "my balance went up" from "I made
money". Without them the equity-delta PnL on the daily page is wrong by exactly
the amount you moved.
"""

from __future__ import annotations

import logging
from typing import Any

from ..db import UPSERT_CASHFLOW, Database
from ..gate_client import GateAPIError, GateClient
from .futures import _f

log = logging.getLogger(__name__)


async def total_balance(client: GateClient, quote: str) -> dict[str, Any]:
    """Valuation of every sub-account, in one call.

    Gate returns a per-account-type breakdown here, which is the cheapest way to
    get a portfolio total that already agrees with the web UI.
    """
    try:
        payload = await client.get("/wallet/total_balance", params={"currency": quote})
    except GateAPIError as exc:
        log.warning("total_balance unavailable: %s", exc)
        return {"total": None, "details": {}, "error": exc.message}

    details = {
        name: {"amount": _f(item.get("amount")), "currency": item.get("currency")}
        for name, item in (payload.get("details") or {}).items()
        if _f(item.get("amount")) != 0
    }
    return {
        "total": _f((payload.get("total") or {}).get("amount")),
        "currency": (payload.get("total") or {}).get("currency", quote),
        "details": details,
    }


async def sync_transfers(
    client: GateClient, db: Database, profile: str, start: int, end: int
) -> dict[str, int]:
    """Record on-chain deposits and withdrawals as cashflow.

    These are booked against the `wallet` pseudo-account. Internal moves between
    spot and futures already arrive via each account's own ledger, so pulling
    them here too would double count.
    """
    written = 0
    for kind, endpoint in (("deposit", "/wallet/deposits"), ("withdraw", "/wallet/withdrawals")):
        try:
            rows = await client.paginate_by_offset(
                endpoint,
                since=start,
                params={"from": start, "to": end},
                limit=100,
                time_field="timestamp",
            )
        except GateAPIError as exc:
            log.warning("%s sync failed: %s", kind, exc)
            continue

        batch = []
        for row in rows:
            ts = int(float(row.get("timestamp", 0)))
            amount = _f(row.get("amount"))
            batch.append(
                (
                    profile,
                    "wallet",
                    kind,
                    str(row.get("id") or row.get("txid") or f"{kind}:{ts}:{amount}"),
                    ts,
                    db.day_of(ts),
                    row.get("currency"),
                    amount if kind == "deposit" else -amount,
                    row.get("status"),
                )
            )
        written += await db.upsert_many(UPSERT_CASHFLOW, batch)

    return {"cashflow_rows": written}
