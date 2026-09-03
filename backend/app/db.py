"""Local SQLite store.

Gate's history endpoints have hard retention limits (spot trades roughly a
month, futures ledgers a few months) and only offer offset/limit paging. The
dashboard therefore treats Gate as a source to sync *from*, and answers every
query out of this database.

Every row is keyed by (profile, account): the profile is which API key it came
from, the account is which product within that key. Both belong in the primary
key — Gate's trade and ledger ids are only unique within one account, so two
profiles can legitimately produce the same id.

The daily equity snapshot is the one table that cannot be backfilled: once a day
rolls over, that day's closing equity is gone from the API forever. It is written
on every refresh so the newest observation for each day wins.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

SCHEMA_VERSION = 4

# Profile id that pre-multi-key rows are attributed to on migration. It matches
# the id a single-key .env setup is loaded as, so an existing history stays
# attached to the credentials that produced it.
DEFAULT_PROFILE = "default"

SCHEMA = """
CREATE TABLE IF NOT EXISTS equity_snapshot (
    profile         TEXT NOT NULL,
    day             TEXT NOT NULL,
    account         TEXT NOT NULL,
    equity          REAL NOT NULL,
    unrealised_pnl  REAL NOT NULL DEFAULT 0,
    available       REAL NOT NULL DEFAULT 0,
    position_margin REAL NOT NULL DEFAULT 0,
    captured_at     INTEGER NOT NULL,
    PRIMARY KEY (profile, day, account)
);
CREATE INDEX IF NOT EXISTS idx_equity_day ON equity_snapshot (day);

CREATE TABLE IF NOT EXISTS ledger (
    profile   TEXT NOT NULL,
    account   TEXT NOT NULL,
    entry_id  TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    day       TEXT NOT NULL,
    type      TEXT NOT NULL,
    change    REAL NOT NULL,
    balance   REAL,
    currency  TEXT,
    contract  TEXT,
    text      TEXT,
    PRIMARY KEY (profile, account, entry_id)
);
CREATE INDEX IF NOT EXISTS idx_ledger_day ON ledger (profile, account, day, type);

