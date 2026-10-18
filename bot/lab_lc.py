"""Laboratori LLARG/CURT amb apalancament (futurs perpetus simulats) sobre ~6 anys de dades reals.

Model de costos (prudent):
  - Comissió de futurs 0,05% + lliscament 0,05% a cada compra o venda.
  - Finançament: els llargs paguen un 0,01% cada 8 hores (~11% anual). Als curts no se'ls
    compta cap ingrés per finançament, per ser prudents.
  - Liquidació: si, en algun moment d'una espelma de 4 hores, el compte cau per sota del 0,5%
    del valor de les posicions, es liquida i queda a zero.
  - Aturada d'emergència: com al bot real, si la caiguda des del màxim supera el límit triat,
    es tanca tot (al pitjor preu de l'espelma) i no es torna a entrar.

Les decisions es prenen amb el tancament diari (00:00 UTC) i s'executen al preu d'obertura de la
següent espelma. A més, cada dilluns es reajusta la mida de les posicions a l'apalancament objectiu.

Criteri d'èxit, FIXAT ABANS de la prova:
  1. Guanyar diners, no passar de la caiguda acceptada, no fer saltar l'aturada ni ser liquidada.
  2. Guanyar més que mantenir Bitcoin per cada euro de caiguda (rendiment anual / caiguda màxima).
  3. Que la seva família d'estratègies superi la prova de robustesa (>= 75% de combinacions positives).
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import backtest as bt
from .data import fetch_history
from .lab import HISTORY_DAYS, curve_stats, yearly
from .paths import LAB_DIR

log = logging.getLogger("lab_lc")

STATUS_FILE = os.path.join(LAB_DIR, "estat_lc.json")
RESULTS_FILE = os.path.join(LAB_DIR, "resultats_lc.json")

FEE = 0.0005
SLIP = 0.0005
FUNDING_8H = 0.0001
MAINT_MARGIN = 0.005
LEVERAGES = [1, 2, 3]
WARMUP_DAYS = 255
CAPITAL = 1000.0

STRATEGIES = [
    {"id": "btc_llarg", "nom": "Bitcoin: llarg o efectiu", "kind": "trend", "assets": ["BTC/EUR"], "short": False, "sma": 200,
     "desc": "Llarg en Bitcoin quan és per sobre de la seva mitjana de 200 dies, efectiu quan és per sota. Serveix de comparació: és la mateixa idea sense curts."},
    {"id": "btc_lc", "nom": "Bitcoin: llarg o curt", "kind": "trend", "assets": ["BTC/EUR"], "short": True, "sma": 200,
     "desc": "Llarg en Bitcoin quan és per sobre de la seva mitjana de 200 dies, curt quan és per sota."},
    {"id": "btceth_lc", "nom": "Bitcoin i Ether: llarg o curt", "kind": "trend", "assets": ["BTC/EUR", "ETH/EUR"], "short": True, "sma": 200,
     "desc": "Meitat del capital a cadascun. Cada un, llarg si és per sobre de la seva mitjana de 200 dies i curt si és per sota."},
    {"id": "rot_lc", "nom": "Rotació llarg/curt", "kind": "rot", "top": 3, "mom": 30, "freq": "W", "short": True,
     "desc": "Cada setmana: si el Bitcoin és per sobre de la seva mitjana de 200 dies, llarg en els 3 valors que més han pujat en 30 dies; si és per sota, curt en els 3 que més han baixat."},
]
TREND_GRID = {"sma": [50, 100, 150, 200, 250],
              "univers": [("Bitcoin", ["BTC/EUR"]), ("BTC + ETH", ["BTC/EUR", "ETH/EUR"]),
                          ("4 grans", ["BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR"])]}
ROT_GRID = {"mom": [14, 30, 60], "top": [2, 3, 4], "freq": ["D", "W"]}


def write_status(**kw) -> None:
    os.makedirs(LAB_DIR, exist_ok=True)
    kw["actualitzat"] = datetime.now(timezone.utc).isoformat()
    kw["pid"] = os.getpid()
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kw, f, default=str)
    os.replace(tmp, STATUS_FILE)


# ------------------------------------------------------------------ dades
class Panel:
    """Preus de tots els valors alineats en espelmes de 4 hores (NaN abans que cotitzin)."""

    def __init__(self, data: dict[str, pd.DataFrame]):
        self.symbols = list(data)
        idx = sorted(set().union(*[df.index for df in data.values()]))
        self.idx = pd.DatetimeIndex(idx)

        def mat(col):
            m = pd.concat({s: df[col] for s, df in data.items()}, axis=1).reindex(self.idx)
            return m

        close = mat("close")
        listed = close.notna().cummax()                      # ja cotitzava
        close = close.ffill().where(listed)
        self.C = close.to_numpy(float)
        self.O = mat("open").where(listed).fillna(close.shift()).fillna(close).to_numpy(float)
        self.H = mat("high").where(listed).fillna(close).to_numpy(float)
        self.L = mat("low").where(listed).fillna(close).to_numpy(float)
        self.daily = close.resample("1D").last()             # tancament de cada dia (00:00 UTC)
        self.col = {s: i for i, s in enumerate(self.symbols)}
        self.bar_of = {t: i for i, t in enumerate(self.idx)}
        self.hours = np.asarray(self.idx.hour)
        self.monday0 = np.asarray((self.idx.dayofweek == 0) & (self.idx.hour == 0))


# ------------------------------------------------------------------ senyals (pes objectiu per dia)
def trend_targets(P: Panel, assets: list, sma: int, short: bool) -> pd.DataFrame:
    w = pd.DataFrame(0.0, index=P.daily.index, columns=P.symbols)
    cols = [a for a in assets if a in P.col]
    for a in cols:
        c = P.daily[a]
        ma = c.rolling(sma, min_periods=sma).mean()
        sig = np.where(c > ma, 1.0, -1.0 if short else 0.0)
        sig = np.where(ma.isna() | c.isna(), 0.0, sig)
        w[a] = sig / len(cols)
    return w


def rot_targets(P: Panel, mom: int, top: int, freq: str, short: bool) -> pd.DataFrame:
    d = P.daily
    m = d / d.shift(mom) - 1
    btc = d["BTC/EUR"]
    up = btc > btc.rolling(200, min_periods=200).mean()
    w = pd.DataFrame(0.0, index=d.index, columns=P.symbols)
    for day in d.index:
        row = m.loc[day].dropna()
        if row.empty or pd.isna(btc.loc[day]):
            continue
        if up.loc[day]:
            pick = row[row > 0].nlargest(top)
            w.loc[day, pick.index] = 1.0 / top
        elif short:
            pick = row[row < 0].nsmallest(top)
            w.loc[day, pick.index] = -1.0 / top
    if freq == "W":                                         # només canvia el diumenge (efectiu dilluns)
        w = w.where(pd.Series(w.index.dayofweek == 6, index=w.index), np.nan).ffill().fillna(0.0)
    return w


# ------------------------------------------------------------------ simulador de futurs
def simulate(P: Panel, targets: pd.DataFrame, lev: float, start, dd_limit: float | None) -> dict:
    T, N = P.C.shape
    sched = {}
    prev = None
    for day, row in targets.iterrows():                    # decisió al tancament del dia -> obertura següent
        v = row.to_numpy(float)
        if prev is not None and np.array_equal(v, prev):
            continue
        b = P.bar_of.get(day + pd.Timedelta(days=1))
        if b is not None:
            sched[b] = v
        prev = v
    t0 = int(P.idx.searchsorted(pd.Timestamp(start)))
    # pes vigent a l'inici (l'últim canvi programat abans de començar)
    cur_w = np.zeros(N)
    for b in sorted(k for k in sched if k <= t0):
        cur_w = sched[b]

    qty = np.zeros(N)
    entry = np.zeros(N)
    coll = CAPITAL
    peak = CAPITAL
    fees = funding = 0.0
    trades = 0
    stopped = None            # ("liquidat"|"aturat", data)
    curve = np.full(T - t0, np.nan)

    for t in range(t0, T):
        o, h, lo, c = P.O[t], P.H[t], P.L[t], P.C[t]
        if stopped is None:
            rebalance = t == t0 or t in sched or (P.monday0[t] and cur_w.any())
            if t in sched:
                cur_w = sched[t]
            if rebalance:
                px = np.where(np.isnan(o), c, o)
                ok = ~np.isnan(px)
                eq = coll + np.nansum(np.where(qty != 0, qty * (px - entry), 0.0))
                coll, entry = eq, np.where(ok, px, 0.0)
                new_qty = np.where(ok & (cur_w != 0), eq * lev * cur_w / np.where(ok, px, 1.0), 0.0)
                traded = np.abs(new_qty - qty) * np.where(ok, px, 0.0)
                cost = float(traded.sum()) * (FEE + SLIP)
                coll -= cost
                fees += cost
                trades += int((traded > 1.0).sum())
                qty = new_qty
            if qty.any():
                if P.hours[t] in (0, 8, 16):                # finançament cada 8 hores (només paguen els llargs)
                    f = float(np.nansum(np.maximum(qty, 0) * c)) * FUNDING_8H
                    coll -= f
                    funding += f
                worst_px = np.where(qty > 0, lo, h)
                worst_eq = coll + np.nansum(np.where(qty != 0, qty * (worst_px - entry), 0.0))
                notional = float(np.nansum(np.abs(qty) * worst_px))
                if worst_eq <= MAINT_MARGIN * notional:
                    coll, qty = 0.0, np.zeros(N)
                    stopped = ("liquidat", str(P.idx[t].date()))
                elif dd_limit is not None and worst_eq < peak * (1 - dd_limit):
                    coll = max(worst_eq - notional * (FEE + SLIP), 0.0)
                    fees += notional * (FEE + SLIP)
                    qty = np.zeros(N)
                    stopped = ("aturat", str(P.idx[t].date()))
        eq = coll + np.nansum(np.where(qty != 0, qty * (c - entry), 0.0))
        peak = max(peak, eq)
        curve[t - t0] = eq

    s = pd.Series(curve, index=P.idx[t0:])
    years = max((s.index[-1] - s.index[0]).days / 365, 0.01)
    st = curve_stats(s.clip(lower=1e-9))
    if stopped and stopped[0] == "liquidat":
        st["cagr"], st["dd"] = -1.0, 1.0
    return {"curve": s, "cagr": st["cagr"], "dd": st["dd"], "total": st["total"], "stopped": stopped,
            "trades": trades, "fees_pct_any": fees / float(s.mean() or 1) / years * 100,
            "funding_pct_any": funding / float(s.mean() or 1) / years * 100}


def calmar(cagr: float, dd: float) -> float:
    return cagr / dd if dd > 0.001 else (cagr * 100 if cagr > 0 else 0.0)


def ser(curve: pd.Series) -> list:
    return [[str(t), round(float(v), 2)] for t, v in bt._downsample_series(curve.resample("1D").last().dropna()).items()]


def verdict(cagrs: list) -> tuple[str, str]:
    a = np.array(cagrs)
    share = float((a > 0).mean()) if len(a) else 0.0
    if share >= 0.75 and np.median(a) > 0:
        return "robusta", "Avantatge sòlid: la gran majoria de combinacions guanyen diners."
    if share >= 0.5:
        return "fragil", "Avantatge feble: el resultat depèn molt dels paràmetres que triïs."
    return "sense_avantatge", "Sense avantatge clar: la majoria de combinacions no guanyen diners."


# ------------------------------------------------------------------ laboratori
def run(P: Panel, dd_limit: float, progress=lambda pct, msg: None) -> dict:
    btc_first = P.daily["BTC/EUR"].first_valid_index()
    start = (btc_first + pd.Timedelta(days=WARMUP_DAYS)).ceil("D")
    n_trend = len(TREND_GRID["sma"]) * len(TREND_GRID["univers"])
    n_rot = int(np.prod([len(v) for v in ROT_GRID.values()]))
    total = len(STRATEGIES) * len(LEVERAGES) + n_trend + n_rot
    done = 0

    def tick(msg):
        nonlocal done
        done += 1
        progress(round(done / total * 100, 1), msg)

    def targets_for(s):
        if s["kind"] == "trend":
            return trend_targets(P, s["assets"], s["sma"], s["short"])
        return rot_targets(P, s["mom"], s["top"], s["freq"], s["short"])

    # Referència: mantenir Bitcoin (sense comissions ni finançament)
    btc = P.daily["BTC/EUR"].loc[start:].dropna()
    btc_curve = btc / btc.iloc[0] * CAPITAL
    btc_st = curve_stats(btc_curve)
    btc_calmar = calmar(btc_st["cagr"], btc_st["dd"])
    basket_r = P.daily.loc[start:].pct_change().mean(axis=1, skipna=True).fillna(0)
    basket = (1 + basket_r).cumprod() * CAPITAL
    refs = []
    for k, nom, c in (("btc", "Comprar Bitcoin i no tocar res", btc_curve),
                      ("cartera", "Comprar els 12 valors a parts iguals i no tocar res", basket)):
        st = curve_stats(c)
        refs.append({"id": k, "nom": nom, "rendiment_anual_pct": round(st["cagr"] * 100, 1),
                     "caiguda_maxima_pct": round(st["dd"] * 100, 1), "per_any": yearly(c), "corba": ser(c)})

    strategies = []
    for s in STRATEGIES:
        tg = targets_for(s)
        row = {"id": s["id"], "nom": s["nom"], "desc": s["desc"], "curts": s["short"],
               "familia": "tendencia" if s["kind"] == "trend" else "rotacio", "per_apalancament": {}}
        for lev in LEVERAGES:
            r = simulate(P, tg, lev, start, dd_limit)
            tick(f"{s['nom']} x{lev}")
            cell = {"rendiment_anual_pct": round(r["cagr"] * 100, 1), "caiguda_maxima_pct": round(r["dd"] * 100, 1),
                    "aturada": r["stopped"], "operacions": r["trades"],
                    "comissions_pct_any": round(r["fees_pct_any"], 1), "financament_pct_any": round(r["funding_pct_any"], 1),
                    "calmar": round(calmar(r["cagr"], r["dd"]), 2)}
            cell["compleix"] = bool(r["cagr"] > 0 and r["stopped"] is None and r["dd"] <= dd_limit + 0.005)
            if lev == 1:
                row["per_any"] = yearly(r["curve"])
                row["corba"] = ser(r["curve"])
            row["per_apalancament"][str(lev)] = cell
        strategies.append(row)

    # Robustesa (x1, sense aturada d'emergència): l'avantatge en brut
    trend_cells = []
    for sma, (uname, assets) in itertools.product(TREND_GRID["sma"], TREND_GRID["univers"]):
        r = simulate(P, trend_targets(P, assets, sma, True), 1, start, None)
        trend_cells.append({"sma": sma, "univers": uname, "rendiment_anual_pct": round(r["cagr"] * 100, 1),
                            "caiguda_maxima_pct": round(r["dd"] * 100, 1), "bat_btc": calmar(r["cagr"], r["dd"]) > btc_calmar})
        tick(f"Robustesa tendència: mitjana {sma} dies, {uname}")
    rot_cells = []
    for mom, top, freq in itertools.product(*ROT_GRID.values()):
        r = simulate(P, rot_targets(P, mom, top, freq, True), 1, start, None)
        rot_cells.append({"mom": mom, "top": top, "freq": freq, "rendiment_anual_pct": round(r["cagr"] * 100, 1),
                          "caiguda_maxima_pct": round(r["dd"] * 100, 1), "bat_btc": calmar(r["cagr"], r["dd"]) > btc_calmar})
        tick(f"Robustesa rotació: {mom} dies, {top} valors, {'diari' if freq == 'D' else 'setmanal'}")

    robust = {}
    for fam, cells in (("tendencia", trend_cells), ("rotacio", rot_cells)):
        v, txt = verdict([c["rendiment_anual_pct"] for c in cells])
        cg = [c["rendiment_anual_pct"] for c in cells]
        robust[fam] = {"combinacions": cells, "total": len(cells), "positives": int(sum(x > 0 for x in cg)),
                       "baten_btc": int(sum(c["bat_btc"] for c in cells)), "mediana_anual_pct": round(float(np.median(cg)), 1),
                       "mediana_caiguda_pct": round(float(np.median([c["caiguda_maxima_pct"] for c in cells])), 1),
                       "veredicte": v, "text": txt}

    # Millor candidata i criteri d'èxit
    cands = [(s, lev, s["per_apalancament"][str(lev)]) for s in strategies for lev in LEVERAGES
             if s["per_apalancament"][str(lev)]["compleix"]]
    best = max(cands, key=lambda x: x[2]["rendiment_anual_pct"]) if cands else None
    crit = None
    if best:
        s, lev, cell = best
        c1 = True
        c2 = cell["calmar"] > btc_calmar
        c3 = robust[s["familia"]]["veredicte"] == "robusta" and s["curts"]
        if not s["curts"]:
            c3 = False
        crit = {"id": s["id"], "nom": s["nom"], "apalancament": lev, "cel": cell,
                "punts": [{"text": f"Guanya diners sense passar del {round(dd_limit * 100)} % de caiguda", "ok": c1},
                          {"text": "Guanya més que mantenir Bitcoin per cada euro de caiguda "
                                   f"({cell['calmar']:.2f} contra {btc_calmar:.2f})", "ok": c2},
                          {"text": "La seva família d'estratègies llarg/curt supera la prova de robustesa", "ok": c3}],
                "superat": bool(c1 and c2 and c3)}
    return {
        "versio": 1,
        "generat": datetime.now(timezone.utc).isoformat(),
        "periode_prova": {"des_de": str(start.date()), "fins": str(P.idx[-1].date())},
        "caiguda_maxima_acceptada_pct": round(dd_limit * 100),
        "costos": {"comissio_pct": FEE * 100, "lliscament_pct": SLIP * 100, "financament_8h_pct": FUNDING_8H * 100,
                   "marge_manteniment_pct": MAINT_MARGIN * 100},
        "apalancaments": LEVERAGES,
        "btc_calmar": round(btc_calmar, 2),
        "estrategies": strategies,
        "referencies": refs,
        "robustesa": robust,
        "candidata": crit,
    }


def load(cfg: dict, synthetic: bool = False) -> Panel:
    symbols = list(dict.fromkeys(list(cfg["symbols"]) + ["BTC/EUR", "ETH/EUR"]))
    if synthetic:
        import run_backtest as rb
        return Panel(rb.synthetic(symbols, "4h", HISTORY_DAYS, seed=5))
    data = {}
    cache = os.path.join(LAB_DIR, "dades")
    for k, s in enumerate(symbols):
        write_status(estat="descarregant", pct=round(k / len(symbols) * 100, 1),
                     missatge=f"Descarregant ~6 anys d'espelmes reals de {s} (4h)")
        try:
            data[s] = fetch_history(s, "4h", HISTORY_DAYS, cache_dir=cache, quote="USDT")
        except Exception as e:
            log.warning("Salto %s: %s", s, e)
    if "BTC/EUR" not in data:
        raise RuntimeError("No s'han pogut descarregar les dades de Bitcoin")
    return Panel(data)


def main(cfg: dict, dd_limit_pct: float, synthetic: bool = False) -> dict:
    t0 = time.time()
    write_status(estat="descarregant", pct=0, missatge="Preparant la descàrrega de dades")
    try:
        P = load(cfg, synthetic)
        res = run(P, dd_limit_pct / 100,
                  lambda pct, msg: write_status(estat="provant", pct=pct, missatge=msg))
        res["durada_min"] = round((time.time() - t0) / 60, 1)
        res["dades_sintetiques"] = synthetic
        tmp = RESULTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(res, f, default=str)
        os.replace(tmp, RESULTS_FILE)
        write_status(estat="fet", pct=100, missatge=f"Prova acabada en {res['durada_min']} minuts")
        log.info("Laboratori llarg/curt acabat en %.1f min", res["durada_min"])
        return res
    except Exception as e:
        log.exception("El laboratori llarg/curt ha fallat")
        write_status(estat="error", pct=0, missatge=f"La prova ha fallat: {e}")
        raise
