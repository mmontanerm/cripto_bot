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
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .lab_wf import DAYS as LAB_DAYS, PAST_END, PAST_START
from .paths import EQUITY, LAB_DIR, LOG_DIR, OVERRIDE, STATUS, TRADES

log = logging.getLogger("panell")
HTML_PATH = os.path.join(os.path.dirname(__file__), "static", "panell.html")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAB_STATUS = os.path.join(LAB_DIR, "estat_wf.json")
LAB_RESULTS = os.path.join(LAB_DIR, "resultats_wf.json")
LAB_RESULTS_PAST = os.path.join(LAB_DIR, "resultats_wf_passat.json")
# Fitxers dels laboratoris retirats: s'esborren en arrencar perquè no quedi cap resultat antic
OLD_LAB_FILES = ["estat.json", "resultats.json", "estat_lc.json", "resultats_lc.json", "estat_ct.json", "resultats_ct.json",
                 "estat_graella.json", "resultats_graella.json"]
_lab_lock = threading.Lock()


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


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _status(path: str) -> dict:
    st = _read_json(path)
    if st.get("estat") in ("descarregant", "provant"):
        age = time.time() - datetime.fromisoformat(st["actualitzat"]).timestamp() if st.get("actualitzat") else 1e9
        if not _pid_alive(st.get("pid", -1)) or age > 900:
            st["estat"] = "interrompuda"
            st["missatge"] = "La prova s'ha interromput (probablement el servei s'ha reiniciat). Torna-la a llançar."
    return st


def lab_state() -> dict:
    return {"estat": _status(LAB_STATUS), "resultats": _read_json(LAB_RESULTS) or None,
            "resultats_passat": _read_json(LAB_RESULTS_PAST) or None}


def lab_start(dd: float, past: bool = False) -> tuple[int, str]:
    with _lab_lock:
        if _status(LAB_STATUS).get("estat") in ("descarregant", "provant"):
            return 409, "Ja hi ha una prova en marxa. Espera que acabi."
        os.makedirs(LAB_DIR, exist_ok=True)
        with open(LAB_STATUS, "w", encoding="utf-8") as f:
            json.dump({"estat": "descarregant", "mode": "passat" if past else "actual", "pct": 0, "missatge": "Arrencant la prova",
                       "actualitzat": datetime.now(timezone.utc).isoformat(), "pid": os.getpid()}, f)

        def lower_priority():  # que el laboratori no alenteixi el bot
            os.nice(10)
        cmd = [sys.executable, os.path.join(ROOT, "run_lab.py"), "--dd", str(dd)] + (["--passat"] if past else [])
        subprocess.Popen(cmd, cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         preexec_fn=lower_priority)
        log.info("Laboratori llarg/curt llançat des del panell (%s, caiguda màxima acceptada %s%%)", "passat" if past else "actual", dd)
        return 202, "Prova en marxa."


def cleanup_old_labs() -> None:
    """Retira els resultats, les dades i els ajustos dels laboratoris anteriors."""
    removed = 0
    for name in OLD_LAB_FILES:
        p = os.path.join(LAB_DIR, name)
        if os.path.exists(p):
            os.remove(p)
            removed += 1
    if os.path.exists(OVERRIDE):            # estratègia aplicada des d'un laboratori antic
        os.replace(OVERRIDE, OVERRIDE + ".retirat")
        log.info("Ajustos d'un laboratori anterior retirats: el bot torna a config.yaml de GitHub")
    cache = os.path.join(LAB_DIR, "dades")
    if os.path.isdir(cache):
        for f in os.listdir(cache):
            if f"_15m_{LAB_DAYS}d" not in f and f"_1h_{PAST_START}_{PAST_END}" not in f:   # dades dels laboratoris actuals
                os.remove(os.path.join(cache, f))
                removed += 1
    if removed:
        log.info("Laboratoris anteriors retirats (%d fitxers esborrats)", removed)


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
        if path == "/api/lab":
            return self._send(200, json.dumps(lab_state(), default=str).encode(), "application/json")
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        if not self._authorized():
            return self._send(401, "Cal contrasenya".encode(), "text/plain; charset=utf-8",
                              {"WWW-Authenticate": 'Basic realm="Cripto-bot", charset="UTF-8"'})
        # Protecció contra peticions des d'altres webs: cal la capçalera pròpia i el mateix origen
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if self.headers.get("X-Panell") != "1" or (origin and origin.split("://", 1)[-1] != host):
            return self._send(403, b"forbidden", "text/plain")
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, b"bad request", "text/plain")
        if path == "/api/lab/start":
            try:
                dd = max(5.0, min(50.0, float(body.get("dd", 25))))
            except Exception:
                dd = 25.0
            code, msg = lab_start(dd, bool(body.get("passat")))
        else:
            return self._send(404, b"not found", "text/plain")
        return self._send(code, json.dumps({"missatge": msg}).encode(), "application/json")


def start(port: int | None = None) -> ThreadingHTTPServer:
    try:
        cleanup_old_labs()
    except Exception as e:
        log.warning("No he pogut netejar els laboratoris anteriors: %s", e)
    port = port or int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="panell").start()
    log.info("Panell web disponible al port %d", port)
    return srv
