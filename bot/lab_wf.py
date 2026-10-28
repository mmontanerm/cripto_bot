"""Laboratori LLARG/CURT amb optimització WALK-FORWARD, prova del mico i reserva final.

Objectiu: trobar paràmetres per a estratègies que guanyin tant amb pujades (llarg) com amb
baixades (curt), sense que la sort de l'optimització ens enganyi.

Dades: els 3 últims anys, espelmes d'1 hora (construïdes a partir de les de 15 minuts),
BTC, ETH, SOL, XRP, DOGE i ADA. Sense apalancament: cada valor té 1/6 del capital.

Costos, el més reals possible:
  - Futurs amb ordres a mercat: 0,05% de comissió + 0,02% de lliscament a cada costat (0,14% per operació).
  - Finançament: les posicions llargues paguen un 0,01% cada 8 hores; a les curtes no se'ls compta cap ingrés.
  - Entrada a l'obertura de l'espelma següent al senyal. Stops dins l'espelma, al stop o pitjor si
    l'espelma obre més enllà. Si el bot canvia de paràmetres, paga el cost de tancar i reobrir.

Famílies (llarg i curt):
  - Tendència: ruptura del màxim/mínim de les últimes N hores (canal de Donchian).
  - Reversió: el preu surt de les bandes de Bollinger i s'aposta que torna a la mitjana.
  - Règim: amb tendència (ràtio d'eficiència alta) opera la ruptura; sense, la reversió.

Mètode:
  1. WALK-FORWARD: tria paràmetres amb 6 mesos, els fa servir els 2 mesos següents (no vistos),
     avança 2 mesos i repeteix. El resultat és l'encadenament dels períodes no vistos.
  2. ALTIPLÀ: no tria el pic sinó la combinació que, juntament amb les veïnes, puntua millor.
  3. PROVA DEL MICO: les mateixes sortides, costos i freqüència d'operacions, però entrades a l'atzar (25 cops).
  4. RESERVA FINAL: els 4 últims mesos no es toquen fins al final i s'hi prova una sola vegada.

Criteri d'èxit, FIXAT ABANS de la prova:
  1. El resultat encadenat fora de mostra guanya diners, amb almenys 100 operacions.
  2. Supera la prova del mico: millor que el 95% de les simulacions amb entrades a l'atzar.
  3. Guanya diners a la reserva final sense passar de la caiguda acceptada ni fer saltar l'aturada.
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

from .data import fetch_history, fetch_range
from .paths import LAB_DIR

log = logging.getLogger("lab_wf")

STATUS_FILE = os.path.join(LAB_DIR, "estat_wf.json")
RESULTS_FILE = os.path.join(LAB_DIR, "resultats_wf.json")
RESULTS_PAST_FILE = os.path.join(LAB_DIR, "resultats_wf_passat.json")

# PROVA AMB DADES DEL PASSAT: la família de tendència, amb EXACTAMENT les mateixes regles, paràmetres
# possibles i criteri, sobre anys que el laboratori principal no ha fet servir mai (abans d'octubre del 2023).
# Inclou un mercat molt alcista (2021) i un de molt baixista (2022). Fixat el 19-10-2026, abans de veure'n el resultat.
PAST_START, PAST_END = "2020-01-01", "2023-10-01"
PAST_FAMILIES = ["tendencia"]
RESULTS_FAST_FILE = os.path.join(LAB_DIR, "resultats_wf_rapid.json")

# PROVA RÀPIDA (demanada el 20-10-2026): les mateixes regles llarg/curt amb espelmes de 15 MINUTS i canals
# curts, per arribar a unes 10-12 operacions al dia entre els 6 valors. Mateixos costos, mètode i criteri.
# Els períodes es compten en espelmes de 15 minuts (16 = 4 hores).
FAST_FAMILIES = {
    "tendencia_rapida": {"tipus": "tendencia", "nom": "Tendència ràpida llarg/curt",
                         "desc": "Espelmes de 15 minuts. Compra quan el preu trenca el màxim de les últimes hores i ven en curt quan trenca el mínim (canals de 4 a 24 hores). Surt amb un canal més curt en contra o amb el stop.",
                         "grid": {"N": [16, 32, 48, 96], "sortida": [0.25, 0.5], "stop": [1.5, 2.0, 3.0]}},
    "reversio_rapida": {"tipus": "reversio", "temps": 16, "nom": "Reversió ràpida llarg/curt",
                        "desc": "Espelmes de 15 minuts. Quan el preu surt de les bandes de Bollinger (de 4 o 8 hores) aposta que tornarà a la mitjana. Surt a la mitjana, amb el stop o al cap de 4 hores.",
                        "grid": {"n": [16, 32], "k": [2.0, 2.5, 3.0], "stop": [1.5, 2.0, 3.0]}},
}

SRC_TF = "15m"
DAYS = 1100                      # mateixes dades que el laboratori anterior (ja descarregades)
SYMBOLS = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "DOGE/EUR", "ADA/EUR"]
CAPITAL = 1000.0
WARMUP = 200                     # espelmes d'1 h per escalfar indicadors
COST_SIDE = 0.0007               # 0,05% comissió + 0,02% lliscament
FUNDING_8H = 0.0001              # els llargs paguen 0,01% cada 8 h
TRAIN_DAYS, TEST_DAYS, HOLDOUT_DAYS = 180, 60, 120
MONKEY_RUNS = 25
MIN_TRADES_TRAIN = 15
MIN_TRADES_WF = 100
REV_TIME = 48                    # sortida per temps de la reversió (hores)
REGIME_STOP = 3.0
# MIDA SEGONS VOLATILITAT (fixat el 19-10-2026 abans de veure'n cap resultat, sense optimitzar):
# cada posició es redueix perquè la seva volatilitat horària (ATR/preu) no passi del 0,5 %, la volatilitat
# horària habitual de Bitcoin. Les monedes i els moments més nerviosos operen amb menys diners. Mai més del
# 100 % de la part del valor: sense apalancament.
VOL_TARGET = 0.005
SIZINGS = ["fixa", "volatilitat"]

FAMILIES = {
    "tendencia": {"tipus": "tendencia", "nom": "Tendència llarg/curt",
                  "desc": "Compra quan el preu trenca el màxim de les últimes N hores i ven en curt quan trenca el mínim. Surt quan el preu trenca un canal més curt en contra o toca el stop.",
                  "grid": {"N": [24, 48, 96, 168], "sortida": [0.25, 0.5], "stop": [2.0, 3.0, 4.0]}},
    "reversio": {"tipus": "reversio", "temps": REV_TIME, "nom": "Reversió llarg/curt",
                 "desc": "Quan el preu surt de les bandes de Bollinger aposta que tornarà a la mitjana: compra si ha caigut massa i ven en curt si ha pujat massa. Surt a la mitjana, amb el stop o al cap de 48 hores.",
                 "grid": {"n": [24, 48], "k": [2.0, 2.5, 3.0], "stop": [2.0, 3.0, 4.0]}},
    "regim": {"tipus": "regim", "nom": "Règim: tendència o reversió",
              "desc": "Mesura si el mercat té tendència (ràtio d'eficiència de 48 hores). Si en té, opera la ruptura; si no, la reversió. Cada part fa servir la meitat del capital del valor.",
              "grid": {"llindar": [0.2, 0.3, 0.4], "N": [48, 96], "k": [2.0, 2.5]}},
}
# PROVA DEL TERME MIG (demanada el 20-10-2026): EXACTAMENT les regles del bot en directe (espelmes d'1 hora,
# sortida amb un canal de la meitat de durada, stop a 4 × ATR de 24 hores, el 60 % del capital repartit entre
# els 6 valors, mateixos costos), sense cap optimització. L'ÚNICA cosa que canvia és la durada del canal d'entrada,
# des de la del bot (168 h) fins a 6 h, per veure què passa quan s'opera més sovint. Es prova en dos períodes
# separats (2020-2023 i 2023-2026) i cada configuració es compara amb 25 simulacions amb entrades a l'atzar.
SCAN_CHANNELS = [168, 120, 96, 72, 48, 24, 12, 6]
SCAN_BOT_CHANNEL = 168
SCAN_EXPOSURE = 0.60
SCAN_STOP = 4.0
RESULTS_SCAN_FILE = os.path.join(LAB_DIR, "resultats_terme_mig.json")
SCAN_FAMILY = {"tendencia_bot": {"tipus": "tendencia", "nom": "Tendència amb les regles del bot", "desc": "", "grid": {}}}
ALL_FAMILIES = {**FAMILIES, **FAST_FAMILIES, **SCAN_FAMILY}

# PROVA D'ESCALAT PROPORCIONAL (demanada el 20-10-2026): l'estratègia del bot amb TOTS els paràmetres escalats
# a la vegada. El bot fa servir espelmes d'1 hora i compta en espelmes: canal de 168, sortida de 84, ATR de 24 i
# stop a 4 × ATR. Aquí es manté exactament el mateix nombre d'espelmes i només canvia la durada de cada
# espelma (de 4 hores a 5 minuts). Així el canal, la sortida, l'ATR i el stop s'escurcen tots en la mateixa proporció.
# Els costos per operació (0,14 %) no es poden escalar: són els mateixos en totes les mides.
# PROVA D'OBJECTIUS DE GUANY (demanada el 26-10-2026): les regles exactes del bot (1 hora, canal 168 h, sortida 84 h,
# stop 4 × ATR, 60 % del capital, mateixos costos) afegint-hi un objectiu de guany fix: es tanca l'operació quan guanya
# aquest percentatge (abans de costos). Sense optimitzar; mateixos dos períodes i mateixa prova de l'atzar.
TP_LEVELS = [0.0, 0.005, 0.01, 0.02, 0.03, 0.05, 0.10]
RESULTS_TP_FILE = os.path.join(LAB_DIR, "resultats_objectius.json")
RESULTS_MICRO_FILE = os.path.join(LAB_DIR, "resultats_micro.json")
RESULTS_CORR_FILE = os.path.join(LAB_DIR, "resultats_correlacio.json")
SCALE_CANDLES_MIN = [240, 120, 90, 60, 45, 30, 20, 15, 10, 5]   # 90-240 min afegits el 20-10-2026 (direcció contrària: menys operacions)
SCALE_PERIODS = {"passat": (PAST_START, PAST_END), "recent": ("2023-10-01", None)}
RESULTS_SCALE_FILE = os.path.join(LAB_DIR, "resultats_escalat.json")


MIDA_FIXA_TXT = " Mida fixa: cada valor opera sempre amb 1/6 del capital."
MIDA_VOL_TXT = (" Mida segons volatilitat: cada posició es redueix perquè la seva volatilitat horària no passi del 0,5 %"
                " (la de Bitcoin en un moment normal); les monedes i els moments més nerviosos operen amb menys diners.")


def write_status(**kw) -> None:
    os.makedirs(LAB_DIR, exist_ok=True)
    kw["actualitzat"] = datetime.now(timezone.utc).isoformat()
    kw["pid"] = os.getpid()
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kw, f, default=str)
    os.replace(tmp, STATUS_FILE)


# ------------------------------------------------------------------ indicadors (per valor)
class Sym:
    def __init__(self, df: pd.DataFrame):
        self.idx = df.index
        self.bar_h = float(pd.Series(df.index[:50]).diff().median() / pd.Timedelta(hours=1)) if len(df) > 2 else 1.0
        self.O, self.H, self.L, self.C = (df[c].to_numpy(float) for c in ("open", "high", "low", "close"))
        c = df["close"]
        prev = c.shift()
        tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
        self.atr = tr.ewm(alpha=1 / 24, adjust=False).mean().to_numpy()
        net = (c - c.shift(48)).abs()
        path = c.diff().abs().rolling(48).sum()
        self.er = (net / path.replace(0, np.nan)).fillna(0).to_numpy()
        self.df = df[["high", "low", "close"]]
        self._cache: dict = {}

    def donchian(self, n):
        k = ("don", n)
        if k not in self._cache:
            self._cache[k] = (self.df["high"].rolling(n).max().shift(1).to_numpy(),
                              self.df["low"].rolling(n).min().shift(1).to_numpy())
        return self._cache[k]

    def boll(self, n, kk):
        key = ("bol", n)
        if key not in self._cache:
            c = self.df["close"]
            self._cache[key] = (c.rolling(n).mean().to_numpy(), c.rolling(n).std().to_numpy())
        ma, sd = self._cache[key]
        return ma, ma - kk * sd, ma + kk * sd


def trend_signals(s: Sym, N: int, frac: float):
    hi, lo = s.donchian(N)
    ehi, elo = s.donchian(max(2, int(N * frac)))
    C = s.C
    with np.errstate(invalid="ignore"):
        entry = np.where(C > hi, 1, np.where(C < lo, -1, 0))
        return entry, C < elo, C > ehi


def rev_signals(s: Sym, n: int, k: float):
    ma, lo, up = s.boll(n, k)
    C = s.C
    with np.errstate(invalid="ignore"):
        entry = np.where(C < lo, 1, np.where(C > up, -1, 0))
        return entry, C >= ma, C <= ma


def _first(mask_fn, e: int, end: int):
    """Primera espelma (des de e) on es compleix la condició, buscant per trams creixents (ràpid)."""
    a, step = e, 256
    while a < end:
        b = min(end, a + step)
        m = mask_fn(a, b)
        if m.any():
            return a + int(np.argmax(m))
        a, step = b, step * 2
    return None


# ------------------------------------------------------------------ motor per esdeveniments
def run(s: Sym, entry, exit_long, exit_short, stop_k: float, T: int, weight: float = 1.0, rng=None, tp: float = 0.0):
    """Una posició a la vegada per valor. Retorna les operacions: espelma d'entrada i de sortida,
    direcció, rendiment net (amb comissions, lliscament i finançament), pitjor moment i pes.

    Amb `rng` és la PROVA DEL MICO: primer es calcula l'estratègia real per saber quantes operacions fa
    i quina part són llargues; després s'entra a l'atzar amb la mateixa freqüència i proporció,
    i se surt exactament amb les mateixes regles (stop, senyal de sortida, temps) i costos.

    `tp` > 0: OBJECTIU DE GUANY. Es tanca quan el preu toca entrada × (1 ± tp) dins l'espelma, exactament a
    aquest preu (si l'espelma toca el stop i l'objectiu alhora, es compta el stop: el cas pitjor)."""
    O, H, L, C, A = s.O, s.H, s.L, s.C, s.atr
    n = len(C)
    start = int(np.argmax(np.isfinite(C))) + WARMUP     # escalfament comptat des del primer dia que cotitza
    if rng is not None:
        real = run(s, entry, exit_long, exit_short, stop_k, T, weight, tp=tp)
        held = int((real[1] - real[0] + 1).sum())
        flat = max(n - start - held, 1)
        p_in = min(1.0, max(len(real[0]), 1) / flat)
        p_long = float((real[2] > 0).mean()) if len(real[0]) else 0.5
    else:
        entry = entry.copy()
        entry[:start] = 0
        sig = np.nonzero(entry)[0]
    out = [[] for _ in range(6)]
    nxt = start
    while True:
        if rng is None:
            j = int(np.searchsorted(sig, nxt))
            if j >= len(sig):
                break
            i = int(sig[j])
            d = int(entry[i])
        else:
            i = nxt + int(rng.geometric(p_in)) - 1
            if i >= n:
                break
            d = 1 if rng.random() < p_long else -1
        e = i + 1
        if e >= n or not np.isfinite(A[i]) or A[i] <= 0 or not np.isfinite(O[e]):
            nxt = i + 1
            continue
        ep = O[e]
        sp = ep - d * stop_k * A[i]
        end = min(n, e + T) if T else n
        if d > 0:
            a = _first(lambda u, v: L[u:v] <= sp, e, end)
            b = _first(lambda u, v: exit_long[u:v], e, end)
        else:
            a = _first(lambda u, v: H[u:v] >= sp, e, end)
            b = _first(lambda u, v: exit_short[u:v], e, end)
        g = None
        if tp > 0:                                         # objectiu de guany dins l'espelma
            tpp = ep * (1 + d * tp)
            g = _first((lambda u, v: H[u:v] >= tpp) if d > 0 else (lambda u, v: L[u:v] <= tpp), e, end)
            if g is not None and (a is None or g < a) and (b is None or g <= b):
                a = b = None                                # l'objectiu arriba primer
            else:
                g = None
        if g is not None:
            x, xp, last = g, tpp, g
        elif a is not None and (b is None or a <= b):       # el stop dins l'espelma té prioritat
            x = a
            xp = sp if x == e else (min(O[x], sp) if d > 0 else max(O[x], sp))
            last = x                                         # pitjor moment: espelmes fins a x-1 i el preu del stop
        elif b is not None and b + 1 < n:                  # senyal al tancament: surt a l'obertura següent
            x = b + 1
            xp = O[x]
            last = x
        else:                                              # límit de temps o final de les dades
            x = end - 1
            xp = C[x]
            last = x + 1
        if d > 0:
            worst = min(L[e:last].min() if last > e else xp, xp)
        else:
            worst = max(H[e:last].max() if last > e else xp, xp)
        gross = d * (xp - ep) / ep
        funding = FUNDING_8H * (x - e + 1) * s.bar_h / 8 if d > 0 else 0.0
        out[0].append(e)
        out[1].append(x)
        out[2].append(d)
        out[3].append(gross - 2 * COST_SIDE - funding)
        out[4].append(d * (worst - ep) / ep - COST_SIDE)
        out[5].append(min(1.0, VOL_TARGET / (A[i] / C[i])))   # mida segons volatilitat, calculada en el moment del senyal
        nxt = max(x, i + 1)
    return (np.asarray(out[0], dtype=np.int64), np.asarray(out[1], dtype=np.int64), np.asarray(out[2], dtype=np.int8),
            np.asarray(out[3], dtype=float), np.asarray(out[4], dtype=float), np.full(len(out[0]), weight),
            np.asarray(out[5], dtype=float))


def sized(tr: dict, sizing: str) -> dict:
    """Mateixes operacions; només canvia la mida (pes) de cada posició."""
    return tr if sizing == "fixa" else {**tr, "w": tr["w"] * tr["f"]}


def combo_trades(syms: dict, fam: str, p: dict, rng=None) -> dict:
    """Operacions de tots els valors per a una combinació. Pes = fracció del capital d'aquell valor;
    `f` = fracció que es fa servir si la mida va segons la volatilitat."""
    spec = ALL_FAMILIES[fam]
    kind, T = spec["tipus"], spec.get("temps", REV_TIME)
    parts = []
    for s in syms.values():
        if kind == "tendencia":
            en, xl, xs = trend_signals(s, p["N"], p["sortida"])
            parts.append(run(s, en, xl, xs, p["stop"], 0, 1.0, rng, tp=p.get("tp", 0.0)))
        elif kind == "reversio":
            en, xl, xs = rev_signals(s, p["n"], p["k"])
            parts.append(run(s, en, xl, xs, p["stop"], T, 1.0, rng))
        else:
            en, xl, xs = trend_signals(s, p["N"], 0.5)
            parts.append(run(s, np.where(s.er > p["llindar"], en, 0), xl, xs, REGIME_STOP, 0, 0.5, rng))
            en, xl, xs = rev_signals(s, 24, p["k"])
            parts.append(run(s, np.where(s.er <= p["llindar"], en, 0), xl, xs, REGIME_STOP, REV_TIME, 0.5, rng))
    cols = list(zip(*parts))
    out = {k: np.concatenate(v) for k, v in zip(("e", "x", "d", "net", "worst", "w", "f"), cols)}
    order = np.argsort(out["x"], kind="stable")
    return {k: v[order] for k, v in out.items()}


# ------------------------------------------------------------------ mètriques per període
def period(tr: dict, i0: int, i1: int, notional: float, dd_limit=None, start=CAPITAL) -> dict:
    """Operacions que TANQUEN dins [i0, i1). La caiguda inclou el pitjor moment de cada operació."""
    m = (tr["x"] >= i0) & (tr["x"] < i1)
    net = tr["net"][m] * tr["w"][m] * notional
    worst = tr["worst"][m] * tr["w"][m] * notional
    cum = start + np.cumsum(net)
    pts = np.empty(2 * len(net) + 1)
    pts[0] = start
    pts[1::2] = cum - net + np.minimum(worst, 0)
    pts[2::2] = cum
    final = cum[-1] if len(cum) else start
    stopped = None
    if dd_limit is not None and len(net):
        pk = np.maximum.accumulate(pts)
        hit = np.nonzero(pts < pk * (1 - dd_limit))[0]
        if len(hit):
            h = int(hit[0])
            final = pts[h]
            stopped = int(tr["x"][m][(h - 1) // 2])
            pts = pts[:h + 1]
    pk = np.maximum.accumulate(pts)
    dd = float(-(pts / pk - 1).min()) if len(pts) else 0.0
    return {"pnl": float(final - start), "dd": dd, "n": int(m.sum()), "stopped_bar": stopped, "mask": m}


def score(st: dict) -> float:
    if st["n"] < MIN_TRADES_TRAIN:
        return -5.0
    pct = st["pnl"] / CAPITAL * 100
    return float(np.clip(pct / max(st["dd"] * 100, 2.0), -5, 5))   # rendiment per cada punt de caiguda


def neighbours(grid: dict) -> tuple[list, dict]:
    keys = list(grid)
    combos = list(itertools.product(*[range(len(grid[k])) for k in keys]))
    pos = {c: i for i, c in enumerate(combos)}
    nb = {i: [pos[c[:j] + (c[j] + s,) + c[j + 1:]] for j in range(len(keys)) for s in (-1, 1)
              if 0 <= c[j] + s < len(grid[keys[j]])] for i, c in enumerate(combos)}
    params = [{k: grid[k][c[j]] for j, k in enumerate(keys)} for c in combos]
    return params, nb


def choose(trades: list, nb: dict, i0: int, i1: int, notional: float) -> int:
    raw = np.array([score(period(t, i0, i1, notional)) for t in trades])
    plateau = np.array([(raw[i] + raw[nb[i]].sum()) / (1 + len(nb[i])) for i in range(len(raw))])
    return int(np.argmax(plateau))


def _hours(bars: int, bar_h: float) -> str:
    h = bars * bar_h
    return f"{h:g} h" if h >= 1 else f"{h * 60:g} min"


def params_text(fam: str, p: dict, bar_h: float = 1.0) -> str:
    kind = ALL_FAMILIES[fam]["tipus"]
    if kind == "tendencia":
        return (f"canal de {_hours(p['N'], bar_h)}, sortida amb canal de {_hours(max(2, int(p['N'] * p['sortida'])), bar_h)}, "
                f"stop {p['stop']:g}× ATR")
    if kind == "reversio":
        return f"Bollinger de {_hours(p['n'], bar_h)} a {p['k']:g} desviacions, stop {p['stop']:g}× ATR"
    return f"llindar de tendència {p['llindar']:g}, canal de {p['N']} h, Bollinger a {p['k']:g} desviacions"


def _pct(v):
    return round(float(v) * 100, 2) if v is not None else None


def _thin(rows: list, n: int = 400) -> list:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    out = [rows[int(i * step)] for i in range(n)]
    out[-1] = rows[-1]
    return out


def run_lab(syms: dict, dd_limit: float, progress=lambda pct, msg: None, families=None,
            family_defs: dict | None = None, sizings: list | None = None) -> dict:
    defs = family_defs or FAMILIES
    sizings = sizings or SIZINGS
    fams = {k: v for k, v in defs.items() if families is None or k in families}
    first = next(iter(syms.values()))
    idx = first.idx
    bar_h = first.bar_h
    n = len(idx)
    notional = CAPITAL / len(syms)
    t_end = idx[-1] + pd.Timedelta(hours=bar_h)
    t_hold = t_end - pd.Timedelta(days=HOLDOUT_DAYS)
    bar = lambda t: int(idx.searchsorted(t))             # noqa: E731
    windows = []
    ts = idx[WARMUP] + pd.Timedelta(days=TRAIN_DAYS)
    while ts + pd.Timedelta(days=TEST_DAYS / 2) <= t_hold:
        windows.append((ts - pd.Timedelta(days=TRAIN_DAYS), ts, min(ts + pd.Timedelta(days=TEST_DAYS), t_hold)))
        ts += pd.Timedelta(days=TEST_DAYS)
    if not windows:
        raise RuntimeError("No hi ha prou dades per fer el walk-forward")
    i_hold = bar(t_hold)
    wf_from, wf_to = bar(windows[0][1]), bar(windows[-1][2])
    years = max((windows[-1][2] - windows[0][1]).days / 365, 0.01)
    hold_days = max((t_end - t_hold).days, 1)

    total_steps = sum(len(list(itertools.product(*f["grid"].values()))) for f in fams.values()) + len(fams) * len(sizings)
    step = 0
    fams_out = []
    for fam, spec in fams.items():
        params, nb = neighbours(spec["grid"])
        raw_trades = []
        for p in params:
            raw_trades.append(combo_trades(syms, fam, p))
            step += 1
            progress(round(step / total_steps * 95, 1), f"{spec['nom']}: {params_text(fam, p, bar_h)}")
        msims: dict = {}                                     # simulacions del mico, compartides per les dues mides
        for sizing in sizings:
            trades = [sized(t, sizing) for t in raw_trades]
            label = spec["nom"] + (" · mida segons volatilitat" if sizing == "volatilitat" else " · mida fixa")

            # 1) Walk-forward: tria amb 6 mesos, opera els 2 següents
            chosen, rows, nets, expo, gross, curve = [], [], [], [], [], [(str(windows[0][1].date()), CAPITAL)]
            bal, prev, switch_cost, longs = CAPITAL, None, 0.0, 0
            fees = funding = 0.0
            for (a, b, c) in windows:
                ci = choose(trades, nb, bar(a), bar(b), notional)
                cost = CAPITAL * 2 * COST_SIDE if prev is not None and ci != prev else 0.0   # tancar i reobrir
                cost = min(cost, max(bal, 0.0))
                switch_cost += cost
                bal -= cost
                prev = ci
                chosen.append(ci)
                t = trades[ci]
                st = period(t, bar(b), bar(c), notional)
                m = st["mask"]
                w = t["w"][m] * notional
                seg = np.maximum(bal + np.cumsum(t["net"][m] * w), 0.0)
                ruin = np.nonzero(seg <= 0)[0]
                if len(ruin) or bal <= 0:                        # compte a zero: no es pot seguir operant
                    k = int(ruin[0]) + 1 if len(ruin) else 0
                    seg, m = seg[:k], m & (np.cumsum(m) <= k)
                    w = t["w"][m] * notional
                curve += [(str(idx[x].date()), round(float(v), 2)) for x, v in zip(t["x"][m], seg)]
                bal = 0.0 if bal <= 0 else (float(seg[-1]) if len(seg) else bal)
                nets.append(t["net"][m])
                expo += list(t["w"][m] / raw_trades[ci]["w"][m])
                longs += int((t["d"][m] > 0).sum())
                fees += float((2 * COST_SIDE * w).sum())
                held = (t["x"][m] - t["e"][m] + 1)
                fund_frac = FUNDING_8H * held * bar_h / 8 * (t["d"][m] > 0)
                funding += float((fund_frac * w).sum())
                gross.append(t["net"][m] + 2 * COST_SIDE + fund_frac)
                rows.append({"des_de": str(b.date()), "fins": str(c.date()), "parametres": params_text(fam, params[ci], bar_h),
                             "rendiment_pct": round((st["pnl"] - cost) / CAPITAL * 100, 1), "operacions": st["n"]})
            vals = np.array([v for _, v in curve])
            chain_pnl = bal - CAPITAL
            chain_dd = float(-(vals / np.maximum.accumulate(vals) - 1).min())
            nets = np.concatenate(nets)
            chain_n = len(nets)

            # 2) Prova del mico: mateixos paràmetres a cada finestra, entrades a l'atzar
            monkey = []
            for r in range(MONKEY_RUNS):
                pnl, prev = 0.0, None
                if len(sizings) == 1:
                    msims.clear()                            # una sola mida: no cal guardar-les (estalvia memòria)
                for wi, (a, b, c) in enumerate(windows):
                    ci = chosen[wi]
                    if (r, ci) not in msims:
                        msims[(r, ci)] = combo_trades(syms, fam, params[ci], rng=np.random.default_rng(7919 * r + ci))
                    pnl += period(sized(msims[(r, ci)], sizing), bar(b), bar(c), notional)["pnl"]
                    pnl -= CAPITAL * 2 * COST_SIDE if prev is not None and ci != prev else 0.0
                    prev = ci
                    if pnl <= -CAPITAL:                          # compte a zero
                        pnl = -CAPITAL
                        break
                monkey.append(pnl)
                progress(round((step + (r + 1) / MONKEY_RUNS) / total_steps * 95, 1),
                         f"{label}: prova del mico {r + 1}/{MONKEY_RUNS}")
            monkey = np.asarray(monkey)
            pctile = float((monkey < chain_pnl).mean() * 100)
            step += 1

            # 3) Reserva final: paràmetres triats amb els 6 mesos anteriors, provats una sola vegada
            hi = choose(trades, nb, bar(t_hold - pd.Timedelta(days=TRAIN_DAYS)), i_hold, notional)
            hold = period(trades[hi], i_hold, n, notional, dd_limit)
            hnets = trades[hi]["net"][hold["mask"]]

            ok1 = bool(chain_pnl > 0 and chain_n >= MIN_TRADES_WF)
            ok2 = bool(pctile >= 95)
            ok3 = bool(hold["pnl"] > 0 and hold["stopped_bar"] is None and hold["dd"] <= dd_limit)
            fams_out.append({
                "id": fam if sizing == "fixa" else fam + "_vol", "familia": fam, "mida": sizing,
                "nom": label, "desc": spec["desc"] + (MIDA_VOL_TXT if sizing == "volatilitat" else MIDA_FIXA_TXT),
                "combinacions": len(params),
                "walk_forward": {
                    "des_de": str(windows[0][1].date()), "fins": str(windows[-1][2].date()), "finestres": len(windows),
                    "rendiment_pct": round(chain_pnl / CAPITAL * 100, 1),
                    "anual_pct": round(((max(bal, 0.01) / CAPITAL) ** (1 / years) - 1) * 100, 1),
                    "caiguda_pct": round(chain_dd * 100, 1), "operacions": chain_n,
                    "guany_per_caiguda": round(chain_pnl / CAPITAL / chain_dd, 2) if chain_dd > 0 else None,
                    "exposicio_mitjana_pct": round(float(np.mean(expo)) * 100) if expo else None,
                    "operacions_setmana": round(chain_n / (years * 52), 1),
                    "operacions_dia": round(chain_n / (years * 365), 1),
                    "brut_mitja_pct": round(float(np.concatenate(gross).mean() * 100), 3) if chain_n else None,
                    "costos_anuals_pct_capital": round((fees + funding + switch_cost) / CAPITAL / years * 100, 1),
                    "llargues_pct": round(longs / chain_n * 100) if chain_n else None,
                    "guanyadores_pct": round(float((nets > 0).mean() * 100), 1) if chain_n else None,
                    "net_mitja_pct": round(float(nets.mean() * 100), 3) if chain_n else None,
                    "net_guanyadora_pct": _pct(nets[nets > 0].mean()) if (nets > 0).any() else None,
                    "net_perdedora_pct": _pct(nets[nets <= 0].mean()) if (nets <= 0).any() else None,
                    "comissions_eur": round(fees, 2), "financament_eur": round(funding, 2),
                    "cost_canvis_eur": round(switch_cost, 2),
                    "canvis_parametres": sum(1 for i in range(1, len(chosen)) if chosen[i] != chosen[i - 1]),
                    "finestres_detall": rows,
                    "corba": _thin(curve),
                },
                "mico": {"execucions": MONKEY_RUNS, "mediana_pct": round(float(np.median(monkey)) / CAPITAL * 100, 1),
                         "p95_pct": round(float(np.percentile(monkey, 95)) / CAPITAL * 100, 1),
                         "millor_pct": round(float(monkey.max()) / CAPITAL * 100, 1),
                         "pitjor_pct": round(float(monkey.min()) / CAPITAL * 100, 1),
                         "percentil_real": round(pctile)},
                "reserva": {"des_de": str(t_hold.date()), "fins": str(idx[-1].date()), "parametres": params_text(fam, params[hi], bar_h),
                            "rendiment_pct": round(hold["pnl"] / CAPITAL * 100, 1), "caiguda_pct": round(hold["dd"] * 100, 1),
                            "operacions": hold["n"], "operacions_setmana": round(hold["n"] / (hold_days / 7), 1),
                            "operacions_dia": round(hold["n"] / hold_days, 1),
                            "aturada": str(idx[hold["stopped_bar"]].date()) if hold["stopped_bar"] is not None else None,
                            "guanyadores_pct": round(float((hnets > 0).mean() * 100), 1) if len(hnets) else None},
                "criteri": [{"text": f"Guanya diners als períodes no vistos, amb almenys {MIN_TRADES_WF} operacions ({chain_n} operacions, {chain_pnl / CAPITAL * 100:+.1f} %)", "ok": ok1},
                            {"text": f"Supera la prova del mico: millor que el 95 % de les simulacions amb entrades a l'atzar (millor que el {pctile:.0f} %)", "ok": ok2},
                            {"text": f"Guanya a la reserva final sense passar del {round(dd_limit * 100)} % de caiguda ni fer saltar l'aturada ({hold['pnl'] / CAPITAL * 100:+.1f} %)", "ok": ok3}],
                "superat": bool(ok1 and ok2 and ok3),
            })

    btc = syms["BTC/EUR"].C if "BTC/EUR" in syms else first.C
    btc_curve = _thin([(str(idx[i].date()), round(float(CAPITAL * btc[i] / btc[wf_from]), 2)) for i in range(wf_from, wf_to, max(1, int(6 / bar_h)))])
    listed = {s.replace("/EUR", ""): str(v.idx[int(np.argmax(np.isfinite(v.C)))].date()) for s, v in syms.items()
              if not np.isfinite(v.C[0])}                   # valors que encara no cotitzaven a l'inici
    best = [f for f in fams_out if f["superat"]]
    return {
        "versio": 5,
        "mida_volatilitat_objectiu_pct": VOL_TARGET * 100,
        "generat": datetime.now(timezone.utc).isoformat(),
        "espelma_minuts": round(bar_h * 60),
        "periode": {"des_de": str(idx[WARMUP].date()), "fins": str(idx[-1].date())},
        "valors": [s.replace("/EUR", "") for s in syms],
        "cotitzen_des_de": listed,
        "costos": {"operacio_pct": round(2 * COST_SIDE * 100, 2), "financament_8h_pct": FUNDING_8H * 100},
        "metode": {"entrenament_dies": TRAIN_DAYS, "prova_dies": TEST_DAYS, "reserva_dies": HOLDOUT_DAYS, "mico": MONKEY_RUNS},
        "caiguda_maxima_acceptada_pct": round(dd_limit * 100),
        "families": fams_out,
        "btc": {"walk_forward_pct": round(float(btc[wf_to - 1] / btc[wf_from] - 1) * 100, 1),
                "reserva_pct": round(float(btc[-1] / btc[i_hold] - 1) * 100, 1), "corba": btc_curve},
        "guanyadora": max(best, key=lambda f: f["walk_forward"]["rendiment_pct"])["id"] if best else None,
    }


def scan_row(t: dict, monkey: list, idx, bar_h: float, notional: float, dd_limit: float, i0: int) -> dict:
    """Mètriques d'una configuració fixa sobre tot un període (regles del bot)."""
    n = len(idx)
    years = max((idx[-1] - idx[i0]).days / 365, 0.01)
    days = max((idx[-1] - idx[i0]).days, 1)
    st = period(t, i0, n, notional)
    brake = period(t, i0, n, notional, dd_limit)
    m = st["mask"]
    nets = t["net"][m]
    fund = FUNDING_8H * (t["x"][m] - t["e"][m] + 1) * bar_h / 8 * (t["d"][m] > 0)
    gross = nets + 2 * COST_SIDE + fund
    costs = float(((2 * COST_SIDE + fund) * t["w"][m] * notional).sum())
    monkey = np.asarray(monkey)
    pctile = float((monkey < st["pnl"]).mean() * 100)
    nn = int(m.sum())
    return {
        "rendiment_pct": round(st["pnl"] / CAPITAL * 100, 1),
        "anual_pct": round(((max(CAPITAL + st["pnl"], 0.01) / CAPITAL) ** (1 / years) - 1) * 100, 1),
        "caiguda_pct": round(st["dd"] * 100, 1),
        "fre": str(idx[brake["stopped_bar"]].date()) if brake["stopped_bar"] is not None else None,
        "rendiment_amb_fre_pct": round(brake["pnl"] / CAPITAL * 100, 1),
        "operacions": nn, "operacions_dia": round(nn / days, 2),
        "guanyadores_pct": round(float((nets > 0).mean() * 100), 1) if nn else None,
        "net_guanyadora_pct": round(float(nets[nets > 0].mean() * 100), 2) if (nets > 0).any() else None,
        "net_perdedora_pct": round(float(nets[nets <= 0].mean() * 100), 2) if (nets <= 0).any() else None,
        "brut_mitja_pct": round(float(gross.mean() * 100), 3) if nn else None,
        "net_mitja_pct": round(float(nets.mean() * 100), 3) if nn else None,
        "costos_anuals_pct_capital": round(costs / CAPITAL / years * 100, 1),
        "mico_mediana_pct": round(float(np.median(monkey)) / CAPITAL * 100, 1),
        "mico_p95_pct": round(float(np.percentile(monkey, 95)) / CAPITAL * 100, 1),
        "mico_percentil": round(pctile),
        "supera": bool(st["pnl"] > 0 and pctile >= 95 and nn >= MIN_TRADES_WF),
    }


