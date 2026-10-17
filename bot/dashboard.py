"""Panell web per veure què fa el bot (des de l'ordinador, l'iPad o el mòbil).

Servidor mínim amb la llibreria estàndard de Python: no afegeix dependències.
Protegit amb contrasenya (variable d'entorn DASHBOARD_PASSWORD).
Només LLEGEIX l'estat del bot: des del panell no es pot comprar ni vendre res.
"""
from __future__ import annotations

import base64
import csv
import hmac
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .paths import EQUITY, LOG_DIR, STATUS, TRADES

log = logging.getLogger("panell")
HTML_PATH = os.path.join(os.path.dirname(__file__), "static", "panell.html")


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _read_csv(path: str) -> list[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _tail(path: str, max_lines: int = 80) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return lines[-max_lines:]
    except Exception:
        return []


def _downsample(rows: list, n: int = 600) -> list:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    out = [rows[int(i * step)] for i in range(n)]
    out[-1] = rows[-1]
    return out


def collect() -> dict:
    trades = _read_csv(TRADES)
    equity = [{"t": r["ts"], "v": float(r["equity"])} for r in _read_csv(EQUITY) if r.get("equity")]
    return {
        "status": _read_json(STATUS),
        "trades": list(reversed(trades))[:150],
        "trades_total": len(trades),
        "trades_guanyadores": sum(1 for r in trades if float(r.get("pnl_eur") or 0) > 0),
        "equity": _downsample(equity),
        "log": _tail(os.path.join(LOG_DIR, "bot.log")),
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "cripto-bot"

    def log_message(self, *args):  # sense soroll als logs del bot
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        password = os.environ.get("DASHBOARD_PASSWORD", "")
        if not password:
            # A un servidor públic (Render) sense contrasenya, no mostrem res
            return not os.environ.get("RENDER")
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return hmac.compare_digest(given.encode(), password.encode())

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        if not self._authorized():
            if not os.environ.get("DASHBOARD_PASSWORD") and os.environ.get("RENDER"):
                msg = "Panell desactivat: cal definir DASHBOARD_PASSWORD a Render (Environment)."
                return self._send(503, msg.encode(), "text/plain; charset=utf-8")
            return self._send(401, "Cal contrasenya".encode(), "text/plain; charset=utf-8",
                              {"WWW-Authenticate": 'Basic realm="Cripto-bot", charset="UTF-8"'})
        if path in ("/", "/index.html"):
            with open(HTML_PATH, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/data":
            body = json.dumps(collect(), default=str).encode()
            return self._send(200, body, "application/json")
        return self._send(404, b"not found", "text/plain")


def start(port: int | None = None) -> ThreadingHTTPServer:
    port = port or int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="panell").start()
    log.info("Panell web disponible al port %d", port)
    return srv
