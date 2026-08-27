from __future__ import annotations

import numpy as np
import pandas as pd


def sample_ohlcv(end: str = "2026-06-12", periods: int = 120) -> pd.DataFrame:
    """Deterministic synthetic OHLCV used only inside isolated unit tests."""
    index = np.arange(periods, dtype="float64")
    close = 40 + 0.02 * index + 0.0005 * index**2 + 0.3 * np.sin(index / 3 + 3.2)
    volume = np.full(periods, 1_000_000.0)
    volume[-1] = 2_000_000.0
    open_price = close - 0.1
    open_price[-1] = close[-1] + 0.1
    return pd.DataFrame(
        {
            "date": pd.bdate_range(end=end, periods=periods),
            "open": open_price,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": volume,
            "amount": close * volume,
        }
    )
