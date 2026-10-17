"""Arrenca el bot 24/7 en mode simulació.

    python run_bot.py
"""
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

from bot.engine import Engine  # noqa: E402
from bot.logsetup import setup  # noqa: E402

if __name__ == "__main__":
    setup("bot")
    try:
        Engine("config.yaml").run()
    except Exception as e:
        import logging
        logging.getLogger("bot").exception("No s'ha pogut arrencar: %s", e)
        sys.exit(1)
