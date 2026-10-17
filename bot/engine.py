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
from .context import TF_MIN, bars_per_day, is_day_close, rank, regime_from_daily
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
            self.last_rebalance = d.pop("_last_rebalance", "")
            self.trader = Trader.from_dict(self.cfg, d)
            log.info("Estat recuperat: %.2f EUR, %d posicions obertes", self.trader.equity(), len(self.trader.positions))
        else:
            self.last_bar = {}
            self.last_rebalance = ""
            self.trader = Trader.new(self.cfg)
            log.info("Compte simulat nou amb %.2f EUR", self.trader.cash)
        self._n_trades = len(self.trader.trades)
        self.unavailable: dict[str, float] = {}  # valors que l'exchange no té (es reintenta cada hora)
        self.market: dict[str, dict] = {}        # què veu el bot de cada valor (per al panell)
        self.started = str(pd.Timestamp(datetime.now(timezone.utc)))
        self._last_equity_log = 0.0
        self._regime_checked = 0.0
        self.rows: dict = {}                     # última espelma tancada (amb indicadors) de cada valor
        sig.signal(sig.SIGTERM, self._stop)
        sig.signal(sig.SIGINT, self._stop)

    def _stop(self, *_):
        log.info("Aturant el bot de forma ordenada...")
        self.running = False

    def _symbols(self) -> list[str]:
        # També vigila posicions de valors que s'hagin tret de la llista
        return list(dict.fromkeys(self.cfg["symbols"] + list(self.trader.positions)))

    def _update_regime(self) -> None:
        """Filtre de mercat: BTC per sobre de la seva mitjana de N dies (es revisa cada hora)."""
        p = self.cfg["strategy"]
        if not (p.get("btc_filter") or p.get("mode") == "rotacio"):
            self.trader.risk_on = True
            return
        if time.time() - self._regime_checked < 3600:
            return
        try:
            daily, _ = self.feed.candles("BTC/EUR", "1d")
            reg = regime_from_daily(daily["close"], int(p.get("btc_ma_days", 200)))
            new = bool(reg.iloc[-1])
            if new != self.trader.risk_on:
                log.info("Filtre BTC: mercat %s", "FAVORABLE" if new else "DESFAVORABLE (no es compra)")
            self.trader.risk_on = new
            self._regime_checked = time.time()
        except Exception as ex:
            log.warning("No he pogut revisar el filtre BTC: %s", ex)

    def cycle(self) -> None:
        if self.watcher.reload():
            self.cfg = self.watcher.cfg
            self.trader.cfg = self.cfg
        now = pd.Timestamp(datetime.now(timezone.utc))
        p = self.cfg["strategy"]
        tf = self.cfg["timeframe"]
        bpd = bars_per_day(tf)
        mode = p.get("mode", "retroces")
        top_n = int(p.get("top_n", 0) or 0)
        self._update_regime()

        # 1) Preus i espelmes de tots els valors
        closed = {}                                # valors amb una espelma nova tancada
        for s in self._symbols():
            if time.time() - self.unavailable.get(s, 0) < 3600:
                continue
            try:
                df, price = self.feed.candles(s, tf)
            except KeyError as ex:
                log.warning("%s · el salto i ho torno a provar d'aquí a una hora", ex)
                self.unavailable[s] = time.time()
                continue
            except Exception as ex:
                log.warning("No he pogut llegir %s: %s", s, ex)
                continue
            self.trader.on_price(s, price, price, price, price, now)
            time.sleep(0.5)                        # respecta el límit de peticions de l'API pública
            if len(df) < warmup_bars(p, bpd):
                continue
            last_ts = str(df.index[-1])
            new_bar = last_ts != self.last_bar.get(s)
            if new_bar or s not in self.market:
                row = add_indicators(df, p, bpd).iloc[-1]
                self.rows[s] = row
                if new_bar and s in self.last_bar:   # el primer cop no actuem sobre un senyal antic
                    closed[s] = (row, df.index[-1], price)
                self.last_bar[s] = last_ts
                code, text = explain(row, p, self.cfg.get("allow_short", False), self.trader.round_trip_cost_pct())
                self.market[s] = {
                    "tendencia": TREND[int(row["trend"])], "rsi": round(float(row["rsi"]), 1),
                    "volatilitat_pct": round(float(row["atr"] / row["close"] * 100), 2),
                    "momentum_pct": None if pd.isna(row["mom"]) else round(float(row["mom"]) * 100, 1),
                    "estat": code, "explicacio": text, "espelma": last_ts,
                }
            if s in self.market:
                self.market[s]["preu"] = price

        # 2) Context: rànquing de momentum
        moms = {s: float(r["mom"]) for s, r in self.rows.items() if s in self.cfg["symbols"]}
        ranking = sorted([s for s in moms if not pd.isna(moms[s])], key=lambda s: moms[s], reverse=True)
        n = top_n or (3 if mode == "rotacio" else 0)
        targets = rank(moms, n, self.trader.risk_on) if mode == "rotacio" else []
        self.trader.allowed = rank(moms, top_n, True) if (top_n and mode == "retroces") else None

        # 3) Espelmes tancades: sortides i entrades
        for s, (row, bar_ts, price) in closed.items():
            self.trader.on_bar_close(s, row, bar_ts)
            self.trader.last_price[s] = price
        if mode == "rotacio" and closed:
            bar_ts = next(iter(closed.values()))[1]
            day = str((pd.Timestamp(bar_ts) + pd.Timedelta(minutes=TF_MIN[tf])).date())
            if is_day_close(bar_ts, tf) and day != self.last_rebalance:
                log.info("Rotació diària: els més forts són %s", ", ".join(targets) or "cap (es queda en efectiu)")
                self.trader.rebalance(targets, {s: float(r["atr"]) for s, r in self.rows.items()}, now)
                self.last_rebalance = day

        # 4) Explicacions per al panell segons el context
        ma = p.get("btc_ma_days", 200)
        for s, m in self.market.items():
            mom = moms.get(s)
            mtxt = "" if mom is None or pd.isna(mom) else f"{mom * 100:+.1f}% en {p.get('momentum_days', 30)} dies"
            if (p.get("btc_filter") or mode == "rotacio") and not self.trader.risk_on:
                m["estat"], m["explicacio"] = "filtre", f"Mercat desfavorable (BTC per sota de la mitjana de {ma} dies): no es compra"
            elif mode == "rotacio":
                k = ranking.index(s) + 1 if s in ranking else None
                if s in targets:
                    m["estat"], m["explicacio"] = "a_punt", f"Entre els {n} més forts ({mtxt}): entra o es manté a la propera rotació (00:00 UTC)"
                elif mom is not None and not pd.isna(mom) and mom <= 0:
                    m["estat"], m["explicacio"] = "baixista", f"Momentum negatiu ({mtxt}): no hi entra"
                else:
                    m["estat"], m["explicacio"] = "esperant", f"Posició {k or '-'} del rànquing ({mtxt}): fora dels {n} més forts"
            elif self.trader.allowed is not None and s not in self.trader.allowed and m["estat"] in ("a_punt", "esperant"):
                m["estat"], m["explicacio"] = "esperant", f"Fora dels {top_n} més forts ({mtxt}): ara no hi entra"
        self.trader.check_risk(now)
        self._persist(now)

    def _persist(self, now) -> None:
        t = self.trader
        state = t.to_dict()
        state["_last_bar"] = self.last_bar
        state["_last_rebalance"] = self.last_rebalance
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
            "filtre_btc_favorable": self.trader.risk_on,
            "config": {"estrategia": self.cfg["strategy"], "risc": self.cfg["risk"], "laboratori": self.watcher.override,
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
