"""On es desen l'estat i els logs.

Per defecte, dins la carpeta del projecte. A Render (o qualsevol servidor)
es defineix la variable d'entorn DATA_DIR apuntant al disc persistent,
perquè el compte simulat no es perdi en reiniciar o actualitzar el bot.
"""
import os

DATA_DIR = os.environ.get("DATA_DIR", ".")
STATE_DIR = os.path.join(DATA_DIR, "state")
LOG_DIR = os.path.join(DATA_DIR, "logs")

STATE = os.path.join(STATE_DIR, "compte_tendencia.json")     # compte simulat de l'estratègia de tendència
STATUS = os.path.join(STATE_DIR, "estat_bot.json")           # el que mostra el panell
TRADES = os.path.join(STATE_DIR, "operacions.csv")
EQUITY = os.path.join(STATE_DIR, "capital.csv")
OVERRIDE = os.path.join(STATE_DIR, "ajustos.yaml")           # ajustos canviats des del panell
COMMANDS = os.path.join(STATE_DIR, "ordres.json")            # ordres del panell (pausa, tancar tot...)
LAB_DIR = os.path.join(STATE_DIR, "lab")
# Fitxers del bot anterior (retrocés/rotació): es retiren en arrencar
OLD_FILES = ["paper_state.json", "status.json", "trades.csv", "equity.csv", "config_lab.yaml",
             "config_lab.yaml.retirat", "config_lab.yaml.invalid"]
