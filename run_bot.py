"""Arrenca el bot 24/7 (tendència llarg/curt, simulació) amb el panell web.

    python run_bot.py      -> panell a http://localhost:8080
"""
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from bot import dashboard  # noqa: E402
from bot.engine import Engine  # noqa: E402
from bot.logsetup import setup  # noqa: E402

if __name__ == "__main__":
    setup("bot")
    try:
        dashboard.start()  # panell web: http://localhost:8080 en local, o l'adreça de Render
    except OSError as e:
        import logging
        logging.getLogger("bot").warning("No s'ha pogut obrir el panell web: %s", e)
    try:
        Engine("config.yaml").run()
    except Exception as e:
        import logging
        logging.getLogger("bot").exception("No s'ha pogut arrencar: %s", e)
        sys.exit(1)
