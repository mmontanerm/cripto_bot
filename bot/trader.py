"""Trader: compte simulat + gestió del risc + gestió de posicions.

El MATEIX codi s'utilitza al backtest i a l'operativa 24/7, perquè els
resultats del backtest siguin representatius del que farà el bot en directe.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

import pandas as pd

from .strategy import signal

log = logging.getLogger("trader")


@dataclass
class Position:
    symbol: str
    side: int              # +1 llarg, -1 curt
    qty: float
    entry: float           # preu d'entrada (amb lliscament)
    entry_ts: str
    atr: float             # ATR en el moment d'entrar (fixa els stops)
    stop: float
    tp: float
    best: float            # millor preu assolit (per al stop dinàmic)
    bars: int = 0
    fee_in: float = 0.0
    trailed: bool = False

    @property
    def collateral(self) -> float:
        return self.qty * self.entry

    def value(self, price: float) -> float:
        """Valor de la posició en EUR (llarg: valor de mercat; curt: garantia + PnL)."""
        if self.side == 1:
            return self.qty * price
        return self.collateral + (self.entry - price) * self.qty


@dataclass
class Trader:
    cfg: dict
    cash: float = 0.0
    positions: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)
    last_price: dict = field(default_factory=dict)
    cooldown_until: dict = field(default_factory=dict)
    peak_equity: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    day_blocked: bool = False
    halted: bool = False
    fees_paid: float = 0.0

    # ------------------------------------------------------------ creació / estat
    @classmethod
    def new(cls, cfg: dict) -> "Trader":
        cap = float(cfg["paper"]["initial_capital"])
        return cls(cfg=cfg, cash=cap, peak_equity=cap, day_start_equity=cap)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "cfg"}
        d["positions"] = {s: asdict(p) for s, p in self.positions.items()}
        return d

    @classmethod
    def from_dict(cls, cfg: dict, d: dict) -> "Trader":
        t = cls(cfg=cfg)
        for k, v in d.items():
            if k == "positions":
                t.positions = {s: Position(**p) for s, p in v.items()}
            elif hasattr(t, k):
                setattr(t, k, v)
        return t

    # ------------------------------------------------------------ utilitats
    @property
    def fee(self) -> float:
        return self.cfg["paper"]["fee_pct"] / 100

    @property
    def slip(self) -> float:
        return self.cfg["paper"]["slippage_pct"] / 100

    def round_trip_cost_pct(self) -> float:
        return 2 * (self.cfg["paper"]["fee_pct"] + self.cfg["paper"]["slippage_pct"])

    def equity(self) -> float:
        return self.cash + sum(p.value(self.last_price.get(s, p.entry)) for s, p in self.positions.items())

    # ------------------------------------------------------------ obrir / tancar
    def _open(self, symbol: str, side: int, price: float, atr: float, ts) -> None:
        p, r = self.cfg["strategy"], self.cfg["risk"]
        fill = price * (1 + self.slip * side)
        eq = self.equity()
        stop_dist = p["stop_atr"] * atr
        qty = (eq * r["risk_per_trade_pct"] / 100) / stop_dist
        notional = qty * fill
        cap = eq * r["max_position_pct"] / 100
        avail = self.cash / (1 + self.fee) * 0.999
        notional = min(notional, cap, avail)
        if notional < self.cfg["paper"]["min_order_eur"]:
            return
        qty = notional / fill
        fee = notional * self.fee
        self.cash -= notional + fee
        self.fees_paid += fee
        self.positions[symbol] = Position(
            symbol=symbol, side=side, qty=qty, entry=fill, entry_ts=str(ts), atr=atr,
            stop=fill - side * stop_dist, tp=fill + side * p["take_profit_atr"] * atr,
            best=fill, fee_in=fee,
        )
        log.info("OBRE %s %s qty=%.6f a %.4f (stop %.4f, tp %.4f)",
                 "LLARG" if side == 1 else "CURT", symbol, qty, fill,
                 self.positions[symbol].stop, self.positions[symbol].tp)

    def _close(self, symbol: str, price: float, ts, reason: str) -> None:
        pos = self.positions.pop(symbol)
        fill = price * (1 - self.slip * pos.side)
        proceeds = pos.value(fill)
        fee = pos.qty * fill * self.fee
        self.cash += proceeds - fee
        self.fees_paid += fee
        pnl = proceeds - fee - pos.collateral - pos.fee_in
        self.trades.append({
            "symbol": symbol, "side": "llarg" if pos.side == 1 else "curt",
            "entry_ts": pos.entry_ts, "exit_ts": str(ts), "entry": round(pos.entry, 8),
            "exit": round(fill, 8), "qty": pos.qty, "pnl_eur": round(pnl, 4),
            "pnl_pct": round(pnl / pos.collateral * 100, 3), "fees_eur": round(pos.fee_in + fee, 4),
            "bars": pos.bars, "reason": reason,
        })
        if pnl < 0:
            tf_min = {"15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}.get(self.cfg["timeframe"], 60)
            until = pd.Timestamp(ts) + pd.Timedelta(minutes=tf_min * self.cfg["risk"]["cooldown_bars_after_loss"])
            self.cooldown_until[symbol] = str(until)
        log.info("TANCA %s a %.4f · %s · PnL %.2f EUR", symbol, fill, reason, pnl)

    def close_all(self, ts, reason: str) -> None:
        for s in list(self.positions):
            self._close(s, self.last_price.get(s, self.positions[s].entry), ts, reason)

    # ------------------------------------------------------------ risc global
    def check_risk(self, ts) -> None:
        r = self.cfg["risk"]
        eq = self.equity()
        today = str(pd.Timestamp(ts).date())
        if today != self.day:
            self.day, self.day_start_equity, self.day_blocked = today, eq, False
        self.peak_equity = max(self.peak_equity, eq)
        if not self.halted and eq < self.peak_equity * (1 - r["max_drawdown_pct"] / 100):
            log.error("ATURADA D'EMERGÈNCIA: caiguda de %.1f%% des del màxim. Ho tanco tot.",
                      (1 - eq / self.peak_equity) * 100)
            self.close_all(ts, "aturada_emergencia")
            self.halted = True
        if not self.day_blocked and eq < self.day_start_equity * (1 - r["daily_max_loss_pct"] / 100):
            log.warning("Pèrdua diària màxima assolida: no obro més posicions fins demà (UTC).")
            self.day_blocked = True

    # ------------------------------------------------------------ esdeveniments de preu
    def on_price(self, symbol: str, o: float, h: float, l: float, c: float, ts) -> None:
        """Revisa stops i objectius. Al backtest rep l'espelma; en directe, el preu actual (o=h=l=c)."""
        self.last_price[symbol] = c
        pos = self.positions.get(symbol)
        if pos is None:
            return
        p = self.cfg["strategy"]
        if pos.side == 1:
            if l <= pos.stop:
                self._close(symbol, min(o, pos.stop), ts, "stop_dinamic" if pos.trailed else "stop")
                return
            if h >= pos.tp:
                self._close(symbol, max(o, pos.tp), ts, "objectiu")
                return
            pos.best = max(pos.best, h)
            if pos.best - pos.entry >= p["trail_activate_atr"] * pos.atr:
                pos.stop = max(pos.stop, pos.best - p["trail_atr"] * pos.atr)
                pos.trailed = True
        else:
            if h >= pos.stop:
                self._close(symbol, max(o, pos.stop), ts, "stop_dinamic" if pos.trailed else "stop")
                return
            if l <= pos.tp:
                self._close(symbol, min(o, pos.tp), ts, "objectiu")
                return
            pos.best = min(pos.best, l)
            if pos.entry - pos.best >= p["trail_activate_atr"] * pos.atr:
                pos.stop = min(pos.stop, pos.best + p["trail_atr"] * pos.atr)
                pos.trailed = True

    def on_bar_close(self, symbol: str, row: pd.Series, ts) -> None:
        """Una espelma s'ha tancat: sortides per tendència/temps i noves entrades."""
        p, r = self.cfg["strategy"], self.cfg["risk"]
        price = float(row["close"])
        self.last_price[symbol] = price
        pos = self.positions.get(symbol)
        if pos is not None:
            pos.bars += 1
            if p.get("exit_on_trend_flip") and row["trend"] == -pos.side:
                self._close(symbol, price, ts, "canvi_tendencia")
            elif p.get("max_hold_bars", 0) and pos.bars >= p["max_hold_bars"]:
                self._close(symbol, price, ts, "temps_maxim")
            return

        if self.halted or self.day_blocked or len(self.positions) >= r["max_open_positions"]:
            return
        cd = self.cooldown_until.get(symbol)
        if cd and pd.Timestamp(ts) < pd.Timestamp(cd):
            return
        sig = signal(row, p, self.cfg.get("allow_short", False), self.round_trip_cost_pct())
        if sig:
            self._open(symbol, sig, price, float(row["atr"]), ts)
