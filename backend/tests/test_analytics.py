"""Tests for the daily PnL math and multi-account aggregation.

These run without credentials: synthetic rows go straight into a temp database,
so the arithmetic that every number on the page depends on is checked in
isolation from Gate.

Run with:  ./.venv/bin/python -m pytest backend/tests -q
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from backend.app.config import Profile
from backend.app.db import (
    UPSERT_CASHFLOW,
    UPSERT_EQUITY,
    UPSERT_LEDGER,
    UPSERT_TRADE,
    Database,
)
from backend.app.gate_client import EMPTY_BODY_HASH, GateClient
from backend.app.services import analytics

from datetime import date as D

TZ = ZoneInfo("Asia/Shanghai")
ACCOUNT = "futures_usdt"
MAIN = "main"
GRID = "grid"
SCOPE = (MAIN, ACCOUNT)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.sqlite3", TZ)
    yield database
    database.close()


def load(db: Database, **batches: list[tuple]) -> None:
    statements = {
        "ledger": UPSERT_LEDGER,
        "equity": UPSERT_EQUITY,
        "cashflow": UPSERT_CASHFLOW,
        "trades": UPSERT_TRADE,
    }

    async def _load() -> None:
        for name, rows in batches.items():
            await db.upsert_many(statements[name], rows)

    asyncio.run(_load())


def series_for(db: Database, scopes: list[tuple[str, str]], days: int = 90):
    return asyncio.run(analytics.daily_series(db, scopes, days=days))


def seed_single(db: Database) -> None:
    """Three days of a grid strategy that books profit while equity slides.

    Day 1: +100 realized, -10 fee, -5 funding. Equity 10000.
    Day 2: +120 realized, -12 fee, -3 funding, but the open position went
           against us, so equity only reaches 10040 (unrealised -65).
    Day 3: +80 realized, -8 fee, and a 5000 deposit lands. Equity 15020.
    """
    load(
        db,
        ledger=[
            (MAIN, ACCOUNT, "p1", 1700000000, "2023-11-15", "pnl", 100.0, 10100.0, "USDT", "BNB_USDT", ""),
            (MAIN, ACCOUNT, "f1", 1700000001, "2023-11-15", "fee", -10.0, 10090.0, "USDT", "BNB_USDT", ""),
            (MAIN, ACCOUNT, "u1", 1700000002, "2023-11-15", "fund", -5.0, 10085.0, "USDT", "BNB_USDT", ""),
            (MAIN, ACCOUNT, "p2", 1700086400, "2023-11-16", "pnl", 120.0, 10205.0, "USDT", "BNB_USDT", ""),
            (MAIN, ACCOUNT, "f2", 1700086401, "2023-11-16", "fee", -12.0, 10193.0, "USDT", "BNB_USDT", ""),
            (MAIN, ACCOUNT, "u2", 1700086402, "2023-11-16", "fund", -3.0, 10190.0, "USDT", "BNB_USDT", ""),
            (MAIN, ACCOUNT, "p3", 1700172800, "2023-11-17", "pnl", 80.0, 10270.0, "USDT", "BNB_USDT", ""),
            (MAIN, ACCOUNT, "f3", 1700172801, "2023-11-17", "fee", -8.0, 10262.0, "USDT", "BNB_USDT", ""),
            (MAIN, ACCOUNT, "d3", 1700172802, "2023-11-17", "dnw", 5000.0, 15262.0, "USDT", "", ""),
        ],
        equity=[
            (MAIN, "2023-11-15", ACCOUNT, 10000.0, 0.0, 9000.0, 1000.0, 1700010000),
            (MAIN, "2023-11-16", ACCOUNT, 10040.0, -65.0, 9000.0, 1000.0, 1700096000),
            (MAIN, "2023-11-17", ACCOUNT, 15020.0, -100.0, 14000.0, 1000.0, 1700182000),
        ],
        cashflow=[
            (MAIN, ACCOUNT, "dnw", "d3", 1700172802, "2023-11-17", "USDT", 5000.0, "done"),
        ],
        trades=[
            (MAIN, ACCOUNT, "t1", 1700000000, "2023-11-15", "BNB_USDT", "buy", "maker",
             600.0, 1.0, 600.0, -10.0, "USDT", "o1"),
            (MAIN, ACCOUNT, "t2", 1700000005, "2023-11-15", "BNB_USDT", "sell", "taker",
             605.0, 1.0, 605.0, -10.0, "USDT", "o2"),
            (MAIN, ACCOUNT, "t3", 1700086400, "2023-11-16", "BNB_USDT", "buy", "maker",
             598.0, 2.0, 1196.0, -12.0, "USDT", "o3"),
        ],
    )


# --------------------------------------------------------------- single scope


def test_realized_and_equity_pnl_disagree_as_expected(db: Database) -> None:
    seed_single(db)
    by_day = {row["day"]: row for row in series_for(db, [SCOPE])}

    day2 = by_day["2023-11-16"]
    # Ledger view: profit minus costs.
    assert day2["realized"] == pytest.approx(120.0)
    assert day2["fee"] == pytest.approx(-12.0)
    assert day2["funding"] == pytest.approx(-3.0)
    assert day2["net_realized"] == pytest.approx(105.0)

    # Equity view: only +40, because the open position moved against us.
    assert day2["equity_delta"] == pytest.approx(40.0)
    assert day2["pnl_equity"] == pytest.approx(40.0)

    # The gap between the two is the unrealised swing. This is the number that
    # catches a grid quietly accumulating a losing position.
    assert day2["unrealised_change"] == pytest.approx(-65.0)


def test_deposit_is_excluded_from_pnl(db: Database) -> None:
    seed_single(db)
    day3 = next(r for r in series_for(db, [SCOPE]) if r["day"] == "2023-11-17")

    # Balance jumped by 4980 but 5000 of that was money moved in.
    assert day3["equity_delta"] == pytest.approx(4980.0)
    assert day3["cashflow_net"] == pytest.approx(5000.0)
    assert day3["pnl_equity"] == pytest.approx(-20.0)
    assert day3["realized"] == pytest.approx(80.0)


def test_return_uses_pnl_not_raw_delta(db: Database) -> None:
    """A deposit that exactly offsets a loss leaves delta at zero.

    Dividing the raw delta would report a 0% day; the real return is negative.
    """
    load(
        db,
        equity=[
            (MAIN, "2024-01-01", ACCOUNT, 1000.0, 0.0, 0.0, 0.0, 1),
            (MAIN, "2024-01-02", ACCOUNT, 1000.0, 0.0, 0.0, 0.0, 2),
        ],
        cashflow=[
            (MAIN, ACCOUNT, "dnw", "c1", 1, "2024-01-02", "USDT", 50.0, "done"),
        ],
    )
    row = next(r for r in series_for(db, [SCOPE]) if r["day"] == "2024-01-02")
    assert row["equity_delta"] == pytest.approx(0.0)
    assert row["pnl_equity"] == pytest.approx(-50.0)
    assert row["return_pct"] == pytest.approx(-0.05)


def test_first_day_has_no_delta(db: Database) -> None:
    seed_single(db)
    day1 = series_for(db, [SCOPE])[0]
    # No prior snapshot exists, so an equity delta would be a fabrication.
    assert day1["pnl_equity"] is None
    assert day1["return_pct"] is None


def test_maker_ratio_and_fee_ratio(db: Database) -> None:
    seed_single(db)
    series = series_for(db, [SCOPE])
    day1 = next(r for r in series if r["day"] == "2023-11-15")
    assert day1["maker_ratio"] == pytest.approx(0.5)
    assert day1["fee_to_gross_ratio"] == pytest.approx(0.1)

    costs = analytics.cost_summary(series)
    assert costs["maker_ratio"] == pytest.approx(2 / 3)
    assert costs["gross_realized"] == pytest.approx(300.0)
    # fees 30 + funding paid 8, against 300 gross.
    assert costs["total_cost"] == pytest.approx(38.0)
    assert costs["cost_to_gross_ratio"] == pytest.approx(38.0 / 300.0)


def test_stats_use_time_weighted_returns(db: Database) -> None:
    seed_single(db)
    stats = analytics.portfolio_stats(series_for(db, [SCOPE]))

    assert stats["days"] == 2
    assert stats["win_rate"] == pytest.approx(0.5)
    # The 5000 deposit must not inflate total return.
    assert stats["total_return"] == pytest.approx((1 + 40 / 10000) * (1 - 20 / 10040) - 1)
    assert stats["max_drawdown"] == pytest.approx(20 / 10040, rel=1e-6)


def test_snapshot_gap_is_flagged(db: Database) -> None:
    load(
        db,
        equity=[
            (MAIN, "2023-11-15", ACCOUNT, 10000.0, 0.0, 0.0, 0.0, 1),
            (MAIN, "2023-11-20", ACCOUNT, 10500.0, 0.0, 0.0, 0.0, 2),
        ],
    )
    later = next(r for r in series_for(db, [SCOPE]) if r["day"] == "2023-11-20")
    # Five days elapsed without a refresh, so this "daily" PnL covers all five.
    assert later["spans_days"] == 5
    assert later["pnl_equity"] == pytest.approx(500.0)


def test_unclassified_ledger_type_is_surfaced(db: Database) -> None:
    load(
        db,
        ledger=[
            (MAIN, ACCOUNT, "x1", 1700000000, "2023-11-15", "some_new_type",
             42.0, 0.0, "USDT", "", ""),
        ],
    )
    row = series_for(db, [SCOPE])[0]
    # An unknown type must not be silently folded into PnL.
    assert row["unclassified"] == pytest.approx(42.0)
    assert row["unclassified_types"] == ["some_new_type"]
    assert row["net_realized"] == pytest.approx(0.0)


# ------------------------------------------------------------- multi-account


def test_aggregate_sums_across_profiles(db: Database) -> None:
    load(
        db,
        ledger=[
            (MAIN, ACCOUNT, "a1", 1, "2024-02-01", "pnl", 100.0, 0.0, "USDT", "", ""),
            (GRID, ACCOUNT, "b1", 1, "2024-02-01", "pnl", 40.0, 0.0, "USDT", "", ""),
            (MAIN, ACCOUNT, "a2", 2, "2024-02-01", "fee", -10.0, 0.0, "USDT", "", ""),
            (GRID, ACCOUNT, "b2", 2, "2024-02-01", "fee", -4.0, 0.0, "USDT", "", ""),
        ],
        equity=[
            (MAIN, "2024-02-01", ACCOUNT, 6000.0, 0.0, 0.0, 0.0, 1),
            (GRID, "2024-02-01", ACCOUNT, 2000.0, 0.0, 0.0, 0.0, 1),
            (MAIN, "2024-02-02", ACCOUNT, 6100.0, 0.0, 0.0, 0.0, 2),
            (GRID, "2024-02-02", ACCOUNT, 2050.0, 0.0, 0.0, 0.0, 2),
        ],
    )
    scopes = [(MAIN, ACCOUNT), (GRID, ACCOUNT)]
    by_day = {r["day"]: r for r in series_for(db, scopes)}

    first = by_day["2024-02-01"]
    assert first["equity_close"] == pytest.approx(8000.0)
    assert first["realized"] == pytest.approx(140.0)
    assert first["fee"] == pytest.approx(-14.0)
    assert first["scopes_reported"] == 2
    assert first["complete"] is True

    second = by_day["2024-02-02"]
    assert second["equity_close"] == pytest.approx(8150.0)
    assert second["pnl_equity"] == pytest.approx(150.0)


def test_partial_coverage_day_gets_no_pnl(db: Database) -> None:
    """A day where one account didn't sync must not look like a crash."""
    load(
        db,
        equity=[
            (MAIN, "2024-02-01", ACCOUNT, 6000.0, 0.0, 0.0, 0.0, 1),
            (GRID, "2024-02-01", ACCOUNT, 2000.0, 0.0, 0.0, 0.0, 1),
            # 02-02: only MAIN reported.
            (MAIN, "2024-02-02", ACCOUNT, 6100.0, 0.0, 0.0, 0.0, 2),
            (MAIN, "2024-02-03", ACCOUNT, 6200.0, 0.0, 0.0, 0.0, 3),
            (GRID, "2024-02-03", ACCOUNT, 2100.0, 0.0, 0.0, 0.0, 3),
        ],
    )
    scopes = [(MAIN, ACCOUNT), (GRID, ACCOUNT)]
    series = series_for(db, scopes)
    by_day = {r["day"]: r for r in series}

    partial = by_day["2024-02-02"]
    assert partial["scopes_reported"] == 1
    assert partial["complete"] is False
    # Summing 6100 alone would show a 1900 loss that never happened.
    assert partial["pnl_equity"] is None
    assert partial["return_pct"] is None

    # The next complete day compares against the last complete one, and says so.
    resumed = by_day["2024-02-03"]
    assert resumed["equity_close"] == pytest.approx(8300.0)
    assert resumed["pnl_equity"] == pytest.approx(300.0)
    assert resumed["spans_days"] == 2

    assert analytics.portfolio_stats(series)["partial_days"] == 1


