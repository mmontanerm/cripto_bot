"""Backtest: prova l'estratègia amb dades històriques, amb el mateix codi que el bot en directe."""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .context import bars_per_day, is_day_close, rank, regime_series
from .strategy import add_indicators, warmup_bars
from .trader import Trader

log = logging.getLogger("backtest")


class _Row:
    """Vista lleugera d'una espelma (evita crear milers de pandas.Series)."""
    __slots__ = ("cols", "i")

    def __init__(self, cols: dict, i: int):
        self.cols, self.i = cols, i

    def __getitem__(self, k):
        return self.cols[k][self.i]


COLS = ("open", "high", "low", "close", "atr", "trend", "rsi", "rsi_prev", "mom")


def run(cfg: dict, data: dict[str, pd.DataFrame], trade_from=None, btc: pd.DataFrame | None = None,
        indicators: dict | None = None) -> tuple[Trader, pd.Series]:
    """Executa el backtest. `trade_from`: no obre posicions abans d'aquesta data (escalfament).
    `btc`: espelmes de BTC per al filtre de mercat (si no, es fa servir data['BTC/...'] si hi és)."""
    p = cfg["strategy"]
    tf = cfg["timeframe"]
    bpd = bars_per_day(tf)
    wu = warmup_bars(p, bpd)
    mode = p.get("mode", "retroces")
    top_n = int(p.get("top_n", 0) or 0)
    use_regime = bool(p.get("btc_filter")) or mode == "rotacio"

    frames = indicators if indicators is not None else {s: add_indicators(df, p, bpd) for s, df in data.items()}
    frames = {s: f for s, f in frames.items() if len(f) > wu}
    cols = {s: {c: f[c].to_numpy(dtype=float) for c in COLS} for s, f in frames.items()}
    pos_of = {s: {ts: i for i, ts in enumerate(f.index)} for s, f in frames.items()}
    index = sorted(set().union(*[f.index for f in frames.values()]))

    regime = {}
    if use_regime:
        src = btc if btc is not None else next((data[s] for s in data if s.startswith("BTC/")), None)
        if src is not None:
            regime = regime_series(src, int(p.get("btc_ma_days", 200)), tf, index).to_dict()

    trade_from = pd.Timestamp(trade_from) if trade_from is not None else None
    t = Trader.new(cfg)
    moms: dict = {}
    atrs: dict = {}
    curve = {}
    for ts in index:
        bars = [(s, pos_of[s][ts]) for s in frames if ts in pos_of[s]]
        for s, i in bars:                      # 1) stops i objectius dins l'espelma
            c = cols[s]
            t.on_price(s, c["open"][i], c["high"][i], c["low"][i], c["close"][i], ts)
        t.check_risk(ts)                       # 2) límits de risc globals
        if use_regime:
            t.risk_on = bool(regime.get(ts, False))
        for s, i in bars:
            moms[s] = cols[s]["mom"][i] if i >= wu else float("nan")
            atrs[s] = cols[s]["atr"][i]
        if top_n and mode == "retroces":
            t.allowed = rank(moms, top_n, True)
        can_trade = trade_from is None or ts >= trade_from
        for s, i in bars:                      # 3) espelma tancada: sortides i entrades
            if i >= wu and can_trade:
                t.on_bar_close(s, _Row(cols[s], i), ts)
        if mode == "rotacio" and can_trade and is_day_close(ts, tf):
            t.rebalance(rank(moms, top_n or 3, t.risk_on), atrs, ts)
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


def _downsample_series(s: pd.Series, n: int = 400) -> pd.Series:
    """Redueix una corba a ~n punts per enviar-la al panell."""
    if len(s) <= n:
        return s
    idx = np.linspace(0, len(s) - 1, n).astype(int)
    return s.iloc[idx]
