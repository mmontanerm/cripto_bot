"""On es desen l'estat i els logs.

Per defecte, dins la carpeta del projecte. A Render (o qualsevol servidor)
es defineix la variable d'entorn DATA_DIR apuntant al disc persistent,
perquè el compte simulat no es perdi en reiniciar o actualitzar el bot.
"""
import os

DATA_DIR = os.environ.get("DATA_DIR", ".")
STATE_DIR = os.path.join(DATA_DIR, "state")
LOG_DIR = os.path.join(DATA_DIR, "logs")

STATE = os.path.join(STATE_DIR, "paper_state.json")
STATUS = os.path.join(STATE_DIR, "status.json")
TRADES = os.path.join(STATE_DIR, "trades.csv")