def test_internal_transfer_nets_out_across_accounts(db: Database) -> None:
    """Moving money between your own accounts is not a portfolio cashflow."""
    load(
        db,
        equity=[
            (MAIN, "2024-03-01", ACCOUNT, 5000.0, 0.0, 0.0, 0.0, 1),
            (GRID, "2024-03-01", ACCOUNT, 5000.0, 0.0, 0.0, 0.0, 1),
            (MAIN, "2024-03-02", ACCOUNT, 3000.0, 0.0, 0.0, 0.0, 2),
            (GRID, "2024-03-02", ACCOUNT, 7000.0, 0.0, 0.0, 0.0, 2),
        ],
        cashflow=[
            (MAIN, ACCOUNT, "dnw", "out", 1, "2024-03-02", "USDT", -2000.0, "done"),
            (GRID, ACCOUNT, "dnw", "in", 1, "2024-03-02", "USDT", 2000.0, "done"),
        ],
    )
    scopes = [(MAIN, ACCOUNT), (GRID, ACCOUNT)]
    row = next(r for r in series_for(db, scopes) if r["day"] == "2024-03-02")

    # Both legs present, so the transfer cancels and the portfolio is flat.
    assert row["cashflow_net"] == pytest.approx(0.0)
    assert row["equity_close"] == pytest.approx(10000.0)
    assert row["pnl_equity"] == pytest.approx(0.0)

    # Viewed alone, each account sees a real cashflow and still nets to zero PnL.
    main_row = next(r for r in series_for(db, [(MAIN, ACCOUNT)]) if r["day"] == "2024-03-02")
    assert main_row["cashflow_net"] == pytest.approx(-2000.0)
    assert main_row["pnl_equity"] == pytest.approx(0.0)


