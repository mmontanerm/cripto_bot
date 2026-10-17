"""Laboratori de proves amb dades històriques reals (walk-forward).

    python run_lab.py                # caiguda màxima acceptada: 25%
    python run_lab.py --dd 15        # més prudent
    python run_lab.py --synthetic    # dades inventades, només per comprovar que funciona

Normalment es llança des del panell web (botó "Fes la prova").
Els resultats es desen a state/lab/resultats.json i es veuen al panell.
"""
import argparse
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from bot import lab  # noqa: E402
from bot.config import ConfigWatcher  # noqa: E402
from bot.logsetup import setup  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dd", type=float, default=25.0, help="caiguda màxima acceptada (%%)")
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()
    setup("lab")
    cfg = ConfigWatcher("config.yaml").cfg   # la configuració que fa servir el bot ara mateix
    lab.main(cfg, max(5.0, min(50.0, a.dd)), synthetic=a.synthetic)
