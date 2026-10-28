"""Dues proves més del laboratori (demanades el 28-10-2026).

1) MICRO-GUANYS AMB ORDRES LIMITADES
   Quan el bot no té posició en un valor, a cada tancament d'espelma deixa dues ordres limitades per a l'espelma
   següent: una de compra a la banda baixa de Bollinger (20 espelmes, 2 desviacions) i una de venda en curt a la
   banda alta. Si el preu hi arriba, entra; després posa un objectiu de guany petit (ordre limitada) i un stop
   ajustat (ordre a mercat). Si al cap de 16 espelmes no ha passat res, tanca a mercat.
   Costos: les ordres limitades (entrada i objectiu) paguen la comissió de creador de mercat, un 0,02 %; els stops
   i els tancaments per temps paguen un 0,05 % més un 0,02 % de lliscament. Els llargs paguen finançament.
   Per no enganyar-nos:
     - una ordre limitada només s'executa si el preu la TRAVESSA (no n'hi ha prou que la toqui);
     - a l'espelma d'entrada es compta el stop si el preu hi arriba, però mai l'objectiu (el cas pitjor);
     - si en una espelma el preu toca el stop i l'objectiu, es compta el stop;
     - si en una espelma s'executarien les dues entrades, no se'n compta cap.
   Cada configuració es compara amb 25 simulacions que entren a l'atzar (mateixa freqüència i costos, mateixes
   sortides), i també es calcula què hauria passat pagant ordres a mercat (0,14 %) per veure el pes dels costos.

2) LÍMIT DE POSICIONS EN LA MATEIXA DIRECCIÓ
   Les regles exactes del bot, però amb un màxim de posicions obertes alhora en la mateixa direcció
   (6 = el bot actual, 3, 2 i 1). Les criptos es mouen juntes: quatre llargs alhora són gairebé una sola aposta.
   Simulació hora a hora de tots els valors junts. Amb el màxim de 6 dona exactament el mateix que el bot.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import lab_wf as L

MAKER = 0.0002                     # comissió de creador de mercat (ordres limitades)
TAKER = L.COST_SIDE                # 0,05 % + 0,02 % de lliscament (ordres a mercat)
MICRO_CANDLES = [15, 60]           # minuts
MICRO_TP = [0.003, 0.005, 0.01]
MICRO_SL = [0.005, 0.01, 0.02]
MICRO_N, MICRO_K, MICRO_T = 20, 2.0, 16
MAX_SAME = [6, 3, 2, 1]


# ------------------------------------------------------------------ 1) micro-guanys
def run_micro(s, tp: float, sl: float, rng=None, stats=None):
    """Operacions d'un valor. Retorna arrays: entrada, sortida, direcció, net (maker), net (taker), pitjor, pes, guany brut."""
    O, H, Lo, C = s.O, s.H, s.L, s.C
    n = len(C)
    ma, lob, upb = s.boll(MICRO_N, MICRO_K)
    start = int(np.argmax(np.isfinite(C))) + L.WARMUP
    with np.errstate(invalid="ignore"):
        prev_lo = np.r_[np.nan, lob[:-1]]
        prev_up = np.r_[np.nan, upb[:-1]]
        buy = Lo < prev_lo                           # l'ordre de compra posada al tancament anterior es travessa
        sell = H > prev_up
    both = buy & sell
    buy &= ~both
    sell &= ~both
    cand = buy | sell
    if rng is not None:
        p_in, p_long = stats
    out = [[] for _ in range(6)]
    nxt = start + 1
    while True:
        if rng is None:
            e = L._first(lambda u, v: cand[u:v], nxt, n)
            if e is None:
                break
            d = 1 if buy[e] else -1
            ep = min(O[e], prev_lo[e]) if d > 0 else max(O[e], prev_up[e])
        else:
            e = nxt + int(rng.geometric(p_in)) - 1
            if e >= n:
                break
            d = 1 if rng.random() < p_long else -1
            ep = O[e]
        if not np.isfinite(ep) or ep <= 0:
            nxt = e + 1
            continue
        sp, tpp = ep * (1 - d * sl), ep * (1 + d * tp)
        end = min(n, e + MICRO_T)
        hit0 = (Lo[e] <= sp) if d > 0 else (H[e] >= sp)
        if hit0:                                     # stop a la mateixa espelma d'entrada
            x, xp, exit_cost = e, sp, TAKER
        else:
            a = L._first((lambda u, v: Lo[u:v] <= sp) if d > 0 else (lambda u, v: H[u:v] >= sp), e + 1, end)
            g = L._first((lambda u, v: H[u:v] > tpp) if d > 0 else (lambda u, v: Lo[u:v] < tpp), e + 1, end)
            if a is not None and (g is None or a <= g):
                x, exit_cost = a, TAKER
                xp = min(O[x], sp) if d > 0 else max(O[x], sp)
            elif g is not None:
                x, xp, exit_cost = g, tpp, MAKER
            else:
                x, xp, exit_cost = end - 1, C[end - 1], TAKER
        gross = d * (xp - ep) / ep
        fund = L.FUNDING_8H * (x - e + 1) * s.bar_h / 8 if d > 0 else 0.0
        out[0].append(e)
        out[1].append(x)
        out[2].append(d)
        out[3].append(gross - MAKER - exit_cost - fund)
        out[4].append(gross - 2 * TAKER - fund)
        out[5].append(gross)
        nxt = x + 1
    k = len(out[0])
    return {"e": np.asarray(out[0], dtype=np.int64), "x": np.asarray(out[1], dtype=np.int64),
            "d": np.asarray(out[2], dtype=np.int8), "net": np.asarray(out[3]), "net_taker": np.asarray(out[4]),
            "gross": np.asarray(out[5]), "worst": np.minimum(np.asarray(out[3]), 0), "w": np.ones(k), "f": np.ones(k)}


