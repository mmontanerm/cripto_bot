"""Laboratori de GRAELLA amb stop petit per lot, sobre els 3 últims anys de dades reals (espelmes de 15 minuts).

Com funciona la graella:
  - Es col·loquen N nivells de compra per sota del preu, separats per un esglaó.
  - L'esglaó és el guany NET desitjat (0,3% / 0,4% / 0,5%) més els costos d'anada i tornada:
    cada lot que es ven un esglaó més amunt deixa exactament el guany net desitjat.
  - STOP PER LOT (la novetat): cada lot es ven si cau K esglaons per sota del seu preu de compra
    (K = 1, 2 o 3). Així cap operació perdedora pot perdre gaire més que K esglaons.
  - Un nivell que ha fet saltar el stop no torna a comprar fins que el preu no recupera aquell nivell.
  - Si no hi ha cap lot obert i el preu ha pujat, la graella es torna a centrar a sobre.
  - Filtre de tendència opcional: només compra quan el preu és per sobre de la seva mitjana
    de 200 espelmes (unes 50 hores).

Execució prudent amb espelmes de 15 minuts: compres i stops dins l'espelma (stop al pitjor preu si
l'espelma obre per sota); un lot comprat no es pot vendre fins a l'espelma següent.

Mètode:
  - ENTRENAMENT: els 2 primers anys dels 3 últims. Es tria la millor combinació.
  - VALIDACIÓ: l'últim any, que l'optimització no ha vist. Només compta aquest resultat.
  - CONSISTÈNCIA: de les 10 millors combinacions de l'entrenament, quantes guanyen a la validació.

Criteri d'èxit, FIXAT ABANS de la prova:
  1. Guanya diners a l'entrenament I a la validació.
  2. A la validació no passa de la caiguda acceptada ni fa saltar l'aturada d'emergència.
  3. Almenys 7 de les 10 millors combinacions de l'entrenament també guanyen a la validació.
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
from .strategy import ema

log = logging.getLogger("lab_graella")

STATUS_FILE = os.path.join(LAB_DIR, "estat_graella.json")
RESULTS_FILE = os.path.join(LAB_DIR, "resultats_graella.json")

TF = "15m"
DAYS = 1100                       # els 3 últims anys (i uns dies d'escalfament)
TEST_DAYS = 365                   # validació: l'últim any
WARMUP_BARS = 400
CAPITAL = 1000.0
SYMBOLS = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "DOGE/EUR", "ADA/EUR"]
COST_SIDE = 0.0007                # futurs, ordres a mercat: 0,05% comissió + 0,02% lliscament per costat

GRID = {
    "objectiu": [0.003, 0.004, 0.005],   # guany net per lot guanyador
    "nivells": [5, 10],
    "stop": [1, 2, 3],                   # stop per lot, en esglaons per sota del preu de compra
    "filtre": [False, True],             # només compra amb el preu per sobre de la mitjana de 200 espelmes
}


def write_status(**kw) -> None:
    os.makedirs(LAB_DIR, exist_ok=True)
    kw["actualitzat"] = datetime.now(timezone.utc).isoformat()
    kw["pid"] = os.getpid()
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kw, f, default=str)
    os.replace(tmp, STATUS_FILE)


# ------------------------------------------------------------------ motor de graella (per esdeveniments)
def run_grid(O, H, L, C, trend_ok, step: float, levels: int, stop_k: int, use_filter: bool):
    """Simula la graella en un valor amb capital 1 (cada lot = 1/levels).

    Retorna:
      trades: llista de (espelma de compra, espelma de venda, rendiment brut del lot, motiu)
      ev_i, ev_q, ev_basis, ev_real, ev_turn: estat després de cada esdeveniment (per reconstruir
      el capital espelma a espelma de manera vectoritzada).
    """
    n = len(C)
    lot = 1.0 / levels
    trades = []
    ev_i, ev_q, ev_basis, ev_real, ev_turn = [WARMUP_BARS], [0.0], [0.0], [0.0], [0.0]
    q_tot = basis = realized = turnover = 0.0

    def make_levels(center):
        return [center * (1 - step * (j + 1)) for j in range(levels)]

    center = C[WARMUP_BARS]
    lv = make_levels(center)
    armed = [True] * levels
    held = {}                                    # nivell -> [preu, quantitat, espelma, preu de stop]
    INF = float("inf")

    def thresholds():
        buy = max((lv[j] for j in range(levels) if armed[j] and j not in held), default=-INF)
        sell = min((lv[j] * (1 + step) for j in held), default=INF)
        stop = max((v[3] for v in held.values()), default=-INF)
        rearm = min((lv[j] for j in range(levels) if not armed[j]), default=INF)
        return buy, sell, stop, rearm

    buy_t, sell_t, stop_t, rearm_t = thresholds()
    recenter_t = center * (1 + 2 * step)
    for i in range(WARMUP_BARS, n):
        lo, h = L[i], H[i]
        can_buy = (not use_filter) or trend_ok[i]
        # camí ràpid: en la gran majoria d'espelmes no passa res
        if (lo > buy_t or not can_buy) and h < sell_t and lo > stop_t and h < rearm_t and (held or C[i] <= recenter_t):
            continue
        o, c = O[i], C[i]
        changed = False
        # 1) stops per lot primer (prudent: si dins l'espelma es podien tocar stop i objectiu, compta el stop)
        for j in list(held):
            px, q, b, sp = held[j]
            if b < i and lo <= sp:
                sell = min(o, sp)
                realized += q * (sell - px)
                turnover += q * sell
                q_tot -= q
                basis -= q * px
                trades.append((b, i, (sell - px) / px, "stop"))
                del held[j]
                armed[j] = False
                changed = True
        # 2) vendes a l'objectiu (només lots comprats en espelmes anteriors)
        for j in list(held):
            px, q, b, sp = held[j]
            target = lv[j] * (1 + step)
            if b < i and h >= target:
                sell = max(o, target)
                realized += q * (sell - px)
                turnover += q * sell
                q_tot -= q
                basis -= q * px
                trades.append((b, i, (sell - px) / px, "objectiu"))
                del held[j]
                changed = True
        # 3) rearmar nivells quan el preu els recupera
        for j in range(levels):
            if not armed[j] and h >= lv[j]:
                armed[j] = True
        # 4) compres; si en la mateixa espelma el preu també arriba al stop del lot, compta el stop
        if can_buy:
            for j in range(levels):
                if armed[j] and j not in held and lo <= lv[j]:
                    px = min(o, lv[j])
                    q = lot / px
                    sp = px * (1 - stop_k * step)
                    turnover += lot
                    changed = True
                    if lo <= sp:
                        realized += q * (sp - px)
                        turnover += q * sp
                        trades.append((i, i, (sp - px) / px, "stop"))
                        armed[j] = False
                        continue
                    held[j] = [px, q, i, sp]
                    q_tot += q
                    basis += q * px
        # 5) recentrar si no hi ha res obert i el preu ha pujat
        if not held and c > center * (1 + 2 * step):
            center = c
            lv = make_levels(center)
            armed = [True] * levels
        recenter_t = center * (1 + 2 * step)
        buy_t, sell_t, stop_t, rearm_t = thresholds()
        if changed:
            ev_i.append(i)
            ev_q.append(q_tot)
            ev_basis.append(basis)
            ev_real.append(realized)
            ev_turn.append(turnover)
    return trades, ev_i, ev_q, ev_basis, ev_real, ev_turn


def equity_series(C: np.ndarray, ev_i, ev_q, ev_basis, ev_real, ev_turn, cost: float) -> np.ndarray:
    """Capital (per unitat) espelma a espelma, a partir dels esdeveniments: realitzat + latent - costos."""
    n = len(C)
    pos = np.searchsorted(np.asarray(ev_i), np.arange(n), side="right") - 1
    pos = np.clip(pos, 0, None)
    q = np.asarray(ev_q)[pos]
    basis = np.asarray(ev_basis)[pos]
    real = np.asarray(ev_real)[pos]
    turn = np.asarray(ev_turn)[pos]
    pnl = real + q * C - basis - cost * turn
    pnl[:WARMUP_BARS] = 0.0
    return pnl


# ------------------------------------------------------------------ mètriques
def seg_stats(eq: np.ndarray, idx: pd.DatetimeIndex, nets: list, t0, t1, dd_limit=None, compound=False) -> dict:
    m = (idx >= t0) & (idx < t1)
    seg = eq[m]
    sidx = idx[m]
    if len(seg) < 2:
        return {"rendiment_pct": 0.0, "anual_pct": 0.0, "caiguda_pct": 0.0, "aturada": None, "operacions_setmana": 0}
    prev = eq[idx < t0]
    base = prev[-1] if len(prev) else CAPITAL
    # Graella: mida fixa, el guany se suma. Comprar i mantenir: el guany es multiplica.
    s = seg / base * CAPITAL if compound else seg - base + CAPITAL
    stopped = None
    ruin = np.nonzero(s <= 0)[0]
    if len(ruin):
        s = s.copy()
        s[int(ruin[0]):] = 0.0
        stopped = str(sidx[int(ruin[0])].date())
    if dd_limit is not None:
        pk = np.maximum.accumulate(s)
        hit = np.nonzero(s < pk * (1 - dd_limit))[0]
        if len(hit) and (stopped is None or hit[0] < ruin[0]):
            s = s.copy()
            s[int(hit[0]):] = s[int(hit[0])]
            stopped = str(sidx[int(hit[0])].date())
    pk = np.maximum.accumulate(s)
    dd = float(-(s / np.where(pk > 0, pk, 1) - 1).min())
    total = float(s[-1] / CAPITAL - 1)
    days = max((t1 - t0).total_seconds() / 86400, 1)
    annual = (1 + total) ** (365 / days) - 1 if total > -1 else -1.0
    bars, vals = nets if nets else (np.array([], dtype=np.int64), np.array([]))
    i0, i1 = idx.searchsorted(t0), idx.searchsorted(t1)
    sel = vals[(bars >= i0) & (bars < i1)]
    win, loss = sel[sel > 0], sel[sel <= 0]
    out = {"rendiment_pct": round(total * 100, 1), "anual_pct": round(annual * 100, 1), "caiguda_pct": round(dd * 100, 1),
           "aturada": stopped, "operacions_setmana": round(len(sel) / (days / 7)),
           "guanyadores_pct": round(float(len(win) / len(sel) * 100), 1) if len(sel) else None,
           "net_guanyadora_pct": round(float(win.mean() * 100), 3) if len(win) else None,
           "net_perdedora_pct": round(float(loss.mean() * 100), 3) if len(loss) else None,
           "net_mitja_pct": round(float(sel.mean() * 100), 3) if len(sel) else None}
    out["_s"], out["_idx"] = s, sidx
    return out


def yearly(eq: np.ndarray, idx: pd.DatetimeIndex, compound=False) -> dict:
    s = pd.Series(eq, index=idx).resample("1D").last().dropna()
    out = {}
    prev = s.iloc[0]
    for y, seg in s.groupby(s.index.year):
        base = prev if compound else CAPITAL            # graella: guany de l'any sobre els 1.000 € de partida
        out[str(y)] = round(float((seg.iloc[-1] - prev) / base * 100), 1)
        prev = seg.iloc[-1]
    return out


def combo_text(p: dict) -> str:
    return (f"objectiu net {p['objectiu'] * 100:g} %, {p['nivells']} nivells, "
            f"stop a {p['stop']} {'esglaó' if p['stop'] == 1 else 'esglaons'}, {'amb' if p['filtre'] else 'sense'} filtre de tendència")


def clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def run(data: dict, dd_limit: float, progress=lambda pct, msg: None) -> dict:
    idx = next(iter(data.values())).index
    # Arrays compactes; es converteixen a llistes (més ràpides per al bucle) un valor cada vegada
    arr = {}
    for s, df in data.items():
        c = df["close"]
        arr[s] = (df["open"].to_numpy(float), df["high"].to_numpy(float), df["low"].to_numpy(float),
                  c.to_numpy(float), (c > ema(c, 200)).to_numpy())
    notional = CAPITAL / len(data)
    t_start = idx[WARMUP_BARS]
    t_end = idx[-1] + pd.Timedelta(minutes=15)
    t_split = t_end - pd.Timedelta(days=TEST_DAYS)
    combos = [dict(zip(GRID, v)) for v in itertools.product(*GRID.values())]
    rows = []
    best_key, best_eq, best_nets = None, None, None

    def key(st):
        return (st["caiguda_pct"] <= dd_limit * 100, st["rendiment_pct"])

    for k, p in enumerate(combos):
        step = p["objectiu"] + 2 * COST_SIDE
        eq = np.full(len(idx), CAPITAL)
        bars_l, nets_l = [], []
        reasons = {"objectiu": 0, "stop": 0}
        for s, (O, H, L, Cn, tr) in arr.items():
            trades, *ev = run_grid(O.tolist(), H.tolist(), L.tolist(), Cn.tolist(), tr.tolist(),
                                   step, p["nivells"], p["stop"], p["filtre"])
            eq += notional * equity_series(Cn, *ev, COST_SIDE)
            for b, x, g, why in trades:
                bars_l.append(b)
                nets_l.append(g - 2 * COST_SIDE)
                reasons[why] += 1
            del trades, ev
        nets = (np.asarray(bars_l, dtype=np.int64), np.asarray(nets_l))
        del bars_l, nets_l
        tr_st = seg_stats(eq, idx, nets, t_start, t_split)
        te_st = seg_stats(eq, idx, nets, t_split, t_end, dd_limit)
        if best_key is None or key(tr_st) > best_key:          # només es guarda la corba de la millor
            best_key, best_eq, best_nets = key(tr_st), eq, nets
        rows.append({"p": p, "train": clean(tr_st), "test": clean(te_st), "motius": reasons})
        progress(round((k + 1) / len(combos) * 100, 1), f"Combinació {k + 1} de {len(combos)}: {combo_text(p)}")

    def ok_train(r):
        return r["train"]["caiguda_pct"] <= dd_limit * 100
    ranked = sorted(rows, key=lambda r: (ok_train(r), r["train"]["rendiment_pct"]), reverse=True)
    best = ranked[0]
    best["eq"] = best_eq
    best["train"] = seg_stats(best_eq, idx, best_nets, t_start, t_split)
    best["test"] = seg_stats(best_eq, idx, best_nets, t_split, t_end, dd_limit)
    top = ranked[:10]
    consist = sum(1 for r in top if r["test"]["rendiment_pct"] > 0)
    tr_b, te_b = best["train"], best["test"]
    ok1 = bool(tr_b["rendiment_pct"] > 0 and te_b["rendiment_pct"] > 0)
    ok2 = bool(te_b["aturada"] is None and te_b["caiguda_pct"] <= dd_limit * 100 + 0.5)
    ok3 = bool(consist >= 7)

    btc = data["BTC/EUR"]["close"].to_numpy(float)
    btc_eq = btc / btc[WARMUP_BARS] * CAPITAL
    btc_tr = seg_stats(btc_eq, idx, [], t_start, t_split, compound=True)
    btc_te = seg_stats(btc_eq, idx, [], t_split, t_end, compound=True)

    def curve(st):
        s = pd.Series(st["_s"], index=st["_idx"]).resample("1D").last().dropna()
        return [[str(t), round(float(v), 2)] for t, v in bt._downsample_series(s).items()]

    full = pd.Series(best["eq"], index=idx).iloc[WARMUP_BARS:].resample("1D").last().dropna()
    btc_full = pd.Series(btc_eq, index=idx).iloc[WARMUP_BARS:].resample("1D").last().dropna()
    return {
        "versio": 3,
        "generat": datetime.now(timezone.utc).isoformat(),
        "entrenament": {"des_de": str(t_start.date()), "fins": str(t_split.date())},
        "validacio": {"des_de": str(t_split.date()), "fins": str(idx[-1].date())},
        "valors": [s.replace("/EUR", "") for s in data],
        "temporalitat": TF,
        "cost_operacio_pct": round(2 * COST_SIDE * 100, 2),
        "caiguda_maxima_acceptada_pct": round(dd_limit * 100),
        "combinacions_total": len(combos),
        "triada": {"text": combo_text(best["p"]), "parametres": best["p"],
                   "esglao_pct": round((best["p"]["objectiu"] + 2 * COST_SIDE) * 100, 2),
                   "entrenament": clean(tr_b), "validacio": clean(te_b), "motius": best["motius"],
                   "per_any": yearly(best["eq"][WARMUP_BARS:], idx[WARMUP_BARS:]),
                   "corba_validacio": curve(te_b),
                   "corba_total": [[str(t), round(float(v), 2)] for t, v in bt._downsample_series(full).items()]},
        "criteri": [{"text": "Guanya diners a l'entrenament i a la validació", "ok": ok1},
                    {"text": f"A la validació no passa del {round(dd_limit * 100)} % de caiguda ni fa saltar l'aturada", "ok": ok2},
                    {"text": f"Almenys 7 de les 10 millors combinacions de l'entrenament guanyen a la validació ({consist} de {len(top)})", "ok": ok3}],
        "superat": bool(ok1 and ok2 and ok3),
        "top10": [{"text": combo_text(r["p"]), "entrenament_pct": r["train"]["rendiment_pct"],
                   "validacio_pct": r["test"]["rendiment_pct"], "validacio_caiguda_pct": r["test"]["caiguda_pct"]} for r in top],
        "totes": [{**r["p"], "entrenament_pct": r["train"]["rendiment_pct"], "validacio_pct": r["test"]["rendiment_pct"]} for r in rows],
        "positives_entrenament": sum(1 for r in rows if r["train"]["rendiment_pct"] > 0),
        "positives_validacio": sum(1 for r in rows if r["test"]["rendiment_pct"] > 0),
        "btc": {"entrenament_pct": btc_tr["rendiment_pct"], "validacio_pct": btc_te["rendiment_pct"],
                "validacio_caiguda_pct": btc_te["caiguda_pct"], "per_any": yearly(btc_eq[WARMUP_BARS:], idx[WARMUP_BARS:], compound=True),
                "corba_validacio": curve(btc_te),
                "corba_total": [[str(t), round(float(v), 2)] for t, v in bt._downsample_series(btc_full).items()]},
    }


def load(synthetic: bool = False) -> dict:
    if synthetic:
        import run_backtest as rb
        return rb.synthetic(SYMBOLS, TF, DAYS, seed=9)
    data = {}
    cache = os.path.join(LAB_DIR, "dades")
    for k, s in enumerate(SYMBOLS):
        write_status(estat="descarregant", pct=round(k / len(SYMBOLS) * 100, 1),
                     missatge=f"Descarregant 3 anys d'espelmes de 15 minuts de {s} (la primera vegada tarda uns minuts)")
        try:
            data[s] = fetch_history(s, TF, DAYS, cache_dir=cache, quote="USDT")
        except Exception as e:
            log.warning("Salto %s: %s", s, e)
    if "BTC/EUR" not in data:
        raise RuntimeError("No s'han pogut descarregar les dades de Bitcoin")
    idx = data["BTC/EUR"].index
    # Valors que encara no cotitzaven: preu pla (la graella no hi opera fins que comencen)
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
        log.exception("El laboratori de graella ha fallat")
        write_status(estat="error", pct=0, missatge=f"La prova ha fallat: {e}")
        raise
