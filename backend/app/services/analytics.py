"""Daily aggregation and portfolio statistics, computed from the local store.

Everything here works on a list of *scopes* — (profile, account) pairs. One scope
is a single-account view; several scopes is the portfolio view. Sharing the code
means the two can never drift apart in how they define a day's PnL.

Two different answers to "how much did I make today" live here, and both are
reported because they disagree in a way that matters:

  net_realized  — what the ledger booked: closed-position PnL, minus fees,
                  plus/minus funding and rebates. Always positive for a healthy
                  grid, even while the account is bleeding.
  pnl_equity    — equity moved this much, after backing out deposits and
                  withdrawals. Includes the mark-to-market swing on open
                  positions, so this is the honest number.

Their difference is the change in unrealised PnL. A grid that shows steady
net_realized and falling pnl_equity is converting open profit into locked-in
inventory, which is the failure mode worth catching early.

Aggregating across scopes has three traps, all handled below:

  * A day only has a real portfolio equity if *every scope that was active that
    day* reported a snapshot. Active means between that scope's first and last
    snapshot — an account added mid-history does not retroactively invalidate
    the days before it existed, and one removed stops being expected after its
    last observation. A scope that is configured but has never synced is
    expected on every day, so the numbers stay empty until the first sync
    rather than silently ignoring it. Summing a partial set invents a crash on
    the missing account, so incomplete days get no PnL and are counted in
    `partial_days`.
  * Drawdown and Sharpe are not additive. They are recomputed from the
    portfolio's own return series, never averaged across accounts — otherwise
    diversification disappears from the numbers.
  * An internal transfer between two of your own accounts nets to zero across
    scopes only when both legs are configured. If one account is missing from
    the dashboard, portfolio PnL is wrong by that amount, so a non-zero
    aggregate cashflow is surfaced rather than assumed benign.
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from ..config import settings
from ..db import Database
from . import ledger_types

TRADING_DAYS = 365  # crypto trades every day; no 252 adjustment

# (profile, account)
Scope = tuple[str, str]

# Deposits and withdrawals pulled from /wallet/* are stored against this
# pseudo-account. They are deliberately never a scope: the same money already
# appears in the spot or futures ledger it landed in, and counting both would
# double the cashflow and corrupt every PnL figure derived from it.
WALLET_PSEUDO_ACCOUNT = "wallet"


def _placeholders(count: int) -> str:
    return ", ".join(["(?, ?)"] * count)


def _flatten(scopes: Sequence[Scope]) -> list[str]:
    return [value for scope in scopes for value in scope]


async def daily_series(
    db: Database, scopes: Sequence[Scope], days: int = 90
) -> list[dict[str, Any]]:
    """Per-day rows for one or more scopes, summed where there are several."""
    if not scopes:
        return []

    scope_count = len(scopes)
    params = _flatten(scopes)
    where = f"(profile, account) IN ({_placeholders(scope_count)})"

    ledger = await db.query(
        f"""
        SELECT day, account, type, SUM(change) AS total, COUNT(*) AS entries
        FROM ledger WHERE {where}
        GROUP BY day, account, type
        """,
        params,
    )
    trades = await db.query(
        f"""
        SELECT day,
               COUNT(*) AS trade_count,
               SUM(quote_amount) AS volume,
               SUM(CASE WHEN role = 'maker' THEN 1 ELSE 0 END) AS maker_count,
               SUM(CASE WHEN role = 'taker' THEN 1 ELSE 0 END) AS taker_count,
               SUM(fee) AS trade_fee
        FROM trades WHERE {where}
        GROUP BY day
        """,
        params,
    )
    cashflow = await db.query(
        f"SELECT day, SUM(amount) AS net FROM cashflow WHERE {where} GROUP BY day",
        params,
    )
    snapshots = await db.query(
        f"""
        SELECT day, profile, account, equity, unrealised_pnl
        FROM equity_snapshot WHERE {where} ORDER BY day
        """,
        params,
    )

    by_day: dict[str, dict[str, Any]] = {}

    def slot(day: str) -> dict[str, Any]:
        return by_day.setdefault(
            day,
            {
                "day": day,
                **{bucket: 0.0 for bucket in ledger_types.BUCKETS},
                "gross_realized": 0.0,
                "ledger_entries": 0,
                "unclassified_types": [],
                "trade_count": 0,
                "volume": 0.0,
                "maker_count": 0,
                "taker_count": 0,
                "cashflow_net": 0.0,
                "equity_close": None,
                "unrealised_close": None,
                "scopes_reported": 0,
                "scopes_expected": 0,
            },
        )

    for row in ledger:
        entry = slot(row["day"])
        # Classified per row's own account: a spot 'trade' and a futures 'pnl'
        # mean different things and cannot share one table.
        bucket = ledger_types.classify(row["account"], row["type"])
        entry[bucket] += row["total"] or 0.0
        entry["ledger_entries"] += row["entries"]
        if bucket == ledger_types.UNCLASSIFIED:
            entry["unclassified_types"].append(row["type"])
        if bucket == ledger_types.REALIZED and (row["total"] or 0) > 0:
            entry["gross_realized"] += row["total"]

    for row in trades:
        entry = slot(row["day"])
        entry["trade_count"] = row["trade_count"] or 0
        entry["volume"] = row["volume"] or 0.0
        entry["maker_count"] = row["maker_count"] or 0
        entry["taker_count"] = row["taker_count"] or 0
        # Spot has no `fee` ledger type of its own, so fall back to trade fees.
        if entry[ledger_types.FEE] == 0 and row["trade_fee"]:
            entry[ledger_types.FEE] = row["trade_fee"]

    for row in cashflow:
        slot(row["day"])["cashflow_net"] = row["net"] or 0.0

    present: dict[str, set[str]] = {}
    first_seen: dict[str, str] = {}
    last_seen: dict[str, str] = {}

    for row in snapshots:
        entry = slot(row["day"])
        entry["equity_close"] = (entry["equity_close"] or 0.0) + row["equity"]
        entry["unrealised_close"] = (
            entry["unrealised_close"] or 0.0
        ) + row["unrealised_pnl"]
        entry["scopes_reported"] += 1

        scope_key = f"{row['profile']}/{row['account']}"
        day = row["day"]
        present.setdefault(day, set()).add(scope_key)
        first_seen[scope_key] = min(first_seen.get(scope_key, day), day)
        last_seen[scope_key] = max(last_seen.get(scope_key, day), day)

    # A scope is expected on a day when it was around: between its first and
    # last snapshot, or always if it has never synced at all. The window comes
    # from the data rather than the config because accounts are added and
    # removed at runtime, mid-history.
    configured = {f"{profile}/{account}" for profile, account in scopes}
    never_synced = configured - set(first_seen)

    def expected_on(day: str) -> set[str]:
        active = {
            key
            for key in configured
            if key in first_seen and first_seen[key] <= day <= last_seen[key]
        }
        return active | never_synced

    series = [by_day[day] for day in sorted(by_day)]
    for entry in series:
        expected = expected_on(entry["day"])
        entry["scopes_expected"] = len(expected)
        # A day is only comparable if every scope that was around reported.
        # Otherwise its total is missing an account and the difference would
        # read as a loss.
        entry["complete"] = (
            entry["equity_close"] is not None
            and present.get(entry["day"], set()) == expected
        )

    _derive(series)
    return series[-days:] if days else series


def _derive(series: list[dict[str, Any]]) -> None:
    """Fill in the computed columns, in place, walking forward through time."""
    prev_equity: float | None = None
    prev_day: str | None = None

    for entry in series:
        fee = entry[ledger_types.FEE]
        entry["net_realized"] = (
            entry[ledger_types.REALIZED]
            + entry[ledger_types.FUNDING]
            + entry[ledger_types.REBATE]
            + entry[ledger_types.INTEREST]
            + fee
        )
        gross = entry["gross_realized"]
        entry["fee_to_gross_ratio"] = abs(fee) / gross if gross > 0 else None
        traded = entry["maker_count"] + entry["taker_count"]
        entry["maker_ratio"] = entry["maker_count"] / traded if traded else None

        if entry["complete"] and prev_equity is not None:
            delta = entry["equity_close"] - prev_equity
            entry["equity_delta"] = delta
            entry["pnl_equity"] = delta - entry["cashflow_net"]
            # Divide the PnL, not the raw delta: a day whose deposit exactly
            # offsets a loss has delta 0 but a real negative return.
            entry["return_pct"] = (
                entry["pnl_equity"] / prev_equity if prev_equity else None
            )
            # A gap means no refresh happened in between, so this row's PnL
            # actually covers more than one day.
            entry["spans_days"] = _day_gap(prev_day, entry["day"])
        else:
            entry["equity_delta"] = None
            entry["pnl_equity"] = None
            entry["return_pct"] = None
            entry["spans_days"] = None

        # Residual between the two PnL definitions. Should equal the change in
        # unrealised PnL; anything left over is an unmodelled ledger type.
        entry["unrealised_change"] = (
            None
            if entry["pnl_equity"] is None
            else entry["pnl_equity"] - entry["net_realized"]
        )

        if entry["complete"]:
            prev_equity = entry["equity_close"]
            prev_day = entry["day"]


def _day_gap(prev_day: str | None, day: str) -> int | None:
    if not prev_day:
        return None
    return (date.fromisoformat(day) - date.fromisoformat(prev_day)).days


def portfolio_stats(series: list[dict[str, Any]]) -> dict[str, Any]:
    """Risk-adjusted summary over the daily equity-based returns.

    Returns are time-weighted (each day compounded on the prior equity) so
    deposits and withdrawals do not show up as performance. For a portfolio this
    runs on the summed series, never on per-account averages — drawdown is a
    property of the combined equity curve.
    """
    returns = [e["return_pct"] for e in series if e.get("return_pct") is not None]
    # Only days that *expected* scopes count as gaps. Days before the first
    # scope existed are not exclusions — there was nothing to exclude.
    partial = sum(
        1 for e in series if e.get("scopes_expected") and not e["complete"]
    )

    if not returns:
        return {
            "days": 0,
            "partial_days": partial,
            "total_return": None,
            "annualized_return": None,
            "max_drawdown": None,
            "sharpe": None,
            "calmar": None,
            "win_rate": None,
            "profit_factor": None,
            "best_day": None,
            "worst_day": None,
        }

    index = [1.0]
    for r in returns:
        index.append(index[-1] * (1 + r))

    peak = index[0]
    max_dd = 0.0
    for value in index:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)

    total_return = index[-1] - 1
    mean = sum(returns) / len(returns)
    variance = (
        sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        if len(returns) > 1
        else 0.0
    )
    stdev = math.sqrt(variance)
    sharpe = (mean / stdev) * math.sqrt(TRADING_DAYS) if stdev > 0 else None

    annualized = (
        (index[-1] ** (TRADING_DAYS / len(returns))) - 1 if index[-1] > 0 else None
    )
    calmar = annualized / max_dd if annualized is not None and max_dd > 0 else None

    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    loss_sum = abs(sum(losses))

    pnl_days = [e for e in series if e.get("pnl_equity") is not None]
    best = max(pnl_days, key=lambda e: e["pnl_equity"], default=None)
    worst = min(pnl_days, key=lambda e: e["pnl_equity"], default=None)

    return {
        "days": len(returns),
        "partial_days": partial,
        "total_return": total_return,
        "annualized_return": annualized,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "calmar": calmar,
        "win_rate": len(wins) / len(returns),
        "profit_factor": (sum(wins) / loss_sum) if loss_sum > 0 else None,
        "best_day": {"day": best["day"], "pnl": best["pnl_equity"]} if best else None,
        "worst_day": {"day": worst["day"], "pnl": worst["pnl_equity"]} if worst else None,
        "equity_index": index,
    }


def cost_summary(series: list[dict[str, Any]]) -> dict[str, Any]:
    """Where the money went. For a grid strategy this is the whole ballgame."""
    fee = sum(e[ledger_types.FEE] for e in series)
    funding = sum(e[ledger_types.FUNDING] for e in series)
    rebate = sum(e[ledger_types.REBATE] for e in series)
    interest = sum(e[ledger_types.INTEREST] for e in series)
    gross = sum(e["gross_realized"] for e in series)
    volume = sum(e["volume"] for e in series)
    maker = sum(e["maker_count"] for e in series)
    taker = sum(e["taker_count"] for e in series)

    total_cost = abs(fee) + abs(min(funding, 0.0)) + abs(interest)

    return {
        "fee": fee,
        "funding": funding,
        "rebate": rebate,
        "interest": interest,
        "gross_realized": gross,
        "total_cost": total_cost,
        # The single number that decides whether the strategy is worth running:
        # what share of gross trading profit the exchange keeps. Ratios are
        # recomputed from summed numerators, never averaged across accounts.
        "cost_to_gross_ratio": total_cost / gross if gross > 0 else None,
        "volume": volume,
        "effective_fee_rate": abs(fee) / volume if volume > 0 else None,
        "maker_count": maker,
        "taker_count": taker,
        "maker_ratio": maker / (maker + taker) if (maker + taker) else None,
        "trade_count": sum(e["trade_count"] for e in series),
    }


async def market_breakdown(
    db: Database, scopes: Sequence[Scope], days: int = 30
) -> list[dict[str, Any]]:
    """Per-symbol activity, so a single bad pair cannot hide inside the total."""
    if not scopes:
        return []
    cutoff = _cutoff_day(db, days)
    params = [*_flatten(scopes), cutoff]
    where = f"(profile, account) IN ({_placeholders(len(scopes))}) AND day >= ?"

    rows = await db.query(
        f"""
        SELECT market,
               COUNT(*) AS trade_count,
               SUM(quote_amount) AS volume,
               SUM(fee) AS fee,
               SUM(CASE WHEN role = 'maker' THEN 1 ELSE 0 END) AS maker_count
        FROM trades WHERE {where}
        GROUP BY market ORDER BY volume DESC
        """,
        params,
    )
    closes = await db.query(
        f"""
        SELECT contract AS market, SUM(pnl) AS realized, COUNT(*) AS closes
        FROM position_close WHERE {where}
        GROUP BY contract
        """,
        params,
    )
    realized_by_market = {r["market"]: r for r in closes}

    out = []
    for row in rows:
        close = realized_by_market.get(row["market"], {})
        out.append(
            {
                "market": row["market"],
                "trade_count": row["trade_count"],
                "volume": row["volume"] or 0.0,
                "fee": row["fee"] or 0.0,
                "maker_ratio": (row["maker_count"] / row["trade_count"])
                if row["trade_count"]
                else None,
                "realized_pnl": close.get("realized"),
                "position_closes": close.get("closes"),
            }
        )
    return out


async def contribution(
    db: Database, scopes: Iterable[Scope], days: int = 90
) -> list[dict[str, Any]]:
    """Per-scope comparison: who is actually making the money.

    Each scope is measured on its own series so its drawdown and maker ratio are
    its own, then given a share of the portfolio's total PnL. That share is the
    question the portfolio view exists to answer.
    """
    rows: list[dict[str, Any]] = []
    for scope in scopes:
        series = await daily_series(db, [scope], days=days)
        stats = portfolio_stats(series)
        costs = cost_summary(series)
        latest = next(
            (e for e in reversed(series) if e["equity_close"] is not None), None
        )
        pnl = sum(e["pnl_equity"] for e in series if e["pnl_equity"] is not None)

        rows.append(
            {
                "profile": scope[0],
                "account": scope[1],
                "equity": latest["equity_close"] if latest else None,
                "equity_day": latest["day"] if latest else None,
                "period_pnl": pnl,
                "net_realized": sum(e["net_realized"] for e in series),
                "fee": costs["fee"],
                "funding": costs["funding"],
                "volume": costs["volume"],
                "trade_count": costs["trade_count"],
                "maker_ratio": costs["maker_ratio"],
                "cost_to_gross_ratio": costs["cost_to_gross_ratio"],
                "total_return": stats["total_return"],
                "max_drawdown": stats["max_drawdown"],
                "sharpe": stats["sharpe"],
                "days": stats["days"],
            }
        )

    total_pnl = sum(abs(r["period_pnl"]) for r in rows)
    for row in rows:
        # Share of absolute movement, so a losing account reads as a real
        # contribution rather than a negative percentage of a small net.
        row["pnl_share"] = abs(row["period_pnl"]) / total_pnl if total_pnl > 0 else None

    rows.sort(key=lambda r: r["equity"] or 0, reverse=True)
    return rows


def _cutoff_day(db: Database, days: int) -> str:
    return (date.fromisoformat(db.today()) - timedelta(days=days)).isoformat()


def _week_start(day: str) -> str:
    """Monday of the ISO week containing `day`."""
    return (
        date.fromisoformat(day) - timedelta(days=date.fromisoformat(day).weekday())
    ).isoformat()


# ---------------------------------------------------------------- fee summary

async def fee_summary(
    db: Database,
    scopes: Sequence[Scope],
    granularity: str = "day",
    from_day: str | None = None,
    to_day: str | None = None,
    days: int | None = None,
    boundary_hour: int = 0,
) -> dict[str, Any]:
    """Fee and cost totals per period, summed across the scopes and per account.

    Buckets are computed straight from row timestamps, not from the stored
    `day` column: the day column is fixed to a midnight boundary at sync time,
    but fee accounting sometimes runs on a trading day that starts at 08:00
    (UTC+8), so the boundary has to be a query-time choice.

    `boundary_hour` is 0 or 8 — the hour (in the dashboard timezone) at which
    a period begins. A day with the 8:00 boundary covers 08:00 to 08:00; the
    period is labelled with its start date. Weeks are Monday-based on the same
    boundary.
    """
    if granularity not in ("day", "week", "month"):
        raise ValueError(f"granularity must be day/week/month, got {granularity!r}")
    if boundary_hour not in (0, 8):
        raise ValueError(f"boundary_hour must be 0 or 8, got {boundary_hour!r}")
    if from_day is not None:
        date.fromisoformat(from_day)
    if to_day is not None:
        date.fromisoformat(to_day)
    if from_day and to_day and from_day > to_day:
        raise ValueError(f"from_day ({from_day}) is after to_day ({to_day})")

    periods = await _fee_periods(
        db, scopes, granularity, from_day, to_day, days, boundary_hour
    )
    # 实际返佣来自同一 profile 的现货账本（返佣到账在现货）。
    rebates = _window_rebates(
        await _rebate_periods(
            db, scopes, granularity, from_day, to_day, days, boundary_hour
        ),
        from_day,
        to_day,
    )
    for period in periods:
        _apply_shares(period, rebates.get(period["period"], 0.0))
    total = _fee_rollup(periods)
    # 返佣合计取映射总和而不是周期行之和：返佣可以在没有费用的日子到账，
    # 那些天没有周期行，但钱是真的到了。
    total["rebate"] = sum(rebates.values())
    total["expected_extra"] = total["fee_70"] - total["rebate"]

    per_account = []
    for scope in scopes:
        scoped = await _fee_periods(
            db, [scope], granularity, from_day, to_day, days, boundary_hour
        )
        if scoped:
            scoped_rebates = _window_rebates(
                await _rebate_periods(
                    db, [scope], granularity, from_day, to_day, days, boundary_hour
                ),
                from_day,
                to_day,
            )
            for period in scoped:
                _apply_shares(period, scoped_rebates.get(period["period"], 0.0))
            rollup = _fee_rollup(scoped)
            rollup["rebate"] = sum(scoped_rebates.values())
            rollup["expected_extra"] = rollup["fee_70"] - rollup["rebate"]
            per_account.append(
                {"profile": scope[0], "account": scope[1], **rollup}
            )
    per_account.sort(key=lambda r: r["total_cost"], reverse=True)

    return {
        "granularity": granularity,
        "boundary_hour": boundary_hour,
        "from_day": from_day,
        "to_day": to_day,
        "periods": periods,
        "total": total,
        "per_account": per_account,
    }


# 返佣份额的固定比例：70% 是主返佣口径，10%/5% 是副口径；预计额外返佣 =
# 70% 应返 − 实际到账，即还没回来的部分。
REBATE_SHARE = 0.70
SIDE_SHARES = (0.10, 0.05)


def _window_rebates(
    rebates: dict[str, float], from_day: str | None, to_day: str | None
) -> dict[str, float]:
    """Restrict the rebate map to the requested from/to period-key window."""
    return {
        key: value
        for key, value in rebates.items()
        if (not from_day or key >= from_day) and (not to_day or key <= to_day)
    }


def _apply_shares(period: dict[str, Any], rebate: float) -> None:
    """给一个周期行补上返佣份额与核对列。

    rebate 是同一 profile 现货账本的实际返佣到账；预计额外返佣 = 70% 应返
    减去它。账本返佣桶（ledger_rebate）不受影响。
    """
    fee_mag = abs(period["fee"])
    period["fee_70"] = fee_mag * REBATE_SHARE
    period["fee_10"] = fee_mag * SIDE_SHARES[0]
    period["fee_05"] = fee_mag * SIDE_SHARES[1]
    period["rebate"] = rebate
    period["expected_extra"] = period["fee_70"] - rebate


async def _rebate_periods(
    db: Database,
    scopes: Sequence[Scope],
    granularity: str,
    from_day: str | None,
    to_day: str | None,
    days: int | None,
    boundary_hour: int,
) -> dict[str, float]:
    """每个周期的实际返佣（来自同 profile 现货账本的返佣条目）。

    返佣到账在现货账户，所以一个合约 scope 的返佣要看同一把 key 的现货
    账本；分桶与费用聚合同一条日界线和周期粒度。
    """
    offset = boundary_hour * 3600
    profiles = sorted({profile for profile, _ in scopes})
    spot_scopes = [(profile, "spot") for profile in profiles]
    if not spot_scopes:
        return {}

    where = f"(profile, account) IN ({_placeholders(len(spot_scopes))})"
    params: list[Any] = _flatten(spot_scopes)
    if days:
        params = [*params, int(time.time()) - days * 86400]
        where += " AND ts >= ?"

    rows = await db.query(
        f"SELECT ts, type, change FROM ledger WHERE {where}", params
    )

    def bucket_of(ts: int) -> str:
        local = (
            datetime.fromtimestamp(ts - offset, tz=timezone.utc).astimezone(
                settings.tz
            )
        )
        day = local.strftime("%Y-%m-%d")
        if granularity == "day":
            return day
        if granularity == "week":
            return _week_start(day)
        return f"{day[:7]}-01"

    out: dict[str, float] = {}
    for row in rows:
        if ledger_types.classify("spot", row["type"]) != ledger_types.REBATE:
            continue
        key = bucket_of(int(row["ts"]))
        out[key] = out.get(key, 0.0) + (row["change"] or 0.0)
    return out


async def _fee_periods(
    db: Database,
    scopes: Sequence[Scope],
    granularity: str,
    from_day: str | None,
    to_day: str | None,
    days: int | None,
    boundary_hour: int,
) -> list[dict[str, Any]]:
    """Raw ledger + trade rows bucketed by period, boundary-shifted."""
    offset = boundary_hour * 3600
    where = f"(profile, account) IN ({_placeholders(len(scopes))})"
    params: list[Any] = _flatten(scopes)

    ts_clause = ""
    if days:
        start_ts = int(time.time()) - days * 86400
        ts_clause = " AND ts >= ?"
        # The same param serves both queries below.
        params_with_ts = [*params, start_ts]
    else:
        params_with_ts = params

    ledger_rows = await db.query(
        f"SELECT account, ts, type, change FROM ledger WHERE {where}{ts_clause}",
        params_with_ts,
    )
    trade_rows = await db.query(
        f"SELECT ts, role, quote_amount, fee FROM trades WHERE {where}{ts_clause}",
        params_with_ts,
    )

    def bucket_of(ts: int) -> tuple[str, str]:
        """(period key, calendar day) for a timestamp under the boundary."""
        local = (
            datetime.fromtimestamp(ts - offset, tz=timezone.utc).astimezone(
                settings.tz
            )
        )
        day = local.strftime("%Y-%m-%d")
        if granularity == "day":
            key = day
        elif granularity == "week":
            key = _week_start(day)
        else:
            key = f"{day[:7]}-01"
        return key, day

    by_period: dict[str, dict[str, Any]] = {}

    def slot(key: str) -> dict[str, Any]:
        return by_period.setdefault(
            key,
            {
                "period": key,
                "granularity": granularity,
                "member_days": set(),
                "days_with_pnl": 0,
                "incomplete_days": 0,
                "fee": 0.0,
                "funding": 0.0,
                "interest": 0.0,
                "rebate": 0.0,
                "realized": 0.0,
                "volume": 0.0,
                "trade_count": 0,
                "maker_count": 0,
                "taker_count": 0,
                "trade_fee": 0.0,
            },
        )

    cost_buckets = (
        ledger_types.FEE,
        ledger_types.FUNDING,
        ledger_types.INTEREST,
        ledger_types.REBATE,
        ledger_types.REALIZED,
    )
    for row in ledger_rows:
        key, day = bucket_of(int(row["ts"]))
        entry = slot(key)
        entry["member_days"].add(day)
        bucket = ledger_types.classify(row["account"], row["type"])
        if bucket in cost_buckets:
            entry[bucket] += row["change"] or 0.0

    for row in trade_rows:
        key, day = bucket_of(int(row["ts"]))
        entry = slot(key)
        entry["member_days"].add(day)
        entry["volume"] += row["quote_amount"] or 0.0
        entry["trade_count"] += 1
        if row["role"] == "maker":
            entry["maker_count"] += 1
        elif row["role"] == "taker":
            entry["taker_count"] += 1
        entry["trade_fee"] += row["fee"] or 0.0

    out = []
    for key in sorted(by_period):
        if from_day and key < from_day:
            continue
        if to_day and key > to_day:
            continue
        entry = by_period[key]
        # Spot has no fee ledger type of its own; fall back to per-trade fees.
        if entry["fee"] == 0 and entry["trade_fee"]:
            entry["fee"] = entry["trade_fee"]
        traded = entry["maker_count"] + entry["taker_count"]
        out.append(
            {
                "period": key,
                "granularity": granularity,
                "member_days": len(entry["member_days"]),
                "days_with_pnl": 0,
                "incomplete_days": 0,
                "fee": entry["fee"],
                "funding": entry["funding"],
                "interest": entry["interest"],
                # 账本返佣桶（合约的 refr 等）与现货实际到账返佣是两个概念：
                # 前者是费用侧的减免，后者是核对 70% 口径的到账数。
                "ledger_rebate": entry["rebate"],
                "realized": entry["realized"],
                "net_realized": (
                    entry["realized"]
                    + entry["fee"]
                    + entry["funding"]
                    + entry["interest"]
                    + entry["rebate"]
                ),
                "pnl_equity": 0.0,
                "cashflow_net": 0.0,
                "equity_close": None,
                "volume": entry["volume"],
                "trade_count": entry["trade_count"],
                "maker_count": entry["maker_count"],
                "taker_count": entry["taker_count"],
                # Ratios recomputed from summed numerators, never averaged.
                "maker_ratio": (
                    entry["maker_count"] / traded if traded else None
                ),
                "effective_fee_rate": (
                    abs(entry["fee"]) / entry["volume"] if entry["volume"] else None
                ),
                "pnl_is_partial": False,
            }
        )
    return out


FEE_FIELD = ledger_types.FEE
FUNDING_FIELD = ledger_types.FUNDING
INTEREST_FIELD = ledger_types.INTEREST
REBATE_FIELD = ledger_types.REBATE
VOLUME_FIELD = "volume"
TRADE_FIELD = "trade_count"
MAKER_FIELD = "maker_count"
TAKER_FIELD = "taker_count"


def _fee_rollup(periods: list[dict[str, Any]]) -> dict[str, Any]:
    fee = sum(p[FEE_FIELD] for p in periods)
    funding = sum(p[FUNDING_FIELD] for p in periods)
    interest = sum(p[INTEREST_FIELD] for p in periods)
    rebate = sum(p[REBATE_FIELD] for p in periods)
    volume = sum(p[VOLUME_FIELD] for p in periods)
    trades = sum(p[TRADE_FIELD] for p in periods)
    maker = sum(p[MAKER_FIELD] for p in periods)
    taker = sum(p[TAKER_FIELD] for p in periods)
    total_cost = abs(fee) + abs(min(funding, 0.0)) + abs(interest)
    gross = sum(p[ledger_types.REALIZED] for p in periods)
    fee_mag = abs(fee)
    ledger_rebate = sum(p.get("ledger_rebate", 0.0) for p in periods)
    rebate = sum(p.get("rebate", 0.0) for p in periods)

    return {
        "fee": fee,
        "funding": funding,
        "interest": interest,
        "rebate": rebate,
        "total_cost": total_cost,
        "volume": volume,
        "trade_count": trades,
        "maker_count": maker,
        "taker_count": taker,
        "maker_ratio": maker / (maker + taker) if (maker + taker) else None,
        "effective_fee_rate": abs(fee) / volume if volume > 0 else None,
        "cost_to_gross_ratio": total_cost / gross if gross > 0 else None,
        "period_count": len(periods),
        "fee_70": fee_mag * REBATE_SHARE,
        "fee_10": fee_mag * SIDE_SHARES[0],
        "fee_05": fee_mag * SIDE_SHARES[1],
        "ledger_rebate": ledger_rebate,
        "rebate": rebate,
        "expected_extra": fee_mag * REBATE_SHARE - rebate,
    }

