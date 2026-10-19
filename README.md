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

## Laboratori llarg/curt (des del panell)

Al panell hi ha un sol laboratori: busca paràmetres per guanyar **amb les pujades i amb les baixades** (posicions llargues i curtes), amb els 3 últims anys d'espelmes reals d'1 hora en BTC, ETH, SOL, XRP, DOGE i ADA. Sense apalancament: cada valor fa servir 1/6 del capital.

**Tres famílies d'estratègia (54 combinacions):**
- **Tendència:** entra quan el preu trenca el màxim o el mínim de les últimes N hores; surt amb un canal més curt en contra o amb el stop (múltiple de l'ATR).
- **Reversió:** entra quan el preu surt de les bandes de Bollinger, apostant que torna a la mitjana; surt a la mitjana, amb el stop o al cap de 48 hores.
- **Règim:** si el mercat té tendència (ràtio d'eficiència) opera la ruptura; si no, la reversió.

**Costos, el més reals possible:** 0,14% per operació (futurs amb ordres a mercat: comissió + lliscament), les llargues paguen un 0,01% de finançament cada 8 hores i a les curtes no se'ls compta cap ingrés, stops al stop o pitjor si el preu salta, i cost de tancar i reobrir cada vegada que el bot canvia de paràmetres.

**Com evita enganyar-se:**
1. **Walk-forward:** tria els paràmetres amb 6 mesos i els fa servir els 2 mesos següents, que no ha vist; avança 2 mesos i repeteix.
2. **Altiplà:** tria la combinació que puntua millor juntament amb les veïnes, no el pic aïllat.
3. **Prova del mico:** 25 simulacions amb entrades a l'atzar, la mateixa freqüència i les mateixes sortides i costos.
4. **Reserva final:** els 4 últims mesos no es fan servir fins al final i s'hi prova una sola vegada, amb l'aturada per caiguda màxima.

**Criteri d'èxit fixat abans de la prova:**
1. guanyar als períodes no vistos, amb almenys 100 operacions;
2. superar el 95% de les simulacions amb entrades a l'atzar;
3. guanyar a la reserva final sense passar de la caiguda acceptada ni fer saltar l'aturada.

**Prova amb anys no vistos (2020–2023):** la tendència llarg/curt, amb exactament les mateixes regles, paràmetres possibles, costos i criteri, sobre dades que el laboratori principal no ha fet servir mai (gener del 2020 – setembre del 2023, amb el mercat alcista del 2021 i el baixista del 2022). Fixada abans de veure'n el resultat. Botó "Fes la prova 2020–2023" al panell, o `python run_lab.py --passat`.

En local: `python run_lab.py --dd 15`. Els laboratoris anteriors s'han retirat: en arrencar, el panell n'esborra els resultats i les dades que quedin al disc.

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