def _merge(parts: list) -> dict:
    keys = parts[0].keys()
    out = {k: np.concatenate([p[k] for p in parts]) for k in keys}
    order = np.argsort(out["x"], kind="stable")
    return {k: v[order] for k, v in out.items()}


def _row(t: dict, monkey: list, idx, notional: float, dd_limit: float) -> dict:
    n = len(idx)
    i0 = L.WARMUP
    years = max((idx[-1] - idx[i0]).days / 365, 0.01)
    days = max((idx[-1] - idx[i0]).days, 1)
    st = L.period(t, i0, n, notional)
    brake = L.period(t, i0, n, notional, dd_limit)
    taker = L.period({**t, "net": t["net_taker"], "worst": np.minimum(t["net_taker"], 0)}, i0, n, notional)
    m = st["mask"]
    nets, gross = t["net"][m], t["gross"][m]
    nn = int(m.sum())
    costs = float(((gross - nets) * notional).sum())
    monkey = np.asarray(monkey)
    pctile = float((monkey < st["pnl"]).mean() * 100) if len(monkey) else None
    return {
        "rendiment_pct": round(st["pnl"] / L.CAPITAL * 100, 1),
        "anual_pct": round(((max(L.CAPITAL + st["pnl"], 0.01) / L.CAPITAL) ** (1 / years) - 1) * 100, 1),
        "rendiment_taker_pct": round(taker["pnl"] / L.CAPITAL * 100, 1),
        "caiguda_pct": round(st["dd"] * 100, 1),
        "fre": str(idx[brake["stopped_bar"]].date()) if brake["stopped_bar"] is not None else None,
        "operacions": nn, "operacions_dia": round(nn / days, 2),
        "guanyadores_pct": round(float((nets > 0).mean() * 100), 1) if nn else None,
        "net_guanyadora_pct": round(float(nets[nets > 0].mean() * 100), 2) if (nets > 0).any() else None,
        "net_perdedora_pct": round(float(nets[nets <= 0].mean() * 100), 2) if (nets <= 0).any() else None,
        "brut_mitja_pct": round(float(gross.mean() * 100), 3) if nn else None,
        "net_mitja_pct": round(float(nets.mean() * 100), 3) if nn else None,
        "costos_anuals_pct_capital": round(costs / L.CAPITAL / years * 100, 1),
        "mico_mediana_pct": round(float(np.median(monkey)) / L.CAPITAL * 100, 1) if len(monkey) else None,
        "mico_percentil": round(pctile) if pctile is not None else None,
        "supera": bool(st["pnl"] > 0 and pctile is not None and pctile >= 95 and nn >= L.MIN_TRADES_WF),
    }


