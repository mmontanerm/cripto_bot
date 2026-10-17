"""Càrrega i validació de la configuració (amb recàrrega en calent)."""
from __future__ import annotations

import logging
import os

import yaml

log = logging.getLogger("config")

REQUIRED = {
    "paper": ["initial_capital", "fee_pct", "slippage_pct", "min_order_eur"],
    "strategy": ["ema_fast", "ema_slow", "rsi_period", "rsi_long", "rsi_short", "atr_period",
                 "stop_atr", "take_profit_atr", "trail_activate_atr", "trail_atr", "min_edge_over_costs"],
    "risk": ["risk_per_trade_pct", "max_position_pct", "max_open_positions",
             "daily_max_loss_pct", "max_drawdown_pct", "cooldown_bars_after_loss"],
}


def validate(cfg: dict) -> None:
    if cfg.get("mode") != "paper":
        raise ValueError("La v1 només admet mode: paper (diners simulats).")
    if cfg.get("timeframe") not in ("15m", "30m", "1h", "4h"):
        raise ValueError("timeframe ha de ser 15m, 30m, 1h o 4h")
    if not cfg.get("symbols"):
        raise ValueError("Cal com a mínim un valor a 'symbols'")
    for sec, keys in REQUIRED.items():
        for k in keys:
            if k not in cfg.get(sec, {}):
                raise ValueError(f"Falta {sec}.{k}")
    s, r = cfg["strategy"], cfg["risk"]
    if s["ema_fast"] >= s["ema_slow"]:
        raise ValueError("ema_fast ha de ser més petita que ema_slow")
    if not 0 < r["risk_per_trade_pct"] <= 5:
        raise ValueError("risk_per_trade_pct ha d'estar entre 0 i 5 (per seguretat)")
    if not 0 < r["max_drawdown_pct"] <= 50:
        raise ValueError("max_drawdown_pct ha d'estar entre 0 i 50")


class ConfigWatcher:
    """Recarrega config.yaml quan canvia. Si el nou fitxer és invàlid, manté l'anterior."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.mtime = 0.0
        self.cfg: dict = {}
        self.reload(force=True)

    def reload(self, force: bool = False) -> bool:
        m = os.path.getmtime(self.path)
        if not force and m == self.mtime:
            return False
        try:
            with open(self.path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            validate(cfg)
        except Exception as e:
            if force:
                raise
            log.error("config.yaml té un error (%s). Continuo amb la configuració anterior.", e)
            self.mtime = m
            return False
        self.cfg, self.mtime = cfg, m
        if not force:
            log.info("Configuració recarregada.")
        return True
