"""Shared setup for app.py (web) and worker.py (background push)."""

import logging
import os

from dotenv import load_dotenv

from .auth import AuthManager
from .connector import SmartThingsConnector

load_dotenv()

DEBUG = bool(os.environ.get("DEBUG"))
DATA_DIR = os.environ.get("DATA_DIR", ".")

# OAuth client credentials you typed into the Developer Center; SmartThings
# presents them when calling /oauth/token.
ST_CLIENT_ID = os.environ.get("ST_CLIENT_ID", "")
ST_CLIENT_SECRET = os.environ.get("ST_CLIENT_SECRET", "")

# Credentials SmartThings issued for this connector; used to obtain callback
# tokens for pushing state (worker.py).
ST_CALLBACK_CLIENT_ID = os.environ.get("ST_CALLBACK_CLIENT_ID", "")
ST_CALLBACK_CLIENT_SECRET = os.environ.get("ST_CALLBACK_CLIENT_SECRET", "")

PUSH_INTERVAL = int(os.environ.get("PUSH_INTERVAL", 600))


def setup_logging():
    logging.basicConfig(
        level=logging.DEBUG if DEBUG else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s – %(message)s",
    )
    # ldata_service logs full Leviton responses at DEBUG; keep that off
    # unless explicitly asked for, even in DEBUG mode.
    logging.getLogger("ldata").setLevel(
        logging.DEBUG if os.environ.get("DEBUG_LEVITON") else logging.INFO
    )


def build() -> tuple[AuthManager, SmartThingsConnector]:
    os.makedirs(DATA_DIR, exist_ok=True)
    auth = AuthManager(data_dir=DATA_DIR)
    connector = SmartThingsConnector(auth, ST_CALLBACK_CLIENT_ID, ST_CALLBACK_CLIENT_SECRET)
    return auth, connector
