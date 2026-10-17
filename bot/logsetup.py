import logging
import os
import time
from logging.handlers import RotatingFileHandler

from .paths import LOG_DIR


def setup(name: str = "bot") -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-8s %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = time.gmtime  # hora UTC als logs (el panell la mostra en hora local)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(os.path.join(LOG_DIR, f"{name}.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.handlers = [fh, ch]
    logging.getLogger("urllib3").setLevel(logging.WARNING)
