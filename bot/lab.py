"""Laboratori de proves: compara estratègies amb dades històriques REALS i sense trampes.

Mètode walk-forward:
  1. Es descarreguen ~3,5 anys d'espelmes reals de Binance.
  2. Per a cada trimestre dels últims 2 anys:
       - s'escullen els millors paràmetres fent servir NOMÉS els 12 mesos anteriors;
       - es proven aquests paràmetres en el trimestre següent, que l'optimització no ha vist.
  3. S'encadenen els trimestres de prova: és el que hauria guanyat el bot de veritat
     si hagués anat reajustant-se cada 3 mesos.

L'estratègia guanyadora és la de més rendiment anual que NO supera la caiguda màxima
que tu acceptes. Els paràmetres recomanats s'obtenen amb els 12 mesos més recents.
"""
from __future__ import annotations

import copy
import itertools
import json
import logging
import math
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import backtest as bt
from .context import bars_per_day
from .data import fetch_history
from .paths import LAB_DIR
from .strategy import add_indicators

log = logging.getLogger("lab")

STATUS_FILE = os.path.join(LAB_DIR, "estat.json")
RESULTS_FILE = os.path.join(LAB_DIR, "resultats.json")

TRAIN_MONTHS = 12
TEST_MONTHS = 3
N_WINDOWS = 8                     # 8 trimestres = 2 anys de prova fora de mostra
WARMUP_DAYS = 220                 # història prèvia per als indicadors (mitjana BTC de 200 dies, etc.)

FAMILIES = [
    {"id": "actual", "nom": "Estratègia actual", "desc": "La configuració que fa servir ara el bot, sense optimitzar.",
     "tf": None, "base": {}, "grid": {}},
    {"id": "retroces_4h", "nom": "Retrocés en tendència (4h)",
     "desc": "La mateixa idea que l'actual, amb espelmes de 4 hores (menys operacions i comissions) i paràmetres optimitzats.",
     "tf": "4h", "base": {"mode": "retroces", "btc_filter": False, "top_n": 0},
     "grid": {"stop_atr": [1.5, 2.5], "take_profit_atr": [3.0, 5.0], "rsi_long": [35, 45], "ema_slow": [50, 100]}},
    {"id": "retroces_filtres", "nom": "Retrocés + filtre BTC + els més forts (4h)",
     "desc": "Només compra quan el Bitcoin és per sobre de la seva mitjana de 200 dies, i només els 4 valors amb més momentum.",
     "tf": "4h", "base": {"mode": "retroces", "btc_filter": True, "top_n": 4},
     "grid": {"stop_atr": [1.5, 2.5], "take_profit_atr": [3.0, 5.0], "momentum_days": [14, 30]}},
    {"id": "rotacio", "nom": "Rotació dels més forts amb filtre BTC (4h)",
     "desc": "Cada dia es queda amb els N valors que més han pujat les últimes setmanes. Si el Bitcoin és per sota de la seva mitjana de 200 dies, tot en efectiu.",
     "tf": "4h", "base": {"mode": "rotacio", "btc_filter": True, "trail_activate_atr": 4.0, "trail_atr": 3.0},
     "grid": {"momentum_days": [14, 30, 60], "top_n": [2, 4], "stop_atr": [3.0, 5.0]}},
]
IND_KEYS = ("ema_fast", "ema_slow", "rsi_period", "atr_period", "momentum_days")


# ------------------------------------------------------------------ estat per al panell
def write_status(**kw) -> None:
    os.makedirs(LAB_DIR, exist_ok=True)
    kw["actualitzat"] = datetime.now(timezone.utc).isoformat()
    kw["pid"] = os.getpid()
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kw, f, default=str)
    os.replace(tmp, STATUS_FILE)


