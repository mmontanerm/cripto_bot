"""Laboratori de CURT TERMINI: moltes operacions petites amb espelmes de 5 minuts (últims ~6 mesos reals).

Tres famílies clàssiques d'operativa ràpida, totes sense apalancament i amb stop a cada operació:
  - Reversió a la mitjana: quan el preu s'allunya molt de la seva mitjana (bandes de Bollinger),
    aposta que hi tornarà (llarg si ha caigut massa, curt si ha pujat massa).
  - Ruptura curta: quan el preu trenca el màxim (o mínim) de les últimes hores, hi entra
    buscant un objectiu petit, amb un stop igual de petit.
  - Graella: ordres de compra escalonades per sota del preu i venda de cada lot un X% més amunt.
    Si el preu cau per sota de la graella, ho ven tot (stop) i la torna a centrar.

Execució: senyals al tancament de l'espelma i entrada a l'obertura de la següent. Stops i objectius
dins l'espelma; si en una mateixa espelma es podien tocar tots dos, se suposa el stop (prudent).
Mida fixa per operació (sense interès compost), de manera que les decisions no depenen dels costos:
es calcula una vegada la corba BRUTA i el volum negociat, i se li apliquen diversos escenaris de
costos. Així es veu exactament amb quins costos l'estratègia deixa de guanyar.

Criteri d'èxit, FIXAT ABANS de la prova (amb costos realistes: futurs amb ordres a mercat):
  1. Guanyar diners en el període.
  2. No passar de la caiguda acceptada ni fer saltar l'aturada d'emergència.
  3. Que la família superi la prova de robustesa (>= 75% de combinacions positives).
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
from .paths import LAB_DIR

log = logging.getLogger("lab_ct")

STATUS_FILE = os.path.join(LAB_DIR, "estat_ct.json")
RESULTS_FILE = os.path.join(LAB_DIR, "resultats_ct.json")

TF = "5m"
DAYS = 183
WARMUP_BARS = 300
CAPITAL = 1000.0
SYMBOLS = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "DOGE/EUR", "ADA/EUR"]   # els més líquids

# Cost per costat (compra o venda), comissió + lliscament
COSTS = [
    {"id": "zero", "nom": "Sense costos", "pct": 0.0,
     "desc": "Ideal i impossible: mostra si l'estratègia té avantatge abans de pagar res."},
    {"id": "maker", "nom": "Futurs, ordres limitades", "pct": 0.02,
     "desc": "Comissió de 0,02% (maker) i sense lliscament. Optimista: suposa que totes les ordres limitades s'executen."},
    {"id": "taker", "nom": "Futurs, ordres a mercat", "pct": 0.07,
     "desc": "Comissió de 0,05% (taker) i 0,02% de lliscament. És l'escenari realista per a un bot."},
    {"id": "spot", "nom": "Kraken spot (com ara)", "pct": 0.45,
     "desc": "Comissió de 0,40% i 0,05% de lliscament: el que paga ara el bot en simulació."},
]
REALISTIC = "taker"

FAMILIES = {
    "reversio": {"nom": "Reversió a la mitjana",
                 "desc": "Si el preu s'allunya molt de la seva mitjana (bandes de Bollinger), aposta que hi tornarà: llarg si ha caigut massa, curt si ha pujat massa. Surt quan torna a la mitjana, al cap de 4 hores o si toca el stop.",
                 "grid": {"n": [20, 50], "k": [2.0, 2.5, 3.0], "sl": [0.01, 0.02]},
                 "default": {"n": 20, "k": 2.5, "sl": 0.02}},
    "ruptura": {"nom": "Ruptura curta",
                "desc": "Si el preu trenca el màxim (o mínim) de les últimes hores, hi entra en aquella direcció buscant un objectiu petit, amb un stop igual de petit. Surt com a molt al cap de 2 hores.",
                "grid": {"n": [12, 48], "tp": [0.003, 0.006, 0.01], "sl": [0.003, 0.006]},
                "default": {"n": 48, "tp": 0.006, "sl": 0.006}},
    "graella": {"nom": "Graella (grid)",
                "desc": "Ordres de compra escalonades per sota del preu; cada lot es ven un esglaó més amunt. Si el preu cau per sota de la graella, ho ven tot i la torna a centrar.",
                "grid": {"g": [0.003, 0.005, 0.01], "levels": [5, 10]},
                "default": {"g": 0.005, "levels": 10}},
}


def write_status(**kw) -> None:
    os.makedirs(LAB_DIR, exist_ok=True)
    kw["actualitzat"] = datetime.now(timezone.utc).isoformat()
    kw["pid"] = os.getpid()
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kw, f, default=str)
    os.replace(tmp, STATUS_FILE)


# ------------------------------------------------------------------ motors (un valor, mida fixa)
class Run:
    """Resultat d'un valor: P&L brut marcat a mercat i volum negociat acumulat, per espelma."""

    def __init__(self, n):
        self.gross = np.zeros(n)
        self.turn = np.zeros(n)
        self.trades: list = []          # (rendiment brut de l'operació, notional d'anada i tornada)


