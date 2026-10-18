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
python run_bot.py               # 2) arrenca el bot 24/7 i el panell a http://localhost:8080 (Ctrl+C per aturar)
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
2. Render llegeix `render.yaml` i et mostra el que crearà: un servei web "cripto-bot-web" amb un disc d'1 GB (pla Starter, uns 7 $/mes + 0,25 $ el disc).
3. Quan et demani **DASHBOARD_PASSWORD**, posa-hi la contrasenya que vulguis per al panell. Si no te la demana, afegeix-la després a *Environment*.
4. En un parell de minuts el bot ja funciona.

**El panell**
Obre l'adreça del servei (la trobaràs a dalt de tot a Render, acaba en `.onrender.com`). El navegador et demanarà usuari i contrasenya: l'usuari pot ser qualsevol, la contrasenya és la de `DASHBOARD_PASSWORD`.

Hi veuràs:
- Una columna de llum com la d'una màquina: verd = treballant, ambre = en pausa o sense notícies, vermell = aturat per seguretat.
- El capital simulat, el rendiment total i d'avui, i la corba del capital.
- Les posicions obertes, amb on és el preu entre el stop i l'objectiu.
- **Què està vigilant:** cada valor amb el que el bot en pensa ara mateix i per què encara no entra.
- Les últimes operacions amb el resultat i el motiu del tancament.
- L'activitat recent i els paràmetres actius.

Es refresca sol cada 30 segons. A l'iPad o al mòbil, *Compartir → Afegir a la pantalla d'inici* el deixa com una app. El panell només mostra informació: no pot comprar ni vendre.

En local, el panell és a http://localhost:8080 mentre el bot està en marxa.

**Dia a dia**
- **Canviar paràmetres:** edita `config.yaml` directament a la web de GitHub (icona del llapis) i desa amb *Commit changes*. Render actualitza el bot sol en 1-2 minuts. El compte simulat es conserva, perquè està al disc persistent.
- **Logs complets:** a Render, servei *cripto-bot-web* → pestanya **Logs**.
- **Aturar-lo:** a Render, *Settings → Suspend Service*. Mentre està suspès no es cobra el servei.
- **Rearmar després d'una aturada d'emergència:** pestanya **Shell** → `python status.py --reset-halt` i després *Manual Deploy → Restart service*.

## Laboratori de proves (des del panell)

Al panell hi ha el **Laboratori de proves**. Tries la caiguda màxima que acceptes (15, 25 o 35%) i prems **Fes la prova**. En uns 10-15 minuts:

1. Descarrega uns 6 anys d'espelmes reals de Binance (parells en USDT) i simula des del gener del 2021 fins avui, amb l'eufòria del 2021 i la caiguda del 2022.
2. Prova cada estratègia amb **paràmetres fixos** en una sola execució contínua, amb l'aturada d'emergència a la caiguda triada, igual que el bot real: si salta, no torna a entrar.
   - Estratègia actual i retrocés de 4 h.
   - Rotació dels 4 més forts amb filtre BTC, amb tot el capital i amb la meitat (el **regulador de risc**, paràmetre `exposure`).
   - Rotació sense filtre BTC, per veure què aporta el filtre.
3. Mostra el rendiment **any per any** i el compara amb comprar Bitcoin, o els 12 valors, i no tocar res.
4. **Prova de robustesa:** prova la rotació amb 24 combinacions de paràmetres. Si la majoria guanyen diners, l'avantatge és real; si no, és sort.

La recomanada és la de més rendiment que **guanya diners**, no passa de la caiguda triada i no fa saltar l'aturada d'emergència. Si cap ho compleix, el laboratori ho diu i no en recomana cap. Amb **Aplica-la al bot (simulació)**, el bot comença a fer-la servir en menys d'un minut (`state/config_lab.yaml`, amb prioritat sobre `config.yaml`). **Torna a la configuració de GitHub** retira els ajustos.

En local: `python run_lab.py --dd 25`.

## Laboratori llarg/curt amb apalancament

Una segona secció del panell prova estratègies que intenten guanyar tant quan el preu puja (llarg) com quan baixa (curt), amb apalancament x1, x2 i x3, sobre els mateixos ~6 anys de dades reals:

- **Bitcoin: llarg o efectiu.** És la referència sense curts.
- **Bitcoin: llarg o curt**, segons la mitjana de 200 dies.
- **Bitcoin i Ether: llarg o curt.**
- **Rotació llarg/curt:** llarg en els més forts quan el mercat puja, curt en els més febles quan baixa.

Simula futurs perpetus amb costos prudents:
- comissió del 0,05% més lliscament del 0,05%;
- els llargs paguen un finançament del 0,01% cada 8 hores (els curts no en cobren res);
- liquidació intra-espelma si el compte cau per sota del 0,5% de la posició;
- aturada d'emergència a la caiguda triada.

També hi ha proves de robustesa per a la tendència (15 combinacions) i per a la rotació (18).

El **criteri d'èxit està fixat abans de la prova**: guanyar diners sense passar de la caiguda acceptada ni ser aturada, guanyar més que mantenir Bitcoin per cada euro de caiguda, i superar la prova de robustesa. Aquestes estratègies encara no es poden aplicar al bot: si alguna supera el criteri, s'implementa.

En local: `python run_lab.py --tipus lc --dd 25`.

## Laboratori de curt termini (5 minuts)

Prova la idea de fer moltes operacions petites cada setmana amb espelmes de 5 minuts dels últims 6 mesos reals, en els 6 valors més líquids. Hi ha tres famílies:

- **Reversió a la mitjana:** quan el preu surt de les bandes de Bollinger, aposta que hi tornarà, en llarg o en curt.
- **Ruptura curta:** quan el preu trenca el rang de les últimes hores, hi entra amb un objectiu i un stop petits.
- **Graella (grid):** compres escalonades per sota del preu; cada lot es ven un esglaó més amunt.

Les estratègies operen sense apalancament, amb stop a cada operació i aturada d'emergència. Els resultats es calculen amb quatre nivells de costos per operació:
- sense costos;
- futurs amb ordres limitades;
- futurs amb ordres a mercat (l'escenari realista);
- Kraken spot.

La clau és la taula **guany brut per operació** comparat amb el **cost per operació**: si el guany és més petit que el cost, l'estratègia perd diners per molt que operi.

Criteri d'èxit fixat abans de la prova: guanyar amb costos realistes, no passar de la caiguda acceptada i superar la robustesa.

En local: `python run_lab.py --tipus ct --dd 15`.

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
