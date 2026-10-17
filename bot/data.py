"""Dades de mercat: preus en directe (Kraken) i històric per al backtest (Binance).

Només fa servir APIs PÚBLIQUES: no cal compte ni claus per a la v1.
"""
from __future__ import annotations

import logging
import os
import time

import pandas as pd
import requests

log = logging.getLogger("data")

TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
COLS = ["open", "high", "low", "close", "volume"]


def _get(url: str, params: dict, retries: int = 3) -> dict | list:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # xarxa, límit de peticions...
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"Error de connexió a {url}: {last}")


# ------------------------------------------------------------------ Kraken
class KrakenFeed:
    """Espelmes en directe des de l'API pública de Kraken."""

    BASE = "https://api.kraken.com/0/public"
    ALIASES = {"BTC": "XBT", "DOGE": "XDG"}

    def __init__(self) -> None:
        self._pairs: dict[str, str] = {}

    def _load_pairs(self) -> None:
        data = _get(f"{self.BASE}/AssetPairs", {})
        if data.get("error"):
            raise RuntimeError(f"Kraken: {data['error']}")
        for name, info in data["result"].items():
            ws = info.get("wsname")
            if ws:
                self._pairs[ws] = name

    def resolve(self, symbol: str) -> str | None:
        if not self._pairs:
            self._load_pairs()
        base, quote = symbol.split("/")
        for b in (base, self.ALIASES.get(base, base)):
            if f"{b}/{quote}" in self._pairs:
                return self._pairs[f"{b}/{quote}"]
        return None

    def candles(self, symbol: str, timeframe: str) -> tuple[pd.DataFrame, float]:
        """Retorna (espelmes TANCADES, preu actual)."""
        pair = self.resolve(symbol)
        if pair is None:
            raise KeyError(f"{symbol} no existeix a Kraken")
        data = _get(f"{self.BASE}/OHLC", {"pair": pair, "interval": TF_MINUTES[timeframe]})
        if data.get("error"):
            raise RuntimeError(f"Kraken {symbol}: {data['error']}")
        rows = next(v for k, v in data["result"].items() if k != "last")
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vwap", "volume", "count"])
        df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="s", utc=True)
        df = df.set_index("ts")[COLS].astype(float)
        price = float(df["close"].iloc[-1])
        return df.iloc[:-1], price  # l'última espelma encara no està tancada


# ------------------------------------------------------------------ Binance (històric)
# Dades públiques de mercat. Si una adreça falla (bloqueig regional, caiguda), prova la següent.
BINANCE_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision", "https://api1.binance.com"]


def _binance(params: dict) -> list:
    last = None
    for host in BINANCE_HOSTS:
        try:
            r = requests.get(f"{host}/api/v3/klines", params=params, timeout=20)
            if r.status_code == 400:      # el parell no existeix
                raise KeyError(params.get("symbol"))
            r.raise_for_status()
            return r.json()
        except KeyError:
            raise
        except Exception as e:
            last = e
    raise RuntimeError(f"No puc descarregar dades de Binance: {last}")


def fetch_history(symbol: str, timeframe: str, days: int, cache_dir: str = "data",
                  quote: str | None = None) -> pd.DataFrame:
    """Descarrega històric de Binance (amb caché a disc).

    `quote`: força una moneda de cotització (p. ex. USDT, amb molt més històric i liquiditat
    que l'EUR). Si no s'indica, prova primer el parell original i, si no existeix, USDT.
    """
    os.makedirs(cache_dir, exist_ok=True)
    base, q = symbol.split("/")
    candidates = [f"{base}{quote}"] if quote else [f"{base}{q}", f"{base}USDT"]
    for sym in candidates:
        cache = os.path.join(cache_dir, f"{sym}_{timeframe}_{days}d.csv")
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 12 * 3600:
            return pd.read_csv(cache, index_col=0, parse_dates=True)
        try:
            _binance({"symbol": sym, "interval": timeframe, "limit": 1})
        except KeyError:
            log.warning("%s no existeix a Binance", sym)
            continue
        end = int(time.time() * 1000)
        start = end - days * 86_400_000
        rows: list = []
        while start < end:
            batch = _binance({"symbol": sym, "interval": timeframe, "startTime": start, "limit": 1000})
            if not batch:
                break
            rows.extend(batch)
            start = batch[-1][0] + 1
            time.sleep(0.15)
        if not rows:
            continue
        df = pd.DataFrame(rows).iloc[:, :6]
        df.columns = ["ts"] + COLS
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("ts").astype(float)
        df = df[~df.index.duplicated()].iloc[:-1]  # treu l'espelma oberta
        df.to_csv(cache)
        return df
    raise KeyError(f"{symbol}: sense dades a Binance")


def load_csv(path: str) -> pd.DataFrame:
    """Carrega espelmes d'un CSV propi (columnes: ts, open, high, low, close, volume)."""
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return df[COLS].astype(float)