def _directional(df: pd.DataFrame, notional: float, entry_sig, exit_fn, tp: float | None, sl: float,
                 max_bars: int) -> Run:
    """Motor comú per a reversió i ruptura: una posició a la vegada, llarg o curt."""
    O, H, L, C = (df[k].tolist() for k in ("open", "high", "low", "close"))
    sig = entry_sig
    n = len(C)
    r = Run(n)
    pos = 0
    entry = qty = 0.0
    ebar = 0
    pend_in = 0
    pend_out = False
    realized = turnover = 0.0
    for i in range(WARMUP_BARS, n):
        o, h, lo, c = O[i], H[i], L[i], C[i]
        if pend_out and pos:                                   # sortida programada: a l'obertura
            realized += pos * qty * (o - entry)
            turnover += qty * o
            r.trades.append((pos * (o - entry) / entry, notional + qty * o))
            pos, pend_out = 0, False
        if pend_in and not pos:                                # entrada programada: a l'obertura
            pos, entry, qty, ebar = pend_in, o, notional / o, i
            turnover += notional
        pend_in = 0
        if pos:
            stop_px = entry * (1 - pos * sl)
            tp_px = entry * (1 + pos * tp) if tp else None
            exit_px = None
            if (pos > 0 and lo <= stop_px) or (pos < 0 and h >= stop_px):
                exit_px = min(o, stop_px) if pos > 0 else max(o, stop_px)
            elif tp_px is not None and ((pos > 0 and h >= tp_px) or (pos < 0 and lo <= tp_px)):
                exit_px = max(o, tp_px) if pos > 0 else min(o, tp_px)
            if exit_px is not None:
                realized += pos * qty * (exit_px - entry)
                turnover += qty * exit_px
                r.trades.append((pos * (exit_px - entry) / entry, notional + qty * exit_px))
                pos = 0
            elif i - ebar + 1 >= max_bars or exit_fn(i, pos):
                pend_out = True
        if not pos and not pend_out:
            pend_in = sig[i]
        r.gross[i] = realized + (pos * qty * (c - entry) if pos else 0.0)
        r.turn[i] = turnover
    return r


def run_reversio(df, notional, n, k, sl):
    c = df["close"]
    ma = c.rolling(n).mean()
    sd = c.rolling(n).std()
    up, dn = (ma + k * sd).tolist(), (ma - k * sd).tolist()
    C, MA = c.tolist(), ma.tolist()
    sig = [1 if C[i] < dn[i] else (-1 if C[i] > up[i] else 0) for i in range(len(C))]
    return _directional(df, notional, sig, lambda i, pos: (pos > 0 and C[i] >= MA[i]) or (pos < 0 and C[i] <= MA[i]),
                        None, sl, 48)