def period_info(idx, btc, i0: int) -> dict:
    i_first = int(np.argmax(np.isfinite(btc[i0:]))) + i0
    return {"des_de": str(idx[i0].date()), "fins": str(idx[-1].date()),
            "btc_pct": round(float(btc[-1] / btc[i_first] - 1) * 100, 1)}


def scan_period(syms: dict, dd_limit: float, progress, step0: int, total: int) -> tuple[list, dict, int]:
    """Totes les durades de canal sobre un període, amb les regles exactes del bot."""
    first = next(iter(syms.values()))
    idx, bar_h = first.idx, first.bar_h
    notional = CAPITAL * SCAN_EXPOSURE / len(syms)
    rows, step = [], step0
    for N in SCAN_CHANNELS:
        p = {"N": N, "sortida": 0.5, "stop": SCAN_STOP}
        t = combo_trades(syms, "tendencia_bot", p)
        monkey = []
        for r in range(MONKEY_RUNS):
            mt = combo_trades(syms, "tendencia_bot", p, rng=np.random.default_rng(104729 * r + N))
            monkey.append(period(mt, WARMUP, len(idx), notional)["pnl"])
            step += 1
            progress(round(step / total * 95, 1), f"Canal de {N} h ({str(idx[WARMUP].year)}-{str(idx[-1].year)}): prova del mico {r + 1}/{MONKEY_RUNS}")
        rows.append({"canal_h": N, "sortida_h": int(N * 0.5), "bot": N == SCAN_BOT_CHANNEL,
                     **scan_row(t, monkey, idx, bar_h, notional, dd_limit, WARMUP)})
    btc = syms["BTC/EUR"].C if "BTC/EUR" in syms else first.C
    return rows, period_info(idx, btc, WARMUP), step


