"""Càrrega i validació de la configuració (amb recàrrega en calent)."""
from __future__ import annotations

import logging
import os

import yaml

from .paths import OVERRIDE

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
    if s.get("mode", "retroces") not in ("retroces", "rotacio"):
        raise ValueError("strategy.mode ha de ser 'retroces' o 'rotacio'")
    if not 0 < float(s.get("exposure", 1.0)) <= 1:
        raise ValueError("strategy.exposure ha d'estar entre 0 i 1 (1 = tot el capital)")
    if int(s.get("top_n", 0) or 0) < 0:
        raise ValueError("strategy.top_n no pot ser negatiu")
    bpd = 1440 / {"15m": 15, "30m": 30, "1h": 60, "4h": 240}[cfg["timeframe"]]
    if (s.get("mode") == "rotacio" or s.get("top_n")) and s.get("momentum_days", 30) * bpd > 650:
        raise ValueError("momentum_days massa llarg per a aquesta temporalitat (Kraken dona ~720 espelmes): "
                         "fes-lo més curt o fes servir una temporalitat més llarga")
    if s["ema_fast"] >= s["ema_slow"]:
        raise ValueError("ema_fast ha de ser més petita que ema_slow")
    if not 0 < r["risk_per_trade_pct"] <= 5:
        raise ValueError("risk_per_trade_pct ha d'estar entre 0 i 5 (per seguretat)")
    if not 0 < r["max_drawdown_pct"] <= 50:
        raise ValueError("max_drawdown_pct ha d'estar entre 0 i 50")


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class ConfigWatcher:
    """Recarrega config.yaml quan canvia. Si el nou fitxer és invàlid, manté l'anterior.

    Si existeix state/config_lab.yaml (l'escriu el panell en aplicar una estratègia del
    laboratori), els seus valors tenen prioritat sobre config.yaml.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.mtime = (0.0, 0.0)
        self.cfg: dict = {}
        self.override: dict | None = None
        self.reload(force=True)

    def _mtimes(self) -> tuple:
        o = os.path.getmtime(OVERRIDE) if os.path.exists(OVERRIDE) else 0.0
        return (os.path.getmtime(self.path), o)

    def reload(self, force: bool = False) -> bool:
        m = self._mtimes()
        if not force and m == self.mtime:
            return False
        try:
            with open(self.path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            override = None
            if os.path.exists(OVERRIDE):
                with open(OVERRIDE, encoding="utf-8") as f:
                    override = yaml.safe_load(f) or None
                cfg = deep_merge(cfg, {k: v for k, v in (override or {}).items() if not k.startswith("_")})
            validate(cfg)
        except Exception as e:
            if force and not self.cfg:
                if os.path.exists(OVERRIDE):
                    log.error("Els ajustos del laboratori no són vàlids (%s): els ignoro.", e)
                    os.replace(OVERRIDE, OVERRIDE + ".invalid")
                    return self.reload(force=True)
                raise
            log.error("Configuració amb error (%s). Continuo amb l'anterior.", e)
            self.mtime = m
            return False
        self.cfg, self.mtime, self.override = cfg, m, override
        if not force:
            log.info("Configuració recarregada%s.", " (amb ajustos del laboratori)" if override else "")
        return True
