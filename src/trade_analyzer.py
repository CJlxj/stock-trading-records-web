from pathlib import Path
import pandas as pd
from src.position_sizer import classify_position, suggest_next_trade_capacity

def load_trades(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if df.empty:
        return df
    numeric_cols = [
        "price", "shares", "gross_amount", "fee", "stamp_tax", "transfer_fee", "other_fee",
        "net_amount", "avg_cost_after_trade", "realized_pnl", "remaining_shares",
        "account_total_asset", "position_market_value", "position_pct"
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df

def summarize_symbol(trades: pd.DataFrame, symbol: str, latest_price: float | None = None, max_position_pct: float = 0.30) -> dict:
    df = trades[trades["symbol"] == symbol].copy()
    if df.empty:
        return {"symbol": symbol, "message": "没有找到该股票交易记录"}

    buy_df = df[df["side"].str.upper() == "BUY"]
    sell_df = df[df["side"].str.upper() == "SELL"]

    total_buy_amount = float(buy_df["gross_amount"].sum())
    total_sell_amount = float(sell_df["gross_amount"].sum())
    realized_pnl = float(df["realized_pnl"].sum()) if "realized_pnl" in df else 0.0
    remaining_shares = int(df.iloc[-1]["remaining_shares"]) if "remaining_shares" in df else int(buy_df["shares"].sum() - sell_df["shares"].sum())
    avg_cost = float(df.iloc[-1]["avg_cost_after_trade"]) if "avg_cost_after_trade" in df else 0.0
    account_total_asset = float(df.iloc[-1]["account_total_asset"]) if "account_total_asset" in df else 0.0

    if latest_price is None:
        latest_price = avg_cost

    market_value = remaining_shares * latest_price
    position_pct = market_value / account_total_asset if account_total_asset else 0

    return {
        "symbol": symbol,
        "trade_count": int(len(df)),
        "buy_count": int(len(buy_df)),
        "sell_count": int(len(sell_df)),
        "total_buy_amount": round(total_buy_amount, 2),
        "total_sell_amount": round(total_sell_amount, 2),
        "realized_pnl": round(realized_pnl, 2),
        "remaining_shares": remaining_shares,
        "avg_cost": round(avg_cost, 4),
        "latest_price_used": round(float(latest_price), 4),
        "market_value": round(float(market_value), 2),
        "account_total_asset": round(account_total_asset, 2),
        "position_pct": round(position_pct, 4),
        "position_level": classify_position(position_pct),
        "capacity": suggest_next_trade_capacity(account_total_asset, market_value, max_position_pct, latest_price)
    }

def write_trade_summary(summary: dict, out_path: str | Path):
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append(f"# {summary.get('symbol')} 操作记录摘要")
    lines.append("")
    if "message" in summary:
        lines.append(summary["message"])
    else:
        lines.append(f"- 交易次数：{summary['trade_count']}")
        lines.append(f"- 买入次数：{summary['buy_count']}")
        lines.append(f"- 卖出次数：{summary['sell_count']}")
        lines.append(f"- 累计买入金额：{summary['total_buy_amount']}")
        lines.append(f"- 累计卖出金额：{summary['total_sell_amount']}")
        lines.append(f"- 已实现盈亏：{summary['realized_pnl']}")
        lines.append(f"- 当前剩余股数：{summary['remaining_shares']}")
        lines.append(f"- 当前平均成本：{summary['avg_cost']}")
        lines.append(f"- 当前使用价格：{summary['latest_price_used']}")
        lines.append(f"- 当前市值：{summary['market_value']}")
        lines.append(f"- 账户总资产：{summary['account_total_asset']}")
        lines.append(f"- 单票仓位比例：{summary['position_pct']:.2%}")
        lines.append(f"- 仓位等级：{summary['position_level']}")
        lines.append("")
        lines.append("## 仓位上限测算")
        cap = summary["capacity"]
        for k, v in cap.items():
            lines.append(f"- {k}: {v}")
        lines.append("")
        lines.append("注意：仓位上限测算不是买卖建议。是否允许操作还要看技术信号、止损计划和情绪规则。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
