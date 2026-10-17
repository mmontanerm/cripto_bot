"""Consulta l'estat del bot i fes accions de manteniment.

    python status.py                 # resum: capital, posicions, últimes operacions
    python status.py --reset-halt    # rearma el bot després d'una aturada d'emergència
    python status.py --reset-account # torna a començar el compte simulat de zero (amb el bot aturat!)
"""
import argparse
import json
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from bot.paths import STATE, STATUS, TRADES  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset-halt", action="store_true")
    ap.add_argument("--reset-account", action="store_true")
    a = ap.parse_args()

    if a.reset_account:
        for f in (STATE, STATUS, TRADES):
            if os.path.exists(f):
                os.remove(f)
        print("Compte simulat reiniciat. Arrenca el bot de nou.")
        return

    if a.reset_halt:
        with open(STATE, encoding="utf-8") as f:
            d = json.load(f)
        d["halted"] = False
        d["peak_equity"] = d.get("cash", 0)  # després d'una aturada tot és efectiu: nou màxim de referència
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
        print("Aturada d'emergència desactivada. Reinicia el bot perquè ho llegeixi.")
        return

    if not os.path.exists(STATUS):
        print("Encara no hi ha estat. El bot s'ha arrencat alguna vegada?")
        return
    with open(STATUS, encoding="utf-8") as f:
        s = json.load(f)
    print(f"\nActualitzat: {s['actualitzat']}  ·  mode: {s['mode']}")
    print(f"Capital: {s['capital_eur']} EUR  ({s['rendiment_total_pct']:+.2f}%)  ·  efectiu: {s['efectiu_eur']} EUR")
    print(f"Caiguda des del màxim: {s['caiguda_des_del_maxim_pct']}%  ·  operacions tancades: {s['operacions_tancades']}")
    if s["aturat_emergencia"]:
        print("⚠  ATURADA D'EMERGÈNCIA ACTIVA (python status.py --reset-halt per rearmar)")
    if s["bloquejat_avui"]:
        print("⚠  Pèrdua diària màxima assolida: no s'obren posicions fins demà")
    print("\nPosicions obertes:" if s["posicions"] else "\nCap posició oberta.")
    for sym, p in s["posicions"].items():
        print(f"  {sym:<10} {p['costat']:<6} entrada {p['entrada']:.4f}  ara {p['preu_actual']}  "
              f"stop {p['stop']}  objectiu {p['objectiu']}  ({p['valor_eur']} EUR)")
    if os.path.exists(TRADES):
        import pandas as pd
        t = pd.read_csv(TRADES).tail(10)
        print("\nÚltimes operacions:")
        print(t[["symbol", "side", "exit_ts", "pnl_eur", "pnl_pct", "reason"]].to_string(index=False))
    print()


if __name__ == "__main__":
    main()
