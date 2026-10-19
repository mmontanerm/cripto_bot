"""Càrrega i validació de la configuració (amb recàrrega en calent).

config.yaml (a GitHub) té la configuració base. Els ajustos que es fan des del panell
(state/ajustos.yaml) tenen prioritat i sobreviuen als reinicis.
"""
from __future__ import annotations

import logging
import os

import yaml

from .paths import OVERRIDE

log = logging.getLogger("config")

REQUIRED = {
    "paper": ["initial_capital", "fee_pct", "slippage_pct", "funding_8h_pct"],
    "estrategia": ["canal_entrada_h", "canal_sortida_h", "stop_atr", "atr_h"],
    "risc": ["exposicio_pct", "caiguda_maxima_pct"],
}
# Valors que es poden canviar des del panell, amb els seus límits
PANEL_LIMITS = {"exposicio_pct": (10, 100), "caiguda_maxima_pct": (5, 50)}


def validate(cfg: dict) -> None:
    if cfg.get("mode") != "paper":
        raise ValueError("Només hi ha mode: paper (diners simulats).")
    if cfg.get("timeframe") != "1h":
        raise ValueError("L'estratègia de tendència treballa amb espelmes d'1 hora (timeframe: 1h)")
    if not cfg.get("symbols"):
        raise ValueError("Cal com a mínim un valor a 'symbols'")
    for sec, keys in REQUIRED.items():
        for k in keys:
            if k not in (cfg.get(sec) or {}):
                raise ValueError(f"Falta {sec}.{k}")
    e, r = cfg["estrategia"], cfg["risc"]
    if not 2 <= e["canal_sortida_h"] <= e["canal_entrada_h"] <= 600:
        raise ValueError("Cal 2 ≤ canal_sortida_h ≤ canal_entrada_h ≤ 600 (Kraken dona unes 720 espelmes)")
    if not 0.5 <= e["stop_atr"] <= 10:
        raise ValueError("stop_atr ha d'estar entre 0,5 i 10")
    for k, (lo, hi) in PANEL_LIMITS.items():
        if not lo <= float(r[k]) <= hi:
            raise ValueError(f"risc.{k} ha d'estar entre {lo} i {hi}")


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class ConfigWatcher:
    """Recarrega config.yaml i state/ajustos.yaml quan canvien. Si el resultat és invàlid, manté l'anterior."""

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
                cfg = deep_merge(cfg, override or {})
            validate(cfg)
        except Exception as e:
            if force and not self.cfg:
                if os.path.exists(OVERRIDE):
                    log.error("Els ajustos del panell no són vàlids (%s): els ignoro.", e)
                    os.replace(OVERRIDE, OVERRIDE + ".invalid")
                    return self.reload(force=True)
                raise
            log.error("Configuració amb error (%s). Continuo amb l'anterior.", e)
            self.mtime = m
            return False
        self.cfg, self.mtime, self.override = cfg, m, override
        if not force:
            log.info("Configuració recarregada%s.", " (amb ajustos del panell)" if override else "")
        return True
