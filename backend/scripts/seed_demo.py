"""Seed the local database with a plausible grid-strategy history.

Useful for two things: seeing the dashboard before wiring up API credentials,
and exercising the layout with realistic shapes (long stretches of small
positive realized PnL, a widening unrealised loss, one deposit, one drawdown).

The data is deterministic — a fixed seed, so the page looks the same every run.

    ./.venv/bin/python -m backend.scripts.seed_demo --days 60

Delete the rows again with --clear.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import zlib
from datetime import date, datetime, time, timedelta, timezone

from ..app.config import settings
from ..app.db import (
    UPSERT_CASHFLOW,
    UPSERT_EQUITY,
    UPSERT_LEDGER,
    UPSERT_POSITION_CLOSE,
    UPSERT_TRADE,
    Database,
)

CONTRACT = "BNB_USDT"
SEED = 20260829

# Ledger type names differ per product, and ledger_types.classify() keys off
# them — seeding futures names into a spot account would land every row in the
# "unclassified" bucket and leave the demo covered in warning chips.
LEDGER_NAMES = {
    "spot": {
        "realized": "trade",
        "fee": "fee",
        "funding": None,  # spot has no funding
        "rebate": "rebate",
        "cashflow": "deposit",
    },
    "futures": {
        "realized": "pnl",
        "fee": "fee",
        "funding": "fund",
        "rebate": "refr",
        "cashflow": "dnw",
    },
}


def _names(account: str) -> dict[str, str | None]:
    return LEDGER_NAMES["spot" if account == "spot" else "futures"]


def _ts(day: date, hour: int, minute: int = 0, second: int = 0) -> int:
    """Unix seconds for a wall-clock moment on `day` in the dashboard timezone."""
    moment = datetime.combine(day, time(hour, minute, second), tzinfo=settings.tz)
    return int(moment.astimezone(timezone.utc).timestamp())


async def seed(db: Database, days: int, profile: str, account: str) -> None:
    # Seed varies with the profile id so several demo profiles produce
    # different curves instead of identical ones.
    rng = random.Random(SEED + zlib.crc32(profile.encode()))
    names = _names(account)
    today = date.fromisoformat(db.today())
    start = today - timedelta(days=days - 1)

    equity = 10_000.0
    unrealised = 0.0
    price = 610.0

    ledger: list[tuple] = []
    equity_rows: list[tuple] = []
    cashflow: list[tuple] = []
    trades: list[tuple] = []
    closes: list[tuple] = []

    deposit_day = start + timedelta(days=days // 2)

    for offset in range(days):
        current = start + timedelta(days=offset)
        day_str = current.isoformat()
        seq = 0

        def entry_id(kind: str) -> str:
            nonlocal seq
            seq += 1
            return f"demo:{kind}:{day_str}:{seq}"

        # The grid books a steady trickle of realized profit — it closes small
        # winners all day and essentially never a loser.
        fills = rng.randint(30, 130)
        realized = equity * rng.uniform(0.0018, 0.0075)
        fee = -realized * rng.uniform(0.18, 0.42)
        funding = equity * rng.uniform(-0.0006, 0.0002)
        rebate = -fee * rng.uniform(0.0, 0.12)

        ledger.append(
            (profile, account, entry_id("pnl"), _ts(current, 23, 55), day_str, names["realized"],
             round(realized, 6), 0.0, "USDT", CONTRACT, "grid")
        )
        ledger.append(
            (profile, account, entry_id("fee"), _ts(current, 23, 56), day_str, names["fee"],
             round(fee, 6), 0.0, "USDT", CONTRACT, "grid")
        )
        if names["funding"]:
            ledger.append(
                (profile, account, entry_id("fund"), _ts(current, 23, 57), day_str,
                 names["funding"], round(funding, 6), 0.0, "USDT", CONTRACT, "funding")
            )
        else:
            funding = 0.0
        if rebate > 0:
            ledger.append(
                (profile, account, entry_id("refr"), _ts(current, 23, 58), day_str,
                 names["rebate"], round(rebate, 6), 0.0, "USDT", CONTRACT, "referral")
            )

        # Price walks with a downtrend in the middle third, which is where a
        # long-biased grid accumulates inventory and the unrealised loss grows.
        drift = -0.011 if days // 3 <= offset < 2 * days // 3 else 0.004
        price *= 1 + drift + rng.gauss(0, 0.016)
        # Open inventory marks against the position on the way down.
        unrealised += -realized * rng.uniform(0.4, 2.6) if drift < 0 else rng.uniform(-8, 22)
        unrealised = max(unrealised, -equity * 0.35)

        day_cashflow = 0.0
        if current == deposit_day:
            day_cashflow = 5_000.0
            ledger.append(
                (profile, account, entry_id("dnw"), _ts(current, 10), day_str, names["cashflow"],
                 day_cashflow, 0.0, "USDT", "", "transfer in")
            )
            cashflow.append(
                (profile, account, names["cashflow"], entry_id("cf"),
                 _ts(current, 10), day_str, "USDT", day_cashflow, "done")
            )

        net_realized = realized + fee + funding + rebate
        equity += net_realized + day_cashflow
        # Snapshot is taken at the last refresh of the day; equity includes the
        # mark-to-market on open inventory.
        equity_rows.append(
            (profile, day_str, account, round(equity + unrealised, 6), round(unrealised, 6),
             round(equity * 0.72, 6), round(equity * 0.28, 6), _ts(current, 23, 59))
        )

        for i in range(fills):
            size = rng.choice([1, 1, 2, 3, 5])
            fill_price = price * (1 + rng.gauss(0, 0.0012))
            trades.append(
                (profile, account, f"demo-t-{day_str}-{i}", _ts(current, rng.randint(0, 23), rng.randint(0, 59)),
                 day_str, CONTRACT, rng.choice(["buy", "sell"]),
                 # A grid lives on maker fills; taker fills are the ones that hurt.
                 "maker" if rng.random() < 0.78 else "taker",
                 round(fill_price, 4), size, round(size * fill_price, 4),
                 round(fee / fills, 8), "USDT", f"demo-o-{day_str}-{i}")
            )

        for i in range(rng.randint(2, 9)):
            closes.append(
                (profile, account, f"demo-c-{day_str}-{i}", _ts(current, rng.randint(0, 23)),
                 day_str, CONTRACT, rng.choice(["long", "short"]),
                 round(realized / 6 * rng.uniform(0.4, 1.8), 6), "grid close")
            )

    await db.upsert_many(UPSERT_LEDGER, ledger)
    await db.upsert_many(UPSERT_EQUITY, equity_rows)
    await db.upsert_many(UPSERT_CASHFLOW, cashflow)
    await db.upsert_many(UPSERT_TRADE, trades)
    await db.upsert_many(UPSERT_POSITION_CLOSE, closes)
    await db.set_state("last_sync", str(_ts(today, 23, 59)))

    print(
        f"seeded {days} days for {profile}/{account}: "
        f"{len(ledger)} ledger, {len(trades)} trades, {len(closes)} closes, "
        f"final equity {equity + unrealised:,.2f} (unrealised {unrealised:,.2f})"
    )


async def clear_everything(db: Database) -> None:
    """Wipe every history table, whatever profile the rows belong to.

    Needed because --clear only knows the profiles in the *current* config: seed
    a demo profile, change the config, and those rows become unreachable. This
    deletes real synced history too, which is why it says so and why it is a
    separate flag.
    """
    tables = ("ledger", "trades", "cashflow", "position_close", "equity_snapshot")
    for table in tables:
        before = await db.query(f"SELECT COUNT(*) AS n FROM {table}")
        await db.query(f"DELETE FROM {table}")
        print(f"  {table}: deleted {before[0]['n']} row(s)")
    await db.set_state("last_sync", "")
    print("all history cleared — the next sync starts from scratch")


async def clear(db: Database, profile: str, account: str) -> None:
    for table in ("ledger", "trades", "cashflow", "position_close", "equity_snapshot"):
        await db.query(
            f"DELETE FROM {table} WHERE profile = ? AND account = ?", (profile, account)
        )
    print(f"cleared demo rows for {profile}/{account}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument(
        "--profile",
        default=None,
        help="profile id to seed; defaults to every configured profile",
    )
    parser.add_argument("--account", default="futures_usdt")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument(
        "--clear-all",
        action="store_true",
        help="wipe every history table for every profile, including real synced data",
    )
    args = parser.parse_args()

    if args.clear_all:
        db = Database(settings.db_path, settings.tz)
        try:
            asyncio.run(clear_everything(db))
        finally:
            db.close()
        return

    targets = (
        [(args.profile, args.account)]
        if args.profile
        else [(p.id, a) for p in settings.profiles for a in p.accounts]
    )

    db = Database(settings.db_path, settings.tz)
    try:
        for profile_id, account_key in targets:
            if args.clear:
                asyncio.run(clear(db, profile_id, account_key))
            else:
                asyncio.run(seed(db, args.days, profile_id, account_key))
    finally:
        db.close()


if __name__ == "__main__":
    main()
