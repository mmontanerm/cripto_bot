# Cripto-bot · tendència llarg/curt en simulació

Bot autònom que opera 6 criptomonedes (BTC, ETH, SOL, XRP, DOGE, ADA) 24/7 amb **diners simulats** i preus reals de Kraken.
No necessita compte a cap exchange ni claus d'API: el risc és zero.

## Què fa

L'estratègia de **tendència llarg/curt** del laboratori, amb paràmetres fixos (la combinació que el laboratori va triar més vegades). Cada hora, en tancar l'espelma de cada valor:

- **Llarg** (guanya si puja): si tanca per sobre del màxim de les últimes 168 h (7 dies).
- **Curt** (guanya si baixa): si tanca per sota del mínim de les últimes 168 h.
- **Sortida:** un llarg es tanca si una hora tanca per sota del mínim de les últimes 84 h (un curt, per sobre del màxim).
- **Stop de pèrdues:** a 4 × ATR (el moviment mitjà per hora de les últimes 24 h), vigilat cada minut.
- **Mida:** el 60 % del capital, repartit a parts iguals entre els 6 valors (una posició com a màxim per valor). Sense apalancament.
- **Costos com al laboratori:** 0,05 % de comissió + 0,02 % de lliscament a cada costat; les posicions llargues paguen un 0,01 % de finançament cada 8 hores.
- **Fre d'emergència:** si el capital cau un 15 % des del seu màxim, ho tanca tot i s'atura fins que el rearmis.

