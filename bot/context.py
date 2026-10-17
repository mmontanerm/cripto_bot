"""Context de mercat compartit pel backtest i pel bot en directe.

- Filtre BTC: el mercat cripto es considera "favorable" quan el Bitcoin tanca
  per sobre de la seva mitjana de N dies. Quan no ho és, el bot no compra
  (i en rotació es queda en efectiu).
- Rànquing de momentum: ordena els valors pel rendiment dels últims N dies.
"""
from __future__ import annotations

import math

import pandas as pd

TF_MIN = {"15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def bars_per_day(tf: str) -> float:
    return 1440 / TF_MIN[tf]


def regime_from_daily(daily_close: pd.Series, ma_days: int) -> pd.Series:
    """Per a cada dia tancat: True si el tancament és per sobre de la mitjana.
    L'índex és el MOMENT EN QUÈ ES CONEIX (final del dia), per no mirar el futur."""
    ma = daily_close.rolling(ma_days, min_periods=ma_days).mean()
    reg = (daily_close > ma) & ma.notna()
    reg.index = reg.index + pd.Timedelta(days=1)
    return reg


def regime_series(btc_bars: pd.DataFrame, ma_days: int, tf: str, index) -> pd.Series:
    """Règim alineat amb les espelmes (`index` = obertura de l'espelma)."""
    daily = btc_bars["close"].resample("1D").last().dropna()
    reg = regime_from_daily(daily, ma_days)
    close_times = pd.DatetimeIndex(index) + pd.Timedelta(minutes=TF_MIN[tf])
    aligned = reg.reindex(reg.index.union(close_times)).ffill().reindex(close_times)
    return pd.Series(aligned.fillna(False).astype(bool).values, index=index)


def rank(moms: dict, top_n: int, risk_on: bool = True) -> list:
    """Els `top_n` valors amb més momentum (només si és positiu i el mercat és favorable)."""
    if not risk_on or top_n <= 0:
        return []
    valid = [(s, m) for s, m in moms.items() if m is not None and not math.isnan(m) and m > 0]
    valid.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in valid[:top_n]]


def is_day_close(bar_open, tf: str) -> bool:
    """L'espelma que comença a `bar_open` tanca exactament a les 00:00 UTC."""
    t = pd.Timestamp(bar_open) + pd.Timedelta(minutes=TF_MIN[tf])
    return t.hour == 0 and t.minute == 0