def test_late_joining_scope_keeps_earlier_history(db: Database) -> None:
    """An account added mid-history must not retroactively void past days.

    GRID joins on day 3. Days 1-2 were a complete portfolio of MAIN alone, and
    pretending GRID was expected then would blank the whole history the moment
    a second key is added — the exact failure mode runtime account management
    makes reachable.
    """
    load(
        db,
        equity=[
            (MAIN, "2024-04-01", ACCOUNT, 1000.0, 0.0, 0.0, 0.0, 1),
            (MAIN, "2024-04-02", ACCOUNT, 1100.0, 0.0, 0.0, 0.0, 2),
            (MAIN, "2024-04-03", ACCOUNT, 1200.0, 0.0, 0.0, 0.0, 3),
            (GRID, "2024-04-03", ACCOUNT, 500.0, 0.0, 0.0, 0.0, 3),
        ],
    )
    series = series_for(db, [SCOPE, (GRID, ACCOUNT)])
    by_day = {r["day"]: r for r in series}

    # Before GRID existed, MAIN alone was the portfolio.
    assert by_day["2024-04-02"]["complete"] is True
    assert by_day["2024-04-02"]["pnl_equity"] == pytest.approx(100.0)

    # Once it exists, both are expected.
    assert by_day["2024-04-03"]["complete"] is True
    assert by_day["2024-04-03"]["pnl_equity"] == pytest.approx(600.0)

    stats = analytics.portfolio_stats(series)
    assert stats["days"] == 2
    assert stats["partial_days"] == 0


