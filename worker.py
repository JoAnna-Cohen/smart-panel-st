"""Background worker: push fresh panel data to SmartThings on a schedule.

Every PUSH_INTERVAL seconds (default 600 = 10 minutes) it fetches each
linked account from Leviton and sends the states to SmartThings'
stateCallback URL, so values stay current for automations even when nobody
opens the app. Runs as its own systemd service next to gunicorn.

  python worker.py          run forever
  python worker.py --once   one pass, then exit (handy for testing)
"""

import logging
import signal
import sys
import threading

from smartpanel_smartthings import callbacks, leviton, runtime
from smartpanel_smartthings.connector import build_device_state

runtime.setup_logging()
_LOGGER = logging.getLogger("worker")

_stop = threading.Event()


def push_all(auth, connector):
    for link_id, link in auth.all_links().items():
        if _stop.is_set():
            return
        if link.get("needs_reauth") or not link.get("callback"):
            continue
        try:
            snapshot = connector.fetch(link_id, link)
        except (leviton.LDATAAuthError, leviton.TwoFactorRequired):
            continue  # marked needs_reauth by fetch()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Fetch failed for link %s***", link_id[:6])
            continue

        device_state = build_device_state(snapshot)
        try:
            cb = callbacks.push_state(
                link["callback"], device_state,
                runtime.ST_CALLBACK_CLIENT_ID, runtime.ST_CALLBACK_CLIENT_SECRET,
            )
            if cb is not link["callback"]:
                auth.set_callback(link_id, cb)
            _LOGGER.info("Pushed %d device(s) for link %s***", len(device_state), link_id[:6])
        except Exception:  # noqa: BLE001
            _LOGGER.exception("State push failed for link %s***", link_id[:6])


def main():
    auth, connector = runtime.build()
    if not (runtime.ST_CALLBACK_CLIENT_ID and runtime.ST_CALLBACK_CLIENT_SECRET):
        _LOGGER.warning("ST_CALLBACK_CLIENT_ID / SECRET not set; pushes will be rejected")

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())

    once = "--once" in sys.argv
    _LOGGER.info("Worker started (interval %ss)", runtime.PUSH_INTERVAL)
    while not _stop.is_set():
        push_all(auth, connector)
        if once:
            break
        _stop.wait(runtime.PUSH_INTERVAL)
    _LOGGER.info("Worker stopped")


if __name__ == "__main__":
    main()