def micro_period(raw: dict, dd_limit: float, progress, step0: int, total: int, label: str):
    notional = L.CAPITAL * L.SCAN_EXPOSURE / len(raw)
    combos = [(tp, sl) for tp in MICRO_TP for sl in MICRO_SL]
    rows, step, info = [], step0, None
    for mins in MICRO_CANDLES:
        base = L.resample_bars(raw["BTC/EUR"], mins).index
        real = {c: [] for c in combos}
        monk = {c: np.zeros(L.MONKEY_RUNS) for c in combos}     # guany de cada simulació (només cal la suma)
        btc = None
        for j, (s, df) in enumerate(raw.items()):
            sym = L.Sym(L.resample_bars(df, mins).reindex(base).ffill())
            if s == "BTC/EUR":
                btc = sym.C.copy()
            n = len(sym.C)
            start = int(np.argmax(np.isfinite(sym.C))) + L.WARMUP
            for c in combos:
                t = run_micro(sym, *c)
                real[c].append(t)
                held = int((t["x"] - t["e"] + 1).sum())
                stats = (min(1.0, max(len(t["e"]), 1) / max(n - start - held, 1)),
                         float((t["d"] > 0).mean()) if len(t["e"]) else 0.5)
                for r in range(L.MONKEY_RUNS):
                    mt = run_micro(sym, *c, rng=np.random.default_rng(49979687 * r + mins * 31 + j * 7
                                                                     + int(c[0] * 1e4) + int(c[1] * 1e5)), stats=stats)
                    monk[c][r] += float(mt["net"].sum()) * notional
            del sym
            step += 1
            progress(round(step / total * 95, 1), f"Micro-guanys, espelmes de {mins} min ({label}): {s.split('/')[0]} fet")
        for c in combos:
            t = _merge(real[c])
            monkey = list(monk[c])
            rows.append({"vela_min": mins, "objectiu_pct": round(c[0] * 100, 2), "stop_pct": round(c[1] * 100, 2),
                         **_row(t, monkey, base, notional, dd_limit)})
        if info is None:
            info = L.period_info(base, btc, L.WARMUP)
    return rows, info, step


def run_micro_lab(dd_limit: float, synthetic_data: bool, progress=lambda pct, msg: None) -> dict:
    total = 2 * len(MICRO_CANDLES) * len(L.SYMBOLS)
    step, out = 0, {}
    for key in ("passat", "recent"):
        raw = L.load_5m(key, synthetic_data,
                        lambda pct, msg: L.write_status(estat="descarregant", mode="micro", pct=pct, missatge=msg))
        label = "2020-2023" if key == "passat" else "2023-2026"
        rows, info, step = micro_period(raw, dd_limit, progress, step, total, label)
        out[key] = {**info, "files": rows}
        del raw
    resum = [{"vela_min": r["vela_min"], "objectiu_pct": r["objectiu_pct"], "stop_pct": r["stop_pct"],
              "supera_tots": all(out[k]["files"][i]["supera"] for k in out)} for i, r in enumerate(out["passat"]["files"])]
    return {
        "versio": 9, "mode": "micro", "generat": datetime.now(timezone.utc).isoformat(),
        "regles": {"bollinger_espelmes": MICRO_N, "desviacions": MICRO_K, "temps_maxim_espelmes": MICRO_T,
                   "comissio_limitada_pct": MAKER * 100, "cost_mercat_pct": TAKER * 100,
                   "exposicio_pct": round(L.SCAN_EXPOSURE * 100)},
        "caiguda_maxima_acceptada_pct": round(dd_limit * 100), "mico": L.MONKEY_RUNS,
        "periodes": out, "resum": resum,
    }


