"""Live portfolio totals across every configured profile and account.

Summing live snapshots is only valid when the parts share a unit, so accounts
that settle in something other than the dashboard quote are listed as excluded
rather than converted — this app stores no exchange rates, and a guessed rate
would silently misstate the total.

A scope whose live call fails is reported too. The alternative, quietly summing
what did come back, produces a total that looks precise and is wrong.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import NON_QUOTE_ACCOUNTS, SETTLE_BY_ACCOUNT, SPOT, UNIFIED, Settings
from ..gate_client import ClientRegistry, GateAPIError, MissingCredentials
from . import demo, futures, spot, unified

log = logging.getLogger(__name__)


async def snapshot(
    registry: ClientRegistry, settings: Settings, db: Any = None
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    errors: list[str] = []
    excluded: list[dict[str, str]] = []

    for profile in settings.profiles:
        for account in profile.accounts:
            if account in NON_QUOTE_ACCOUNTS:
                excluded.append(
                    {
                        "profile": profile.id,
                        "account": account,
                        "reason": f"以 {account.split('_')[-1].upper()} 结算，无法直接加总",
                    }
                )
                continue
            try:
                snap = await _one(registry, settings, profile.id, account, db)
            except MissingCredentials as exc:
                errors.append(f"{profile.id}/{account}: {exc}")
                continue
            except GateAPIError as exc:
                errors.append(f"{profile.id}/{account}: {exc.label} — {exc.message}")
                continue
            except Exception as exc:  # noqa: BLE001
                log.exception("portfolio snapshot failed for %s/%s", profile.id, account)
                errors.append(f"{profile.id}/{account}: {type(exc).__name__} — {exc}")
                continue

            parts.append(
                {
                    "profile": profile.id,
                    "profile_label": profile.label,
                    "account": account,
                    "equity": snap.get("equity") or 0.0,
                    "unrealised_pnl": snap.get("unrealised_pnl") or 0.0,
                    "available": snap.get("available") or 0.0,
                    "position_margin": snap.get("position_margin") or 0.0,
                    "maintenance_margin": snap.get("maintenance_margin") or 0.0,
                    "liabilities": snap.get("liabilities") or 0.0,
                    "position_count": len(snap.get("positions") or []),
                    # Carried up so the portfolio view can lead with the worst
                    # liquidation distance anywhere in the account set.
                    "nearest_liq": _nearest_liq(snap),
                }
            )

    equity = sum(p["equity"] for p in parts)
    maintenance = sum(p["maintenance_margin"] for p in parts)
    margin = sum(p["position_margin"] for p in parts)
    liq_candidates = [p["nearest_liq"] for p in parts if p["nearest_liq"] is not None]

    parts.sort(key=lambda p: p["equity"], reverse=True)

    return {
        "demo": settings.demo,
        "currency": settings.quote,
        "equity": equity,
        "unrealised_pnl": sum(p["unrealised_pnl"] for p in parts),
        "available": sum(p["available"] for p in parts),
        "position_margin": margin,
        "maintenance_margin": maintenance,
        "liabilities": sum(p["liabilities"] for p in parts),
        "margin_ratio": margin / equity if equity else 0.0,
        "maintenance_ratio": maintenance / equity if equity else 0.0,
        "position_count": sum(p["position_count"] for p in parts),
        "nearest_liq": min(liq_candidates) if liq_candidates else None,
        "parts": parts,
        "excluded": excluded,
        "errors": errors,
    }


def _nearest_liq(snap: dict[str, Any]) -> float | None:
    distances = [
        p["liq_distance_pct"]
        for p in (snap.get("positions") or [])
        if p.get("liq_distance_pct") is not None
    ]
    return min(distances) if distances else None


async def _one(
    registry: ClientRegistry,
    settings: Settings,
    profile_id: str,
    account: str,
    db: Any,
) -> dict[str, Any]:
    if settings.demo:
        return await demo.snapshot_for(account, profile_id, settings.quote, db)

    client = registry.get(profile_id)
    if account == SPOT:
        return await spot.snapshot(client, settings.quote)
    if account == UNIFIED:
        return await unified.snapshot(client, settings.quote)
    return await futures.snapshot(client, SETTLE_BY_ACCOUNT[account])