def test_removed_scope_stops_being_expected(db: Database) -> None:
    """After an account's last snapshot it no longer gates completeness."""
    load(
        db,
        equity=[
            (MAIN, "2024-04-01", ACCOUNT, 1000.0, 0.0, 0.0, 0.0, 1),
            (GRID, "2024-04-01", ACCOUNT, 500.0, 0.0, 0.0, 0.0, 1),
            (MAIN, "2024-04-02", ACCOUNT, 1100.0, 0.0, 0.0, 0.0, 2),
            (GRID, "2024-04-02", ACCOUNT, 520.0, 0.0, 0.0, 0.0, 2),
            # GRID was removed after 04-02; only MAIN reports from here.
            (MAIN, "2024-04-03", ACCOUNT, 1150.0, 0.0, 0.0, 0.0, 3),
        ],
    )
    row = next(r for r in series_for(db, [SCOPE, (GRID, ACCOUNT)]) if r["day"] == "2024-04-03")
    assert row["complete"] is True
    assert row["scopes_expected"] == 1
    # And the PnL is against the combined high-water mark, not MAIN alone.
    assert row["pnl_equity"] == pytest.approx(-470.0)


def test_days_before_first_scope_are_not_gaps(db: Database) -> None:
    """Ledger-only days from before any account existed are not 'excluded'."""
    load(
        db,
        ledger=[
            (MAIN, ACCOUNT, "l1", 1, "2024-03-01", "pnl", 5.0, 0.0, "USDT", "", ""),
        ],
        equity=[
            # GRID only starts on the 5th; nothing existed on the 1st.
            (GRID, "2024-03-05", ACCOUNT, 1000.0, 0.0, 0.0, 0.0, 5),
            (GRID, "2024-03-06", ACCOUNT, 1100.0, 0.0, 0.0, 0.0, 6),
        ],
    )
    series = series_for(db, [(GRID, ACCOUNT)])
    stats = analytics.portfolio_stats(series)
    # The 03-01 ledger row creates a series entry with no expected scopes; it
    # must not be reported as an incomplete day.
    assert stats["partial_days"] == 0
    assert stats["days"] == 1


def test_never_synced_scope_blocks_pnl_until_first_sync(db: Database) -> None:
    """A configured scope with no snapshots is expected everywhere.

    Otherwise the portfolio would quietly report totals that never included an
    account the user believes is connected.
    """
    load(
        db,
        equity=[
            (MAIN, "2024-04-01", ACCOUNT, 1000.0, 0.0, 0.0, 0.0, 1),
            (MAIN, "2024-04-02", ACCOUNT, 1100.0, 0.0, 0.0, 0.0, 2),
        ],
    )
    series = series_for(db, [SCOPE, (GRID, ACCOUNT)])
    assert all(r["complete"] is False for r in series)
    assert all(r["pnl_equity"] is None for r in series)
    assert analytics.portfolio_stats(series)["days"] == 0


def test_(db: Database) -> None:
    """Drawdown is recomputed on the combined curve, never averaged or maxed.

    Account A dips 10% and recovers while B climbs steadily. A has a real
    drawdown; the portfolio never does. Taking the worst component would report
    a loss the portfolio never experienced.
    """
    load(
        db,
        equity=[
            (MAIN, "2024-04-01", ACCOUNT, 1000.0, 0.0, 0.0, 0.0, 1),
            (MAIN, "2024-04-02", ACCOUNT, 900.0, 0.0, 0.0, 0.0, 2),
            (MAIN, "2024-04-03", ACCOUNT, 1000.0, 0.0, 0.0, 0.0, 3),
            (GRID, "2024-04-01", ACCOUNT, 1000.0, 0.0, 0.0, 0.0, 1),
            (GRID, "2024-04-02", ACCOUNT, 1100.0, 0.0, 0.0, 0.0, 2),
            (GRID, "2024-04-03", ACCOUNT, 1200.0, 0.0, 0.0, 0.0, 3),
        ],
    )
    solo = analytics.portfolio_stats(series_for(db, [(MAIN, ACCOUNT)]))
    combined = analytics.portfolio_stats(
        series_for(db, [(MAIN, ACCOUNT), (GRID, ACCOUNT)])
    )

    assert solo["max_drawdown"] == pytest.approx(0.10)
    # Portfolio equity goes 2000 -> 2000 -> 2200: it never falls.
    assert combined["max_drawdown"] == pytest.approx(0.0)
    assert combined["max_drawdown"] < solo["max_drawdown"]


def test_contribution_splits_pnl_by_account(db: Database) -> None:
    load(
        db,
        equity=[
            (MAIN, "2024-05-01", ACCOUNT, 1000.0, 0.0, 0.0, 0.0, 1),
            (MAIN, "2024-05-02", ACCOUNT, 1300.0, 0.0, 0.0, 0.0, 2),
            (GRID, "2024-05-01", ACCOUNT, 1000.0, 0.0, 0.0, 0.0, 1),
            (GRID, "2024-05-02", ACCOUNT, 900.0, 0.0, 0.0, 0.0, 2),
        ],
    )
    rows = asyncio.run(
        analytics.contribution(db, [(MAIN, ACCOUNT), (GRID, ACCOUNT)], days=90)
    )
    by_profile = {r["profile"]: r for r in rows}

    assert by_profile[MAIN]["period_pnl"] == pytest.approx(300.0)
    assert by_profile[GRID]["period_pnl"] == pytest.approx(-100.0)
    # Share of absolute movement, so the losing account still reads as 25%.
    assert by_profile[MAIN]["pnl_share"] == pytest.approx(0.75)
    assert by_profile[GRID]["pnl_share"] == pytest.approx(0.25)


