import numpy as np
import pandas as pd

def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window, min_periods=window).mean()

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for n in [5, 10, 20, 30, 60, 120, 250]:
        min_periods = min(n, max(5, len(out) // 3))
        out[f"ma{n}"] = out["close"].rolling(n, min_periods=min_periods).mean()

    out["ema12"] = ema(out["close"], 12)
    out["ema26"] = ema(out["close"], 26)
    out["macd"] = out["ema12"] - out["ema26"]
    out["macd_signal"] = ema(out["macd"], 9)
    out["macd_hist"] = out["macd"] - out["macd_signal"]
    out["macd_hist_diff"] = out["macd_hist"].diff()

    out["rsi14"] = rsi(out["close"], 14)

    out["bb_mid"] = out["close"].rolling(20, min_periods=20).mean()
    out["bb_std"] = out["close"].rolling(20, min_periods=20).std()
    out["bb_upper"] = out["bb_mid"] + 2 * out["bb_std"]
    out["bb_lower"] = out["bb_mid"] - 2 * out["bb_std"]

    out["atr14"] = atr(out, 14)
    out["atr_pct"] = out["atr14"] / out["close"]

    out["vol_ma5"] = out["volume"].rolling(5, min_periods=5).mean()
    out["vol_ma20"] = out["volume"].rolling(20, min_periods=20).mean()
    out["volume_ratio"] = out["volume"] / out["vol_ma20"]

    direction = np.sign(out["close"].diff()).fillna(0)
    out["obv"] = (direction * out["volume"]).cumsum()
    out["obv_slope_10"] = out["obv"].diff(10)

    out["ret_1d"] = out["close"].pct_change()
    out["roc20"] = out["close"].pct_change(20)
    out["gap_pct"] = out["open"] / out["close"].shift(1) - 1
    out["high_20"] = out["high"].rolling(20, min_periods=20).max()
    out["low_20"] = out["low"].rolling(20, min_periods=20).min()
    out["high_60"] = out["high"].rolling(60, min_periods=60).max()
    out["low_60"] = out["low"].rolling(60, min_periods=60).min()
    out["drawdown_from_20d_high"] = out["close"] / out["high_20"] - 1
    out["distance_from_ma20"] = out["close"] / out["ma20"] - 1
    out["ma20_slope"] = out["ma20"].diff(3)

    return out
