from __future__ import annotations

from copy import deepcopy
from typing import Any


# One registry for fields that the safe expression DSL can reference.  It only
# describes base OHLCV fields and columns already produced by add_indicators;
# it does not add a second calculation path.
TECHNICAL_FIELD_METADATA: dict[str, dict[str, Any]] = {
    "open": {"label": "开盘价", "category": "price", "lookback": 1, "format": "price"},
    "high": {"label": "最高价", "category": "price", "lookback": 1, "format": "price"},
    "low": {"label": "最低价", "category": "price", "lookback": 1, "format": "price"},
    "close": {"label": "收盘价", "category": "price", "lookback": 1, "format": "price"},
    "volume": {"label": "成交量", "category": "volume", "lookback": 1, "format": "volume"},
    "amount": {"label": "成交额", "category": "volume", "lookback": 1, "format": "amount"},
    "ma5": {"label": "5 日均线", "evidence_label": "MA5", "category": "trend", "lookback": 5, "format": "price"},
    "ma10": {"label": "10 日均线", "evidence_label": "MA10", "category": "trend", "lookback": 10, "format": "price"},
    "ma20": {"label": "20 日均线", "evidence_label": "MA20", "category": "trend", "lookback": 20, "format": "price"},
    "ma30": {"label": "30 日均线", "evidence_label": "MA30", "category": "trend", "lookback": 30, "format": "price"},
    "ma60": {"label": "60 日均线", "evidence_label": "MA60", "category": "trend", "lookback": 60, "format": "price"},
    "ma120": {"label": "120 日均线", "evidence_label": "MA120", "category": "trend", "lookback": 120, "format": "price"},
    "ma250": {"label": "250 日均线", "evidence_label": "MA250", "category": "trend", "lookback": 250, "format": "price"},
    "ema12": {"label": "12 日指数均线", "evidence_label": "EMA12", "category": "trend", "lookback": 12, "format": "price"},
    "ema26": {"label": "26 日指数均线", "evidence_label": "EMA26", "category": "trend", "lookback": 26, "format": "price"},
    "macd": {"label": "MACD 差离值", "evidence_label": "MACD", "category": "momentum", "lookback": 26, "format": "indicator"},
    "macd_signal": {"label": "MACD 信号线", "category": "momentum", "lookback": 35, "format": "indicator"},
    "macd_hist": {"label": "MACD 柱", "category": "momentum", "lookback": 35, "format": "indicator"},
    "macd_hist_diff": {"label": "MACD 柱变化", "category": "momentum", "lookback": 36, "format": "indicator"},
    "rsi14": {"label": "RSI14", "category": "momentum", "lookback": 14, "format": "indicator"},
    "bb_mid": {"label": "布林中轨", "category": "volatility", "lookback": 20, "format": "price"},
    "bb_std": {"label": "布林标准差", "category": "volatility", "lookback": 20, "format": "price"},
    "bb_upper": {"label": "布林上轨", "category": "volatility", "lookback": 20, "format": "price"},
    "bb_lower": {"label": "布林下轨", "category": "volatility", "lookback": 20, "format": "price"},
    "atr14": {"label": "ATR14", "category": "volatility", "lookback": 14, "format": "price"},
    "atr_pct": {"label": "ATR 波动率", "evidence_label": "ATR 占比", "category": "volatility", "lookback": 14, "format": "percent"},
    "vol_ma5": {"label": "5 日均量", "category": "volume", "lookback": 5, "format": "volume"},
    "vol_ma20": {"label": "20 日均量", "category": "volume", "lookback": 20, "format": "volume"},
    "volume_ratio": {"label": "量比", "category": "volume", "lookback": 20, "format": "ratio"},
    "obv": {"label": "OBV", "category": "volume", "lookback": 1, "format": "indicator"},
    "obv_slope_10": {"label": "OBV 十日变化", "category": "volume", "lookback": 10, "format": "indicator"},
    "ret_1d": {"label": "当日涨跌幅", "evidence_label": "当日涨跌", "category": "momentum", "lookback": 1, "format": "percent"},
    "roc20": {"label": "20 日动量", "category": "momentum", "lookback": 21, "format": "percent"},
    "gap_pct": {"label": "跳空幅度", "category": "volatility", "lookback": 1, "format": "percent"},
    "high_20": {"label": "20 日最高价", "evidence_label": "20 日最高", "category": "location", "lookback": 20, "format": "price"},
    "low_20": {"label": "20 日最低价", "evidence_label": "20 日最低", "category": "location", "lookback": 20, "format": "price"},
    "high_60": {"label": "60 日最高价", "evidence_label": "60 日最高", "category": "location", "lookback": 60, "format": "price"},
    "low_60": {"label": "60 日最低价", "evidence_label": "60 日最低", "category": "location", "lookback": 60, "format": "price"},
    "drawdown_from_20d_high": {"label": "距 20 日高点", "category": "location", "lookback": 20, "format": "percent"},
    "distance_from_ma20": {"label": "距 MA20", "category": "location", "lookback": 20, "format": "percent"},
    "ma20_slope": {"label": "MA20 三日变化", "evidence_label": "MA20 方向", "category": "trend", "lookback": 23, "format": "price"},
}

TECHNICAL_FIELD_NAMES = frozenset(TECHNICAL_FIELD_METADATA)
TECHNICAL_FIELD_LOOKBACKS = {
    name: int(metadata["lookback"])
    for name, metadata in TECHNICAL_FIELD_METADATA.items()
}


def technical_field_contract() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            **deepcopy(metadata),
            "evidence_label": metadata.get("evidence_label", metadata["label"]),
        }
        for name, metadata in sorted(TECHNICAL_FIELD_METADATA.items())
    ]
