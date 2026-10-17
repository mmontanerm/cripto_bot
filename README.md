# Cripto-bot v1 · mode simulació

Bot autònom que opera 12 criptomonedes 24/7 amb **diners simulats** i preus reals de Kraken.
No necessita compte a cap exchange ni claus d'API: el risc és zero.

## Què fa

Cada minut llegeix els preus, revisa stops i objectius, i a cada espelma tancada (1 h per defecte) decideix si obre posicions:

- **Tendència:** EMA 20 per sobre de l'EMA 100 i preu per sobre de l'EMA 100.
- **Entrada:** en tendència alcista, compra quan el RSI torna a pujar per sobre de 40 (compra el retrocés).
- **Sortida:** stop a 2×ATR, objectiu a 4×ATR, stop dinàmic que protegeix guanys, i tancament si la tendència es gira.
- **Filtre anti-microguanys:** només entra si el guany objectiu és com a mínim 3 vegades el cost de l'operació (comissions + lliscament). Sense això, les comissions et menjarien vius.
- **Curts** (`allow_short`): desactivats per defecte. En simulació els pots provar; en real requereixen marge o futurs, que impliquen més risc.

### Proteccions
| Protecció | Valor per defecte |
|---|---|
| Pèrdua màxima per operació | 1% del capital |
| Mida màxima per posició | 15% del capital |
| Posicions simultànies | 6 |
| Pèrdua diària màxima | 3% → no obre res més fins l'endemà |
| Caiguda màxima | 15% → **ho tanca tot i s'atura** |
| Espera després d'una pèrdua | 3 espelmes al mateix valor |

## Instal·lació (Windows, Mac o Linux)

Cal Python 3.10 o més nou.

```bash
cd cripto-bot
python -m venv .venv
# Windows:      .venv\Scripts\activate
# Mac / Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

## Ús

```bash
python run_backtest.py          # 1) prova l'estratègia amb 1 any d'històric real
python run_bot.py               # 2) arrenca el bot 24/7 (Ctrl+C per aturar-lo)
python status.py                # 3) mira com va: capital, posicions, últimes operacions
```

Fitxers que genera:
- `results/backtest.png` i `results/backtest_trades.csv`: resultats del backtest
- `state/status.json`: estat en directe · `state/trades.csv`: totes les operacions
- `logs/bot.log`: tot el que fa i per què

Altres ordres:
```bash
python run_backtest.py --days 180       # canvia el període del backtest
python run_backtest.py --synthetic      # dades inventades: només per comprovar que el codi funciona
python status.py --reset-halt           # rearma el bot després d'una aturada d'emergència
python status.py --reset-account        # torna a començar de zero (amb el bot aturat)
```

## Ajustar paràmetres

Tot es controla des de `config.yaml`, amb comentaris a cada línia. **Es pot editar amb el bot en marxa**: es recarrega sol. Si t'equivoques de format, el bot ho avisa al log i manté la configuració anterior.

Mètode recomanat per millorar resultats:
1. Canvia **un sol paràmetre** cada vegada.
2. Executa `python run_backtest.py` i compara amb el resultat anterior.
3. Desconfia de millores espectaculars: sovint vol dir que l'estratègia s'ha ajustat massa al passat i no funcionarà en el futur. Prova el canvi també amb `--days 180` i `--days 730`.
4. Només passa al bot en directe els canvis que milloren en tots els períodes.

Paràmetres amb més impacte: `timeframe`, `stop_atr`, `take_profit_atr`, `rsi_long`, `min_edge_over_costs` i `risk_per_trade_pct`.

## Tenir-lo encès 24/7 amb Render + GitHub (tot des de l'iPad)

El repositori ja inclou `render.yaml`, que ho configura tot sol.

**Posada en marxa (un sol cop)**
1. A [render.com](https://render.com): **New → Blueprint** i tria aquest repositori de GitHub.
2. Render llegeix `render.yaml` i et mostra el que crearà: un *Background Worker* "cripto-bot" amb un disc d'1 GB. Accepta-ho (cal una targeta a Render: pla Starter, uns 7 $/mes + 0,25 $ el disc).
3. En un parell de minuts el bot ja funciona.

**Dia a dia**
- **Veure què fa:** a Render, servei *cripto-bot* → pestanya **Logs**. Cada hora hi apareix el capital simulat, i cada compra i venda amb el motiu.
- **Veure l'estat complet:** pestanya **Shell** → escriu `python status.py`.
- **Canviar paràmetres:** edita `config.yaml` directament a la web de GitHub (icona del llapis) i desa amb *Commit changes*. Render actualitza el bot sol en 1-2 minuts. El compte simulat es conserva, perquè està al disc persistent.
- **Aturar-lo:** a Render, *Settings → Suspend Service*. Mentre està suspès no es cobra el servei.
- **Rearmar després d'una aturada d'emergència:** pestanya **Shell** → `python status.py --reset-halt` i després *Manual Deploy → Restart service*.

## Altres maneres de tenir-lo encès 24/7

Si no vols fer servir Render:

- **Servidor al núvol (VPS)**, uns 5 €/mes (Hetzner, OVH, DigitalOcean...). La més fiable.
- **Raspberry Pi** a casa, ~60 € d'una vegada.
- El teu PC, només per provar (si s'apaga o s'adorm, el bot s'atura; quan torna, recupera l'estat).

A Linux (VPS o Raspberry Pi), perquè arrenqui sol i es reiniciï si falla:
```bash
# edita deploy/cripto-bot.service i canvia USUARI pel teu usuari
sudo cp deploy/cripto-bot.service /etc/systemd/system/
sudo systemctl enable --now cripto-bot
journalctl -u cripto-bot -f        # veure el log en directe
```

## Full de ruta

| Fase | Què | Criteri per passar a la següent |
|---|---|---|
| **1. Backtest** | Validar i ajustar amb 1-2 anys d'històric | Bat "comprar i mantenir" ajustat a risc, caiguda < 20% |
| **2. Simulació en directe** (ara) | 2-3 mesos amb preus reals | Resultats coherents amb el backtest |
| **3. Capital mínim real** | 200-500 € a Kraken amb claus d'API **sense permís de retirada** | 3 mesos positius després de costos |
| **4. Escalar** | Augmentar capital gradualment | — |

La fase 3 no està programada a propòsit: no s'ha d'activar fins que les fases 1 i 2 ho justifiquin.

## Avisos importants

- Cap resultat passat garanteix resultats futurs. Les criptomonedes poden caure un 50% o més en poques setmanes.
- A Espanya, cada venda amb guany o pèrdua tributa a l'IRPF. El fitxer `state/trades.csv` et servirà per a la declaració.
- Aquest programari és una eina, no assessorament financer.
