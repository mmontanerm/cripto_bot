"""Estratègia: seguiment de tendència amb entrada als retrocessos.

Tota la lògica de senyals viu aquí. Si vols provar una altra estratègia,
només cal canviar `add_indicators` i `signal`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def add_indicators(df: pd.DataFrame, p: dict, bars_per_day: float = 24) -> pd.DataFrame:
    out = df.copy()
    out["ema_fast"] = ema(out["close"], p["ema_fast"])
    out["ema_slow"] = ema(out["close"], p["ema_slow"])
    out["rsi"] = rsi(out["close"], p["rsi_period"])
    out["atr"] = atr(out, p["atr_period"])
    out["trend"] = 0
    up = (out["ema_fast"] > out["ema_slow"]) & (out["close"] > out["ema_slow"])
    down = (out["ema_fast"] < out["ema_slow"]) & (out["close"] < out["ema_slow"])
    out.loc[up, "trend"] = 1
    out.loc[down, "trend"] = -1
    out["rsi_prev"] = out["rsi"].shift()
    mom_bars = max(1, int(p.get("momentum_days", 30) * bars_per_day))
    out["mom"] = out["close"] / out["close"].shift(mom_bars) - 1   # rendiment dels últims N dies
    return out


def warmup_bars(p: dict, bars_per_day: float = 24) -> int:
    base = max(p["ema_slow"], p["rsi_period"], p["atr_period"]) + 5
    if p.get("mode") == "rotacio" or p.get("top_n", 0):
        base = max(base, int(p.get("momentum_days", 30) * bars_per_day) + 5)
    return base


def explain(row: pd.Series, p: dict, allow_short: bool, round_trip_cost_pct: float) -> tuple[str, str]:
    """Què pensa el bot d'aquest valor ara mateix, en paraules (per al panell)."""
    if np.isnan(row["atr"]) or row["atr"] <= 0:
        return "sense_dades", "Encara no hi ha prou dades"
    target_pct = p["take_profit_atr"] * row["atr"] / row["close"] * 100
    need = p["min_edge_over_costs"] * round_trip_cost_pct
    if target_pct < need:
        return "poc_moviment", f"Es mou massa poc: el guany possible ({target_pct:.1f}%) no cobreix prou els costos (cal {need:.1f}%)"
    if row["trend"] == 1:
        if row["rsi"] < p["rsi_long"]:
            return "a_punt", f"Tendència alcista en retrocés (RSI {row['rsi']:.0f}): comprarà quan el RSI torni a superar {p['rsi_long']}"
        return "esperant", f"Tendència alcista. Espera un retrocés (RSI {row['rsi']:.0f}, entra per sota de {p['rsi_long']} i rebot)"
    if row["trend"] == -1:
        if not allow_short:
            return "baixista", "Tendència baixista: no compra (els curts estan desactivats)"
        if row["rsi"] > p["rsi_short"]:
            return "a_punt", f"Tendència baixista en rebot (RSI {row['rsi']:.0f}): vendrà en curt quan baixi de {p['rsi_short']}"
        return "esperant", f"Tendència baixista. Espera un rebot per vendre en curt (RSI {row['rsi']:.0f})"
    return "lateral", "Sense tendència clara: s'espera"


def signal(row: pd.Series, p: dict, allow_short: bool, round_trip_cost_pct: float) -> int:
    """+1 obrir llarg, -1 obrir curt, 0 res. `row` és l'última espelma tancada."""
    if np.isnan(row["atr"]) or row["atr"] <= 0:
        return 0
    # Filtre anti-microguanys: el guany objectiu ha de cobrir els costos amb marge
    target_pct = p["take_profit_atr"] * row["atr"] / row["close"] * 100
    if target_pct < p["min_edge_over_costs"] * round_trip_cost_pct:
        return 0
    if row["trend"] == 1 and row["rsi_prev"] < p["rsi_long"] <= row["rsi"]:
        return 1
    if allow_short and row["trend"] == -1 and row["rsi_prev"] > p["rsi_short"] >= row["rsi"]:
        return -1
    return 0
