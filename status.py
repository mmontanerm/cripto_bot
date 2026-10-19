"""Consulta l'estat del bot des de la consola (a Render: pestanya Shell).

    python status.py      # resum: capital, posicions, últimes operacions

Totes les accions (pausa, reprendre, tancar-ho tot, rearmar el fre, reiniciar el compte)
es fan des del panell web.
"""
import json
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from bot.paths import STATUS, TRADES  # noqa: E402


def main():
    if not os.path.exists(STATUS):
        print("Encara no hi ha estat. El bot s'ha arrencat alguna vegada?")
        return
    with open(STATUS, encoding="utf-8") as f:
        s = json.load(f)
    print(f"\nActualitzat: {s['actualitzat']}  ·  estat: {s['estat']}")
    print(f"Capital: {s['capital_eur']} EUR  ({s['rendiment_total_pct']:+.2f} %)  ·  efectiu: {s['efectiu_eur']} EUR")
    print(f"Caiguda des del màxim: {s['caiguda_pct']} %  ·  operacions tancades: {s['estadistiques']['operacions']}")
    print("\nPosicions obertes:" if s["posicions"] else "\nCap posició oberta.")
    for sym, p in s["posicions"].items():
        print(f"  {sym:<10} {p['costat']:<6} entrada {p['entrada']:.6g}  ara {p['preu']}  stop {p['stop']:.6g}  "
              f"{p['pnl_eur']:+.2f} EUR")
    if os.path.exists(TRADES):
        import pandas as pd
        t = pd.read_csv(TRADES).tail(10)
        if len(t):
            print("\nÚltimes operacions:")
            print(t[["symbol", "side", "exit_ts", "pnl_eur", "pnl_pct", "reason"]].to_string(index=False))
    print()


if __name__ == "__main__":
    main()
