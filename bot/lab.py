"""Laboratori de proves v2: estratègies amb paràmetres FIXOS sobre ~5 anys de dades REALS.

Per què paràmetres fixos i no optimitzats?
  A la v1, l'optimització trimestre a trimestre donava resultats molt diferents segons un
  sol ajust (+47% o +13% amb la mateixa estratègia). És el senyal que els paràmetres
  "guanyadors" ho eren per casualitat. Ara:

  1. Cada estratègia es prova amb paràmetres senzills i fixats d'avançada, en UNA sola
     execució contínua del 2021 al 2026 (inclou l'eufòria del 2021 i la caiguda del 2022).
     L'aturada d'emergència funciona exactament com al bot real: si salta, no torna a entrar.
  2. Prova de robustesa: la rotació es prova amb 24 combinacions de paràmetres. Si la majoria
     guanyen diners, l'avantatge és real; si només en guanyen unes poques, és sort.
  3. Mai es recomana una estratègia que perdi diners: si cap guanya sense passar de la
     caiguda acceptada, el laboratori ho diu.
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

HISTORY_DAYS = 2330               # ~6,4 anys d'espelmes: prova des de gener del 2021 + escalfament previ
WARMUP_DAYS = 220                 # història prèvia per als indicadors (mitjana BTC de 200 dies, etc.)

ROT_BASE = {"mode": "rotacio", "btc_filter": True, "top_n": 4, "momentum_days": 30,
            "stop_atr": 4.0, "trail_activate_atr": 4.0, "trail_atr": 3.0, "exposure": 1.0}

STRATEGIES = [
    {"id": "actual", "nom": "Estratègia actual", "tf": None, "params": {},
     "desc": "La configuració que fa servir ara el bot."},
    {"id": "retroces_4h", "nom": "Retrocés en tendència (4h)", "tf": "4h",
     "params": {"mode": "retroces", "btc_filter": False, "top_n": 0},
     "desc": "La mateixa idea que l'actual, amb espelmes de 4 hores (menys operacions i comissions)."},
    {"id": "rotacio_100", "nom": "Rotació dels 4 més forts, tot el capital", "tf": "4h", "params": dict(ROT_BASE),
     "desc": "Cada dia a les 00:00 UTC es queda amb els 4 valors que més han pujat en 30 dies. "
             "Si el Bitcoin és per sota de la seva mitjana de 200 dies, tot en efectiu."},
    {"id": "rotacio_50", "nom": "Rotació dels 4 més forts, meitat del capital", "tf": "4h",
     "params": {**ROT_BASE, "exposure": 0.5},
     "desc": "La mateixa rotació, però només inverteix la meitat del capital: menys rendiment i menys caigudes."},
    {"id": "rotacio_sense_filtre", "nom": "Rotació sense filtre BTC", "tf": "4h",
     "params": {**ROT_BASE, "btc_filter": False},
     "desc": "La rotació sense apartar-se quan el Bitcoin baixa. Serveix per veure què aporta el filtre."},
]
GRID = {"momentum_days": [14, 30, 60], "top_n": [2, 3, 4, 6], "stop_atr": [3.0, 5.0]}
IND_KEYS = ("ema_fast", "ema_slow", "rsi_period", "atr_period", "momentum_days")


def write_status(**kw) -> None:
    os.makedirs(LAB_DIR, exist_ok=True)
    kw["actualitzat"] = datetime.now(timezone.utc).isoformat()
    kw["pid"] = os.getpid()
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kw, f, default=str)
    os.replace(tmp, STATUS_FILE)


def curve_stats(curve: pd.Series) -> dict:
    curve = curve.dropna()
    if len(curve) < 2 or curve.iloc[0] <= 0:
        return {"cagr": 0.0, "dd": 0.0, "total": 0.0}
    days = max((curve.index[-1] - curve.index[0]).total_seconds() / 86400, 1)
    total = curve.iloc[-1] / curve.iloc[0] - 1
    cagr = (1 + total) ** (365 / days) - 1 if total > -1 else -1.0
    dd = float(-(curve / curve.cummax() - 1).min())
    return {"cagr": float(cagr), "dd": dd, "total": float(total)}


def yearly(curve: pd.Series) -> dict:
    """Rendiment de cada any natural (el primer i l'últim, parcials)."""
    if len(curve) < 2:
        return {}
    out = {}
    for y, seg in curve.groupby(curve.index.year):
        prev = curve[curve.index < seg.index[0]]
        start = prev.iloc[-1] if len(prev) else seg.iloc[0]
        out[str(y)] = round((seg.iloc[-1] / start - 1) * 100, 1)
    return out


def ser(curve: pd.Series) -> list:
    return [[str(t), round(float(v), 2)] for t, v in bt._downsample_series(curve).items()]


class Lab:
    def __init__(self, cfg: dict, data: dict, btc: dict, dd_limit: float):
        self.base_cfg = cfg
        self.data, self.btc = data, btc
        self.dd_limit = dd_limit
        self._ind: dict = {}
        self.done, self.total = 0, 1
        self.started = datetime.now(timezone.utc).isoformat()

    def cfg_for(self, tf: str | None, params: dict, kill_switch: bool = True) -> dict:
        c = copy.deepcopy(self.base_cfg)
        c["timeframe"] = tf or c["timeframe"]
        c["strategy"].update(params)
        # Coherent amb el bot real: l'aturada d'emergència a la caiguda que acceptes
        c["risk"]["max_drawdown_pct"] = min(50.0, self.dd_limit * 100) if kill_switch else 99.0
        return c

    def indicators(self, cfg: dict) -> dict:
        p, tf = cfg["strategy"], cfg["timeframe"]
        key = (tf,) + tuple(p.get(k, 30) for k in IND_KEYS)
        if key not in self._ind:
            bpd = bars_per_day(tf)
            self._ind[key] = {s: add_indicators(df, p, bpd) for s, df in self.data[tf].items()}
        return self._ind[key]

    def run_cfg(self, cfg: dict, start, end):
        frames = {s: f.loc[start - pd.Timedelta(days=WARMUP_DAYS):end] for s, f in self.indicators(cfg).items()}
        frames = {s: f for s, f in frames.items() if len(f) > 50}
        btc = self.btc[cfg["timeframe"]].loc[:end]
        trader, curve = bt.run(cfg, {}, trade_from=start, btc=btc, indicators=frames)
        return trader, curve.loc[start:]

    def tick(self, msg: str) -> None:
        self.done += 1
        write_status(estat="provant", pct=round(self.done / self.total * 100, 1), missatge=msg, inici=self.started)

    def evaluate(self, strat: dict, start, end) -> dict:
        cfg = self.cfg_for(strat["tf"], strat["params"])
        trader, curve = self.run_cfg(cfg, start, end)
        self.tick(f"Provada: {strat['nom']}")
        st = curve_stats(curve)
        years = max((curve.index[-1] - curve.index[0]).days / 365, 0.01)
        tdf = pd.DataFrame(trader.trades)
        daily = curve.resample("1D").last().pct_change().dropna()
        monthly = curve.resample("ME").last().pct_change().dropna()
        halted_at = None
        if trader.halted and len(tdf):
            stops = tdf[tdf.reason == "aturada_emergencia"]
            if len(stops):
                halted_at = str(pd.Timestamp(stops.exit_ts.iloc[0]).date())
        return {
            "id": strat["id"], "nom": strat["nom"], "desc": strat["desc"],
            "temporalitat": cfg["timeframe"],
            "parametres": strat["params"],
            "rendiment_anual_pct": round(st["cagr"] * 100, 1),
            "rendiment_total_pct": round(st["total"] * 100, 1),
            "caiguda_maxima_pct": round(st["dd"] * 100, 1),
            # Si l'aturada d'emergència salta, el bot real s'hauria quedat aturat: no compleix
            "compleix_caiguda": st["dd"] <= self.dd_limit + 0.005 and not trader.halted,
            "aturada_el": halted_at,
            "sharpe": round(float(daily.mean() / daily.std() * math.sqrt(365)), 2) if len(daily) > 5 and daily.std() > 0 else None,
            "operacions": len(tdf),
            "operacions_any": round(len(tdf) / years, 0),
            "guanyadores_pct": round(float((tdf.pnl_eur > 0).mean() * 100), 1) if len(tdf) else None,
            # comissions respecte del capital mitjà (si el capital creix, les comissions en euros també)
            "comissions_pct_any": round(trader.fees_paid / float(curve.mean()) / years * 100, 1),
            "mesos_positius_pct": round(float((monthly > 0).mean() * 100), 0) if len(monthly) else None,
            "per_any": yearly(curve),
            "corba": ser(curve),
        }

    def robustness(self, start, end, btc_cagr: float) -> dict:
        """Totes les combinacions de la rotació, sense aturada d'emergència: l'avantatge en brut."""
        cells = []
        for m, n, s in itertools.product(*GRID.values()):
            params = {**ROT_BASE, "momentum_days": m, "top_n": n, "stop_atr": s}
            cfg = self.cfg_for("4h", params, kill_switch=False)
            _, curve = self.run_cfg(cfg, start, end)
            st = curve_stats(curve)
            cells.append({"momentum_days": m, "top_n": n, "stop_atr": s,
                          "rendiment_anual_pct": round(st["cagr"] * 100, 1), "caiguda_maxima_pct": round(st["dd"] * 100, 1)})
            self.tick(f"Prova de robustesa: momentum {m} dies, {n} valors, stop {s:g} ATR")
        cagrs = np.array([c["rendiment_anual_pct"] for c in cells])
        pos = int((cagrs > 0).sum())
        beat = int((cagrs > btc_cagr * 100).sum())
        share = pos / len(cells)
        if share >= 0.75 and np.median(cagrs) > 0:
            verdict, txt = "robusta", "Avantatge sòlid: la gran majoria de combinacions guanyen diners."
        elif share >= 0.5:
            verdict, txt = "fragil", "Avantatge feble: el resultat depèn molt dels paràmetres que triïs."
        else:
            verdict, txt = "sense_avantatge", "Sense avantatge clar: la majoria de combinacions no guanyen diners."
        return {"combinacions": cells, "total": len(cells), "positives": pos, "baten_btc": beat,
                "mediana_anual_pct": round(float(np.median(cagrs)), 1),
                "mediana_caiguda_pct": round(float(np.median([c["caiguda_maxima_pct"] for c in cells])), 1),
                "millor_anual_pct": round(float(cagrs.max()), 1), "pitjor_anual_pct": round(float(cagrs.min()), 1),
                "veredicte": verdict, "text": txt}

    def run(self) -> dict:
        tf4 = self.data["4h"]
        first = max(min(df.index[0] for df in tf4.values()), self.btc["4h"].index[0])
        start = (first + pd.Timedelta(days=WARMUP_DAYS)).ceil("D")
        end = max(df.index[-1] for df in tf4.values())
        self.total = len(STRATEGIES) + int(np.prod([len(v) for v in GRID.values()]))

        results = [self.evaluate(s, start, end) for s in STRATEGIES]

        btc = self.btc["4h"].loc[start:end]["close"]
        btc_curve = btc / btc.iloc[0] * 1000
        closes = pd.concat({s: df.loc[start:end]["close"] for s, df in tf4.items()}, axis=1)
        rets = closes.pct_change().mean(axis=1, skipna=True).fillna(0)   # els valors nous entren quan cotitzen
        basket = (1 + rets).cumprod() * 1000
        refs = []
        for k, nom, c in (("btc", "Comprar Bitcoin i no tocar res", btc_curve),
                          ("cartera", "Comprar els 12 valors a parts iguals i no tocar res", basket)):
            st = curve_stats(c)
            refs.append({"id": k, "nom": nom, "rendiment_anual_pct": round(st["cagr"] * 100, 1),
                         "caiguda_maxima_pct": round(st["dd"] * 100, 1), "per_any": yearly(c), "corba": ser(c)})
        btc_cagr = curve_stats(btc_curve)["cagr"]

        robust = self.robustness(start, end, btc_cagr)

        candidates = [r for r in results if r["rendiment_anual_pct"] > 0 and r["compleix_caiguda"] and r["operacions"] > 0]
        best = max(candidates, key=lambda r: r["rendiment_anual_pct"]) if candidates else None
        return {
            "versio": 2,
            "generat": datetime.now(timezone.utc).isoformat(),
            "periode_prova": {"des_de": str(start.date()), "fins": str(end.date())},
            "caiguda_maxima_acceptada_pct": round(self.dd_limit * 100),
            "comissio_pct": self.base_cfg["paper"]["fee_pct"],
            "estrategies": results,
            "referencies": refs,
            "robustesa": robust,
            "recomanada": best["id"] if best else None,
        }


def load_data(cfg: dict, synthetic: bool = False) -> tuple[dict, dict]:
    tfs = sorted({s["tf"] or cfg["timeframe"] for s in STRATEGIES})
    symbols = list(cfg["symbols"])
    data, btc = {}, {}
    if synthetic:
        import run_backtest as rb
        for tf in tfs:
            d = rb.synthetic(list(dict.fromkeys(symbols + ["BTC/EUR"])), tf, HISTORY_DAYS, seed=3)
            btc[tf] = d["BTC/EUR"]
            data[tf] = {s: d[s] for s in symbols}
        return data, btc
    cache = os.path.join(LAB_DIR, "dades")
    n, total = 0, len(tfs) * len(symbols)
    for tf in tfs:
        data[tf] = {}
        for s in symbols:
            write_status(estat="descarregant", pct=round(n / total * 100, 1),
                         missatge=f"Descarregant ~6 anys d'espelmes reals de {s} ({tf})")
            try:
                data[tf][s] = fetch_history(s, tf, HISTORY_DAYS, cache_dir=cache, quote="USDT")
            except Exception as e:
                log.warning("Salto %s (%s): %s", s, tf, e)
            n += 1
        btc[tf] = data[tf].get("BTC/EUR")
        if btc[tf] is None:
            btc[tf] = fetch_history("BTC/EUR", tf, HISTORY_DAYS, cache_dir=cache, quote="USDT")
    return data, btc


def main(cfg: dict, dd_limit_pct: float, synthetic: bool = False) -> dict:
    logging.getLogger("trader").setLevel(logging.CRITICAL)   # sense soroll: milers d'operacions simulades
    t0 = time.time()
    write_status(estat="descarregant", pct=0, missatge="Preparant la descàrrega de dades")
    try:
        data, btc = load_data(cfg, synthetic)
        res = Lab(cfg, data, btc, dd_limit_pct / 100).run()
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
