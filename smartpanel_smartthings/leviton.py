"""Leviton cloud access, reusing the Home Assistant integration's client.

custom_components/ldata/ldata_service.py already knows how to log in
(including 2FA), find residences, wake panels with a bandwidth toggle and
parse LDATA / LWHEM breaker and CT data. It only depends on its sibling
const.py, not on Home Assistant, so we load those two files as a
standalone package ("ldata_core") without running the integration's
__init__.py (which imports homeassistant).

fetch_snapshot() turns its parsed output into a flat, JSON-safe dict of
SmartThings devices keyed by externalDeviceId.
"""

import importlib
import logging
import sys
import time
import types
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_LDATA_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "ldata"


def _load_ldata_service():
    name = "ldata_core.ldata_service"
    if name in sys.modules:
        return sys.modules[name]
    pkg = types.ModuleType("ldata_core")
    pkg.__path__ = [str(_LDATA_DIR)]
    sys.modules["ldata_core"] = pkg
    return importlib.import_module(name)


_svc = _load_ldata_service()
LDATAService = _svc.LDATAService
TwoFactorRequired = _svc.TwoFactorRequired
LDATAAuthError = _svc.LDATAAuthError
LDATAConnectionError = _svc.LDATAConnectionError


class _Entry:
    """Minimal stand-in for a Home Assistant ConfigEntry."""

    def __init__(self, token: str = "", userid: str = ""):
        self.data = {"refresh_token": token, "userid": userid}
        self.options = {}


def make_service(creds: dict) -> "LDATAService":
    """Build a client from decrypted credentials {email, password, token, userid}."""
    return LDATAService(
        creds.get("email", ""),
        creds.get("password", ""),
        _Entry(creds.get("token", ""), creds.get("userid", "")),
    )


def login(email: str, password: str, code: str | None = None) -> dict:
    """Log in to Leviton. Returns {token, userid}.

    Raises TwoFactorRequired (no code given but the account needs one),
    LDATAAuthError (bad password / code) or LDATAConnectionError.
    """
    svc = LDATAService(email, password, None)
    if code:
        svc.complete_2fa(code)
    else:
        svc.auth_with_credentials()
    return {"token": svc.auth_token, "userid": svc.userid}


def breaker_is_on(b: dict) -> bool:
    # Same rule as the HA integration (ldata_base_entity.is_breaker_on).
    return b.get("state") == "ManualON" and b.get("remoteState") == "RemoteON"


def _num(value, digits=1):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _is_solar(label) -> bool:
    label = (label or "").lower()
    return "solar" in label or "generat" in label


def _leg(items: list, power_key: str, current_key: str, voltage) -> dict:
    """Sum one leg's power/current over breakers (or a single CT)."""
    powers = [i.get(power_key) for i in items if i.get(power_key) is not None]
    currents = [i.get(current_key) for i in items if i.get(current_key) is not None]
    return {
        "power": _num(sum(powers)) if powers else None,
        "current": _num(sum(currents), 2) if currents else None,
        "voltage": _num(voltage),
    }


def fetch_snapshot(creds: dict) -> tuple[dict, dict]:
    """Fetch every panel, breaker and CT on the account.

    Returns (snapshot, creds). creds may carry a new Leviton token if the old
    one expired and the client logged in again with the stored password;
    the caller should persist it.
    """
    svc = make_service(creds)
    status = svc.status()

    new_creds = dict(creds)
    if svc.auth_token and svc.auth_token != creds.get("token"):
        new_creds["token"] = svc.auth_token
    if svc.userid:
        new_creds["userid"] = svc.userid

    devices: dict[str, dict] = {}
    panels = {p["id"]: p for p in status.get("panels", []) if p.get("id")}

    breakers = list(status.get("breakers", {}).values())

    for pid, p in panels.items():
        # ldata_service normalises every breaker so power1/current1 are on
        # leg A and power2/current2 on leg B, whatever slot it sits in, so
        # the panel's per-leg load is the sum over its breakers.
        own = [b for b in breakers if b.get("panel_id") == pid]
        devices[f"panel-{pid}"] = {
            "kind": "panel",
            "role": "consumption",
            "panel_id": pid,
            "name": p.get("name") or "Leviton Panel",
            "model": p.get("model") or "LDATA",
            "firmware": p.get("firmware") or "unknown",
            "online": bool(p.get("connected")),
            "power": _num(status.get(f"{pid}totalPower")),
            "voltage": _num(p.get("voltage")),
            "legs": {
                "legA": _leg(own, "power1", "current1", p.get("voltage1")),
                "legB": _leg(own, "power2", "current2", p.get("voltage2")),
            },
            "over_voltage": bool(p.get("overVoltage")),
            "under_voltage": bool(p.get("underVoltage")),
        }

    for b in breakers:
        pid = b.get("panel_id")
        panel = panels.get(pid, {})
        position = b.get("position")
        name = (b.get("name") or "").strip() or f"Breaker {position}"
        devices[f"breaker-{b.get('stable_id') or b['id']}"] = {
            "kind": "breaker",
            "role": "solar" if _is_solar(b.get("branch_type")) else "consumption",
            "breaker_id": b["id"],
            "panel_id": pid,
            "panel_name": panel.get("name") or "",
            "name": name,
            "position": position,
            "poles": b.get("poles"),
            "rating": b.get("rating"),
            "model": b.get("model") or "Smart Breaker",
            "firmware": b.get("firmware") or "unknown",
            "online": bool(panel.get("connected", True)),
            "on": breaker_is_on(b),
            "can_remote_on": bool(b.get("canRemoteOn")),
            "power": _num(b.get("power")),
            "current": _num(b.get("current"), 2),
            "voltage": _num(b.get("voltage")),
        }

    for ct in status.get("cts", {}).values():
        pid = ct.get("panel_id")
        panel = panels.get(pid, {})
        usage = ct.get("name") or "CT"
        devices[f"ct-{pid}-{ct['id']}"] = {
            "kind": "ct",
            # Leviton's CT usage type ("Grid", "Solar", …) decides whether
            # SmartThings treats it as consumption or production.
            "role": "solar" if _is_solar(usage) else "consumption",
            "panel_id": pid,
            "name": f"{panel.get('name') or 'Panel'} {usage}".strip(),
            "model": "CT Clamp",
            "firmware": panel.get("firmware") or "unknown",
            "online": bool(panel.get("connected", True)),
            "power": _num(ct.get("power")),
            "legs": {
                "legA": _leg([ct], "power1", "current1", panel.get("voltage1")),
                "legB": _leg([ct], "power2", "current2", panel.get("voltage2")),
            },
        }

    return {"fetched_at": time.time(), "devices": devices}, new_creds


def set_breaker(creds: dict, breaker_id: str, on: bool) -> tuple[bool, dict]:
    """Remotely turn a breaker on or off. Returns (success, creds)."""
    svc = make_service(creds)
    try:
        valid = svc.refresh_auth()
    except LDATAAuthError:
        valid = False
    if not valid:
        # Same fallback status() uses: log in again with the stored password.
        # Raises TwoFactorRequired / LDATAAuthError if that is not possible.
        svc.auth_with_credentials()

    new_creds = dict(creds, token=svc.auth_token, userid=svc.userid)
    result = svc.remote_on(breaker_id) if on else svc.remote_off(breaker_id)
    return result is not None, new_creds