def test_same_trade_id_in_two_profiles_does_not_collide(db: Database) -> None:
    """Gate ids are unique per account, so the profile must be in the key."""
    load(
        db,
        trades=[
            (MAIN, ACCOUNT, "999", 1, "2024-06-01", "BNB_USDT", "buy", "maker",
             600.0, 1.0, 600.0, -1.0, "USDT", "o1"),
            (GRID, ACCOUNT, "999", 1, "2024-06-01", "BNB_USDT", "sell", "taker",
             601.0, 2.0, 1202.0, -2.0, "USDT", "o2"),
        ],
    )
    rows = asyncio.run(db.query("SELECT profile, trade_id FROM trades ORDER BY profile"))
    assert [r["profile"] for r in rows] == [GRID, MAIN]

    combined = series_for(db, [(MAIN, ACCOUNT), (GRID, ACCOUNT)])[0]
    assert combined["trade_count"] == 2
    assert combined["volume"] == pytest.approx(1802.0)


def test_wallet_pseudo_account_is_never_a_scope(db: Database) -> None:
    """On-chain deposits also land in the spot ledger; counting both doubles them."""
    load(
        db,
        cashflow=[
            ("main", "wallet", "deposit", "tx1", 1, "2024-07-01", "USDT", 1000.0, "done"),
            ("main", "spot", "deposit", "sp1", 1, "2024-07-01", "USDT", 1000.0, "done"),
        ],
        equity=[
            ("main", "2024-07-01", "spot", 1000.0, 0.0, 0.0, 0.0, 1),
            ("main", "2024-07-02", "spot", 1000.0, 0.0, 0.0, 0.0, 2),
        ],
    )
    row = series_for(db, [("main", "spot")])[0]
    # Only the spot leg is in scope, so the deposit is counted once.
    assert row["cashflow_net"] == pytest.approx(1000.0)
    assert analytics.WALLET_PSEUDO_ACCOUNT == "wallet"


# ------------------------------------------------------------------ plumbing


def test_day_boundary_follows_configured_timezone(tmp_path: Path) -> None:
    # 2023-11-15T16:30:00Z is already the 16th in Shanghai but still the 15th in UTC.
    ts = 1700065800
    shanghai = Database(tmp_path / "sh.sqlite3", ZoneInfo("Asia/Shanghai"))
    utc = Database(tmp_path / "utc.sqlite3", ZoneInfo("UTC"))
    try:
        assert shanghai.day_of(ts) == "2023-11-16"
        assert utc.day_of(ts) == "2023-11-15"
        # Gate's spot account book mixes seconds and milliseconds in its time
        # field; a ms value read as seconds is the year 58638 and raises.
        assert shanghai.day_of(ts * 1000) == "2023-11-16"
    finally:
        shanghai.close()
        utc.close()