CREATE TABLE IF NOT EXISTS trades (
    profile      TEXT NOT NULL,
    account      TEXT NOT NULL,
    trade_id     TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    day          TEXT NOT NULL,
    market       TEXT NOT NULL,
    side         TEXT,
    role         TEXT,
    price        REAL,
    size         REAL,
    quote_amount REAL,
    fee          REAL,
    fee_currency TEXT,
    order_id     TEXT,
    PRIMARY KEY (profile, account, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_day ON trades (profile, account, day);

CREATE TABLE IF NOT EXISTS cashflow (
    profile  TEXT NOT NULL,
    account  TEXT NOT NULL,
    kind     TEXT NOT NULL,
    ref_id   TEXT NOT NULL,
    ts       INTEGER NOT NULL,
    day      TEXT NOT NULL,
    currency TEXT,
    amount   REAL NOT NULL,
    status   TEXT,
    PRIMARY KEY (profile, account, kind, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_cashflow_day ON cashflow (profile, account, day);

CREATE TABLE IF NOT EXISTS position_close (
    profile         TEXT NOT NULL,
    account         TEXT NOT NULL,
    ref_id          TEXT NOT NULL,
    ts              INTEGER NOT NULL,
    day             TEXT NOT NULL,
    contract        TEXT,
    side            TEXT,
    pnl             REAL,
    text            TEXT,
    first_open_time INTEGER,
    max_size        REAL,
    long_price      REAL,
    short_price     REAL,
    pnl_pnl         REAL,
    pnl_fund        REAL,
    pnl_fee         REAL,
    PRIMARY KEY (profile, account, ref_id)
);
CREATE INDEX IF NOT EXISTS idx_position_close_day ON position_close (profile, account, day);

CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at INTEGER NOT NULL
);
"""

# v1 stored no profile column. The rebuild stamps existing rows as the implicit
# "default" profile, which is what a single-key .env setup is loaded as, so a
# pre-existing history stays attached to the right credentials.
_V1_TABLES = ("equity_snapshot", "ledger", "trades", "cashflow", "position_close")


class Database:
    def __init__(self, path: Path, tz: ZoneInfo) -> None:
        self._path = path
        self._tz = tz
        self._lock = asyncio.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------------------------------------------------------------- migration

    def _migrate(self) -> None:
        """Bring an older database up to the current schema in place."""
        tables = {
            row["name"]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "ledger" not in tables:
            return  # fresh database; SCHEMA will create everything

        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(ledger)")
        }
        if "profile" not in columns:
            self._rebuild_v1_to_v2(tables)

        # v3: position_close learns when the position was opened and how big
        # it got, so the history panel can show holding duration. Idempotent.
        self._ensure_column("position_close", "first_open_time", "INTEGER")
        self._ensure_column("position_close", "max_size", "REAL")
        # v4: entry/exit prices and the PnL decomposition, so the history panel
        # matches what the export shows.
        self._ensure_column("position_close", "long_price", "REAL")
        self._ensure_column("position_close", "short_price", "REAL")
        self._ensure_column("position_close", "pnl_pnl", "REAL")
        self._ensure_column("position_close", "pnl_fund", "REAL")
        self._ensure_column("position_close", "pnl_fee", "REAL")
        # Millisecond timestamps slipped into the raw ts columns before the
        # ingest-side normalization existed; heal them once, idempotently.
        with self._conn:
            for table in ("ledger", "cashflow", "trades", "position_close"):
                cursor = self._conn.execute(
                    f"UPDATE {table} SET ts = ts / 1000 WHERE ts > 100000000000"
                )
                if cursor.rowcount:
                    log.info("normalized %d millisecond rows in %s", cursor.rowcount, table)

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        present = {
            row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")
        }
        if column in present:
            return
        with self._conn:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        log.info("added column %s.%s", table, column)

    def _rebuild_v1_to_v2(self, tables: set[str]) -> None:

        log.warning(
            "migrating %s to schema v2: adding the profile column and rebuilding "
            "primary keys; existing rows are stamped as profile 'default'",
            self._path.name,
        )

        present = [table for table in _V1_TABLES if table in tables]
        old_columns = {
            table: [
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})")
            ]
            for table in present
        }

        # Step 1: move the v1 tables aside. Renaming a table keeps its indexes
        # under their original names, which would make the CREATE INDEX IF NOT
        # EXISTS statements below silently no-op and leave the new tables
        # unindexed once the old ones are dropped — so drop those indexes now.
        with self._conn:
            for table in present:
                for row in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name = ? AND name NOT LIKE 'sqlite_%'",
                    (table,),
                ).fetchall():
                    self._conn.execute(f"DROP INDEX IF EXISTS {row['name']}")
                self._conn.execute(f"ALTER TABLE {table} RENAME TO {table}__v1")

        # Step 2: create the v2 tables and indexes. executescript commits, so it
        # stays outside any transaction block of ours.
        self._conn.executescript(SCHEMA)

        # Step 3: copy the rows across, then discard the originals.
        with self._conn:
            for table in present:
                projected = ", ".join(old_columns[table])
                self._conn.execute(
                    f"INSERT INTO {table} (profile, {projected}) "
                    f"SELECT '{DEFAULT_PROFILE}', {projected} FROM {table}__v1"
                )
                moved = self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM {table}"
                ).fetchone()["n"]
                self._conn.execute(f"DROP TABLE {table}__v1")
                log.info("  %s: %d row(s) migrated", table, moved)

            self._conn.execute(
                "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    "schema_version",
                    str(SCHEMA_VERSION),
                    int(datetime.now(tz=timezone.utc).timestamp()),
                ),
            )

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ helpers

    def day_of(self, ts: float) -> str:
        """Calendar day for a unix timestamp, in the configured dashboard tz.

        Gate's spot account book mixes seconds and milliseconds in its `time`
        field depending on entry type, so anything past 1e11 is unambiguously
        milliseconds (seconds there would be the year 5138) and is scaled down.
        """
        seconds = float(ts)
        if seconds > 1e11:
            seconds /= 1000.0
        return (
            datetime.fromtimestamp(seconds, tz=timezone.utc)
            .astimezone(self._tz)
            .strftime("%Y-%m-%d")
        )

    def today(self) -> str:
        return datetime.now(tz=self._tz).strftime("%Y-%m-%d")

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._query_sync, sql, params)

    def _query_sync(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        cursor = self._conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    async def upsert_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        batch = list(rows)
        if not batch:
            return 0
        async with self._lock:
            await asyncio.to_thread(self._upsert_sync, sql, batch)
        return len(batch)

    def _upsert_sync(self, sql: str, batch: list[Sequence[Any]]) -> None:
        with self._conn:
            self._conn.executemany(sql, batch)

    async def set_state(self, key: str, value: str) -> None:
        await self.upsert_many(
            "INSERT INTO sync_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            [(key, value, int(datetime.now(tz=timezone.utc).timestamp()))],
        )

    async def get_state(self, key: str) -> str | None:
        rows = await self.query("SELECT value FROM sync_state WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None


# Upsert statements used by the sync services. Kept next to the schema so a
# column change is visible in one place.

UPSERT_EQUITY = """
INSERT INTO equity_snapshot
    (profile, day, account, equity, unrealised_pnl, available, position_margin, captured_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(profile, day, account) DO UPDATE SET
    equity=excluded.equity,
    unrealised_pnl=excluded.unrealised_pnl,
    available=excluded.available,
    position_margin=excluded.position_margin,
    captured_at=excluded.captured_at
"""

UPSERT_LEDGER = """
INSERT INTO ledger
    (profile, account, entry_id, ts, day, type, change, balance, currency, contract, text)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(profile, account, entry_id) DO NOTHING
"""

UPSERT_TRADE = """
INSERT INTO trades
    (profile, account, trade_id, ts, day, market, side, role, price, size,
     quote_amount, fee, fee_currency, order_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(profile, account, trade_id) DO NOTHING
"""

UPSERT_CASHFLOW = """
INSERT INTO cashflow (profile, account, kind, ref_id, ts, day, currency, amount, status)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(profile, account, kind, ref_id) DO UPDATE SET
    status=excluded.status,
    amount=excluded.amount
"""

UPSERT_POSITION_CLOSE = """
INSERT INTO position_close
    (profile, account, ref_id, ts, day, contract, side, pnl, text,
     first_open_time, max_size, long_price, short_price, pnl_pnl, pnl_fund, pnl_fee)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(profile, account, ref_id) DO UPDATE SET
    first_open_time = COALESCE(excluded.first_open_time, position_close.first_open_time),
    max_size = COALESCE(excluded.max_size, position_close.max_size),
    long_price = COALESCE(excluded.long_price, position_close.long_price),
    short_price = COALESCE(excluded.short_price, position_close.short_price),
    pnl_pnl = COALESCE(excluded.pnl_pnl, position_close.pnl_pnl),
    pnl_fund = COALESCE(excluded.pnl_fund, position_close.pnl_fund),
    pnl_fee = COALESCE(excluded.pnl_fee, position_close.pnl_fee)
"""
