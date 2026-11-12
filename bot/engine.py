"""Bot 24/7 · TENDÈNCIA LLARG/CURT en simulació, amb preus reals de Kraken.

Les mateixes regles que al laboratori:
  - Cada hora, en tancar l'espelma, mira cada valor:
      · si tanca per sobre del màxim de les últimes 168 h → obre una posició LLARGA (guanya si puja);
      · si tanca per sota del mínim de les últimes 168 h  → obre una posició CURTA (guanya si baixa).
  - Surt d'un llarg quan tanca per sota del mínim de les últimes 84 h (d'un curt, per sobre del màxim).
  - Stop de pèrdues a 4 × ATR del preu d'entrada, vigilat cada minut.
  - Una posició com a màxim per valor. Mida fixa: (capital × exposició) / nombre de valors.
  - Costos de futurs amb ordres a mercat: comissió + lliscament a cada costat; els llargs paguen finançament.
  - Fre d'emergència: si el capital cau més de la caiguda màxima des del seu màxim, ho tanca tot i s'atura.
  - Gestió de la sortida (estrategia.sortida, es tria al panell): protecció a preu d'entrada, stop seguidor o
    canal de sortida més curt quan l'operació ja guanya. Exactament les mateixes regles que la prova de sortides
    del laboratori: el millor preu i el stop s'actualitzen a cada tancament d'hora; el stop es vigila cada minut.
  - Límit de posicions en la mateixa direcció (risc.max_mateixa_direccio, es tria al panell).

El panell pot donar ordres (pausa, reprèn, tancar-ho tot, rearmar, reiniciar el compte) a través de
state/ordres.json, i canviar l'exposició i el fre a través de state/ajustos.yaml.
"""
from __future__ import annotations

import json
import logging
import os
import signal as sig
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .config import EXIT_MODES, ConfigWatcher
from .data import KrakenFeed
from .paths import COMMANDS, EQUITY, STATE, STATE_DIR, STATUS, TRADES

log = logging.getLogger("bot")

REASON = {"sortida_canal": "sortida per canal", "stop": "stop de pèrdues", "manual": "tancada des del panell",
          "aturada_emergencia": "aturada d'emergència", "reinici": "reinici del compte"}
# Referència del laboratori (tendència, mida fixa, períodes no vistos 2020-2023 i 2023-2026)
LAB_REF = {"guanyadores_pct": [26, 30], "net_guanyadora_pct": [7.4, 15.4], "net_perdedora_pct": [-4.1, -2.7],
           "net_mitja_pct": [0.33, 0.99], "operacions_setmana": [6.3, 8.0], "caiguda_pct_100": [19.6, 23.7]}


def now_utc() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc))


def save_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, path)       # escriptura atòmica: mai queda un fitxer a mitges


def indicators(df: pd.DataFrame, e: dict) -> dict:
    """Nivells de l'última espelma TANCADA (igual que al laboratori)."""
    n_in, n_out = int(e["canal_entrada_h"]), int(e["canal_sortida_h"])
    n_fast = int(EXIT_MODES.get(e.get("sortida", "actual"), ("", {}))[1].get("fast_n", 0)) or n_out
    h, lo, c = df["high"], df["low"], df["close"]
    prev = c.shift()
    tr = pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / float(e["atr_h"]), adjust=False).mean()
    return {
        "ts": df.index[-1], "close": float(c.iloc[-1]), "high": float(h.iloc[-1]), "low": float(lo.iloc[-1]),
        "max_entrada": float(h.rolling(n_in).max().shift(1).iloc[-1]),
        "min_entrada": float(lo.rolling(n_in).min().shift(1).iloc[-1]),
        "max_sortida": float(h.rolling(n_out).max().shift(1).iloc[-1]),
        "min_sortida": float(lo.rolling(n_out).min().shift(1).iloc[-1]),
        "max_rapida": float(h.rolling(n_fast).max().shift(1).iloc[-1]),
        "min_rapida": float(lo.rolling(n_fast).min().shift(1).iloc[-1]),
        "atr": float(atr.iloc[-1]),
    }


