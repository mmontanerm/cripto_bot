"""Laboratori de CURT TERMINI v2: objectiu de guany NET fixat (0,3% / 0,4% / 0,5% per operació guanyadora).

Idea: cada operació té un objectiu de venda situat exactament al guany net desitjat MÉS els costos
d'anada i tornada. Així, cada operació guanyadora deixa un 0,3-0,5% net. El que decideix si
l'estratègia guanya diners és quantes operacions arriben a l'objectiu i quant perden les altres
(stop i límit de temps). Per això es mostren les dues coses per separat.

Famílies d'entrada (espelmes de 5 minuts, 6 valors més líquids, llarg i curt, sense apalancament):
  - Reversió amb tendència: el preu surt de les bandes de Bollinger a favor de la tendència de fons.
  - Retrocés RSI curt: RSI de 2 períodes extrem a favor de la tendència (EMA de 200 espelmes).
  - Ruptura amb volum: el preu trenca el rang de les últimes hores amb un volum molt superior al normal.
  - Graella: esglaons de mida = objectiu net + costos; cada lot deixa exactament l'objectiu net.

Mètode (per no enganyar-nos):
  1. ENTRENAMENT: els ~4 primers mesos. Es proven totes les combinacions i es tria la millor.
  2. VALIDACIÓ: els 2 últims mesos, que l'optimització no ha vist. Només compta aquest resultat.
  3. CONSISTÈNCIA: de les 10 millors combinacions de l'entrenament, quantes guanyen a la validació.

Criteri d'èxit, FIXAT ABANS de la prova (a la validació):
  1. Guanyar diners.
  2. No passar de la caiguda acceptada ni fer saltar l'aturada d'emergència.
  3. Que almenys 7 de les 10 millors combinacions de l'entrenament també guanyin a la validació.
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
from .strategy import ema, rsi

log = logging.getLogger("lab_ct")

STATUS_FILE = os.path.join(LAB_DIR, "estat_ct.json")
RESULTS_FILE = os.path.join(LAB_DIR, "resultats_ct.json")

TF = "5m"
DAYS = 183
TEST_DAYS = 60
WARMUP_BARS = 300
CAPITAL = 1000.0
SYMBOLS = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "DOGE/EUR", "ADA/EUR"]
MIN_TRADES_WEEK = 5               # vols moltes operacions: menys que això no compta com a curt termini

# Cost per costat (compra o venda): comissió + lliscament
COSTS = [
    {"id": "taker", "nom": "Futurs, ordres a mercat", "pct": 0.07,
     "desc": "Comissió 0,05% + lliscament 0,02% a cada costat (0,14% per operació). L'escenari realista per a un bot."},
    {"id": "maker", "nom": "Futurs, ordres limitades", "pct": 0.02,
     "desc": "Comissió 0,02% a cada costat (0,04% per operació). Optimista: suposa que totes les ordres limitades s'executen."},
]
TARGETS = [0.003, 0.004, 0.005]   # guany NET per operació guanyadora
SL_RATIOS = [0.75, 1.5, 3.0]      # stop = ràtio × distància de l'objectiu
TIME_STOPS = [12, 36]             # sortida per temps: 1 h o 3 h

FAMILIES = {
    "reversio": {"nom": "Reversió amb tendència",
                 "desc": "Entra quan el preu surt de les bandes de Bollinger (20 espelmes) a favor de la tendència de fons (EMA de 200): compra caigudes en tendència alcista i ven pujades en tendència baixista.",
                 "entry": {"k": [2.0, 2.5, 3.0], "filtre": [True, False]}},
    "rsi": {"nom": "Retrocés RSI curt",
            "desc": "Entra quan el RSI de 2 períodes és extrem (sobrevenut o sobrecomprat) a favor de la tendència de fons (EMA de 200).",
            "entry": {"llindar": [5, 10, 20]}},
    "ruptura": {"nom": "Ruptura amb volum",
                "desc": "Entra quan el preu trenca el màxim o el mínim de les últimes hores amb un volum molt superior a la mitjana.",
                "entry": {"n": [12, 36], "vol": [1.5, 2.5]}},
    "graella": {"nom": "Graella",
                "desc": "Compres escalonades per sota del preu; cada lot es ven un esglaó més amunt. L'esglaó és l'objectiu net més els costos. Si el preu cau per sota de la graella, ho ven tot.",
                "entry": {"nivells": [5, 10], "stop_esglaons": [2, 4]}},
}


def write_status(**kw) -> None:
    os.makedirs(LAB_DIR, exist_ok=True)
    kw["actualitzat"] = datetime.now(timezone.utc).isoformat()
    kw["pid"] = os.getpid()
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kw, f, default=str)
    os.replace(tmp, STATUS_FILE)


# ------------------------------------------------------------------ senyals d'entrada (vectoritzats)
def signals(df: pd.DataFrame, fam: str, p: dict) -> tuple[np.ndarray, np.ndarray]:
    """Retorna (índexs d'espelma amb senyal al tancament, direcció +1/-1)."""
    c = df["close"]
    trend = c > ema(c, 200)
    if fam == "reversio":
        ma, sd = c.rolling(20).mean(), c.rolling(20).std()
        lo, up = c < ma - p["k"] * sd, c > ma + p["k"] * sd
        long_, short = (lo & trend, up & ~trend) if p["filtre"] else (lo, up)
    elif fam == "rsi":
        r = rsi(c, 2)
        long_, short = (r < p["llindar"]) & trend, (r > 100 - p["llindar"]) & ~trend
    elif fam == "ruptura":
        hi = df["high"].rolling(p["n"]).max().shift(1)
        lw = df["low"].rolling(p["n"]).min().shift(1)
        v = df["volume"] > p["vol"] * df["volume"].rolling(50).mean().shift(1)
        long_, short = (c > hi) & v, (c < lw) & v
    else:
        raise ValueError(fam)
    d = np.where(long_.to_numpy(), 1, np.where(short.to_numpy(), -1, 0))
    d[:WARMUP_BARS] = 0
    idx = np.nonzero(d)[0]
    return idx, d[idx]


# ------------------------------------------------------------------ motor direccional per esdeveniments
def run_directional(arr: dict, sig_idx, sig_dir, tp: float, sl: float, T: int) -> list:
    """Una posició a la vegada. Entrada a l'obertura de l'espelma següent al senyal.
    Retorna operacions (espelma entrada, espelma sortida, rendiment brut, pitjor moment brut, motiu)."""
    O, H, L, C = arr["O"], arr["H"], arr["L"], arr["C"]
    n = len(C)
    out = []
    nxt = 0
    ns = len(sig_idx)
    while True:
        j = int(np.searchsorted(sig_idx, nxt))
        if j >= ns:
            break
        i, d = int(sig_idx[j]), int(sig_dir[j])
        e = i + 1
        if e >= n:
            break
        ep = O[e]
        end = min(e + T, n)
        hh, ll = H[e:end], L[e:end]
        if d > 0:
            tp_px, sl_px = ep * (1 + tp), ep * (1 - sl)
            hit_sl, hit_tp = ll <= sl_px, hh >= tp_px
        else:
            tp_px, sl_px = ep * (1 - tp), ep * (1 + sl)
            hit_sl, hit_tp = hh >= sl_px, ll <= tp_px
        a = int(np.argmax(hit_sl)) if hit_sl.any() else None
        b = int(np.argmax(hit_tp)) if hit_tp.any() else None
        if a is not None and (b is None or a <= b):            # empat dins l'espelma: suposem el stop
            k, why = a, "stop"
            xp = sl_px if k == 0 else (min(O[e + k], sl_px) if d > 0 else max(O[e + k], sl_px))
        elif b is not None:
            k, why = b, "objectiu"
            xp = tp_px if k == 0 else (max(O[e + k], tp_px) if d > 0 else min(O[e + k], tp_px))
        else:
            k, why = end - 1 - e, "temps"
            xp = C[end - 1]
        x = e + k
        worst = max(ll[:k + 1].min(), min(xp, ep)) if d > 0 else min(hh[:k + 1].max(), max(xp, ep))
        out.append((e, x, d * (xp - ep) / ep, d * (worst - ep) / ep, why))
        nxt = x
    return out


def run_grid(arr: dict, step: float, levels: int, stop_steps: int) -> tuple[list, np.ndarray, np.ndarray]:
    """Graella només llarga. Retorna operacions, P&L brut per espelma (per unitat de capital) i volum."""
    O, H, L, C = arr["O"], arr["H"], arr["L"], arr["C"]
    n = len(C)
    lot = 1.0 / levels
    gross = np.zeros(n)
    turn = np.zeros(n)
    trades = []
    realized = turnover = 0.0
    center = C[WARMUP_BARS]
    lv = [center * (1 - step * (j + 1)) for j in range(levels)]
    held: dict = {}
    for i in range(WARMUP_BARS, n):
        o, h, lo, c = O[i], H[i], L[i], C[i]
        for j in list(held):
            px, q, b = held[j]
            target = lv[j] * (1 + step)
            if b < i and h >= target:
                sell = max(o, target)
                realized += q * (sell - px)
                turnover += q * sell
                trades.append((b, i, (sell - px) / px, 0.0, "objectiu", lot))
                del held[j]
        for j in range(levels):
            if j not in held and lo <= lv[j]:
                px = min(o, lv[j])
                held[j] = (px, lot / px, i)
                turnover += lot
        if c < lv[-1] * (1 - stop_steps * step):
            for j, (px, q, b) in held.items():
                realized += q * (c - px)
                turnover += q * c
                trades.append((b, i, (c - px) / px, 0.0, "stop", lot))
            held.clear()
            center = c
            lv = [center * (1 - step * (j + 1)) for j in range(levels)]
        elif not held and c > center * (1 + 2 * step):
            center = c
            lv = [center * (1 - step * (j + 1)) for j in range(levels)]
        gross[i] = realized + sum(q * (c - px) for px, q, b in held.values())
        turn[i] = turnover
    return trades, gross, turn


# ------------------------------------------------------------------ cartera, segments i mètriques
def segment_stats(trades: list, curve: pd.Series, t0, t1, dd_limit: float | None, cost: float) -> dict:
    """Mètriques d'un segment [t0, t1). `trades`: (entrada_ts, net, notional). `curve`: capital (sense reiniciar)."""
    seg = curve[(curve.index >= t0) & (curve.index < t1)]
    if len(seg) < 2:
        return {"rendiment_pct": 0.0, "caiguda_pct": 0.0, "aturada": None, "operacions_setmana": 0,
                "guanyadores_pct": None, "net_guanyadora_pct": None, "net_perdedora_pct": None, "net_mitja_pct": None}
    prev = curve[curve.index < t0]
    base = prev.iloc[-1] if len(prev) else CAPITAL
    eq = (seg - base + CAPITAL).to_numpy()                      # el segment comença amb 1.000 €
    stopped = None
    ruin = np.nonzero(eq <= 0)[0]
    if len(ruin):
        eq = eq.copy()
        eq[int(ruin[0]):] = 0.0
    if dd_limit is not None:
        peak = np.maximum.accumulate(eq)
        hit = np.nonzero(eq < peak * (1 - dd_limit))[0]
        if len(hit):
            eq = eq.copy()
            eq[int(hit[0]):] = eq[int(hit[0])]
            stopped = str(seg.index[int(hit[0])].date())
    if len(ruin) and stopped is None:
        stopped = str(seg.index[int(ruin[0])].date())
    peak = np.maximum.accumulate(eq)
    dd = float(-(eq / np.where(peak > 0, peak, 1) - 1).min())
    nets = np.array([nt for ts, nt, nl in trades if t0 <= ts < t1])
    weeks = max((t1 - t0).total_seconds() / 604800, 1e-9)
    win = nets[nets > 0]
    loss = nets[nets <= 0]
    return {"rendiment_pct": round(float(eq[-1] / CAPITAL - 1) * 100, 2), "caiguda_pct": round(dd * 100, 1),
            "aturada": stopped, "operacions_setmana": round(len(nets) / weeks),
            "guanyadores_pct": round(float(len(win) / len(nets) * 100), 1) if len(nets) else None,
            "net_guanyadora_pct": round(float(win.mean() * 100), 3) if len(win) else None,
            "net_perdedora_pct": round(float(loss.mean() * 100), 3) if len(loss) else None,
            "net_mitja_pct": round(float(nets.mean() * 100), 3) if len(nets) else None,
            "_eq": eq, "_idx": seg.index}


class Lab:
    def __init__(self, data: dict, dd_limit: float):
        self.data = data
        self.dd_limit = dd_limit
        idx = next(iter(data.values())).index
        self.idx = idx
        self.arr = {s: {"O": df["open"].to_numpy(float), "H": df["high"].to_numpy(float),
                        "L": df["low"].to_numpy(float), "C": df["close"].to_numpy(float)} for s, df in data.items()}
        self.notional = CAPITAL / len(data)
        self.t_start = idx[WARMUP_BARS]
        self.t_end = idx[-1] + pd.Timedelta(minutes=5)
        self.t_split = self.t_end - pd.Timedelta(days=TEST_DAYS)
        self._sig: dict = {}

    def sig(self, s, fam, p):
        key = (s, fam, tuple(sorted(p.items())))
        if key not in self._sig:
            self._sig[key] = signals(self.data[s], fam, p)
        return self._sig[key]

    def evaluate(self, fam: str, entry: dict, target: float, cost: float, sl_ratio=None, T=None) -> dict:
        """Executa una combinació a tot el període i en separa entrenament i validació."""
        tp = target + 2 * cost                                   # objectiu brut = net desitjat + costos
        events = []                                              # (temps, delta de capital)
        trades = []                                              # (entrada_ts, net, notional)
        reasons = {"objectiu": 0, "stop": 0, "temps": 0}
        if fam == "graella":
            gross = turn = None
            for s, a in self.arr.items():
                tr, g, t = run_grid(a, tp, entry["nivells"], entry["stop_esglaons"])
                gross = g if gross is None else gross + g
                turn = t if turn is None else turn + t
                for b, x, gr, _, why, lot in tr:
                    trades.append((self.idx[b], gr - 2 * cost, lot))
                    reasons[why] += 1
            curve = pd.Series(CAPITAL + self.notional * (gross - cost * turn), index=self.idx).iloc[WARMUP_BARS:]
        else:
            sl = sl_ratio * tp
            for s, a in self.arr.items():
                si, sd = self.sig(s, fam, entry)
                for e, x, gr, worst, why in run_directional(a, si, sd, tp, sl, T):
                    nt = gr - 2 * cost
                    trades.append((self.idx[e], nt, self.notional))
                    reasons[why] += 1
                    t_exit = self.idx[x]
                    # pitjor moment de l'operació (just abans de tancar) i resultat final
                    events.append((t_exit - pd.Timedelta(seconds=1), self.notional * (worst - cost), True))
                    events.append((t_exit, self.notional * nt, False))
            curve = self._curve(events)
        return {"train": segment_stats(trades, curve, self.t_start, self.t_split, None, cost),
                "test": segment_stats(trades, curve, self.t_split, self.t_end, self.dd_limit, cost),
                "motius": reasons}

    def _curve(self, events: list) -> pd.Series:
        """Capital a partir de les operacions tancades, amb el pitjor moment de cada operació."""
        if not events:
            return pd.Series([CAPITAL, CAPITAL], index=[self.t_start, self.t_end - pd.Timedelta(minutes=5)])
        events.sort(key=lambda x: (x[0], x[2] is False))
        t, v = [self.t_start], [CAPITAL]
        cap = CAPITAL
        for ts, delta, is_worst in events:
            if is_worst:
                t.append(ts)
                v.append(cap + delta)
            else:
                cap += delta
                t.append(ts)
                v.append(cap)
        t.append(self.t_end - pd.Timedelta(minutes=5))
        v.append(cap)
        return pd.Series(v, index=pd.DatetimeIndex(t))


def params_text(fam: str, p: dict) -> str:
    if fam == "reversio":
        return f"bandes de {p['k']:g} desviacions, {'amb' if p['filtre'] else 'sense'} filtre de tendència"
    if fam == "rsi":
        return f"RSI(2) per sota de {p['llindar']} (o per sobre de {100 - p['llindar']})"
    if fam == "ruptura":
        return f"rang de {p['n'] * 5 / 60:g} h, volum {p['vol']:g} vegades la mitjana"
    return f"{p['nivells']} nivells, stop a {p['stop_esglaons']} esglaons per sota"


def combo_text(fam, entry, target, sl_ratio, T):
    base = f"objectiu net {target * 100:g} %, {params_text(fam, entry)}"
    if fam == "graella":
        return base
    return base + f", stop {sl_ratio:g}× l'objectiu, màxim {T * 5 // 60} h"


def clean(st: dict) -> dict:
    return {k: v for k, v in st.items() if not k.startswith("_")}


def run(data: dict, dd_limit: float, progress=lambda pct, msg: None) -> dict:
    lab = Lab(data, dd_limit)
    plan = []
    for c in COSTS:
        for fam, spec in FAMILIES.items():
            entries = [dict(zip(spec["entry"], x)) for x in itertools.product(*spec["entry"].values())]
            if fam == "graella":
                plan += [(c, fam, e, tg, None, None) for e in entries for tg in TARGETS]
            else:
                plan += [(c, fam, e, tg, r, T) for e in entries for tg in TARGETS for r in SL_RATIOS for T in TIME_STOPS]
    results: dict = {}
    for k, (c, fam, e, tg, r, T) in enumerate(plan):
        res = lab.evaluate(fam, e, tg, c["pct"] / 100, r, T)
        results.setdefault((c["id"], fam), []).append({"entry": e, "target": tg, "sl": r, "T": T, **res})
        if k % 5 == 0 or k == len(plan) - 1:
            progress(round((k + 1) / len(plan) * 100, 1), f"{c['nom']} · {FAMILIES[fam]['nom']}: {combo_text(fam, e, tg, r, T)}")

    btc = data["BTC/EUR"]["close"]
    btc_test = btc[btc.index >= lab.t_split]
    btc_eq = btc_test / btc_test.iloc[0] * CAPITAL
    btc_dd = float(-(btc_eq / btc_eq.cummax() - 1).min())

    scenarios = []
    for c in COSTS:
        fams = []
        for fam, spec in FAMILIES.items():
            rows = results[(c["id"], fam)]

            def ok_train(x):
                t = x["train"]
                return t["caiguda_pct"] <= dd_limit * 100 and t["operacions_setmana"] >= MIN_TRADES_WEEK

            ranked = sorted(rows, key=lambda x: (ok_train(x), x["train"]["rendiment_pct"]), reverse=True)
            best = ranked[0]
            top = ranked[:10]
            consist = sum(1 for x in top if x["test"]["rendiment_pct"] > 0)
            pos_train = sum(1 for x in rows if x["train"]["rendiment_pct"] > 0)
            pos_test = sum(1 for x in rows if x["test"]["rendiment_pct"] > 0)
            te = best["test"]
            ok1 = bool(te["rendiment_pct"] > 0)
            ok2 = bool(te["aturada"] is None and te["caiguda_pct"] <= dd_limit * 100 + 0.5)
            ok3 = bool(consist >= 7)
            curve = pd.Series(te["_eq"], index=te["_idx"]).resample("1h").last().dropna()
            fams.append({
                "id": fam, "nom": spec["nom"], "desc": spec["desc"],
                "triada": combo_text(fam, best["entry"], best["target"], best["sl"], best["T"]),
                "objectiu_net_pct": round(best["target"] * 100, 2),
                "objectiu_brut_pct": round((best["target"] + 2 * c["pct"] / 100) * 100, 2),
                "entrenament": clean(best["train"]), "validacio": clean(te), "motius": best["motius"],
                "combinacions": len(rows), "positives_entrenament": pos_train, "positives_validacio": pos_test,
                "top10_positives_validacio": consist, "top10_total": len(top),
                "top10": [{"text": combo_text(fam, x["entry"], x["target"], x["sl"], x["T"]),
                           "entrenament_pct": x["train"]["rendiment_pct"], "validacio_pct": x["test"]["rendiment_pct"]} for x in top],
                "criteri": [{"text": "Guanya diners a la validació (2 mesos no vistos)", "ok": ok1},
                            {"text": f"A la validació no passa del {round(dd_limit * 100)} % de caiguda ni fa saltar l'aturada", "ok": ok2},
                            {"text": f"Almenys 7 de les 10 millors combinacions de l'entrenament guanyen a la validació ({consist} de {len(top)})", "ok": ok3}],
                "superat": bool(ok1 and ok2 and ok3),
                "corba": [[str(t), round(float(v), 2)] for t, v in bt._downsample_series(curve).items()],
            })
        scenarios.append({"id": c["id"], "nom": c["nom"], "desc": c["desc"], "cost_operacio_pct": round(2 * c["pct"], 2),
                          "families": fams})
    # Preferència: l'escenari realista (ordres a mercat). Les ordres limitades només si no n'hi ha cap.
    winners = [(s["id"], f["id"], f["validacio"]["rendiment_pct"]) for s in scenarios for f in s["families"] if f["superat"]]
    real = [w for w in winners if w[0] == COSTS[0]["id"]]
    best = max(real or winners, key=lambda x: x[2]) if winners else None
    return {
        "versio": 2,
        "generat": datetime.now(timezone.utc).isoformat(),
        "entrenament": {"des_de": str(lab.t_start.date()), "fins": str(lab.t_split.date())},
        "validacio": {"des_de": str(lab.t_split.date()), "fins": str(lab.idx[-1].date())},
        "valors": [s.replace("/EUR", "") for s in data],
        "caiguda_maxima_acceptada_pct": round(dd_limit * 100),
        "objectius_net_pct": [t * 100 for t in TARGETS],
        "combinacions_total": len(plan),
        "escenaris": scenarios,
        "btc_validacio": {"rendiment_pct": round(float(btc_eq.iloc[-1] / CAPITAL - 1) * 100, 1), "caiguda_pct": round(btc_dd * 100, 1),
                          "corba": [[str(t), round(float(v), 2)] for t, v in bt._downsample_series(btc_eq.resample("1h").last().dropna()).items()]},
        "guanyadora": {"escenari": best[0], "familia": best[1]} if best else None,
    }


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
