"""Bucle 24/7: llegeix preus, gestiona posicions i desa l'estat. Totalment autònom."""
from __future__ import annotations

import json
import logging
import os
import signal as sig
import time
from datetime import datetime, timezone

import pandas as pd

from .config import ConfigWatcher
from .data import KrakenFeed
from .paths import EQUITY, STATE, STATE_DIR, STATUS, TRADES
from .strategy import add_indicators, explain, warmup_bars
from .trader import Trader

log = logging.getLogger("engine")

TREND = {1: "alcista", -1: "baixista", 0: "lateral"}


def save_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, default=str)
    os.replace(tmp, path)  # escriptura atòmica: mai queda un fitxer a mitges


class Engine:
    def __init__(self, config_path: str = "config.yaml") -> None:
        os.makedirs(STATE_DIR, exist_ok=True)
        self.watcher = ConfigWatcher(config_path)
        self.cfg = self.watcher.cfg
        self.feed = KrakenFeed()
        self.running = True
        if os.path.exists(STATE):
            with open(STATE, encoding="utf-8") as f:
                d = json.load(f)
            self.last_bar = d.pop("_last_bar", {})
            self.trader = Trader.from_dict(self.cfg, d)
            log.info("Estat recuperat: %.2f EUR, %d posicions obertes", self.trader.equity(), len(self.trader.positions))
        else:
            self.last_bar = {}
            self.trader = Trader.new(self.cfg)
            log.info("Compte simulat nou amb %.2f EUR", self.trader.cash)
        self._n_trades = len(self.trader.trades)
        self.unavailable: dict[str, float] = {}  # valors que l'exchange no té (es reintenta cada hora)
        self.market: dict[str, dict] = {}        # què veu el bot de cada valor (per al panell)
        self.started = str(pd.Timestamp(datetime.now(timezone.utc)))
        self._last_equity_log = 0.0
        sig.signal(sig.SIGTERM, self._stop)
        sig.signal(sig.SIGINT, self._stop)

    def _stop(self, *_):
        log.info("Aturant el bot de forma ordenada...")
        self.running = False

    def _symbols(self) -> list[str]:
        # També vigila posicions de valors que s'hagin tret de la llista
        return list(dict.fromkeys(self.cfg["symbols"] + list(self.trader.positions)))

    def cycle(self) -> None:
        if self.watcher.reload():
            self.cfg = self.watcher.cfg
            self.trader.cfg = self.cfg
        now = pd.Timestamp(datetime.now(timezone.utc))
        p = self.cfg["strategy"]
        for s in self._symbols():
            if time.time() - self.unavailable.get(s, 0) < 3600:
                continue
            try:
                df, price = self.feed.candles(s, self.cfg["timeframe"])
            except KeyError as e:
                log.warning("%s · el salto i ho torno a provar d'aquí a una hora", e)
                self.unavailable[s] = time.time()
                continue
            except Exception as e:
                log.warning("No he pogut llegir %s: %s", s, e)
                continue
            self.trader.on_price(s, price, price, price, price, now)
            if len(df) < warmup_bars(p):
                continue
            last_ts = str(df.index[-1])
            new_bar = last_ts != self.last_bar.get(s)
            if new_bar or s not in self.market:
                row = add_indicators(df, p).iloc[-1]
                # Primer cop que veiem el valor: no actuem sobre un senyal antic
                if new_bar and s in self.last_bar:
                    self.trader.on_bar_close(s, row, df.index[-1])
                    self.trader.last_price[s] = price
                self.last_bar[s] = last_ts
                code, text = explain(row, p, self.cfg.get("allow_short", False), self.trader.round_trip_cost_pct())
                self.market[s] = {
                    "tendencia": TREND[int(row["trend"])], "rsi": round(float(row["rsi"]), 1),
                    "volatilitat_pct": round(float(row["atr"] / row["close"] * 100), 2),
                    "estat": code, "explicacio": text, "espelma": last_ts,
                }
            self.market[s]["preu"] = price
            time.sleep(0.5)                      # respecta el límit de peticions de l'API pública
        self.trader.check_risk(now)
        self._persist(now)

    def _persist(self, now) -> None:
        t = self.trader
        state = t.to_dict()
        state["_last_bar"] = self.last_bar
        save_json(STATE, state)
        if len(t.trades) != self._n_trades:
            pd.DataFrame(t.trades).to_csv(TRADES, index=False)
            self._n_trades = len(t.trades)
        cap = self.cfg["paper"]["initial_capital"]
        eq = t.equity()
        if time.time() - self._last_equity_log >= 300:   # corba de capital: un punt cada 5 minuts
            new = not os.path.exists(EQUITY)
            with open(EQUITY, "a", encoding="utf-8") as f:
                if new:
                    f.write("ts,equity\n")
                f.write(f"{now.isoformat()},{eq:.4f}\n")
            self._last_equity_log = time.time()
        for s in list(self.market):
            if s not in self._symbols():
                self.market.pop(s)
        save_json(STATUS, {
            "en_marxa_des_de": self.started,
            "capital_inicial_eur": cap,
            "timeframe": self.cfg["timeframe"],
            "mercat": self.market,
            "config": {"estrategia": self.cfg["strategy"], "risc": self.cfg["risk"],
                       "comissio_pct": self.cfg["paper"]["fee_pct"], "curts": self.cfg.get("allow_short", False)},
            "actualitzat": str(now),
            "mode": self.cfg["mode"],
            "capital_eur": round(eq, 2),
            "rendiment_total_pct": round((eq / cap - 1) * 100, 3),
            "efectiu_eur": round(t.cash, 2),
            "rendiment_avui_pct": round((eq / t.day_start_equity - 1) * 100, 3) if t.day_start_equity else 0,
            "caiguda_des_del_maxim_pct": round((1 - eq / t.peak_equity) * 100, 2) if t.peak_equity else 0,
            "bloquejat_avui": t.day_blocked,
            "aturat_emergencia": t.halted,
            "operacions_tancades": len(t.trades),
            "posicions": {
                s: {"costat": "llarg" if pos.side == 1 else "curt", "entrada": pos.entry, "obert": pos.entry_ts,
                    "pnl_eur": round(pos.value(t.last_price.get(s, pos.entry)) - pos.collateral - pos.fee_in, 2),
                    "preu_actual": t.last_price.get(s), "stop": round(pos.stop, 6), "objectiu": round(pos.tp, 6),
                    "valor_eur": round(pos.value(t.last_price.get(s, pos.entry)), 2)}
                for s, pos in t.positions.items()
            },
        })

    def run(self) -> None:
        log.info("Bot en marxa · mode %s · %s · %d valors", self.cfg["mode"], self.cfg["timeframe"], len(self.cfg["symbols"]))
        n = 0
        while self.running:
            start = time.time()
            try:
                self.cycle()
            except Exception:
                log.exception("Error inesperat al cicle; continuo al següent")
            n += 1
            if n % 60 == 1:
                log.info("Capital: %.2f EUR · posicions obertes: %d · operacions: %d",
                         self.trader.equity(), len(self.trader.positions), len(self.trader.trades))
            wait = max(1, self.cfg["poll_seconds"] - (time.time() - start))
            while self.running and wait > 0:
                time.sleep(min(1, wait))
                wait -= 1
        self._persist(pd.Timestamp(datetime.now(timezone.utc)))
        log.info("Bot aturat. Estat desat.")
