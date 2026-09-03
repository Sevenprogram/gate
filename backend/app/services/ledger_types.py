"""Classification of ledger entry types into economic buckets.

Every number on the daily page traces back to this mapping, so it is kept
explicit rather than inferred. Gate adds enum values over time; anything not
listed here lands in `UNCLASSIFIED` and is reported to the UI as such instead
of being silently dropped, because a quietly ignored ledger type shows up as an
unexplained gap between realized PnL and equity change.
"""

from __future__ import annotations

# economic buckets
REALIZED = "realized"      # PnL booked when a position is reduced
FEE = "fee"                # trading fee paid
FUNDING = "funding"        # perpetual funding, signed (negative = you paid)
REBATE = "rebate"          # referral / maker rebate received
INTEREST = "interest"      # borrowing cost in margin or unified mode
CASHFLOW = "cashflow"      # transfers in and out — not profit
BONUS = "bonus"            # promotional credit
UNCLASSIFIED = "unclassified"

BUCKETS = (REALIZED, FEE, FUNDING, REBATE, INTEREST, CASHFLOW, BONUS, UNCLASSIFIED)

# /futures/{settle}/account_book -> bucket
FUTURES_LEDGER_TYPES: dict[str, str] = {
    "pnl": REALIZED,
    "fee": FEE,
    "fund": FUNDING,
    "refr": REBATE,
    "dnw": CASHFLOW,
    "point_dnw": CASHFLOW,
    "point_fee": FEE,
    "point_refr": REBATE,
    "bonus_dnw": BONUS,
    "bonus_offset": BONUS,
    "cross_settle": REALIZED,
}

# /spot/account_book -> bucket
SPOT_LEDGER_TYPES: dict[str, str] = {
    "trade": REALIZED,
    "fee": FEE,
    "rebate": REBATE,
    "deposit": CASHFLOW,
    "withdraw": CASHFLOW,
    "sub_account_transfer": CASHFLOW,
    "margin_in": CASHFLOW,
    "margin_out": CASHFLOW,
    "futures_in": CASHFLOW,
    "futures_out": CASHFLOW,
    "delivery_in": CASHFLOW,
    "delivery_out": CASHFLOW,
    "options_in": CASHFLOW,
    "options_out": CASHFLOW,
    "unified_in": CASHFLOW,
    "unified_out": CASHFLOW,
    "lend": CASHFLOW,
    "interest": INTEREST,
    "new_order": CASHFLOW,
    "order_fill": REALIZED,
    "referral_fee": REBATE,
    "order_fee": FEE,
    # Types observed in real account books but absent from the docs:
    "pu_rebate": REBATE,        # 返佣实际到账的类型码
    "perp_in": CASHFLOW,        # 转出至合约
    "perp_out": CASHFLOW,       # 从合约转入
    "subaccount_trf": CASHFLOW, # 子账户划转
}

# /unified/account_book -> bucket
UNIFIED_LEDGER_TYPES: dict[str, str] = {
    **FUTURES_LEDGER_TYPES,
    "interest": INTEREST,
    "borrow": CASHFLOW,
    "repay": CASHFLOW,
}


def classify(account: str, raw_type: str) -> str:
    table = SPOT_LEDGER_TYPES if account == "spot" else (
        UNIFIED_LEDGER_TYPES if account == "unified" else FUTURES_LEDGER_TYPES
    )
    return table.get(raw_type, UNCLASSIFIED)
