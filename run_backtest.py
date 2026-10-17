"""Prova l'estratègia amb dades històriques.

    python run_backtest.py                 # descarrega l'històric (Binance) i fa el backtest
    python run_backtest.py --days 180      # canvia el període
    python run_backtest.py --csv-dir dades # fa servir CSV propis (un per valor: BTCEUR.csv...)
    python run_backtest.py --synthetic     # dades INVENTADES, només per comprovar que tot funciona
"""
import argparse
import logging
import os

import numpy as np
import pandas as pd
import yaml

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from bot import backtest as bt  # noqa: E402
from bot.config import validate  # noqa: E402
from bot.data import fetch_history, load_csv  # noqa: E402
from bot.logsetup import setup  # noqa: E402

log = logging.getLogger("backtest")


def synthetic(symbols, timeframe, days, seed=7):
    """Preus aleatoris amb fases de tendència. NO serveixen per avaluar l'estratègia."""
    rng = np.random.default_rng(seed)
    mins = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}[timeframe]
    n = days * 1440 // mins
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("h"), periods=n, freq=f"{mins}min")
    out = {}
    for s in symbols:
        vol = rng.uniform(0.004, 0.012) * np.sqrt(mins / 60)
        drift = np.repeat(rng.normal(0, vol / 4, n // 300 + 1), 300)[:n]
        r = drift + rng.normal(0, vol, n)
        close = 100 * np.exp(np.cumsum(r))
        openp = np.r_[close[0], close[:-1]]
        spread = np.abs(rng.normal(0, vol, n)) * close
        out[s] = pd.DataFrame({"open": openp, "high": np.maximum(openp, close) + spread,
                               "low": np.minimum(openp, close) - spread, "close": close,
                               "volume": rng.uniform(1, 10, n)}, index=idx)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--days", type=int)
    ap.add_argument("--csv-dir")
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()

    setup("backtest")
    with open(a.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    validate(cfg)
    days = a.days or cfg["backtest"]["days"]
    tf = cfg["timeframe"]

    if a.synthetic:
        log.warning("DADES SINTÈTIQUES: els resultats NO diuen res de l'estratègia, només proven el codi.")
        data = synthetic(cfg["symbols"], tf, days)
    else:
        data = {}
        for s in cfg["symbols"]:
            try:
                if a.csv_dir:
                    data[s] = load_csv(os.path.join(a.csv_dir, s.replace("/", "") + ".csv"))
                else:
                    log.info("Descarregant %s (%s, %d dies)...", s, tf, days)
                    data[s] = fetch_history(s, tf, days)
            except Exception as e:
                log.warning("Salto %s: %s", s, e)
    if not data:
        raise SystemExit("No hi ha dades per fer el backtest.")

    trader, curve = bt.run(cfg, data)
    bench = bt.buy_and_hold(cfg, data, curve.index)
    m = bt.metrics(trader, curve, cfg)
    m["Comprar i mantenir (referència)"] = f"{(bench.iloc[-1] / bench.iloc[0] - 1) * 100:.2f}%"

    os.makedirs("results", exist_ok=True)
    pd.DataFrame(trader.trades).to_csv("results/backtest_trades.csv", index=False)
    curve.to_csv("results/backtest_equity.csv", header=["equity"])
    bt.plot(curve, bench, "results/backtest.png")

    print("\n================  RESULTATS DEL BACKTEST  ================")
    for k, v in m.items():
        print(f"  {k:<34} {v}")
    print("==========================================================")
    print("  Detall: results/backtest_trades.csv · Gràfic: results/backtest.png\n")


if __name__ == "__main__":
    main()