Al laboratori aquesta estratègia **no va demostrar ser millor que entrar a l'atzar**. Això és un experiment per veure-la treballar amb preus reals, sense diners.

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
python run_bot.py      # arrenca el bot 24/7 i el panell a http://localhost:8080 (Ctrl+C per aturar)
python status.py       # resum a la consola: capital, posicions, últimes operacions
python run_lab.py      # laboratori amb dades històriques (normalment es llança des del panell)
```

Fitxers que genera (a `state/`): `estat_bot.json` (el que mostra el panell), `compte_tendencia.json` (el compte simulat), `operacions.csv` (totes les operacions), `capital.csv` (la corba del capital i el preu del Bitcoin). El log és a `logs/bot.log`.

## Ajustar paràmetres

- **Des del panell** (secció *Control*): la part del capital que fa servir i el fre d'emergència. També s'hi pot posar en pausa, reprendre, tancar-ho tot, rearmar després d'una aturada i reiniciar el compte simulat.
- **A `config.yaml`** (a GitHub): la resta. Es recarrega sol; si hi ha un error de format, el bot ho avisa al log i continua amb l'anterior. Els paràmetres de l'estratègia estan fixats a propòsit: canviar-los després de veure resultats és la manera més ràpida d'enganyar-se.

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
- Una columna de llum: verd = treballant, ambre = en pausa o sense notícies, vermell = aturat pel fre d'emergència.
- El capital simulat i el rendiment, comparat amb haver comprat Bitcoin el mateix dia que va començar el compte.
- **Control:** pausa, reprendre, tancar-ho tot, rearmar, reiniciar el compte, i quina part del capital fa servir i on salta el fre.
- Les posicions obertes, amb el stop i el nivell de sortida.
- **Què està vigilant:** per a cada valor, a quin preu entraria en llarg o en curt.
- **Com va respecte al laboratori:** guanyadores, guany i pèrdua mitjans i operacions per setmana, comparats amb el que va fer al laboratori.
- Les últimes operacions, l'activitat recent i, plegat a baix, el laboratori.

Es refresca sol cada 30 segons. A l'iPad o al mòbil, *Compartir → Afegir a la pantalla d'inici* el deixa com una app.

En local, el panell és a http://localhost:8080 mentre el bot està en marxa.

**Dia a dia**
- **Canviar paràmetres:** edita `config.yaml` directament a la web de GitHub (icona del llapis) i desa amb *Commit changes*. Render actualitza el bot sol en 1-2 minuts. El compte simulat es conserva, perquè està al disc persistent.
- **Logs complets:** a Render, servei *cripto-bot-web* → pestanya **Logs**.
- **Aturar-lo:** a Render, *Settings → Suspend Service*. Mentre està suspès no es cobra el servei.
- **Rearmar després d'una aturada d'emergència:** botó *Rearma* al panell.

## Laboratori llarg/curt (des del panell)

Al panell hi ha un sol laboratori: busca paràmetres per guanyar **amb les pujades i amb les baixades** (posicions llargues i curtes), amb els 3 últims anys d'espelmes reals d'1 hora en BTC, ETH, SOL, XRP, DOGE i ADA. Sense apalancament: cada valor fa servir 1/6 del capital.

**Tres famílies d'estratègia (54 combinacions):**
- **Tendència:** entra quan el preu trenca el màxim o el mínim de les últimes N hores; surt amb un canal més curt en contra o amb el stop (múltiple de l'ATR).
- **Reversió:** entra quan el preu surt de les bandes de Bollinger, apostant que torna a la mitjana; surt a la mitjana, amb el stop o al cap de 48 hores.
- **Règim:** si el mercat té tendència (ràtio d'eficiència) opera la ruptura; si no, la reversió.

**Dues mides de posició per a cada estratègia:** mida fixa (1/6 del capital per valor) i mida segons volatilitat (cada posició es redueix perquè la seva volatilitat horària no passi del 0,5 %, la de Bitcoin en un moment normal; sense apalancament). Per comparar-les, el panell mostra el guany dividit per la pitjor caiguda.

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

**Micro-guanys amb ordres limitades:** ordres limitades a les bandes de Bollinger (20 espelmes, 2 desviacions) amb objectiu petit (0,3 %, 0,5 % o 1 %) i stop ajustat (0,5 %, 1 % o 2 %), espelmes de 15 min i 1 h. Ordres limitades al 0,02 % (creador de mercat), stops a mercat al 0,07 %. Una ordre limitada només s'executa si el preu la travessa. També mostra el resultat pagant ordres a mercat. `python run_lab.py --micro`.

**Límit de posicions en la mateixa direcció:** les regles del bot amb un màxim de 6 (sense límit), 3, 2 o 1 posicions alhora en la mateixa direcció. `python run_lab.py --correlacio`.

**Objectius de guany:** les regles exactes del bot amb un objectiu de guany fix (0,5 %, 1 %, 2 %, 3 %, 5 % o 10 %, abans de costos) comparat amb no tenir-ne (el bot actual). Si el preu continua fora del canal després de tancar, hi torna a entrar. Botó "Fes la prova d'objectius", o `python run_lab.py --objectius`.

**Escalat proporcional:** les regles del bot amb tots els paràmetres escalats a la vegada: mateix nombre d'espelmes (canal 168, sortida 84, ATR 24, stop 4 × ATR) amb espelmes de 4 h, 2 h, 90, 60, 45, 30, 20, 15, 10 i 5 minuts. Fa servir espelmes de 5 minuts de Binance (2020–2026, desades comprimides; la primera descàrrega tarda 30-40 minuts). La fila de 60 minuts és idèntica al bot. Botó "Fes l'escalat proporcional", o `python run_lab.py --escalat`.

**Terme mig:** les regles exactes del bot en directe (1 hora, sortida amb canal de la meitat, stop 4 × ATR de 24 h, 60 % del capital, mateixos costos), sense optimitzar, canviant només la durada del canal d'entrada de 168 h a 6 h. Es prova el 2020–2023 i el 2023–2026, cada durada contra 25 simulacions a l'atzar. Comprovat: amb els mateixos preus, el laboratori i el bot fan les mateixes operacions. Botó "Fes la prova del terme mig", o `python run_lab.py --terme-mig`.

**Prova ràpida (10-12 operacions al dia):** *No fa servir les regles del bot:* ATR de 6 h, stops d'1,5 a 3 × ATR, 100 % del capital, paràmetres triats a cada període i també la reversió. les mateixes regles llarg/curt amb espelmes de 15 minuts i canals de 4 a 24 hores (tendència ràpida i reversió ràpida), amb els mateixos costos, mètode i criteri. Mostra el guany per operació abans i després de costos i quina part del capital s'emporten els costos cada any. Botó "Fes la prova ràpida" al panell, o `python run_lab.py --rapid`.

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
| **1. Laboratori** (fet) | Walk-forward, prova del mico i anys no vistos | Cap estratègia ho ha superat |
| **2. Simulació en directe** (ara) | La tendència llarg/curt amb preus reals, uns 3-6 mesos | Comportament coherent amb el laboratori (veure el panell) |
| **3. Diners reals** | No programat a propòsit | Només si hi hagués un avantatge demostrat, que ara no hi és |

## Avisos importants

- Cap resultat passat garanteix resultats futurs. Les criptomonedes poden caure un 50% o més en poques setmanes.
- A Espanya, cada venda amb guany o pèrdua tributa a l'IRPF. El fitxer d'operacions (`state/operacions.csv`) serviria per a la declaració si mai s'operés amb diners reals.
- Aquest programari és una eina, no assessorament financer.
