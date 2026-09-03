"""Unified (portfolio margin) account view.

In unified mode collateral is shared across spot, margin and futures, so the
numbers that matter are different from either single-product account: one
equity figure, one liability figure, and a leverage ratio that moves when any
leg moves. Borrowing interest is a real cost here and has no analogue in the
plain futures account.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..db import UPSERT_EQUITY, Database
from ..gate_client import GateAPIError, GateClient
from .futures import _f

log = logging.getLogger(__name__)

ACCOUNT_KEY = "unified"


async def snapshot(client: GateClient, quote: str) -> dict[str, Any]:
    account = await client.get("/unified/accounts")

    equity = _f(account.get("unified_account_total_equity"))
    liabilities = _f(account.get("unified_account_total_liab"))
    margin_balance = _f(account.get("total_margin_balance"))
    maintenance = _f(account.get("total_maintenance_margin"))

    balances = []
    for currency, item in (account.get("balances") or {}).items():
        equity_c = _f(item.get("equity"))
        if equity_c == 0 and _f(item.get("borrowed")) == 0:
            continue
        balances.append(
            {
                "currency": currency,
                "available": _f(item.get("available")),
                "freeze": _f(item.get("freeze")),
                "borrowed": _f(item.get("borrowed")),
                "interest": _f(item.get("interest")),
                "equity": equity_c,
                "unrealised_pnl": _f(item.get("unrealised_pnl")),
            }
        )
    balances.sort(key=lambda b: abs(b["equity"]), reverse=True)

    return {
        "account_type": ACCOUNT_KEY,
        "currency": quote,
        "equity": equity,
        "wallet_balance": _f(account.get("unified_account_total")),
        "unrealised_pnl": sum(b["unrealised_pnl"] for b in balances),
        "available": _f(account.get("total_available_margin")),
        "position_margin": _f(account.get("total_initial_margin")),
        "maintenance_margin": maintenance,
        "liabilities": liabilities,
        "leverage": _f(account.get("leverage")),
        # Ratio of maintenance requirement to margin balance: at 1.0 the account
        # is being liquidated. Belongs at the top of the page in this mode.
        "margin_ratio": maintenance / margin_balance if margin_balance else 0.0,
        "accrued_interest": sum(b["interest"] for b in balances),
        "balances": balances,
        "risk_units": await _risk_units(client),
    }


async def _risk_units(client: GateClient) -> list[dict[str, Any]]:
    try:
        units = await client.get("/unified/risk_units")
    except GateAPIError as exc:
        log.warning("risk_units unavailable: %s", exc)
        return []
    if isinstance(units, dict):
        units = units.get("risk_units") or []
    return units or []


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
                snap["unrealised_pnl"],
                snap["available"],
                snap["position_margin"],
                int(time.time()),
            )
        ],
    )


async def interest_records(client: GateClient, limit: int = 100) -> list[dict[str, Any]]:
    """Borrowing cost history — a line item that only exists in unified mode."""
    try:
        rows = await client.get("/unified/interest_records", params={"limit": limit}) or []
    except GateAPIError as exc:
        log.warning("interest_records unavailable: %s", exc)
        return []
    return [
        {
            "time": int(float(r.get("create_time", 0))),
            "currency": r.get("currency"),
            "actual_rate": _f(r.get("actual_rate")),
            "interest": _f(r.get("interest")),
            "type": r.get("type"),
        }
        for r in rows
    ]