def test_v1_database_migrates_and_keeps_its_rows(tmp_path: Path) -> None:
    """An existing single-key history is preserved and attributed to 'default'."""
    path = tmp_path / "legacy.sqlite3"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE ledger (
            account TEXT NOT NULL, entry_id TEXT NOT NULL, ts INTEGER NOT NULL,
            day TEXT NOT NULL, type TEXT NOT NULL, change REAL NOT NULL,
            balance REAL, currency TEXT, contract TEXT, text TEXT,
            PRIMARY KEY (account, entry_id)
        );
        CREATE INDEX idx_ledger_day ON ledger (account, day, type);
        CREATE TABLE equity_snapshot (
            day TEXT NOT NULL, account TEXT NOT NULL, equity REAL NOT NULL,
            unrealised_pnl REAL NOT NULL DEFAULT 0, available REAL NOT NULL DEFAULT 0,
            position_margin REAL NOT NULL DEFAULT 0, captured_at INTEGER NOT NULL,
            PRIMARY KEY (day, account)
        );
        INSERT INTO ledger VALUES
            ('futures_usdt','e1',1,'2024-01-01','pnl',12.5,0,'USDT','BNB_USDT','');
        INSERT INTO equity_snapshot VALUES ('2024-01-01','futures_usdt',999.0,0,0,0,1);
        """
    )
    legacy.commit()
    legacy.close()

    database = Database(path, TZ)
    try:
        rows = asyncio.run(database.query("SELECT * FROM ledger"))
        assert len(rows) == 1
        assert rows[0]["profile"] == "default"
        assert rows[0]["change"] == pytest.approx(12.5)

        snaps = asyncio.run(database.query("SELECT * FROM equity_snapshot"))
        assert snaps[0]["profile"] == "default"

        # The index must survive the rebuild — dropping the renamed table takes
        # the old one with it, so it has to be recreated on the new table.
        indexes = asyncio.run(
            database.query(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='ledger' AND name NOT LIKE 'sqlite_%'"
            )
        )
        assert any(r["name"] == "idx_ledger_day" for r in indexes)
    finally:
        database.close()

    # Re-opening must be a no-op, not a second migration.
    again = Database(path, TZ)
    try:
        assert len(asyncio.run(again.query("SELECT * FROM ledger"))) == 1
    finally:
        again.close()


def test_signature_matches_apiv4_scheme() -> None:
    """Pin the signing scheme: a wrong sign string means every call 401s."""
    profile = Profile(
        id="main",
        label="主账户",
        api_key="testkey",
        api_secret="testsecret",
        accounts=("spot",),
    )
    client = GateClient(profile, "https://api.gateio.ws")
    headers = client._sign("GET", "/api/v4/futures/usdt/accounts", "contract=BNB_USDT", "")

    expected_string = "\n".join(
        ["GET", "/api/v4/futures/usdt/accounts", "contract=BNB_USDT",
         EMPTY_BODY_HASH, headers["Timestamp"]]
    )
    expected = hmac.new(
        b"testsecret", expected_string.encode(), hashlib.sha512
    ).hexdigest()

    assert headers["KEY"] == "testkey"
    assert headers["SIGN"] == expected
    assert EMPTY_BODY_HASH == hashlib.sha512(b"").hexdigest()
    asyncio.run(client.aclose())


def test_each_profile_signs_with_its_own_secret() -> None:
    """A registry must not leak one account's key into another's requests."""
    from backend.app.gate_client import ClientRegistry

    profiles = (
        Profile(id="a", label="A", api_key="ka", api_secret="sa", accounts=("spot",)),
        Profile(id="b", label="B", api_key="kb", api_secret="sb", accounts=("spot",)),
    )
    registry = ClientRegistry(profiles, "https://api.gateio.ws")
    try:
        a = registry.get("a")._sign("GET", "/api/v4/spot/accounts", "", "")
        b = registry.get("b")._sign("GET", "/api/v4/spot/accounts", "", "")
        assert a["KEY"] == "ka"
        assert b["KEY"] == "kb"
        assert a["SIGN"] != b["SIGN"]
        with pytest.raises(KeyError):
            registry.get("missing")
    finally:
        asyncio.run(registry.aclose())


# ---------------------------------------------------------------- fee summary


def ts_of(day: str, hour: int = 12, minute: int = 0) -> int:
    """Unix seconds for a wall-clock moment on `day` (dashboard tz)."""
    from datetime import datetime as dt

    y, m, d = (int(x) for x in day.split("-"))
    return int(dt(y, m, d, hour, minute, tzinfo=TZ).timestamp())


def test_fee_summary_groups_by_day(db: Database) -> None:
    """Daily granularity buckets by calendar day, midnight boundary."""
    load(
        db,
        ledger=[
            (MAIN, ACCOUNT, "f1", ts_of("2024-02-01"), "2024-02-01", "fee", -10.0, 0.0, "USDT", "", ""),
            (MAIN, ACCOUNT, "f2", ts_of("2024-02-02"), "2024-02-02", "fee", -15.0, 0.0, "USDT", "", ""),
        ],
        trades=[
            (MAIN, ACCOUNT, "t1", ts_of("2024-02-01"), "2024-02-01", "BNB_USDT", "buy", "maker",
             600.0, 1.0, 600.0, 0.0, "USDT", "o1"),
            (MAIN, ACCOUNT, "t2", ts_of("2024-02-02"), "2024-02-02", "BNB_USDT", "sell", "taker",
             601.0, 3.0, 1803.0, 0.0, "USDT", "o2"),
        ],
    )
    result = asyncio.run(analytics.fee_summary(db, [SCOPE], granularity="day"))
    assert [p["period"] for p in result["periods"]] == ["2024-02-01", "2024-02-02"]
    by_period = {p["period"]: p for p in result["periods"]}
    assert by_period["2024-02-01"]["fee"] == pytest.approx(-10.0)
    assert by_period["2024-02-02"]["fee"] == pytest.approx(-15.0)
    assert result["total"]["fee"] == pytest.approx(-25.0)
    assert result["total"]["volume"] == pytest.approx(2403.0)
    assert result["total"]["period_count"] == 2


def test_fee_summary_groups_by_iso_week(db: Database) -> None:
    """Weeks key on Monday and straddle month boundaries correctly.

    2024-02-01 is a Thursday, 2024-02-05 the next Monday. Fee days land in two
    ISO weeks: Thu-Sun in the week of Jan 29, Mon+ in the week of Feb 5.
    """
    days = ["2024-02-01", "2024-02-02", "2024-02-03", "2024-02-04", "2024-02-05", "2024-02-06"]
    load(
        db,
        ledger=[
            (MAIN, ACCOUNT, f"a{i}", ts_of(day), day, "fee", -10.0, 0.0, "USDT", "", "")
            for i, day in enumerate(days)
        ],
    )
    result = asyncio.run(analytics.fee_summary(db, [SCOPE], granularity="week"))
    by_period = {p["period"]: p for p in result["periods"]}

    assert set(by_period) == {"2024-01-29", "2024-02-05"}
    assert by_period["2024-01-29"]["member_days"] == 4  # Thu-Sun
    assert by_period["2024-01-29"]["fee"] == pytest.approx(-40.0)
    assert by_period["2024-02-05"]["fee"] == pytest.approx(-20.0)
    assert result["total"]["fee"] == pytest.approx(-60.0)
    assert result["total"]["period_count"] == 2


def test_fee_summary_boundary_eight_reassigns_morning_rows(db: Database) -> None:
    """An 08:00 trading day: rows before 8am belong to the previous period.

    Fees at 07:00 and 09:00 on 2024-03-01 (+08). Under the midnight boundary
    both land on 03-01; under the 8am boundary the 07:00 row belongs to the
    02-29 trading day.
    """
    load(
        db,
        ledger=[
            (MAIN, ACCOUNT, "early", ts_of("2024-03-01", 7), "2024-03-01", "fee", -10.0, 0.0, "USDT", "", ""),
            (MAIN, ACCOUNT, "late", ts_of("2024-03-01", 9), "2024-03-01", "fee", -20.0, 0.0, "USDT", "", ""),
        ],
    )

    midnight = asyncio.run(analytics.fee_summary(db, [SCOPE], granularity="day"))
    assert [p["period"] for p in midnight["periods"]] == ["2024-03-01"]
    assert midnight["total"]["fee"] == pytest.approx(-30.0)

    eight = asyncio.run(
        analytics.fee_summary(db, [SCOPE], granularity="day", boundary_hour=8)
    )
    by_period = {p["period"]: p for p in eight["periods"]}
    assert set(by_period) == {"2024-02-29", "2024-03-01"}
    assert by_period["2024-02-29"]["fee"] == pytest.approx(-10.0)
    assert by_period["2024-03-01"]["fee"] == pytest.approx(-20.0)


def test_fee_summary_boundary_eight_week(db: Database) -> None:
    """The 8am boundary also moves the week a row belongs to.

    Monday 2024-03-04 07:00 is inside the week starting 02-26 under the 8am
    boundary (07:00 minus 8h is still Sunday), but inside the week of 03-04
    under midnight.
    """
    load(
        db,
        ledger=[
            (MAIN, ACCOUNT, "m", ts_of("2024-03-04", 7), "2024-03-04", "fee", -5.0, 0.0, "USDT", "", ""),
        ],
    )
    midnight = asyncio.run(analytics.fee_summary(db, [SCOPE], granularity="week"))
    assert [p["period"] for p in midnight["periods"]] == ["2024-03-04"]

    eight = asyncio.run(
        analytics.fee_summary(db, [SCOPE], granularity="week", boundary_hour=8)
    )
    assert [p["period"] for p in eight["periods"]] == ["2024-02-26"]


def test_fee_summary_custom_window(db: Database) -> None:
    """from/to filter by period key and reject bad arguments."""
    load(
        db,
        ledger=[
            (MAIN, ACCOUNT, "a", ts_of("2024-02-01"), "2024-02-01", "fee", -10.0, 0.0, "USDT", "", ""),
            (MAIN, ACCOUNT, "b", ts_of("2024-02-10"), "2024-02-10", "fee", -20.0, 0.0, "USDT", "", ""),
            (MAIN, ACCOUNT, "c", ts_of("2024-02-20"), "2024-02-20", "fee", -40.0, 0.0, "USDT", "", ""),
        ],
    )
    result = asyncio.run(
        analytics.fee_summary(
            db, [SCOPE], granularity="day", from_day="2024-02-05", to_day="2024-02-15"
        )
    )
    assert [p["period"] for p in result["periods"]] == ["2024-02-10"]
    assert result["total"]["fee"] == pytest.approx(-20.0)

    with pytest.raises(ValueError):
        asyncio.run(
            analytics.fee_summary(db, [SCOPE], from_day="2024-02-15", to_day="2024-02-05")
        )
    with pytest.raises(ValueError):
        asyncio.run(analytics.fee_summary(db, [SCOPE], granularity="year"))
    with pytest.raises(ValueError):
        asyncio.run(analytics.fee_summary(db, [SCOPE], boundary_hour=6))


def test_fee_summary_per_account_sums_to_total(db: Database) -> None:
    """The breakout is computed per scope through the same aggregation."""
    load(
        db,
        ledger=[
            (MAIN, ACCOUNT, "m1", ts_of("2024-03-01"), "2024-03-01", "fee", -30.0, 0.0, "USDT", "", ""),
            (GRID, ACCOUNT, "g1", ts_of("2024-03-01"), "2024-03-01", "fee", -12.0, 0.0, "USDT", "", ""),
            (MAIN, ACCOUNT, "m2", ts_of("2024-03-01"), "2024-03-01", "fund", -5.0, 0.0, "USDT", "", ""),
            (GRID, ACCOUNT, "g2", ts_of("2024-03-01"), "2024-03-01", "refr", 3.0, 0.0, "USDT", "", ""),
        ],
        trades=[
            (MAIN, ACCOUNT, "t1", ts_of("2024-03-01"), "2024-03-01", "BNB_USDT", "buy", "maker",
             600.0, 1.0, 600.0, 0.0, "USDT", "o1"),
            (GRID, ACCOUNT, "t2", ts_of("2024-03-01"), "2024-03-01", "BNB_USDT", "buy", "taker",
             600.0, 1.0, 600.0, 0.0, "USDT", "o2"),
        ],
    )
    result = asyncio.run(
        analytics.fee_summary(db, [SCOPE, (GRID, ACCOUNT)], granularity="day")
    )
    # Cost = fees + funding paid; ledger rebate tracked but not subtracted.
    assert result["total"]["total_cost"] == pytest.approx(47.0)
    assert result["total"]["ledger_rebate"] == pytest.approx(3.0)

    parts = {r["profile"]: r for r in result["per_account"]}
    assert parts[MAIN]["total_cost"] == pytest.approx(35.0)
    assert parts[GRID]["total_cost"] == pytest.approx(12.0)
    assert parts[GRID]["ledger_rebate"] == pytest.approx(3.0)
    assert (
        parts[MAIN]["total_cost"] + parts[GRID]["total_cost"]
        == pytest.approx(result["total"]["total_cost"])
    )

    assert parts[MAIN]["volume"] == pytest.approx(600.0)
    assert parts[MAIN]["maker_ratio"] == pytest.approx(1.0)
    assert parts[GRID]["maker_ratio"] == pytest.approx(0.0)


def test_weekly_ratios_recomputed_not_averaged(db: Database) -> None:
    """A weekly ratio comes from summed numerators, not the mean of daily ones."""
    rows = []
    for i in range(100):
        day = "2024-02-05" if i < 1 else "2024-02-06"
        rows.append(
            (MAIN, ACCOUNT, f"t{i}", ts_of(day) + i, day, "BNB_USDT", "buy", "maker",
             600.0, 1.0, 600.0, 0.0, "USDT", f"o{i}")
        )
    rows.append(
        (MAIN, ACCOUNT, "tX", ts_of("2024-02-06") + 99, "2024-02-06", "BNB_USDT", "sell", "taker",
         600.0, 1.0, 600.0, 0.0, "USDT", "oX")
    )
    load(db, trades=rows)

    result = asyncio.run(analytics.fee_summary(db, [SCOPE], granularity="week"))
    assert result["total"]["maker_ratio"] == pytest.approx(100 / 101)
    assert result["total"]["trade_count"] == 101


def test_fee_summary_spot_falls_back_to_trade_fees(db: Database) -> None:
    """Spot has no fee ledger type of its own; per-trade fees stand in."""
    load(
        db,
        ledger=[
            ("main", "spot", "tr1", ts_of("2024-02-01"), "2024-02-01", "trade", 100.0, 0.0, "USDT", "BTC_USDT", ""),
        ],
        trades=[
            ("main", "spot", "t1", ts_of("2024-02-01"), "2024-02-01", "BTC_USDT", "buy", "maker",
             60000.0, 0.01, 600.0, -1.5, "USDT", "o1"),
        ],
    )
    result = asyncio.run(analytics.fee_summary(db, [("main", "spot")], granularity="day"))
    assert result["total"]["fee"] == pytest.approx(-1.5)
    assert result["total"]["volume"] == pytest.approx(600.0)

def test_fee_summary_monthly_buckets(db: Database) -> None:
    """Month buckets key on the first of the month."""
    load(
        db,
        ledger=[
            (MAIN, ACCOUNT, "a", ts_of("2024-02-28"), "2024-02-28", "fee", -10.0, 0.0, "USDT", "", ""),
            (MAIN, ACCOUNT, "b", ts_of("2024-03-01"), "2024-03-01", "fee", -20.0, 0.0, "USDT", "", ""),
            (MAIN, ACCOUNT, "c", ts_of("2024-03-31"), "2024-03-31", "fee", -40.0, 0.0, "USDT", "", ""),
        ],
    )
    result = asyncio.run(analytics.fee_summary(db, [SCOPE], granularity="month"))
    by_period = {p["period"]: p for p in result["periods"]}
    assert set(by_period) == {"2024-02-01", "2024-03-01"}
    assert by_period["2024-02-01"]["fee"] == pytest.approx(-10.0)
    assert by_period["2024-03-01"]["fee"] == pytest.approx(-60.0)


def test_fee_summary_links_spot_rebate_and_shares(db: Database) -> None:
    """Rebate shares and the expected-extra reconciliation against spot rebates.

    Futures fee -100 on 03-01; the same profile's spot book received +65 of
    pu_rebate that day. 70% of the fee is 70, so 5 is still expected. The 8am
    boundary moves a 07:00 rebate to the previous period.
    """
    load(
        db,
        ledger=[
            (MAIN, ACCOUNT, "f", ts_of("2024-03-01", 10), "2024-03-01", "fee", -100.0, 0.0, "USDT", "", ""),
            (MAIN, "spot", "r1", ts_of("2024-03-01", 12), "2024-03-01", "pu_rebate", 65.0, 0.0, "USDT", "", ""),
            (MAIN, "spot", "r2", ts_of("2024-03-01", 7), "2024-03-01", "pu_rebate", 10.0, 0.0, "USDT", "", ""),
        ],
    )

    midnight = asyncio.run(analytics.fee_summary(db, [SCOPE], granularity="day"))
    row = midnight["periods"][0]
    assert row["fee_70"] == pytest.approx(70.0)
    assert row["fee_10"] == pytest.approx(10.0)
    assert row["fee_05"] == pytest.approx(5.0)
    assert row["rebate"] == pytest.approx(75.0)
    # 70 应返 − 75 实返 = 已经多到账 5。
    assert row["expected_extra"] == pytest.approx(-5.0)
    assert midnight["total"]["rebate"] == pytest.approx(75.0)

    eight = asyncio.run(
        analytics.fee_summary(db, [SCOPE], granularity="day", boundary_hour=8)
    )
    by_period = {p["period"]: p for p in eight["periods"]}
    # 07:00 的返佣归 02-29，但那天没有合约费用、不产生行（与导出口径一致，
    # 周期行由费用/成交驱动）；03-01 只剩 65，正好差 5 补齐 70 应返。
    assert set(by_period) == {"2024-03-01"}
    assert by_period["2024-03-01"]["rebate"] == pytest.approx(65.0)
    assert by_period["2024-03-01"]["expected_extra"] == pytest.approx(5.0)
    # 总返佣不受分桶影响。
    assert eight["total"]["rebate"] == pytest.approx(75.0)

