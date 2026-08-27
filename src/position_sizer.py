def classify_position(position_pct: float) -> str:
    if position_pct >= 0.50:
        return "重仓"
    if position_pct >= 0.40:
        return "高风险仓位"
    if position_pct >= 0.30:
        return "偏高仓位"
    if position_pct >= 0.20:
        return "正常仓位"
    return "轻仓"

def suggest_next_trade_capacity(account_total_asset: float, current_market_value: float, max_position_pct: float, price: float, lot_size: int = 100) -> dict:
    max_value = account_total_asset * max_position_pct
    remaining_value = max(0, max_value - current_market_value)
    raw_shares = int(remaining_value // price)
    shares_by_lot = (raw_shares // lot_size) * lot_size

    return {
        "account_total_asset": account_total_asset,
        "current_market_value": current_market_value,
        "max_position_pct": max_position_pct,
        "max_position_value": max_value,
        "remaining_buy_capacity_value": remaining_value,
        "suggested_max_add_shares_by_lot": shares_by_lot,
        "note": "这是仓位上限测算，不是买入建议。是否操作还要通过 technical_signal_review 和 trading_review。"
    }
