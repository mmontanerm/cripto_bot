"""Laboratori llarg/curt amb walk-forward, prova del mico i reserva final (3 anys de dades reals).

    python run_lab.py                # caiguda màxima acceptada: 15%
    python run_lab.py --dd 10        # més prudent
    python run_lab.py --passat       # tendència amb dades del 2020 al 2023 (anys no vistos)
    python run_lab.py --rapid        # espelmes de 15 minuts, unes 10-12 operacions al dia
    python run_lab.py --terme-mig    # regles exactes del bot amb canals de 168 h a 6 h
    python run_lab.py --sortides     # maneres diferents d'assegurar els guanys (stop seguidor, protecció...)
    python run_lab.py --micro        # micro-guanys amb ordres limitades
    python run_lab.py --correlacio   # límit de posicions en la mateixa direcció
    python run_lab.py --objectius    # regles del bot amb objectius de guany fixos
    python run_lab.py --escalat      # regles del bot amb tots els paràmetres escalats (espelmes de 60 a 5 min)
    python run_lab.py --tot          # les 4 proves pendents seguides: sortides, límit de posicions, objectius i micro-guanys
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
    ap.add_argument("--sortides", action="store_true", help="regles del bot amb maneres diferents d'assegurar els guanys")
    ap.add_argument("--micro", action="store_true", help="micro-guanys amb ordres limitades")
    ap.add_argument("--correlacio", action="store_true", help="regles del bot amb límit de posicions en la mateixa direcció")
    ap.add_argument("--objectius", action="store_true",
                    help="regles del bot amb un objectiu de guany fix (0,5 % a 10 %)")
    ap.add_argument("--escalat", action="store_true",
                    help="regles del bot amb tots els paràmetres escalats (espelmes de 60 a 5 minuts)")
    ap.add_argument("--tot", action="store_true",
                    help="fa seguides les proves de sortides, límit de posicions, objectius i micro-guanys")
    ap.add_argument("--rapid", action="store_true", help="prova ràpida: espelmes de 15 minuts, 10-12 operacions al dia")
    a = ap.parse_args()
    setup("lab")
    cfg = ConfigWatcher("config.yaml").cfg
    dd = max(5.0, min(50.0, a.dd))
    if a.tot:
        # Una darrere l'altra; si una falla, continua amb la següent. Cada una desa els seus resultats.
        tests = [("sortides", "com assegurar els guanys", {"exits": True}),
                 ("correlacio", "límit de posicions", {"corr": True}),
                 ("objectius", "objectius de guany", {"tp": True}),
                 ("micro", "micro-guanys", {"micro": True})]
        original = lab_wf.write_status
        for k, (mode, name, kw) in enumerate(tests, 1):
            def prefixed(_k=k, _name=name, **st):
                st["mode"] = "tot"
                if st.get("estat") in ("descarregant", "provant"):
                    st["missatge"] = f"Prova {_k} de {len(tests)} ({_name}) · {st.get('missatge', '')}"
                elif st.get("estat") == "fet" and _k < len(tests):
                    st.update(estat="provant", missatge=f"Prova {_k} de {len(tests)} ({_name}) acabada. Començant la següent…")
                original(**st)
            lab_wf.write_status = prefixed
            try:
                lab_wf.main(cfg, dd, synthetic=a.synthetic, **kw)
            except Exception:
                pass                                   # l'error ja queda al log; continua amb la següent
        lab_wf.write_status = original
        original(estat="fet", pct=100, missatge="Les 4 proves han acabat. Els resultats són a cada secció del laboratori.")
    else:
        lab_wf.main(cfg, dd, synthetic=a.synthetic, past=a.passat, fast=a.rapid, scan=a.terme_mig, scale=a.escalat,
                    tp=a.objectius, micro=a.micro, corr=a.correlacio, exits=a.sortides)
