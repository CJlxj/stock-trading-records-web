from pathlib import Path
import pandas as pd
from src.position_sizer import classify_position, suggest_next_trade_capacity
from src.personal_data import ORDER_KNOWN, read_trade_rows, summarize_trade_rows

# 成交台账里这几列可能为空——源文件没给就必须留空（见 src/personal_data.py）。
# 对它们 fillna(0) 会把「不知道」静默变成「0」，所以这里必须保持缺失。
LEDGER_COLUMNS = ("remaining_shares", "avg_cost_after_trade", "realized_pnl")
FEE_COLUMNS = ("fee", "stamp_tax", "transfer_fee", "other_fee")

def load_trades(path: str | Path) -> pd.DataFrame:
    # 先在原始字符串仍未被 pandas 数值化前补兼容引用，避免 45.0000 → 45.0
    # 之类的格式变化让旧成交在不同只读入口得到不同 record_ref。
    df = pd.DataFrame(read_trade_rows(path))
    if df.empty:
        return df
    numeric_cols = [
        "price", "shares", "gross_amount", "fee", "stamp_tax", "transfer_fee", "other_fee",
        "net_amount", "avg_cost_after_trade", "realized_pnl", "remaining_shares",
        "account_total_asset", "position_market_value", "position_pct"
    ]
    keep_missing = set(LEDGER_COLUMNS) | set(FEE_COLUMNS)
    for col in numeric_cols:
        if col in df.columns:
            values = pd.to_numeric(df[col], errors="coerce")
            df[col] = values if col in keep_missing else values.fillna(0)
    return df

def summarize_symbol(
    trades: pd.DataFrame,
    symbol: str,
    latest_price: float | None = None,
    max_position_pct: float = 0.30,
    *,
    ledger_summary: dict | None = None,
) -> dict:
    df = trades[trades["symbol"] == symbol].copy()
    if df.empty:
        return {"symbol": symbol, "message": "没有找到该股票交易记录"}

    buy_df = df[df["side"].str.upper() == "BUY"]
    sell_df = df[df["side"].str.upper() == "SELL"]

    total_buy_amount = float(buy_df["gross_amount"].sum())
    total_sell_amount = float(sell_df["gross_amount"].sum())

    # 复用操作记录的唯一顺序与账本汇总口径。这里不能再用 df.iloc[-1]：
    # 同日时间不足时，CSV 行序不具备证明「最后一笔」的资格。
    ledger = (
        ledger_summary
        if ledger_summary is not None
        else summarize_trade_rows(df.to_dict(orient="records"))[0]
    )
    last = df.iloc[-1]
    remaining_shares = ledger["remaining_shares"]
    avg_cost = ledger["avg_cost"]
    realized_pnl = (
        None
        if "realized_pnl" not in df.columns or df["realized_pnl"].isna().any()
        else round(float(df["realized_pnl"].sum()), 2)
    )
    account_total_asset = float(last["account_total_asset"]) if "account_total_asset" in df else 0.0
    ledger_complete = (
        ledger["order_status"] == ORDER_KNOWN
        and ledger["complete"]
        and remaining_shares is not None
        and avg_cost is not None
        and realized_pnl is not None
    )

    if latest_price is None and avg_cost is not None and avg_cost > 0:
        latest_price = avg_cost

    market_value = (
        0.0
        if remaining_shares == 0
        else remaining_shares * latest_price
        if remaining_shares is not None and latest_price is not None
        else None
    )
    position_pct = (
        market_value / account_total_asset
        if market_value is not None and account_total_asset
        else None
    )

    return {
        "symbol": symbol,
        "trade_count": int(len(df)),
        "buy_count": int(len(buy_df)),
        "sell_count": int(len(sell_df)),
        "total_buy_amount": round(total_buy_amount, 2),
        "total_sell_amount": round(total_sell_amount, 2),
        "realized_pnl": realized_pnl,
        "remaining_shares": remaining_shares,
        "avg_cost": None if avg_cost is None else round(avg_cost, 4),
        "latest_price_used": None if latest_price is None else round(float(latest_price), 4),
        "market_value": None if market_value is None else round(float(market_value), 2),
        "account_total_asset": round(account_total_asset, 2),
        "position_pct": None if position_pct is None else round(position_pct, 4),
        "position_level": None if position_pct is None else classify_position(position_pct),
        "capacity": (
            None
            if market_value is None or latest_price is None or latest_price <= 0
            else suggest_next_trade_capacity(account_total_asset, market_value, max_position_pct, latest_price)
        ),
        "ledger_complete": ledger_complete,
        "order_status": ledger["order_status"],
        "order_status_label": ledger["order_status_label"],
        "order_reasons": ledger["order_reasons"],
        "basis_record_ref": ledger["basis_record_ref"],
    }


def _recorded_int(value) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(value)


def _recorded_float(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)

def write_trade_summary(summary: dict, out_path: str | Path):
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append(f"# {summary.get('symbol')} 操作记录摘要")
    lines.append("")
    if "message" in summary:
        lines.append(summary["message"])
    else:
        # 未知一律写「未记录」，不写 0。
        def show(key: str, suffix: str = "") -> str:
            value = summary.get(key)
            return "未记录" if value is None else f"{value}{suffix}"

        lines.append(f"- 交易次数：{summary['trade_count']}")
        lines.append(f"- 买入次数：{summary['buy_count']}")
        lines.append(f"- 卖出次数：{summary['sell_count']}")
        lines.append(f"- 累计买入金额：{summary['total_buy_amount']}")
        lines.append(f"- 累计卖出金额：{summary['total_sell_amount']}")
        lines.append(f"- 已实现盈亏：{show('realized_pnl')}")
        lines.append(f"- 成交账本剩余股数：{show('remaining_shares')}")
        lines.append(f"- 成交账本平均成本：{show('avg_cost')}")
        lines.append(f"- 当前使用价格：{show('latest_price_used')}")
        lines.append(f"- 当前市值：{show('market_value')}")
        lines.append(f"- 账户总资产：{summary['account_total_asset']}")
        position_pct = summary.get("position_pct")
        lines.append(
            f"- 单票仓位比例：{'未记录' if position_pct is None else format(position_pct, '.2%')}"
        )
        lines.append(f"- 仓位等级：{show('position_level')}")
        if not summary.get("ledger_complete", True):
            lines.append("- 注意：成交台账缺少必要字段，以上剩余股数与成本不能视为完整事实。")
        lines.append("")
        lines.append("## 仓位上限测算")
        cap = summary["capacity"]
        if cap is None:
            lines.append("- 台账未记录剩余股数，无法测算。")
        else:
            for k, v in cap.items():
                lines.append(f"- {k}: {v}")
        lines.append("")
        lines.append("注意：仓位上限测算不是买卖建议。是否允许操作还要看技术信号、止损计划和情绪规则。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