@dataclass
class Position:
    symbol: str
    side: int              # +1 llarg, -1 curt
    qty: float
    entry: float           # preu d'entrada (amb lliscament)
    entry_ts: str
    atr: float
    stop: float
    fee_in: float = 0.0
    funding: float = 0.0   # finançament pagat (llargs)
    bars: int = 0
    best: float = 0.0      # millor preu des de l'entrada (màxim en un llarg, mínim en un curt)

    @property
    def collateral(self) -> float:
        return self.qty * self.entry

    def value(self, price: float) -> float:
        """Valor en EUR. Sense apalancament: llarg = valor de mercat; curt = garantia + guany/pèrdua."""
        return self.qty * price if self.side == 1 else self.collateral + (self.entry - price) * self.qty

    def pnl(self, price: float) -> float:
        return self.value(price) - self.collateral - self.fee_in - self.funding


@dataclass
class Account:
    cash: float
    initial: float
    created: str
    positions: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)
    last_price: dict = field(default_factory=dict)
    last_bar: dict = field(default_factory=dict)
    peak: float = 0.0
    halted: bool = False
    halted_ts: str = ""
    paused: bool = False
    fees: float = 0.0
    funding: float = 0.0
    btc_start: float = 0.0

    def equity(self) -> float:
        return self.cash + sum(p.value(self.last_price.get(s, p.entry)) for s, p in self.positions.items())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["positions"] = {s: asdict(p) for s, p in self.positions.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        pos = {s: Position(**p) for s, p in d.pop("positions", {}).items()}
        a = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        a.positions = pos
        return a


class Engine:
    def __init__(self, config_path: str = "config.yaml") -> None:
        os.makedirs(STATE_DIR, exist_ok=True)
        self.watcher = ConfigWatcher(config_path)
        self.cfg = self.watcher.cfg
        self.feed = KrakenFeed()
        self.running = True
        self.started = str(now_utc())
        self.market: dict = {}
        self.blocked: dict = {}
        self.unavailable: dict = {}
        self._last_equity_log = 0.0
        self._n_trades = -1
        if os.path.exists(STATE):
            with open(STATE, encoding="utf-8") as f:
                self.acc = Account.from_dict(json.load(f))
            log.info("Compte recuperat: %.2f EUR, %d posicions obertes", self.acc.equity(), len(self.acc.positions))
        else:
            self.acc = self._new_account()
        sig.signal(sig.SIGTERM, self._stop)
        sig.signal(sig.SIGINT, self._stop)

    def _new_account(self) -> Account:
        cap = float(self.cfg["paper"]["initial_capital"])
        for f in (TRADES, EQUITY):
            if os.path.exists(f):
                os.remove(f)
        log.info("Compte simulat nou amb %.2f EUR · estratègia de tendència llarg/curt", cap)
        return Account(cash=cap, initial=cap, created=str(now_utc()), peak=cap)

    def _stop(self, *_):
        log.info("Aturant el bot de forma ordenada...")
        self.running = False

    # ------------------------------------------------------------------ costos
    @property
    def fee(self) -> float:
        return self.cfg["paper"]["fee_pct"] / 100

    @property
    def slip(self) -> float:
        return self.cfg["paper"]["slippage_pct"] / 100

    # ------------------------------------------------------------------ obrir / tancar
    def _open(self, s: str, side: int, price: float, atr: float, ts) -> None:
        a, r, e = self.acc, self.cfg["risc"], self.cfg["estrategia"]
        limit = int(r.get("max_mateixa_direccio", 6))
        same = sum(1 for p in a.positions.values() if p.side == side)
        if same >= limit:
            log.info("No obre %s %s: ja hi ha %d posicions %s (límit %d)", "LLARG" if side == 1 else "CURT", s, same,
                     "llargues" if side == 1 else "curtes", limit)
            self.blocked[s] = side
            return
        fill = price * (1 + self.slip * side)
        notional = a.equity() * r["exposicio_pct"] / 100 / len(self.cfg["symbols"])
        notional = min(notional, a.cash / (1 + self.fee) * 0.999)
        if notional < 5:
            log.warning("No hi ha prou efectiu per obrir %s", s)
            return
        fee = notional * self.fee
        a.cash -= notional + fee
        a.fees += fee
        pos = Position(symbol=s, side=side, qty=notional / fill, entry=fill, entry_ts=str(ts), atr=atr,
                       stop=fill - side * float(e["stop_atr"]) * atr, fee_in=fee, best=fill)
        a.positions[s] = pos
        log.info("OBRE %s %s · %.2f EUR a %s · stop %s", "LLARG" if side == 1 else "CURT", s, notional,
                 f"{fill:.6g}", f"{pos.stop:.6g}")

    def _close(self, s: str, price: float, ts, reason: str) -> None:
        a = self.acc
        pos = a.positions.pop(s)
        fill = price * (1 - self.slip * pos.side)
        proceeds = pos.value(fill)
        fee = pos.qty * fill * self.fee
        a.cash += proceeds - fee
        a.fees += fee
        pnl = proceeds - fee - pos.collateral - pos.fee_in - pos.funding
        hours = max((pd.Timestamp(ts) - pd.Timestamp(pos.entry_ts)).total_seconds() / 3600, 0)
        a.trades.append({
            "symbol": s, "side": "llarg" if pos.side == 1 else "curt", "entry_ts": pos.entry_ts, "exit_ts": str(ts),
            "entry": round(pos.entry, 8), "exit": round(fill, 8), "eur": round(pos.collateral, 2),
            "pnl_eur": round(pnl, 4), "pnl_pct": round(pnl / pos.collateral * 100, 3),
            "fees_eur": round(pos.fee_in + fee, 4), "funding_eur": round(pos.funding, 4),
            "hours": round(hours, 1), "reason": reason,
        })
        log.info("TANCA %s %s a %s · %s · %+.2f EUR (%+.2f %%)", "LLARG" if pos.side == 1 else "CURT", s,
                 f"{fill:.6g}", REASON.get(reason, reason), pnl, pnl / pos.collateral * 100)

    @staticmethod
    def _update_best(pos: Position, df: pd.DataFrame) -> None:
        """Millor preu des de l'entrada, amb les espelmes tancades (inclou les anteriors si la posició ja era oberta)."""
        since = df[df.index >= pd.Timestamp(pos.entry_ts).floor("h")]
        if not pos.best:
            pos.best = pos.entry
        if len(since):
            pos.best = max(pos.best, float(since["high"].max())) if pos.side == 1 else min(pos.best, float(since["low"].min()))

    @staticmethod
    def _manage_stop(pos: Position, mg: dict, gain_atr: float) -> None:
        """Mou el stop a favor (mai en contra) segons la gestió de sortida triada."""
        new = pos.stop
        if mg.get("trail"):
            t = pos.best - pos.side * mg["trail"] * pos.atr
            new = max(new, t) if pos.side == 1 else min(new, t)
        if mg.get("be") and gain_atr >= mg["be"]:
            new = max(new, pos.entry) if pos.side == 1 else min(new, pos.entry)
        if new != pos.stop:
            log.info("STOP %s mogut a favor: %s → %s", pos.symbol, f"{pos.stop:.6g}", f"{new:.6g}")
            pos.stop = new

    def close_all(self, ts, reason: str) -> None:
        for s in list(self.acc.positions):
            self._close(s, self.acc.last_price.get(s, self.acc.positions[s].entry), ts, reason)

    # ------------------------------------------------------------------ ordres del panell
    def handle_commands(self) -> bool:
        if not os.path.exists(COMMANDS):
            return False
        try:
            with open(COMMANDS, encoding="utf-8") as f:
                cmds = json.load(f)
        except Exception:
            cmds = []
        os.remove(COMMANDS)
        ts = now_utc()
        for c in cmds if isinstance(cmds, list) else []:
            what = c.get("accio")
            if what == "pausa":
                self.acc.paused = True
                log.info("PANELL: pausa. No obrirà posicions noves; les obertes continuen protegides.")
            elif what == "repren":
                self.acc.paused = False
                log.info("PANELL: es reprèn l'operativa.")
            elif what == "tanca_tot":
                log.info("PANELL: tancar totes les posicions i posar en pausa.")
                self.close_all(ts, "manual")
                self.acc.paused = True
            elif what == "rearma" and self.acc.halted:
                self.acc.halted = False
                self.acc.peak = self.acc.equity()
                log.info("PANELL: fre d'emergència rearmat. Nou màxim de referència: %.2f EUR", self.acc.peak)
            elif what == "reinicia":
                log.info("PANELL: reinici del compte simulat.")
                self.close_all(ts, "reinici")
                self.acc = self._new_account()
                self._n_trades = -1
                self._last_equity_log = 0.0
        self._persist(ts)
        return True

    # ------------------------------------------------------------------ cicle principal
    def cycle(self) -> None:
        if self.watcher.reload():
            self.cfg = self.watcher.cfg
        a, e = self.acc, self.cfg["estrategia"]
        ts = now_utc()
        symbols = list(dict.fromkeys(self.cfg["symbols"] + list(a.positions)))
        pending: list = []
        for s in symbols:
            if time.time() - self.unavailable.get(s, 0) < 3600:
                continue
            try:
                df, price = self.feed.candles(s, "1h")
            except KeyError as ex:
                log.warning("%s · el salto i ho torno a provar d'aquí a una hora", ex)
                self.unavailable[s] = time.time()
                continue
            except Exception as ex:
                log.warning("No he pogut llegir %s: %s", s, ex)
                continue
            time.sleep(0.4)                                   # respecta el límit de l'API pública
            a.last_price[s] = price
            if s == "BTC/EUR" and not a.btc_start:
                a.btc_start = price
            pos = a.positions.get(s)

            # 1) Stop vigilat cada minut (s'executa al stop o pitjor, si el preu ja l'ha passat)
            if pos and ((pos.side == 1 and price <= pos.stop) or (pos.side == -1 and price >= pos.stop)):
                self._close(s, min(price, pos.stop) if pos.side == 1 else max(price, pos.stop), ts, "stop")
                pos = None

            if len(df) < int(e["canal_entrada_h"]) + 50:
                self.market[s] = {"preu": price, "estat": "sense_dades", "text": "Encara no hi ha prou historial"}
                continue
            ind = indicators(df, e)
            bar = str(ind["ts"])
            new_bar = bar != a.last_bar.get(s)
            fresh = (ts - (ind["ts"] + pd.Timedelta(hours=1))) < pd.Timedelta(minutes=30)

            if new_bar:
                a.last_bar[s] = bar
                if pos:
                    # Stop tocat dins l'espelma entre dues revisions (un pic curt): es dona per executat al stop
                    after_entry = ind["ts"] >= pd.Timestamp(pos.entry_ts).floor("h")
                    hit = (ind["low"] <= pos.stop) if pos.side == 1 else (ind["high"] >= pos.stop)
                    if after_entry and hit:
                        self._close(s, pos.stop, ts, "stop")
                        pos = None
                if pos:
                    pos.bars += 1
                    if pos.side == 1:                         # finançament dels llargs, hora a hora
                        f = pos.collateral * self.cfg["paper"]["funding_8h_pct"] / 100 / 8
                        pos.funding += f
                        a.cash -= f
                        a.funding += f
                    mg = EXIT_MODES.get(e.get("sortida", "actual"), ("", {}))[1]
                    self._update_best(pos, df)
                    gain_atr = pos.side * (pos.best - pos.entry) / pos.atr if pos.atr > 0 else 0.0
                    out = (ind["close"] < ind["min_sortida"]) if pos.side == 1 else (ind["close"] > ind["max_sortida"])
                    if mg.get("fast_atr") and gain_atr >= mg["fast_atr"]:   # ja guanya prou: canal de sortida curt
                        out = out or ((ind["close"] < ind["min_rapida"]) if pos.side == 1 else (ind["close"] > ind["max_rapida"]))
                    if out:
                        self._close(s, price, ts, "sortida_canal")   # a l'obertura de l'espelma següent ≈ preu actual
                    else:
                        self._manage_stop(pos, mg, gain_atr)
                elif fresh and not a.paused and not a.halted:
                    # les entrades es fan quan s'han revisat tots els valors (primer les sortides, com al laboratori)
                    if ind["close"] > ind["max_entrada"]:
                        pending.append((s, 1, price, ind["atr"], ind))
                    elif ind["close"] < ind["min_entrada"]:
                        pending.append((s, -1, price, ind["atr"], ind))
            self.market[s] = self._explain(s, price, ind)
        for s, side, price, atr, ind in pending:
            self._open(s, side, price, atr, ts)
            self.market[s] = self._explain(s, price, ind)

        # Fre d'emergència
        eq = a.equity()
        a.peak = max(a.peak, eq)
        limit = self.cfg["risc"]["caiguda_maxima_pct"] / 100
        if not a.halted and eq < a.peak * (1 - limit):
            log.error("ATURADA D'EMERGÈNCIA: caiguda del %.1f %% des del màxim. Ho tanco tot.", (1 - eq / a.peak) * 100)
            self.close_all(ts, "aturada_emergencia")
            a.halted, a.halted_ts = True, str(ts)
            for m in self.market.values():
                m.update(estat="aturat", text="Fre d'emergència actiu: no obre posicions.", sortida=None, stop=None)
        self._persist(ts)

    def _explain(self, s: str, price: float, ind: dict) -> dict:
        a = self.acc
        pos = a.positions.get(s)
        up, dn = ind["max_entrada"], ind["min_entrada"]
        info = {"preu": price, "tancament": ind["close"], "espelma": str(ind["ts"]),
                "entrada_llarg": up, "entrada_curt": dn, "dist_llarg_pct": (up / price - 1) * 100,
                "dist_curt_pct": (dn / price - 1) * 100, "atr_pct": ind["atr"] / price * 100}
        if pos:
            lvl = ind["min_sortida"] if pos.side == 1 else ind["max_sortida"]
            mg = EXIT_MODES.get(self.cfg["estrategia"].get("sortida", "actual"), ("", {}))[1]
            gain_atr = pos.side * ((pos.best or pos.entry) - pos.entry) / pos.atr if pos.atr > 0 else 0.0
            if mg.get("fast_atr") and gain_atr >= mg["fast_atr"]:
                lvl = max(lvl, ind["min_rapida"]) if pos.side == 1 else min(lvl, ind["max_rapida"])
            info.update(estat="llarg" if pos.side == 1 else "curt", sortida=lvl, stop=pos.stop)
            info["text"] = (f"Posició {'llarga' if pos.side == 1 else 'curta'}. Surt si una hora tanca "
                            f"{'per sota' if pos.side == 1 else 'per sobre'} de {lvl:.6g} € o si toca el stop ({pos.stop:.6g} €).")
        elif a.halted:
            info.update(estat="aturat", text="Fre d'emergència actiu: no obre posicions.")
        elif a.paused:
            info.update(estat="pausa", text="En pausa: no obre posicions noves.")
        elif s in self.blocked and ((self.blocked[s] == 1 and ind["close"] > up) or (self.blocked[s] == -1 and ind["close"] < dn)):
            info.update(estat="limit", text="Ha donat senyal, però ja hi ha el màxim de posicions en aquesta direcció.")
        else:
            self.blocked.pop(s, None)
            near = min(abs(info["dist_llarg_pct"]), abs(info["dist_curt_pct"]))
            info["estat"] = "a_prop" if near < 1.0 else "esperant"
            info["text"] = (f"Entrarà en llarg si una hora tanca per sobre de {up:.6g} € ({info['dist_llarg_pct']:+.1f} %) "
                            f"o en curt si tanca per sota de {dn:.6g} € ({info['dist_curt_pct']:+.1f} %).")
        return info

    # ------------------------------------------------------------------ estat per al panell
    def _stats(self) -> dict:
        t = self.acc.trades
        n = len(t)
        days = max((now_utc() - pd.Timestamp(self.acc.created)).total_seconds() / 86400, 1 / 24)
        out = {"operacions": n, "operacions_setmana": round(n / days * 7, 1), "dies": round(days, 1), "referencia": LAB_REF}
        if n:
            p = np.array([x["pnl_pct"] for x in t])
            out.update(guanyadores_pct=round(float((p > 0).mean() * 100), 1), net_mitja_pct=round(float(p.mean()), 3),
                       net_guanyadora_pct=round(float(p[p > 0].mean()), 2) if (p > 0).any() else None,
                       net_perdedora_pct=round(float(p[p <= 0].mean()), 2) if (p <= 0).any() else None,
                       llargues_pct=round(sum(x["side"] == "llarg" for x in t) / n * 100),
                       millor_eur=round(max(x["pnl_eur"] for x in t), 2), pitjor_eur=round(min(x["pnl_eur"] for x in t), 2))
        return out

    def _persist(self, ts) -> None:
        a = self.acc
        save_json(STATE, a.to_dict())
        if len(a.trades) != self._n_trades:
            pd.DataFrame(a.trades, columns=["symbol", "side", "entry_ts", "exit_ts", "entry", "exit", "eur", "pnl_eur",
                                            "pnl_pct", "fees_eur", "funding_eur", "hours", "reason"]).to_csv(TRADES, index=False)
            self._n_trades = len(a.trades)
        eq = a.equity()
        btc = a.last_price.get("BTC/EUR")
        if time.time() - self._last_equity_log >= 300:     # corba de capital: un punt cada 5 minuts
            new = not os.path.exists(EQUITY)
            with open(EQUITY, "a", encoding="utf-8") as f:
                if new:
                    f.write("ts,equity,btc\n")
                f.write(f"{ts.isoformat()},{eq:.4f},{btc or ''}\n")
            self._last_equity_log = time.time()
        invested = sum(p.value(a.last_price.get(s, p.entry)) for s, p in a.positions.items())
        state = "aturat" if a.halted else "pausa" if a.paused else "treballant"
        save_json(STATUS, {
            "actualitzat": str(ts), "en_marxa_des_de": self.started, "compte_des_de": a.created, "mode": self.cfg["mode"],
            "estat": state, "aturat_des_de": a.halted_ts or None,
            "capital_inicial_eur": a.initial, "capital_eur": round(eq, 2), "efectiu_eur": round(a.cash, 2),
            "invertit_pct": round(invested / eq * 100, 1) if eq else 0,
            "rendiment_total_pct": round((eq / a.initial - 1) * 100, 3),
            "btc_rendiment_pct": round((btc / a.btc_start - 1) * 100, 2) if btc and a.btc_start else None,
            "maxim_eur": round(a.peak, 2), "caiguda_pct": round((1 - eq / a.peak) * 100, 2) if a.peak else 0,
            "comissions_eur": round(a.fees, 2), "financament_eur": round(a.funding, 2),
            "sortida_text": EXIT_MODES.get(self.cfg["estrategia"].get("sortida", "actual"), ("", {}))[0],
            "config": {"estrategia": self.cfg["estrategia"], "risc": self.cfg["risc"], "costos": self.cfg["paper"],
                       "valors": self.cfg["symbols"], "ajustos_panell": self.watcher.override},
            "valors": self.market,
            "posicions": {s: {"costat": "llarg" if p.side == 1 else "curt", "entrada": p.entry, "obert": p.entry_ts,
                              "preu": a.last_price.get(s), "eur": round(p.collateral, 2), "stop": p.stop,
                              "sortida": self.market.get(s, {}).get("sortida"), "hores": p.bars,
                              "millor_pct": round(p.side * ((p.best or p.entry) - p.entry) / p.entry * 100, 2),
                              "pnl_eur": round(p.pnl(a.last_price.get(s, p.entry)), 2),
                              "pnl_pct": round(p.pnl(a.last_price.get(s, p.entry)) / p.collateral * 100, 2)}
                          for s, p in a.positions.items()},
            "estadistiques": self._stats(),
        })

    def run(self) -> None:
        log.info("Bot en marxa · tendència llarg/curt · simulació · %d valors · exposició %s %% · fre %s %%",
                 len(self.cfg["symbols"]), self.cfg["risc"]["exposicio_pct"], self.cfg["risc"]["caiguda_maxima_pct"])
        n = 0
        while self.running:
            start = time.time()
            try:
                self.handle_commands()
                self.cycle()
            except Exception:
                log.exception("Error inesperat al cicle; continuo al següent")
            n += 1
            if n % 60 == 1:
                log.info("Capital: %.2f EUR · posicions obertes: %d · operacions: %d",
                         self.acc.equity(), len(self.acc.positions), len(self.acc.trades))
            wait = max(1, self.cfg["poll_seconds"] - (time.time() - start))
            while self.running and wait > 0:
                time.sleep(1)
                wait -= 1
                try:
                    if self.handle_commands():                 # les ordres del panell s'executen al moment
                        break
                except Exception:
                    log.exception("Error executant una ordre del panell")
        self._persist(now_utc())
        log.info("Bot aturat. Estat desat.")
