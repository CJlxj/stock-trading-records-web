"""Read-only example of the advanced local Python rule contract."""

RULE_META = {
    "schema_version": 1,
    "id": "example.close_above_recent_mean",
    "version": 1,
    "name": "示例：收盘高于近期均值",
    "description": "演示高级插件契约，不预测次日涨跌，默认不加入正式方案。",
    "kind": "diagnostic",
    "group": "custom",
    "group_label": "自定义",
    "family": "example",
    "level": "示例",
    "timeframe": "day",
    "lookback": 5,
    "inputs": ["close"],
    "params_schema": {
        "period": {"type": "integer", "default": 5, "min": 2, "max": 60}
    },
    "missing_policy": "DATA_GAP",
    "plain_template": "收盘价高于最近 {period} 日均值",
    "status": "VALIDATED",
    "tests": ["内置示例验证"],
}


def evaluate(frame, params):
    period = int(params.get("period", 5))
    if "close" not in frame or len(frame) < period:
        raise ValueError("close 历史不足")
    mean_value = float(frame["close"].tail(period).mean())
    close_value = float(frame["close"].iloc[-1])
    return {
        "boolean_result": close_value > mean_value,
        "observed": {"close": close_value, "recent_mean": mean_value},
        "plain_explanation": (
            f"收盘价 {close_value:.2f}，最近 {period} 日均值 {mean_value:.2f}。"
        ),
    }
