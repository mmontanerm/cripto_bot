"""Laboratori llarg/curt amb walk-forward, prova del mico i reserva final (3 anys de dades reals).

    python run_lab.py                # caiguda màxima acceptada: 15%
    python run_lab.py --dd 10        # més prudent
    python run_lab.py --passat       # tendència amb dades del 2020 al 2023 (anys no vistos)
    python run_lab.py --rapid        # espelmes de 15 minuts, unes 10-12 operacions al dia
    python run_lab.py --terme-mig    # regles exactes del bot amb canals de 168 h a 12 h
    python run_lab.py --synthetic    # dades inventades, només per comprovar que funciona

Normalment es llança des del panell web (botó "Fes la prova").
Els resultats es desen a state/lab/resultats_wf.json i es veuen al panell.
"""
import argparse
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from bot import lab_wf  # noqa: E402
from bot.config import ConfigWatcher  # noqa: E402
from bot.logsetup import setup  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dd", type=float, default=15.0, help="caiguda màxima acceptada (%%)")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--passat", action="store_true", help="prova la tendència amb dades del 2020 al 2023")
    ap.add_argument("--terme-mig", dest="terme_mig", action="store_true",
                    help="regles exactes del bot, només canvia la durada del canal (168 h a 12 h)")
    ap.add_argument("--rapid", action="store_true", help="prova ràpida: espelmes de 15 minuts, 10-12 operacions al dia")
    a = ap.parse_args()
    setup("lab")
    cfg = ConfigWatcher("config.yaml").cfg
    lab_wf.main(cfg, max(5.0, min(50.0, a.dd)), synthetic=a.synthetic, past=a.passat, fast=a.rapid, scan=a.terme_mig)