# ------------------------------------------------------------------ mètriques
def curve_stats(curve: pd.Series) -> dict:
    curve = curve.dropna()
    if len(curve) < 2 or curve.iloc[0] <= 0:
        return {"cagr": 0.0, "dd": 0.0, "total": 0.0}
    days = max((curve.index[-1] - curve.index[0]).total_seconds() / 86400, 1)
    total = curve.iloc[-1] / curve.iloc[0] - 1
    cagr = (1 + total) ** (365 / days) - 1 if total > -1 else -1.0
    dd = float(-(curve / curve.cummax() - 1).min())
    return {"cagr": float(cagr), "dd": dd, "total": float(total)}


def score(stats: dict, dd_limit: float) -> tuple:
    """Ordre de preferència: primer les que respecten la caiguda màxima, i entre aquestes la de més rendiment."""
    ok = stats["dd"] <= dd_limit
    return (1, stats["cagr"]) if ok else (0, -stats["dd"])


# ------------------------------------------------------------------ laboratori
class Lab:
    def __init__(self, cfg: dict, data_by_tf: dict, btc_by_tf: dict, dd_limit: float):
        self.base_cfg = cfg
        self.data = data_by_tf          # {tf: {symbol: df}}
        self.btc = btc_by_tf            # {tf: df}
        self.dd_limit = dd_limit
        self._ind_cache: dict = {}
        self.done = 0
        self.total = 1

    def cfg_for(self, fam: dict, params: dict) -> dict:
        c = copy.deepcopy(self.base_cfg)
        c["timeframe"] = fam["tf"] or c["timeframe"]
        c["strategy"].update(fam["base"])
        c["strategy"].update(params)
        c["risk"]["max_drawdown_pct"] = min(50.0, self.dd_limit * 100)   # el bot real s'aturaria aquí
        return c

    def indicators(self, cfg: dict) -> dict:
        p, tf = cfg["strategy"], cfg["timeframe"]
        key = (tf,) + tuple(p.get(k, 30) for k in IND_KEYS)
        if key not in self._ind_cache:
            bpd = bars_per_day(tf)
            self._ind_cache[key] = {s: add_indicators(df, p, bpd) for s, df in self.data[tf].items()}
        return self._ind_cache[key]

    def run_window(self, cfg: dict, start, end) -> tuple:
        """Backtest de [start, end) amb escalfament previ. Retorna (trader, corba des de start)."""
        tf = cfg["timeframe"]
        w0 = start - pd.Timedelta(days=WARMUP_DAYS)
        frames = {s: f.loc[w0:end - pd.Timedelta(seconds=1)] for s, f in self.indicators(cfg).items()}
        frames = {s: f for s, f in frames.items() if len(f)}
        btc = self.btc[tf].loc[:end - pd.Timedelta(seconds=1)]
        trader, curve = bt.run(cfg, {}, trade_from=start, btc=btc, indicators=frames)
        return trader, curve.loc[start:]

    def tick(self, msg: str) -> None:
        self.done += 1
        if self.done % 3 == 0 or self.done == self.total:
            write_status(estat="provant", pct=round(self.done / self.total * 100, 1), missatge=msg,
                         inici=self.started)

    def optimize(self, fam: dict, start, end, label: str) -> tuple[dict, dict]:
        grid = fam["grid"]
        combos = [dict(zip(grid, v)) for v in itertools.product(*grid.values())] if grid else [{}]
        best, best_stats = None, None
        for params in combos:
            cfg = self.cfg_for(fam, params)
            _, curve = self.run_window(cfg, start, end)
            st = curve_stats(curve)
            if best is None or score(st, self.dd_limit) > score(best_stats, self.dd_limit):
                best, best_stats = params, st
            self.tick(f"{fam['nom']}: {label}")
        return best, best_stats

    def evaluate(self, fam: dict, test_starts: list, end) -> dict:
        equity, pieces, windows = 1000.0, [], []
        trades, fees_pct = [], 0.0
        for k, ts0 in enumerate(test_starts):
            ts1 = test_starts[k + 1] if k + 1 < len(test_starts) else end
            params, train_st = self.optimize(fam, ts0 - pd.DateOffset(months=TRAIN_MONTHS), ts0,
                                             f"trimestre {k + 1} de {len(test_starts)}")
            cfg = self.cfg_for(fam, params)
            trader, curve = self.run_window(cfg, ts0, ts1)
            self.tick(f"{fam['nom']}: trimestre {k + 1} de {len(test_starts)}")
            if len(curve) < 2:
                continue
            seg = curve / curve.iloc[0] * equity
            pieces.append(seg if not pieces else seg.iloc[1:])
            equity = float(seg.iloc[-1])
            trades.extend(trader.trades)
            fees_pct += trader.fees_paid / self.base_cfg["paper"]["initial_capital"]
            windows.append({"des_de": str(ts0.date()), "fins": str(ts1.date()), "parametres": params,
                            "rendiment_pct": round((seg.iloc[-1] / seg.iloc[0] - 1) * 100, 2),
                            "entrenament_rendiment_anual_pct": round(train_st["cagr"] * 100, 1),
                            "entrenament_caiguda_pct": round(train_st["dd"] * 100, 1)})
        oos = pd.concat(pieces) if pieces else pd.Series(dtype=float)
        # Paràmetres recomanats per a ARA: optimitzats amb els 12 mesos més recents
        rec, rec_st = self.optimize(fam, end - pd.DateOffset(months=TRAIN_MONTHS), end, "paràmetres per a ara")
        st = curve_stats(oos)
        years = max((oos.index[-1] - oos.index[0]).days / 365, 0.01) if len(oos) else 1
        tdf = pd.DataFrame(trades)
        daily = oos.resample("1D").last().pct_change().dropna() if len(oos) else pd.Series(dtype=float)
        monthly = oos.resample("ME").last().pct_change().dropna() if len(oos) else pd.Series(dtype=float)
        return {
            "id": fam["id"], "nom": fam["nom"], "desc": fam["desc"],
            "temporalitat": fam["tf"] or self.base_cfg["timeframe"],
            "rendiment_anual_pct": round(st["cagr"] * 100, 1),
            "rendiment_total_pct": round(st["total"] * 100, 1),
            "caiguda_maxima_pct": round(st["dd"] * 100, 1),
            "compleix_caiguda": st["dd"] <= self.dd_limit,
            "sharpe": round(float(daily.mean() / daily.std() * math.sqrt(365)), 2) if len(daily) > 5 and daily.std() > 0 else None,
            "operacions": len(tdf),
            "operacions_any": round(len(tdf) / years, 0),
            "guanyadores_pct": round(float((tdf.pnl_eur > 0).mean() * 100), 1) if len(tdf) else None,
            "comissions_pct_any": round(fees_pct / years * 100, 1),
            "mesos_positius_pct": round(float((monthly > 0).mean() * 100), 0) if len(monthly) else None,
            "finestres": windows,
            "parametres_recomanats": {**fam["base"], **rec},
            "recomanats_rendiment_anual_pct": round(rec_st["cagr"] * 100, 1),
            "recomanats_caiguda_pct": round(rec_st["dd"] * 100, 1),
            "corba": [[str(t), round(float(v), 2)] for t, v in bt._downsample_series(oos).items()],
        }

    def run(self) -> dict:
        self.started = datetime.now(timezone.utc).isoformat()
        any_tf = next(iter(self.data))
        end = max(df.index[-1] for df in self.data[any_tf].values()).floor("D")
        test_starts = [end - pd.DateOffset(months=TEST_MONTHS * (N_WINDOWS - k)) for k in range(N_WINDOWS)]
        sizes = [max(1, len(list(itertools.product(*f["grid"].values())))) if f["grid"] else 1 for f in FAMILIES]
        self.total = sum(s * (N_WINDOWS + 1) + N_WINDOWS for s in sizes)
        results = [self.evaluate(f, test_starts, end) for f in FAMILIES]

        # Referències: comprar i mantenir durant el mateix període de prova
        t0 = test_starts[0]
        bench = {}
        btc = self.btc["4h"].loc[t0:end]["close"]
        bench["btc"] = btc / btc.iloc[0] * 1000
        closes = pd.concat({s: df.loc[t0:end]["close"] for s, df in self.data["4h"].items()}, axis=1).ffill().bfill()
        bench["cartera"] = (closes / closes.iloc[0]).mean(axis=1) * 1000
        refs = []
        for k, nom in (("btc", "Comprar Bitcoin i no tocar res"), ("cartera", "Comprar els 12 valors a parts iguals i no tocar res")):
            st = curve_stats(bench[k])
            refs.append({"id": k, "nom": nom, "rendiment_anual_pct": round(st["cagr"] * 100, 1),
                         "caiguda_maxima_pct": round(st["dd"] * 100, 1),
                         "corba": [[str(t), round(float(v), 2)] for t, v in bt._downsample_series(bench[k]).items()]})

        ok = [r for r in results if r["compleix_caiguda"] and r["operacions"] > 0]
        best = max(ok, key=lambda r: r["rendiment_anual_pct"]) if ok else min(results, key=lambda r: r["caiguda_maxima_pct"])
        return {
            "generat": datetime.now(timezone.utc).isoformat(),
            "periode_prova": {"des_de": str(t0.date()), "fins": str(end.date())},
            "caiguda_maxima_acceptada_pct": round(self.dd_limit * 100),
            "comissio_pct": self.base_cfg["paper"]["fee_pct"],
            "estrategies": results,
            "referencies": refs,
            "recomanada": best["id"],
            "recomanada_compleix": bool(ok),
        }