def run_ruptura(df, notional, n, tp, sl):
    hi = df["high"].rolling(n).max().shift(1).tolist()
    lo = df["low"].rolling(n).min().shift(1).tolist()
    C = df["close"].tolist()
    sig = [1 if C[i] > hi[i] else (-1 if C[i] < lo[i] else 0) for i in range(len(C))]
    return _directional(df, notional, sig, lambda i, pos: False, tp, sl, 24)


def run_graella(df, notional, g, levels):
    O, H, L, C = (df[k].tolist() for k in ("open", "high", "low", "close"))
    n = len(C)
    r = Run(n)
    lot = notional / levels
    realized = turnover = 0.0
    center = C[WARMUP_BARS]
    lv = [center * (1 - g * (j + 1)) for j in range(levels)]
    held: dict[int, tuple[float, float, int]] = {}             # nivell -> (preu, quantitat, espelma)
    for i in range(WARMUP_BARS, n):
        o, h, lo, c = O[i], H[i], L[i], C[i]
        for j in list(held):                                    # vendes: un esglaó més amunt
            px, q, b = held[j]
            target = lv[j] * (1 + g)
            if b < i and h >= target:
                sell = max(o, target)
                realized += q * (sell - px)
                turnover += q * sell
                r.trades.append(((sell - px) / px, lot + q * sell))
                del held[j]
        for j in range(levels):                                 # compres: si el preu toca el nivell
            if j not in held and lo <= lv[j]:
                px = min(o, lv[j])
                held[j] = (px, lot / px, i)
                turnover += lot
        if c < lv[-1] * (1 - 2 * g):                            # stop: el preu ha sortit per sota
            for j, (px, q, b) in held.items():
                realized += q * (c - px)
                turnover += q * c
                r.trades.append(((c - px) / px, lot + q * c))
            held.clear()
            center = c
            lv = [center * (1 - g * (j + 1)) for j in range(levels)]
        elif not held and c > center * (1 + 2 * g):             # el preu ha pujat: recentra
            center = c
            lv = [center * (1 - g * (j + 1)) for j in range(levels)]
        r.gross[i] = realized + sum(q * (c - px) for px, q, b in held.values())
        r.turn[i] = turnover
    return r


ENGINES = {"reversio": run_reversio, "ruptura": run_ruptura, "graella": run_graella}


# ------------------------------------------------------------------ cartera i mètriques
def portfolio(data: dict, fam: str, params: dict) -> dict:
    notional = CAPITAL / len(data)
    gross = turn = None
    trades = []
    for s, df in data.items():
        r = ENGINES[fam](df, notional, **params)
        g = pd.Series(r.gross, index=df.index)
        t = pd.Series(r.turn, index=df.index)
        gross = g if gross is None else gross + g          # tots els valors comparteixen el mateix índex
        turn = t if turn is None else turn + t
        trades.extend(r.trades)
    idx = gross.index[WARMUP_BARS:]
    return {"gross": gross.loc[idx].to_numpy(), "turn": turn.loc[idx].to_numpy(), "idx": idx, "trades": trades}


def net(p: dict, cost_pct: float, dd_limit: float | None) -> dict:
    eq = CAPITAL + p["gross"] - cost_pct / 100 * p["turn"]
    stopped = None
    ruin = np.nonzero(eq <= 0)[0]                     # amb mida fixa, les pèrdues podrien superar el capital:
    if len(ruin):                                      # el compte s'acaba quan arriba a zero
        eq = eq.copy()
        eq[int(ruin[0]):] = 0.0
    if dd_limit is not None:
        peak = np.maximum.accumulate(eq)
        hit = np.nonzero(eq < peak * (1 - dd_limit))[0]
        if len(hit):
            k = int(hit[0])
            eq = eq.copy()
            eq[k:] = eq[k]
            stopped = str(p["idx"][k].date())
    peak = np.maximum.accumulate(eq)
    dd = float(-(eq / peak - 1).min())
    if len(ruin) and stopped is None:
        stopped = str(p["idx"][int(ruin[0])].date())
    days = max((p["idx"][-1] - p["idx"][0]).total_seconds() / 86400, 1)
    total = eq[-1] / CAPITAL - 1
    annual = (1 + total) ** (365 / days) - 1 if total > -1 else -1.0
    tr = p["trades"]
    weeks = days / 7
    wins = sum(1 for g, nt in tr if g - 2 * cost_pct / 100 > 0)
    return {"eq": eq, "total": total, "annual": annual, "dd": dd, "stopped": stopped,
            "trades_week": len(tr) / weeks, "win_pct": wins / len(tr) * 100 if tr else None}