# ------------------------------------------------------------------ 2) límit de posicions en la mateixa direcció
def portfolio_bot(syms: dict, max_same: int) -> dict:
    """Regles del bot amb tots els valors junts, hora a hora, i un màxim de posicions en la mateixa direcció."""
    names = list(syms)
    S = [syms[s] for s in names]
    n = len(S[0].C)
    sig = []
    for s in S:
        en, xl, xs = L.trend_signals(s, L.SCAN_BOT_CHANNEL, 0.5)
        en = en.copy()
        en[:int(np.argmax(np.isfinite(s.C))) + L.WARMUP] = 0
        sig.append((en, xl, xs))
    pos = [None] * len(S)                       # (direcció, espelma d'entrada, preu, stop, pitjor)
    out = [[] for _ in range(5)]

    def close(k, x, xp):
        d, e, ep, sp, worst = pos[k]
        gross = d * (xp - ep) / ep
        fund = L.FUNDING_8H * (x - e + 1) * S[k].bar_h / 8 if d > 0 else 0.0
        out[0].append(e); out[1].append(x); out[2].append(d)
        out[3].append(gross - 2 * L.COST_SIDE - fund)
        out[4].append(min(d * (worst - ep) / ep, gross) - L.COST_SIDE)
        pos[k] = None

    for t in range(n):
        # 1) posicions obertes: stop dins l'espelma (prioritat) o senyal de sortida al tancament
        for k, s in enumerate(S):
            p = pos[k]
            if p is None or t < p[1]:
                continue
            d, e, ep, sp, worst = p
            hit = (s.L[t] <= sp) if d > 0 else (s.H[t] >= sp)
            if hit:
                pos[k] = (d, e, ep, sp, min(worst, sp) if d > 0 else max(worst, sp))
                close(k, t, sp if t == e else (min(s.O[t], sp) if d > 0 else max(s.O[t], sp)))
                continue
            pos[k] = (d, e, ep, sp, min(worst, s.L[t]) if d > 0 else max(worst, s.H[t]))
            ex = sig[k][1][t] if d > 0 else sig[k][2][t]
            if ex and t + 1 < n:
                pos[k] = (*pos[k][:4], min(pos[k][4], s.O[t + 1]) if d > 0 else max(pos[k][4], s.O[t + 1]))
                close(k, t + 1, s.O[t + 1])
                pos[k] = "sortint"                 # no pot tornar a entrar amb el senyal d'aquesta mateixa espelma
        # 2) entrades: senyal al tancament d'aquesta espelma, entrada a l'obertura de la següent
        if t + 1 < n:
            for k, s in enumerate(S):
                if pos[k] is not None:
                    continue
                d = int(sig[k][0][t])
                if not d or not np.isfinite(s.atr[t]) or s.atr[t] <= 0 or not np.isfinite(s.O[t + 1]):
                    continue
                same = sum(1 for p in pos if isinstance(p, tuple) and p[0] == d)
                if same >= max_same:
                    continue
                ep = s.O[t + 1]
                pos[k] = (d, t + 1, ep, ep - d * L.SCAN_STOP * s.atr[t], ep)
        pos = [None if p == "sortint" else p for p in pos]
    for k, p in enumerate(pos):                 # final de les dades: es tanca al darrer preu, com al laboratori
        if isinstance(p, tuple):
            pos[k] = (*p[:4], min(p[4], S[k].L[n - 1]) if p[0] > 0 else max(p[4], S[k].H[n - 1]))
            close(k, n - 1, S[k].C[n - 1])
    k = len(out[0])
    order = np.argsort(np.asarray(out[1]), kind="stable")
    res = {"e": np.asarray(out[0], dtype=np.int64), "x": np.asarray(out[1], dtype=np.int64),
           "d": np.asarray(out[2], dtype=np.int8), "net": np.asarray(out[3]), "worst": np.asarray(out[4]),
           "w": np.ones(k), "f": np.ones(k)}
    return {kk: v[order] for kk, v in res.items()}


def run_corr_lab(periods: dict, dd_limit: float, progress=lambda pct, msg: None) -> dict:
    out, step, total = {}, 0, len(periods) * len(MAX_SAME)
    for key, syms in periods.items():
        first = next(iter(syms.values()))
        idx = first.idx
        notional = L.CAPITAL * L.SCAN_EXPOSURE / len(syms)
        rows = []
        for m in MAX_SAME:
            t = portfolio_bot(syms, m)
            years = max((idx[-1] - idx[L.WARMUP]).days / 365, 0.01)
            st = L.period(t, L.WARMUP, len(idx), notional)
            brake = L.period(t, L.WARMUP, len(idx), notional, dd_limit)
            nets = t["net"][st["mask"]]
            nn = len(nets)
            rows.append({"max_mateixa_direccio": m, "bot": m == 6,
                         "rendiment_pct": round(st["pnl"] / L.CAPITAL * 100, 1),
                         "anual_pct": round(((max(L.CAPITAL + st["pnl"], 0.01) / L.CAPITAL) ** (1 / years) - 1) * 100, 1),
                         "caiguda_pct": round(st["dd"] * 100, 1),
                         "guany_per_caiguda": round(st["pnl"] / L.CAPITAL / st["dd"], 2) if st["dd"] > 0 else None,
                         "fre": str(idx[brake["stopped_bar"]].date()) if brake["stopped_bar"] is not None else None,
                         "operacions": nn, "operacions_setmana": round(nn / years / 52, 1),
                         "guanyadores_pct": round(float((nets > 0).mean() * 100), 1) if nn else None,
                         "net_mitja_pct": round(float(nets.mean() * 100), 3) if nn else None})
            step += 1
            progress(round(step / total * 95, 1), f"Límit de {m} posicions en la mateixa direcció ({key})")
        btc = syms["BTC/EUR"].C if "BTC/EUR" in syms else first.C
        out[key] = {**L.period_info(idx, btc, L.WARMUP), "files": rows}
    return {"versio": 10, "mode": "correlacio", "generat": datetime.now(timezone.utc).isoformat(),
            "caiguda_maxima_acceptada_pct": round(dd_limit * 100), "periodes": out}
