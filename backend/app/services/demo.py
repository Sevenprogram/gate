"""Synthetic account snapshots for previewing the dashboard without credentials.

This exists so the page can be looked at before an API key is wired up, and so
the risk panels can be exercised with positions at different distances from
liquidation.

It is deliberately hard to enable and impossible to mistake for real data:
DASHBOARD_DEMO=1 must be set explicitly, every response carries `demo: true`,
and the frontend shows a standing banner while it is on. Fabricated numbers on a
financial dashboard are only safe if they announce themselves.
"""

from __future__ import annotations

import zlib
from typing import Any

from ..config import SPOT, UNIFIED


def _profile_factor(profile_id: str) -> float:
    """A stable per-profile multiplier so several demo profiles don't look identical.

    Derived from the id rather than randomness, so the preview renders the same
    numbers on every reload.
    """
    return 0.55 + (zlib.crc32(profile_id.encode()) % 130) / 100.0


async def snapshot_for(
    account: str, profile_id: str, quote: str, db: Any = None
) -> dict[str, Any]:
    """Fabricated snapshot for one (profile, account), anchored to local history.

    Reusing whatever equity the local store already holds keeps the headline
    figure consistent with the charts below it, so the preview exercises the real
    layout rather than a set of numbers that visibly disagree.
    """
    equity = 16_259.58
    unrealised = -1_227.72
    if db is not None:
        rows = await db.query(
            "SELECT equity, unrealised_pnl FROM equity_snapshot "
            "WHERE profile = ? AND account = ? ORDER BY day DESC LIMIT 1",
            (profile_id, account),
        )
        if rows:
            equity = rows[0]["equity"]
            unrealised = rows[0]["unrealised_pnl"]
        else:
            factor = _profile_factor(profile_id)
            equity *= factor
            unrealised *= factor

    if account == SPOT:
        return spot_snapshot(quote, _profile_factor(profile_id))
    if account == UNIFIED:
        return unified_snapshot(quote, equity)
    return futures_snapshot(quote, equity, unrealised)


def futures_snapshot(quote: str, equity: float, unrealised: float) -> dict[str, Any]:
    """A grid-strategy account: three legs, one uncomfortably close to the line."""
    wallet = equity - unrealised
    # Notional is given directly rather than derived from contract size, so the
    # margin usage stays plausible against the seeded equity instead of
    # depending on each contract's multiplier.
    positions = [
        _position("BNB_USDT", 620.40, 611.85, 486.20, 981, 60_000, unrealised * 0.62, 20),
        _position("ETH_USDT", 3175.00, 3208.40, 3612.10, -140, 45_000, unrealised * 0.24, 25),
        _position("SOL_USDT", 168.20, 166.05, 151.90, 1506, 25_000, unrealised * 0.14, 10),
    ]
    position_margin = sum(p["margin"] for p in positions)
    order_margin = position_margin * 0.06
    maintenance = position_margin * 0.28

    return {
        "demo": True,
        "account_type": "futures_usdt",
        "currency": quote,
        "equity": equity,
        "wallet_balance": wallet,
        "unrealised_pnl": unrealised,
        "available": max(0.0, wallet - position_margin - order_margin),
        "position_margin": position_margin,
        "order_margin": order_margin,
        "maintenance_margin": maintenance,
        "margin_ratio": (position_margin + order_margin) / equity if equity else 0.0,
        "maintenance_ratio": maintenance / equity if equity else 0.0,
        "in_dual_mode": False,
        "positions": positions,
        "lifetime": {
            "realized_pnl": 21_480.16,
            "fee": -7_912.44,
            "funding": -1_186.03,
            "rebate": 402.77,
            "net_transfer": 12_000.0,
            "net_pnl": 21_480.16 - 7_912.44 - 1_186.03 + 402.77,
        },
    }


def _position(
    contract: str,
    entry: float,
    mark: float,
    liq: float,
    size: float,
    notional: float,
    unrealised: float,
    leverage: float,
) -> dict[str, Any]:
    distance = (mark - liq) / mark if size > 0 else (liq - mark) / mark
    return {
        "contract": contract,
        "side": "long" if size > 0 else "short",
        "size": size,
        "value": notional,
        "leverage": leverage,
        "entry_price": entry,
        "mark_price": mark,
        "liq_price": liq,
        "liq_distance_pct": distance,
        "margin": notional / leverage if leverage else notional,
        "maintenance_rate": 0.005,
        "unrealised_pnl": unrealised,
        "realised_pnl": 0.0,
        "mode": "single",
    }


def spot_snapshot(quote: str, factor: float = 1.0) -> dict[str, Any]:
    holdings = [
        {"currency": quote, "available": 4_182.55 * factor, "locked": 1_240.00 * factor},
        {
            "currency": "BNB",
            "available": 6.4820 * factor,
            "locked": 2.1000 * factor,
            "price": 611.85,
        },
        {"currency": "BTC", "available": 0.04120 * factor, "locked": 0.0, "price": 96_240.0},
        {"currency": "GT", "available": 148.20 * factor, "locked": 0.0, "price": 12.44},
    ]
    priced = []
    equity = 0.0
    for row in holdings:
        total = row["available"] + row["locked"]
        price = 1.0 if row["currency"] == quote else row.get("price")
        value = total * price if price else None
        if value:
            equity += value
        priced.append({**row, "total": total, "price": price, "value": value})

    priced.sort(key=lambda h: h["value"] or 0, reverse=True)

    return {
        "demo": True,
        "account_type": "spot",
        "currency": quote,
        "equity": equity,
        "wallet_balance": equity,
        "unrealised_pnl": 0.0,
        "available": sum(h["value"] or 0 for h in priced if h["locked"] == 0),
        "position_margin": 0.0,
        "holdings": priced,
        "unpriced": [],
        "fee_tier": {
            "maker": 0.00012,
            "taker": 0.00036,
            "base_maker": 0.0002,
            "base_taker": 0.0005,
            "gt_discount": True,
            "point_type": "0",
            "futures_maker": 0.0001,
            "futures_taker": 0.00035,
        },
    }


def unified_snapshot(quote: str, equity: float) -> dict[str, Any]:
    balances = [
        {
            "currency": quote,
            "available": equity * 0.44,
            "freeze": equity * 0.08,
            "borrowed": 0.0,
            "interest": 0.0,
            "equity": equity * 0.52,
            "unrealised_pnl": 0.0,
        },
        {
            "currency": "BNB",
            "available": 7.24,
            "freeze": 0.0,
            "borrowed": 3.10,
            "interest": 0.00042180,
            "equity": equity * 0.48,
            "unrealised_pnl": -318.40,
        },
    ]
    maintenance = equity * 0.19
    margin_balance = equity * 0.94

    return {
        "demo": True,
        "account_type": "unified",
        "currency": quote,
        "equity": equity,
        "wallet_balance": equity,
        "unrealised_pnl": sum(b["unrealised_pnl"] for b in balances),
        "available": equity * 0.41,
        "position_margin": equity * 0.33,
        "maintenance_margin": maintenance,
        "liabilities": 1_896.74,
        "leverage": 3.0,
        "margin_ratio": maintenance / margin_balance,
        "accrued_interest": sum(b["interest"] for b in balances),
        "balances": balances,
        "risk_units": [],
    }
