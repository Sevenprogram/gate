"""Manual-refresh orchestration.

One entry point that pulls everything every configured profile needs and writes
it to the local store. The frontend calls this from its refresh button; nothing
runs on a timer, so what you see is always "as of the last time you asked".

Failures are collected per step rather than raised: one profile with a revoked
key, or one account type that isn't opened, must not blank out the rest of the
dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..config import SETTLE_BY_ACCOUNT, SPOT, UNIFIED, Profile, Settings
from ..db import Database
from ..gate_client import ClientRegistry, GateAPIError, GateClient, MissingCredentials
from . import futures, spot, unified, wallet

log = logging.getLogger(__name__)


async def refresh_all(
    registry: ClientRegistry, db: Database, settings: Settings, days: int = 30
) -> dict[str, Any]:
    end = int(time.time())
    start = end - days * 86400

    results: dict[str, Any] = {}
    errors: list[str] = []

    async def run(label: str, coro: Any) -> None:
        try:
            results[label] = await coro
        except MissingCredentials as exc:
            errors.append(f"{label}: {exc}")
        except GateAPIError as exc:
            errors.append(f"{label}: {exc.label} — {exc.message}")
        except Exception as exc:  # noqa: BLE001 - surface, don't crash the refresh
            log.exception("sync step %s failed", label)
            errors.append(f"{label}: {type(exc).__name__} — {exc}")

    tasks = []
    for profile in settings.profiles:
        if not profile.has_credentials and not settings.demo:
            errors.append(f"{profile.id}: 没有配置密钥，跳过")
            continue
        client = registry.get(profile.id)
        tasks.extend(_profile_tasks(run, client, db, settings, profile, start, end))

    await asyncio.gather(*tasks)

    # Futures trade sync needs the contract list, which the ledger sync above
    # just populated, so it runs after the first wave rather than alongside it.
    second_wave = []
    for profile in settings.profiles:
        if not profile.has_credentials and not settings.demo:
            continue
        client = registry.get(profile.id)
        for account in profile.accounts:
            settle = SETTLE_BY_ACCOUNT.get(account)
            if settle is None:
                continue
            contracts = await _traded_contracts(db, profile.id, account)
            second_wave.append(
                run(
                    f"{profile.id}/{account} 成交",
                    futures.sync_trades(
                        client, db, profile.id, account, settle, start, contracts
                    ),
                )
            )
    await asyncio.gather(*second_wave)

    await db.set_state("last_sync", str(end))
    await db.set_state("last_sync_days", str(days))

    return {
        "synced_at": end,
        "window_days": days,
        "profiles": [p.id for p in settings.profiles],
        "steps": results,
        "errors": errors,
    }


def _profile_tasks(
    run: Any,
    client: GateClient,
    db: Database,
    settings: Settings,
    profile: Profile,
    start: int,
    end: int,
) -> list[Any]:
    """Everything that can be pulled for one profile in parallel."""
    tag = profile.id
    tasks = [
        run(f"{tag} 出入金", wallet.sync_transfers(client, db, tag, start, end)),
    ]

    for account in profile.accounts:
        if account == SPOT:
            tasks.append(
                run(f"{tag}/spot 账本", spot.sync_ledger(client, db, tag, start, end))
            )
            tasks.append(
                run(
                    f"{tag}/spot 成交",
                    spot.sync_trades(
                        client, db, tag, start, end, list(settings.spot_pairs)
                    ),
                )
            )
            tasks.append(
                run(f"{tag}/spot 快照", _spot_snapshot(client, db, tag, settings))
            )
        elif account in SETTLE_BY_ACCOUNT:
            settle = SETTLE_BY_ACCOUNT[account]
            tasks.append(
                run(
                    f"{tag}/{account} 账本",
                    futures.sync_ledger(client, db, tag, account, settle, start, end),
                )
            )
            tasks.append(
                run(
                    f"{tag}/{account} 平仓记录",
                    futures.sync_position_close(
                        client, db, tag, account, settle, start, end
                    ),
                )
            )
            tasks.append(
                run(
                    f"{tag}/{account} 快照",
                    _futures_snapshot(client, db, tag, account, settle),
                )
            )
        elif account == UNIFIED:
            tasks.append(
                run(f"{tag}/unified 快照", _unified_snapshot(client, db, tag, settings))
            )

    return tasks


async def _traded_contracts(db: Database, profile: str, account: str) -> list[str]:
    """Contracts seen in the ledger, so trade sync knows what to ask for."""
    rows = await db.query(
        "SELECT DISTINCT contract FROM ledger "
        "WHERE profile = ? AND account = ? AND contract IS NOT NULL AND contract != ''",
        (profile, account),
    )
    return [r["contract"] for r in rows]


async def _spot_snapshot(
    client: GateClient, db: Database, profile: str, settings: Settings
) -> dict[str, Any]:
    snap = await spot.snapshot(client, settings.quote)
    await spot.record_equity_snapshot(db, profile, snap)
    return {"equity": snap["equity"], "holdings": len(snap.get("holdings", []))}


async def _futures_snapshot(
    client: GateClient, db: Database, profile: str, account: str, settle: str
) -> dict[str, Any]:
    snap = await futures.snapshot(client, settle)
    await futures.record_equity_snapshot(db, profile, account, snap)
    return {"equity": snap["equity"], "positions": len(snap["positions"])}


async def _unified_snapshot(
    client: GateClient, db: Database, profile: str, settings: Settings
) -> dict[str, Any]:
    snap = await unified.snapshot(client, settings.quote)
    await unified.record_equity_snapshot(db, profile, snap)
    return {"equity": snap["equity"], "liabilities": snap["liabilities"]}