def load_data(cfg: dict, synthetic: bool = False) -> tuple[dict, dict]:
    days = (TRAIN_MONTHS + TEST_MONTHS * N_WINDOWS) * 31 + WARMUP_DAYS
    tfs = sorted({f["tf"] or cfg["timeframe"] for f in FAMILIES})
    symbols = list(cfg["symbols"])
    data, btc = {}, {}
    if synthetic:
        import run_backtest as rb
        for tf in tfs:
            d = rb.synthetic(list(dict.fromkeys(symbols + ["BTC/EUR"])), tf, days, seed=3)
            btc[tf] = d["BTC/EUR"]
            data[tf] = {s: d[s] for s in symbols}
        return data, btc
    n, total = 0, len(tfs) * len(symbols)
    for tf in tfs:
        data[tf] = {}
        for s in symbols:
            write_status(estat="descarregant", pct=round(n / total * 100, 1),
                         missatge=f"Descarregant 3,5 anys d'espelmes de {s} ({tf})")
            try:
                data[tf][s] = fetch_history(s, tf, days, cache_dir=os.path.join(LAB_DIR, "dades"), quote="USDT")
            except Exception as e:
                log.warning("Salto %s (%s): %s", s, tf, e)
            n += 1
        btc[tf] = data[tf].get("BTC/EUR")
        if btc[tf] is None:
            btc[tf] = fetch_history("BTC/EUR", tf, days, cache_dir=os.path.join(LAB_DIR, "dades"), quote="USDT")
    return data, btc


def main(cfg: dict, dd_limit_pct: float, synthetic: bool = False) -> dict:
    logging.getLogger("trader").setLevel(logging.CRITICAL)   # sense soroll: milers d'operacions simulades
    t0 = time.time()
    write_status(estat="descarregant", pct=0, missatge="Preparant la descàrrega de dades")
    try:
        data, btc = load_data(cfg, synthetic)
        lab = Lab(cfg, data, btc, dd_limit_pct / 100)
        res = lab.run()
        res["durada_min"] = round((time.time() - t0) / 60, 1)
        res["dades_sintetiques"] = synthetic
        tmp = RESULTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(res, f, default=str)
        os.replace(tmp, RESULTS_FILE)
        write_status(estat="fet", pct=100, missatge=f"Prova acabada en {res['durada_min']} minuts")
        log.info("Laboratori acabat en %.1f min. Recomanada: %s", res["durada_min"], res["recomanada"])
        return res
    except Exception as e:
        log.exception("El laboratori ha fallat")
        write_status(estat="error", pct=0, missatge=f"La prova ha fallat: {e}")
        raise
