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
BINANCE_URL = "https://api.binance.com/api/v3/klines"


def _binance_symbol_exists(sym: str, interval: str) -> bool:
    try:
        r = requests.get(BINANCE_URL, params={"symbol": sym, "interval": interval, "limit": 1}, timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def fetch_history(symbol: str, timeframe: str, days: int, cache_dir: str = "data") -> pd.DataFrame:
    """Descarrega històric de Binance (amb caché a disc).

    Si el parell en EUR no existeix a Binance, fa servir el parell en USDT
    com a aproximació (el comportament del preu és pràcticament el mateix).
    """
    os.makedirs(cache_dir, exist_ok=True)
    base, quote = symbol.split("/")
    cache = os.path.join(cache_dir, f"{base}{quote}_{timeframe}_{days}d.csv")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 12 * 3600:
        return pd.read_csv(cache, index_col=0, parse_dates=True)

    sym = f"{base}{quote}"
    if not _binance_symbol_exists(sym, timeframe):
        alt = f"{base}USDT"
        log.warning("%s no existeix a Binance, faig servir %s per al backtest", sym, alt)
        sym = alt

    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    rows: list = []
    while start < end:
        batch = _get(BINANCE_URL, {"symbol": sym, "interval": timeframe, "startTime": start, "limit": 1000})
        if not batch:
            break
        rows.extend(batch)
        start = batch[-1][0] + 1
        time.sleep(0.25)
    df = pd.DataFrame(rows).iloc[:, :6]
    df.columns = ["ts"] + COLS
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").astype(float)
    df = df[~df.index.duplicated()].iloc[:-1]  # treu l'espelma oberta
    df.to_csv(cache)
    return df


def load_csv(path: str) -> pd.DataFrame:
    """Carrega espelmes d'un CSV propi (columnes: ts, open, high, low, close, volume)."""
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    return df[COLS].astype(float)