def fetch_5m(symbol: str, start: str, end: str | None, cache_dir: str, status=lambda pct, msg: None) -> pd.DataFrame:
    """Espelmes de 5 minuts de Binance (USDT) entre dues dates, desades comprimides al disc.
    El període sense data final es refresca si la còpia té més d'una setmana."""
    from .data import COLS, _binance
    os.makedirs(cache_dir, exist_ok=True)
    sym = symbol.split("/")[0] + "USDT"
    cache = os.path.join(cache_dir, f"{sym}_5m_{start}_{end or 'recent'}.csv.gz")
    if os.path.exists(cache) and (end or time.time() - os.path.getmtime(cache) < 7 * 86400):
        return pd.read_csv(cache, index_col=0, parse_dates=True).astype("float32")
    t0 = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    t1 = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000) if end else int(time.time() * 1000)
    total = max((t1 - t0) / 300_000 / 1000, 1)
    rows, k, cur = [], 0, t0
    while cur < t1:
        batch = _binance({"symbol": sym, "interval": "5m", "startTime": cur, "endTime": t1 - 1, "limit": 1000})
        if not batch:
            break
        rows.extend(b[:6] for b in batch)
        cur = batch[-1][0] + 1
        k += 1
        if k % 20 == 0:
            status(min(k / total, 1.0), f"Descarregant espelmes de 5 minuts de {symbol.split('/')[0]}")
        time.sleep(0.05)
    if not rows:
        raise KeyError(f"{symbol}: sense dades de 5 minuts")
    df = pd.DataFrame(rows, columns=["ts"] + COLS)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts")[["open", "high", "low", "close"]].astype("float32")
    df = df[~df.index.duplicated()]
    if not end:
        df = df.iloc[:-1]                                   # treu l'espelma oberta
    df.to_csv(cache, compression="gzip")
    return df