def ser(idx, eq) -> list:
    s = pd.Series(eq, index=idx).resample("6h").last().dropna()
    return [[str(t), round(float(v), 2)] for t, v in bt._downsample_series(s).items()]


def verdict(vals: list) -> tuple[str, str]:
    a = np.array(vals)
    share = float((a > 0).mean()) if len(a) else 0.0
    if share >= 0.75 and np.median(a) > 0:
        return "robusta", "Avantatge sòlid amb costos realistes: la gran majoria de combinacions guanyen diners."
    if share >= 0.5:
        return "fragil", "Avantatge feble: amb costos realistes, el resultat depèn molt dels paràmetres."
    return "sense_avantatge", "Sense avantatge amb costos realistes: la majoria de combinacions perden diners."


def run(data: dict, dd_limit: float, progress=lambda pct, msg: None) -> dict:
    combos = {f: [dict(zip(v["grid"], x)) for x in itertools.product(*v["grid"].values())] for f, v in FAMILIES.items()}
    total = sum(len(c) for c in combos.values())
    done = 0
    families, robust = [], {}
    for fam, spec in FAMILIES.items():
        cells = []
        default_res = None
        for params in combos[fam]:
            p = portfolio(data, fam, params)
            done += 1
            progress(round(done / total * 100, 1), f"{spec['nom']}: {params_text(fam, params)}")
            real = net(p, next(c["pct"] for c in COSTS if c["id"] == REALISTIC), None)
            cells.append({**params, "rendiment_pct": round(float(real["total"]) * 100, 1), "caiguda_pct": round(real["dd"] * 100, 1),
                          "etiqueta": params_text(fam, params)})
            if params == spec["default"]:
                default_res = p
        p = default_res
        gross_per_trade = float(np.mean([g for g, nt in p["trades"]]) * 100) if p["trades"] else 0.0
        scen = {}
        curve = None
        for c in COSTS:
            r = net(p, c["pct"], dd_limit)
            scen[c["id"]] = {"rendiment_pct": round(float(r["total"]) * 100, 1), "anual_pct": round(float(r["annual"]) * 100, 1),
                             "caiguda_pct": round(r["dd"] * 100, 1), "aturada": r["stopped"],
                             "guanyadores_pct": None if r["win_pct"] is None else round(r["win_pct"], 0)}
            if c["id"] == REALISTIC:
                curve = ser(p["idx"], r["eq"])
        weeks = max((p["idx"][-1] - p["idx"][0]).days / 7, 1)
        v, txt = verdict([x["rendiment_pct"] for x in cells])
        robust[fam] = {"combinacions": cells, "total": len(cells), "positives": int(sum(x["rendiment_pct"] > 0 for x in cells)),
                       "mediana_pct": round(float(np.median([x["rendiment_pct"] for x in cells])), 1), "veredicte": v, "text": txt}
        families.append({"id": fam, "nom": spec["nom"], "desc": spec["desc"], "parametres": params_text(fam, spec["default"]),
                         "operacions_setmana": round(len(p["trades"]) / weeks),
                         "guany_brut_operacio_pct": round(gross_per_trade, 3),
                         "cost_anada_tornada_pct": {c["id"]: round(2 * c["pct"], 2) for c in COSTS},
                         "escenaris": scen, "corba": curve})

    any_df = next(iter(data.values()))
    idx = any_df.index[WARMUP_BARS:]
    btc = data["BTC/EUR"]["close"].reindex(idx).ffill()
    btc_eq = (btc / btc.iloc[0] * CAPITAL).to_numpy()
    btc_dd = float(-(btc_eq / np.maximum.accumulate(btc_eq) - 1).min())

    cands = []
    for f in families:
        sc = f["escenaris"][REALISTIC]
        ok1 = bool(sc["rendiment_pct"] > 0)
        ok2 = bool(sc["aturada"] is None and sc["caiguda_pct"] <= dd_limit * 100 + 0.5)
        ok3 = bool(robust[f["id"]]["veredicte"] == "robusta")
        f["criteri"] = [{"text": "Guanya diners amb costos realistes (futurs, ordres a mercat)", "ok": ok1},
                        {"text": f"No passa del {round(dd_limit * 100)} % de caiguda ni fa saltar l'aturada", "ok": ok2},
                        {"text": "Supera la prova de robustesa amb costos realistes", "ok": ok3}]
        f["superat"] = bool(ok1 and ok2 and ok3)
        if f["superat"]:
            cands.append(f)
    best = max(cands, key=lambda f: f["escenaris"][REALISTIC]["rendiment_pct"]) if cands else None
    return {
        "versio": 1,
        "generat": datetime.now(timezone.utc).isoformat(),
        "periode_prova": {"des_de": str(idx[0].date()), "fins": str(idx[-1].date())},
        "temporalitat": TF, "valors": [s.replace("/EUR", "") for s in data],
        "caiguda_maxima_acceptada_pct": round(dd_limit * 100),
        "costos": COSTS, "realista": REALISTIC,
        "families": families, "robustesa": robust,
        "btc": {"rendiment_pct": round(float(btc_eq[-1] / CAPITAL - 1) * 100, 1), "caiguda_pct": round(btc_dd * 100, 1),
                "corba": ser(idx, btc_eq)},
        "guanyadora": best["id"] if best else None,
    }


