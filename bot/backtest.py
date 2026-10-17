"""Backtest: prova l'estratègia amb dades històriques, amb el mateix codi que el bot en directe."""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .strategy import add_indicators, warmup_bars
from .trader import Trader

log = logging.getLogger("backtest")


def run(cfg: dict, data: dict[str, pd.DataFrame]) -> tuple[Trader, pd.Series]:
    p = cfg["strategy"]
    wu = warmup_bars(p)
    frames = {s: add_indicators(df, p) for s, df in data.items() if len(df) > wu}
    for s, df in frames.items():
        df["_i"] = np.arange(len(df))
    index = sorted(set().union(*[df.index for df in frames.values()]))
    t = Trader.new(cfg)
    curve = {}
    for ts in index:
        bars = {s: df.loc[ts] for s, df in frames.items() if ts in df.index}
        for s, b in bars.items():  # 1) stops i objectius dins l'espelma
            t.on_price(s, b["open"], b["high"], b["low"], b["close"], ts)
        t.check_risk(ts)           # 2) límits de risc globals
        for s, b in bars.items():  # 3) espelma tancada: sortides i entrades
            if b["_i"] >= wu:
                t.on_bar_close(s, b, ts)
        curve[ts] = t.equity()
    if t.positions:
        t.close_all(index[-1], "fi_backtest")
        curve[index[-1]] = t.equity()
    return t, pd.Series(curve)


def buy_and_hold(cfg: dict, data: dict[str, pd.DataFrame], index) -> pd.Series:
    """Referència: repartir el capital a parts iguals entre tots els valors i no tocar res."""
    cap = cfg["paper"]["initial_capital"]
    norm = pd.concat({s: df["close"] / df["close"].iloc[0] for s, df in data.items()}, axis=1)
    norm = norm.reindex(index).ffill().bfill()
    return norm.mean(axis=1) * cap


def metrics(t: Trader, curve: pd.Series, cfg: dict) -> dict:
    cap = cfg["paper"]["initial_capital"]
    trades = pd.DataFrame(t.trades)
    days = max((curve.index[-1] - curve.index[0]).total_seconds() / 86400, 1)
    total = curve.iloc[-1] / cap - 1
    daily = curve.resample("1D").last().pct_change().dropna()
    dd = (curve / curve.cummax() - 1).min()
    m = {
        "Capital inicial (EUR)": round(cap, 2),
        "Capital final (EUR)": round(curve.iloc[-1], 2),
        "Rendiment total": f"{total * 100:.2f}%",
        "Rendiment anualitzat": f"{((1 + total) ** (365 / days) - 1) * 100:.2f}%",
        "Rendiment diari mitjà": f"{daily.mean() * 100:.3f}%" if len(daily) else "n/a",
        "Caiguda màxima": f"{dd * 100:.2f}%",
        "Sharpe (anual)": round(daily.mean() / daily.std() * np.sqrt(365), 2) if len(daily) > 2 and daily.std() > 0 else "n/a",
        "Operacions": len(trades),
        "Comissions pagades (EUR)": round(t.fees_paid, 2),
        "Dies": round(days, 1),
    }
    if len(trades):
        wins = trades[trades.pnl_eur > 0]
        losses = trades[trades.pnl_eur <= 0]
        m["% guanyadores"] = f"{len(wins) / len(trades) * 100:.1f}%"
        m["Factor de benefici"] = round(wins.pnl_eur.sum() / abs(losses.pnl_eur.sum()), 2) if len(losses) and losses.pnl_eur.sum() != 0 else "∞"
        m["Guany mitjà per operació"] = f"{trades.pnl_pct.mean():.2f}%"
    return m


def plot(curve: pd.Series, bench: pd.Series, path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True, gridspec_kw={"height_ratios": [3, 1]})
    a1.plot(curve.index, curve.values, label="Bot", color="#1f6feb", lw=1.6)
    a1.plot(bench.index, bench.values, label="Comprar i mantenir", color="#8b949e", lw=1.2, ls="--")
    a1.set_ylabel("EUR")
    a1.legend(frameon=False)
    a1.grid(alpha=0.25)
    a1.set_title("Evolució del capital")
    ddv = (curve / curve.cummax() - 1) * 100
    a2.fill_between(ddv.index, ddv.values, 0, color="#d1242f", alpha=0.35)
    a2.set_ylabel("Caiguda %")
    a2.grid(alpha=0.25)
    for a in (a1, a2):
        for side in ("top", "right"):
            a.spines[side].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