def load_5m(key: str, synthetic_data: bool, mode_status) -> dict:
    if synthetic_data:
        raw = synthetic(SYMBOLS, "5m", 240, seed=31 if key == "passat" else 37)
        if key == "passat":
            raw["SOL/EUR"] = raw["SOL/EUR"].iloc[20000:]
        return {s: df[["open", "high", "low", "close"]].astype("float32") for s, df in raw.items()}
    start, end = SCALE_PERIODS[key]
    cache = os.path.join(LAB_DIR, "dades")
    raw = {}
    for j, s in enumerate(SYMBOLS):
        def st(frac, msg, j=j):
            mode_status(round((j + frac) / len(SYMBOLS) * 100, 1), f"{msg} ({'2020-2023' if key == 'passat' else '2023-2026'})")
        st(0, f"Preparant {s.split('/')[0]}")
        try:
            raw[s] = fetch_5m(s, start, end, cache, st)
        except Exception as e:
            log.warning("Salto %s: %s", s, e)
    if "BTC/EUR" not in raw:
        raise RuntimeError("No s'han pogut carregar les dades de Bitcoin")
    return raw


def resample_bars(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if minutes == 5:
        return df
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    return df.resample(f"{minutes}min", origin="epoch").agg(agg).dropna()


def scale_period(raw: dict, dd_limit: float, progress, step0: int, total: int, label: str) -> tuple[list, dict, int]:
    """Les mateixes 168/84/24 espelmes i stop 4 × ATR, amb espelmes de 60 a 5 minuts. Valor a valor per estalviar memòria."""
    notional = CAPITAL * SCAN_EXPOSURE / len(raw)
    rows, step, info = [], step0, None
    for mins in SCALE_CANDLES_MIN:
        base = resample_bars(raw["BTC/EUR"], mins).index
        real, monk = [], [[] for _ in range(MONKEY_RUNS)]
        btc = None
        for s, df in raw.items():
            sym = Sym(resample_bars(df, mins).reindex(base).ffill())      # sense omplir abans de cotitzar
            if s == "BTC/EUR":
                btc = sym.C.copy()
            en, xl, xs = trend_signals(sym, 168, 0.5)
            real.append(run(sym, en, xl, xs, SCAN_STOP, 0, 1.0))
            for r in range(MONKEY_RUNS):
                monk[r].append(run(sym, en, xl, xs, SCAN_STOP, 0, 1.0, rng=np.random.default_rng(15485863 * r + mins * 7 + len(real))))
            bar_h = sym.bar_h
            del sym, en, xl, xs
            step += 1
            progress(round(step / total * 95, 1), f"Espelmes de {mins} minuts ({label}): {s.split('/')[0]} fet")

        def merge(parts):
            cols = list(zip(*parts))
            out = {k: np.concatenate(v) for k, v in zip(("e", "x", "d", "net", "worst", "w", "f"), cols)}
            order = np.argsort(out["x"], kind="stable")
            return {k: v[order] for k, v in out.items()}
        t = merge(real)
        monkey = [period(merge(mp), WARMUP, len(base), notional)["pnl"] for mp in monk]
        del monk
        h = mins / 60
        rows.append({"vela_min": mins, "canal_h": round(168 * h, 1), "sortida_h": round(84 * h, 1), "atr_h": round(24 * h, 1),
                     "bot": mins == 60, **scan_row(t, monkey, base, bar_h, notional, dd_limit, WARMUP)})
        if mins == 60:
            info = period_info(base, btc, WARMUP)
    return rows, info, step


def run_scale(dd_limit: float, synthetic_data: bool, progress=lambda pct, msg: None) -> dict:
    total = len(SCALE_PERIODS) * len(SCALE_CANDLES_MIN) * len(SYMBOLS)
    step, out = 0, {}
    for key in SCALE_PERIODS:
        raw = load_5m(key, synthetic_data, lambda pct, msg: write_status(estat="descarregant", mode="escalat", pct=pct, missatge=msg))
        label = "2020-2023" if key == "passat" else "2023-2026"
        rows, info, step = scale_period(raw, dd_limit, progress, step, total, label)
        out[key] = {**info, "files": rows}
        del raw
    resum = [{"vela_min": m, "canal_h": out["passat"]["files"][k]["canal_h"],
              "supera_tots": all(out[p]["files"][k]["supera"] for p in out)} for k, m in enumerate(SCALE_CANDLES_MIN)]
    return {
        "versio": 7, "mode": "escalat", "generat": datetime.now(timezone.utc).isoformat(),
        "regles": {"canal_espelmes": 168, "sortida_espelmes": 84, "atr_espelmes": 24, "stop_atr": SCAN_STOP,
                   "exposicio_pct": round(SCAN_EXPOSURE * 100)},
        "costos": {"operacio_pct": round(2 * COST_SIDE * 100, 2), "financament_8h_pct": FUNDING_8H * 100},
        "caiguda_maxima_acceptada_pct": round(dd_limit * 100), "mico": MONKEY_RUNS,
        "periodes": out, "resum": resum,
    }


def run_tp(periods: dict, dd_limit: float, progress=lambda pct, msg: None) -> dict:
    total = len(periods) * len(TP_LEVELS) * MONKEY_RUNS
    step, out = 0, {}
    for key, syms in periods.items():
        first = next(iter(syms.values()))
        idx, bar_h = first.idx, first.bar_h
        notional = CAPITAL * SCAN_EXPOSURE / len(syms)
        label = "2020-2023" if key == "passat" else "2023-2026"
        rows = []
        for tp in TP_LEVELS:
            p = {"N": SCAN_BOT_CHANNEL, "sortida": 0.5, "stop": SCAN_STOP, "tp": tp}
            t = combo_trades(syms, "tendencia_bot", p)
            monkey = []
            for r in range(MONKEY_RUNS):
                mt = combo_trades(syms, "tendencia_bot", p, rng=np.random.default_rng(32452843 * r + int(tp * 1000)))
                monkey.append(period(mt, WARMUP, len(idx), notional)["pnl"])
                step += 1
                progress(round(step / total * 95, 1),
                         f"{'Sense objectiu' if not tp else f'Objectiu del {tp * 100:g} %'} ({label}): prova del mico {r + 1}/{MONKEY_RUNS}")
            rows.append({"objectiu_pct": round(tp * 100, 1) if tp else None, "bot": tp == 0,
                         **scan_row(t, monkey, idx, bar_h, notional, dd_limit, WARMUP)})
        btc = syms["BTC/EUR"].C if "BTC/EUR" in syms else first.C
        out[key] = {**period_info(idx, btc, WARMUP), "files": rows}
    resum = [{"objectiu_pct": round(tp * 100, 1) if tp else None,
              "supera_tots": all(out[k]["files"][j]["supera"] for k in out)} for j, tp in enumerate(TP_LEVELS)]
    return {
        "versio": 8, "mode": "objectius", "generat": datetime.now(timezone.utc).isoformat(),
        "regles": {"canal_h": SCAN_BOT_CHANNEL, "sortida_h": SCAN_BOT_CHANNEL // 2, "stop_atr": SCAN_STOP,
                   "exposicio_pct": round(SCAN_EXPOSURE * 100)},
        "costos": {"operacio_pct": round(2 * COST_SIDE * 100, 2), "financament_8h_pct": FUNDING_8H * 100},
        "caiguda_maxima_acceptada_pct": round(dd_limit * 100), "mico": MONKEY_RUNS,
        "periodes": out, "resum": resum,
    }


def run_scan(periods: dict, dd_limit: float, progress=lambda pct, msg: None) -> dict:
    total = len(periods) * len(SCAN_CHANNELS) * MONKEY_RUNS
    step, out = 0, {}
    for key, syms in periods.items():
        rows, info, step = scan_period(syms, dd_limit, progress, step, total)
        out[key] = {**info, "files": rows}
    both = []
    for k, N in enumerate(SCAN_CHANNELS):
        rs = [out[key]["files"][k] for key in out]
        both.append({"canal_h": N, "supera_tots": all(r["supera"] for r in rs)})
    return {
        "versio": 6, "mode": "terme_mig", "generat": datetime.now(timezone.utc).isoformat(),
        "regles": {"espelma_minuts": 60, "sortida": "canal de la meitat de durada", "stop_atr": SCAN_STOP, "atr_h": 24,
                   "exposicio_pct": round(SCAN_EXPOSURE * 100), "canal_bot_h": SCAN_BOT_CHANNEL},
        "costos": {"operacio_pct": round(2 * COST_SIDE * 100, 2), "financament_8h_pct": FUNDING_8H * 100},
        "caiguda_maxima_acceptada_pct": round(dd_limit * 100), "mico": MONKEY_RUNS,
        "periodes": out, "resum": both,
    }


def synthetic(symbols, timeframe, days, seed=7):
    """Preus INVENTATS amb fases de tendència. Només serveixen per comprovar que el codi funciona."""
    rng = np.random.default_rng(seed)
    mins = {"5m": 5, "15m": 15, "1h": 60}[timeframe]
    n = days * 1440 // mins
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor(f"{mins}min"), periods=n, freq=f"{mins}min")
    out = {}
    for s in symbols:
        vol = rng.uniform(0.004, 0.012) * np.sqrt(mins / 60)
        drift = np.repeat(rng.normal(0, vol / 4, n // 300 + 1), 300)[:n]
        close = 100 * np.exp(np.cumsum(drift + rng.normal(0, vol, n)))
        openp = np.r_[close[0], close[:-1]]
        spread = np.abs(rng.normal(0, vol, n)) * close
        out[s] = pd.DataFrame({"open": openp, "high": np.maximum(openp, close) + spread,
                               "low": np.minimum(openp, close) - spread, "close": close,
                               "volume": rng.uniform(1, 10, n)}, index=idx)
    return out


def load_past(synthetic_data: bool = False) -> dict:
    """1 hora, de PAST_START a PAST_END. Sense omplir enrere: abans de cotitzar, el valor no opera."""
    if synthetic_data:
        raw = {s: df.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
               for s, df in synthetic(SYMBOLS, "1h", 1370, seed=29).items()}
        raw["SOL/EUR"] = raw["SOL/EUR"].iloc[5000:]          # simula un valor que comença a cotitzar més tard
    else:
        raw = {}
        cache = os.path.join(LAB_DIR, "dades")
        for k, s in enumerate(SYMBOLS):
            write_status(estat="descarregant", mode="passat", pct=round(k / len(SYMBOLS) * 100, 1),
                         missatge=f"Descarregant {s.replace('/EUR', '')} del {PAST_START[:4]} al {PAST_END[:4]}")
            try:
                raw[s] = fetch_range(s, "1h", PAST_START, PAST_END, cache_dir=cache, quote="USDT")
            except Exception as e:
                log.warning("Salto %s: %s", s, e)
        if "BTC/EUR" not in raw:
            raise RuntimeError("No s'han pogut carregar les dades de Bitcoin")
    base = raw["BTC/EUR"].index
    return {s: Sym(raw.pop(s).reindex(base).ffill()) for s in list(raw)}   # el ffill no omple abans del primer preu


def load_fast(synthetic_data: bool = False) -> dict:
    """Espelmes de 15 minuts dels 3 últims anys (les mateixes dades ja descarregades), sense agrupar."""
    if synthetic_data:
        raw = synthetic(SYMBOLS, SRC_TF, DAYS, seed=17)
    else:
        raw = {}
        cache = os.path.join(LAB_DIR, "dades")
        for k, s in enumerate(SYMBOLS):
            write_status(estat="descarregant", mode="rapid", pct=round(k / len(SYMBOLS) * 100, 1),
                         missatge=f"Carregant 3 anys d'espelmes de 15 minuts de {s.replace('/EUR', '')}")
            try:
                raw[s] = fetch_history(s, SRC_TF, DAYS, cache_dir=cache, quote="USDT")
            except Exception as e:
                log.warning("Salto %s: %s", s, e)
        if "BTC/EUR" not in raw:
            raise RuntimeError("No s'han pogut carregar les dades de Bitcoin")
    base = raw["BTC/EUR"].index
    cols = ["open", "high", "low", "close"]
    return {s: Sym(raw.pop(s)[cols].reindex(base).ffill().bfill()) for s in list(raw)}


def load(synthetic_data: bool = False) -> dict:
    if synthetic_data:
        raw = synthetic(SYMBOLS, SRC_TF, DAYS, seed=13)
    else:
        raw = {}
        cache = os.path.join(LAB_DIR, "dades")
        for k, s in enumerate(SYMBOLS):
            write_status(estat="descarregant", pct=round(k / len(SYMBOLS) * 100, 1),
                         missatge=f"Carregant 3 anys d'espelmes de {s.replace('/EUR', '')}")
            try:
                raw[s] = fetch_history(s, SRC_TF, DAYS, cache_dir=cache, quote="USDT")
            except Exception as e:
                log.warning("Salto %s: %s", s, e)
        if "BTC/EUR" not in raw:
            raise RuntimeError("No s'han pogut carregar les dades de Bitcoin")
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    base = raw["BTC/EUR"].resample("1h").agg(agg).dropna().index
    syms = {}
    for s in list(raw):
        h = raw.pop(s).resample("1h").agg(agg).dropna()
        syms[s] = Sym(h.reindex(base).ffill().bfill())
    return syms


def main(cfg: dict, dd_limit_pct: float, synthetic: bool = False, past: bool = False, fast: bool = False,
         scan: bool = False, scale: bool = False, tp: bool = False, micro: bool = False, corr: bool = False) -> dict:
    t0 = time.time()
    mode = "micro" if micro else "correlacio" if corr else "objectius" if tp else "escalat" if scale else "terme_mig" if scan else "rapid" if fast else "passat" if past else "actual"
    write_status(estat="descarregant", mode=mode, pct=0, missatge="Preparant les dades")
    try:
        prog = lambda pct, msg: write_status(estat="provant", mode=mode, pct=pct, missatge=msg)   # noqa: E731
        if micro or corr:
            from . import lab_micro
            if micro:
                res = lab_micro.run_micro_lab(dd_limit_pct / 100, synthetic, prog)
            else:
                res = lab_micro.run_corr_lab({"passat": load_past(synthetic), "recent": load(synthetic)}, dd_limit_pct / 100, prog)
        elif tp:
            res = run_tp({"passat": load_past(synthetic), "recent": load(synthetic)}, dd_limit_pct / 100, prog)
        elif scale:
            res = run_scale(dd_limit_pct / 100, synthetic, prog)
        elif scan:
            periods = {"passat": load_past(synthetic), "recent": load(synthetic)}
            res = run_scan(periods, dd_limit_pct / 100, prog)
        elif fast:
            res = run_lab(load_fast(synthetic), dd_limit_pct / 100, prog, family_defs=FAST_FAMILIES, sizings=["fixa"])
        else:
            syms = load_past(synthetic) if past else load(synthetic)
            res = run_lab(syms, dd_limit_pct / 100, prog, families=PAST_FAMILIES if past else None)
        res["mode"] = mode
        res["durada_min"] = round((time.time() - t0) / 60, 1)
        res["dades_sintetiques"] = synthetic
        out = RESULTS_MICRO_FILE if micro else RESULTS_CORR_FILE if corr else RESULTS_TP_FILE if tp else RESULTS_SCALE_FILE if scale else RESULTS_SCAN_FILE if scan else RESULTS_FAST_FILE if fast else RESULTS_PAST_FILE if past else RESULTS_FILE
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(res, f, default=str)
        os.replace(tmp, out)
        write_status(estat="fet", mode=mode, pct=100, missatge=f"Prova acabada en {res['durada_min']} minuts")
        return res
    except Exception as e:
        log.exception("El laboratori ha fallat")
        write_status(estat="error", pct=0, missatge=f"La prova ha fallat: {e}")
        raise