def params_text(fam: str, p: dict) -> str:
    if fam == "reversio":
        return f"mitjana {p['n']} espelmes, {p['k']:g} desviacions, stop {p['sl'] * 100:g} %"
    if fam == "ruptura":
        return f"rang de {p['n'] * 5 / 60:g} h, objectiu {p['tp'] * 100:g} %, stop {p['sl'] * 100:g} %"
    return f"esglaons del {p['g'] * 100:g} %, {p['levels']} nivells"


def load(synthetic: bool = False) -> dict:
    if synthetic:
        import run_backtest as rb
        return rb.synthetic(SYMBOLS, TF, DAYS, seed=9)
    data = {}
    cache = os.path.join(LAB_DIR, "dades")
    for k, s in enumerate(SYMBOLS):
        write_status(estat="descarregant", pct=round(k / len(SYMBOLS) * 100, 1),
                     missatge=f"Descarregant 6 mesos d'espelmes de 5 minuts de {s}")
        try:
            data[s] = fetch_history(s, TF, DAYS, cache_dir=cache, quote="USDT")
        except Exception as e:
            log.warning("Salto %s: %s", s, e)
    if "BTC/EUR" not in data:
        raise RuntimeError("No s'han pogut descarregar les dades de Bitcoin")
    idx = data["BTC/EUR"].index
    return {s: df.reindex(idx).ffill().bfill() for s, df in data.items()}


def main(cfg: dict, dd_limit_pct: float, synthetic: bool = False) -> dict:
    t0 = time.time()
    write_status(estat="descarregant", pct=0, missatge="Preparant la descàrrega de dades")
    try:
        data = load(synthetic)
        res = run(data, dd_limit_pct / 100, lambda pct, msg: write_status(estat="provant", pct=pct, missatge=msg))
        res["durada_min"] = round((time.time() - t0) / 60, 1)
        res["dades_sintetiques"] = synthetic
        tmp = RESULTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(res, f, default=str)
        os.replace(tmp, RESULTS_FILE)
        write_status(estat="fet", pct=100, missatge=f"Prova acabada en {res['durada_min']} minuts")
        return res
    except Exception as e:
        log.exception("El laboratori de curt termini ha fallat")
        write_status(estat="error", pct=0, missatge=f"La prova ha fallat: {e}")
        raise
