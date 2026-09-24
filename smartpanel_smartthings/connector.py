"""SmartThings Schema Connector request handler.

Interactions:
- discoveryRequest      -> one device per panel, smart breaker and CT clamp
- stateRefreshRequest   -> power / energy / voltage / current / on-off state
- commandRequest        -> breaker on/off (only if control was allowed at login)
- grantCallbackAccess   -> exchange for callback tokens (used by worker.py)
- integrationDeleted    -> forget the link and its credentials

Panels and CT clamps follow the layout of SmartThings' own whole-home
meters (Aeotec Home Energy Meter, 2-phase power meter): totals on the main
component, plus legA / legB components with power, current and voltage.
Every device also sends powerConsumptionReport. SmartThings accepts it from
cloud connectors, but SmartThings Energy only lists devices from certified
("Works with SmartThings") integrations, so for now the useful values are
powerMeter / energyMeter. Profile definitions live in smartthings/profiles/.

https://developer.smartthings.com/docs/devices/cloud-connected/st-schema
"""

import logging
import os
import time

from . import callbacks, energy, leviton

_LOGGER = logging.getLogger(__name__)

ST_SCHEMA = "st-schema"
ST_VERSION = "1.0"

# stateRefresh reuses data fetched within this many seconds. Every fresh
# fetch wakes the panel (bandwidth toggle), so don't do it on every request.
SNAPSHOT_MAX_AGE = int(os.environ.get("SNAPSHOT_MAX_AGE", 60))

# Device profile IDs created from smartthings/profiles/ (see SMARTTHINGS.md).
# Breakers fall back to a built-in handler type if no custom profile is
# configured; panels and CTs need their own profile. Solar CTs use
# ST_PROFILE_SOLAR if set, otherwise the CT profile.
PROFILE_BREAKER = os.environ.get("ST_PROFILE_BREAKER", "")
PROFILE_PANEL = os.environ.get("ST_PROFILE_PANEL", "")
PROFILE_CT = os.environ.get("ST_PROFILE_CT", "")
PROFILE_SOLAR = os.environ.get("ST_PROFILE_SOLAR", "") or PROFILE_CT
DEFAULT_BREAKER_HANDLER = "c2c-switch-power-energy"

# Category SmartThings' own power-meter drivers use for energy meters.
METER_CATEGORY = "CurbPowerMeter"


def _headers(interaction_type: str, request_id: str) -> dict:
    return {
        "schema": ST_SCHEMA,
        "version": ST_VERSION,
        "interactionType": interaction_type,
        "requestId": request_id,
    }


def _state(capability: str, attribute: str, value, unit: str | None = None,
           component: str = "main") -> dict:
    s = {"component": component, "capability": capability, "attribute": attribute, "value": value}
    if unit:
        s["unit"] = unit
    return s


def _electrical(values: dict, component: str) -> list:
    states = []
    for key, cap, unit in (
        ("power", "st.powerMeter", "W"),
        ("voltage", "st.voltageMeasurement", "V"),
        ("current", "st.currentMeasurement", "A"),
    ):
        if values.get(key) is not None:
            # The attribute name matches the snapshot key (power/voltage/current).
            states.append(_state(cap, key, values[key], unit, component))
    return states


def build_states(dev: dict) -> list:
    """SmartThings states for one snapshot device."""
    states = [
        _state("st.healthCheck", "healthStatus", "online" if dev.get("online") else "offline")
    ]
    if dev["kind"] == "breaker":
        states.append(_state("st.switch", "switch", "on" if dev.get("on") else "off"))
    states += _electrical(dev, "main")
    if dev.get("energy") is not None:
        states.append(_state("st.energyMeter", "energy", dev["energy"], "kWh"))
    if dev.get("consumption_report"):
        states.append(
            _state("st.powerConsumptionReport", "powerConsumption", dev["consumption_report"])
        )
    for leg_id, leg in (dev.get("legs") or {}).items():
        states += _electrical(leg, leg_id)
    return states


def build_device_state(snapshot: dict, ids=None) -> list:
    devices = snapshot.get("devices", {})
    ids = devices.keys() if ids is None else ids
    return [
        {"externalDeviceId": i, "states": build_states(devices[i])}
        for i in ids
        if i in devices
    ]


class SmartThingsConnector:
    def __init__(self, auth_manager, callback_client_id: str = "", callback_client_secret: str = ""):
        self._auth = auth_manager
        self._cb_id = callback_client_id
        self._cb_secret = callback_client_secret

    # ------------------------------------------------------------------
    # Snapshot access (shared with worker.py)
    # ------------------------------------------------------------------

    def fetch(self, link_id: str, link: dict) -> dict:
        """Fetch fresh data from Leviton, persist it and any new token.

        Raises leviton.LDATAAuthError if the account can't be logged in to
        any more (the link is marked as needing re-authentication).
        """
        creds = self._auth.get_creds(link)
        if creds is None:
            self._auth.mark_needs_reauth(link_id)
            raise leviton.LDATAAuthError("stored credentials could not be decrypted")
        try:
            snapshot, new_creds = leviton.fetch_snapshot(creds)
        except leviton.LDATAAuthError:
            self._auth.mark_needs_reauth(link_id)
            raise
        now = snapshot["fetched_at"]
        self._auth.update_energy(link_id, lambda st: energy.apply(st, snapshot, now))
        self._auth.save_creds(link_id, new_creds)
        self._auth.save_snapshot(link_id, snapshot)
        return snapshot

    def snapshot(self, link_id: str, link: dict, max_age: float) -> dict:
        cached = self._auth.get_snapshot(link_id)
        if cached and time.time() - cached.get("fetched_at", 0) <= max_age:
            return cached
        try:
            return self.fetch(link_id, link)
        except leviton.LDATAAuthError:
            raise
        except Exception:
            if cached:
                _LOGGER.exception("Leviton fetch failed; serving cached data")
                return cached
            raise

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def handle(self, body: dict) -> dict:
        headers = body.get("headers", {})
        interaction = headers.get("interactionType", "")
        request_id = headers.get("requestId", "")
        access_token = body.get("authentication", {}).get("token", "")

        _LOGGER.debug("Connector received interactionType=%s", interaction)

        if interaction == "integrationDeleted":
            link_id = self._auth.link_id_for_token(access_token)
            if link_id:
                self._auth.revoke_link(link_id)
            return {"headers": _headers("integrationDeletedResponse", request_id)}

        handlers = {
            "discoveryRequest": ("discoveryResponse", self._discovery),
            "stateRefreshRequest": ("stateRefreshResponse", self._state_refresh),
            "commandRequest": ("commandResponse", self._command),
            "grantCallbackAccess": ("grantCallbackAccessResponse", self._grant_callback),
        }
        if interaction not in handlers:
            _LOGGER.warning("Unknown interactionType: %s", interaction)
            return self._global_error(
                f"{interaction}Response", request_id, "INVALID-INTERACTION-TYPE",
                f"Unknown interaction: {interaction}",
            )

        response_type, handler = handlers[interaction]
        link_id, link, error = self._auth.link_for_token(access_token)
        if error:
            return self._global_error(response_type, request_id, error, "Invalid or expired token")

        try:
            result = handler(body, link_id, link)
        except (leviton.LDATAAuthError, leviton.TwoFactorRequired):
            return self._global_error(
                response_type, request_id, "INVALID-TOKEN",
                "Leviton login failed; re-link the account in SmartThings",
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("%s failed", interaction)
            return self._global_error(response_type, request_id, "BAD-REQUEST", str(exc))
        return {"headers": _headers(response_type, request_id), **result}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _discovery(self, body, link_id, link) -> dict:
        snapshot = self.fetch(link_id, link)
        devices = []
        for ext_id, dev in snapshot["devices"].items():
            st_dev = self._build_st_device(ext_id, dev)
            if st_dev:
                devices.append(st_dev)
        _LOGGER.info("Discovery: %d device(s) for link %s***", len(devices), link_id[:6])
        return {"devices": devices}

    def _build_st_device(self, ext_id: str, dev: dict) -> dict | None:
        kind, name = dev["kind"], dev["name"]
        profile, category = {
            "breaker": (PROFILE_BREAKER, "Switch"),
            "panel": (PROFILE_PANEL, METER_CATEGORY),
            "ct": (PROFILE_SOLAR if dev.get("role") == "solar" else PROFILE_CT, METER_CATEGORY),
        }[kind]

        entry = {
            "externalDeviceId": ext_id,
            "friendlyName": name,
            "manufacturerInfo": {
                "manufacturerName": "Leviton",
                "modelName": dev.get("model") or "LDATA",
                "hwVersion": "1.0",
                "swVersion": str(dev.get("firmware") or "unknown"),
            },
            "deviceContext": {"categories": [category]},
        }
        if profile:
            entry["deviceUniqueId"] = profile
        elif kind == "breaker":
            entry["deviceHandlerType"] = DEFAULT_BREAKER_HANDLER
        else:
            _LOGGER.warning(
                "Skipping %s '%s': set ST_PROFILE_%s to a device profile id",
                kind, name, kind.upper(),
            )
            return None
        return entry

    # ------------------------------------------------------------------
    # State refresh
    # ------------------------------------------------------------------

    def _state_refresh(self, body, link_id, link) -> dict:
        snapshot = self.snapshot(link_id, link, SNAPSHOT_MAX_AGE)
        ids = [d.get("externalDeviceId") for d in body.get("devices", [])]
        known = snapshot.get("devices", {})
        device_state = build_device_state(snapshot, ids)
        for i in ids:
            if i and i not in known:
                device_state.append(
                    {
                        "externalDeviceId": i,
                        "deviceError": [
                            {"errorEnum": "DEVICE-DELETED", "detail": "Not found on the Leviton account"}
                        ],
                    }
                )
        return {"deviceState": device_state}

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _command(self, body, link_id, link) -> dict:
        snapshot = self.snapshot(link_id, link, max_age=3600)
        devices = snapshot.get("devices", {})
        device_state = []
        changed = False

        for dev_cmd in body.get("devices", []):
            ext_id = dev_cmd.get("externalDeviceId")
            dev = devices.get(ext_id)
            if not dev:
                device_state.append(self._device_error(ext_id, [], "DEVICE-DELETED", "Unknown device"))
                continue

            error = None
            for cmd in dev_cmd.get("commands", []):
                if cmd.get("capability") != "st.switch" or dev["kind"] != "breaker":
                    error = ("CAPABILITY-NOT-SUPPORTED", "Only breaker on/off is supported")
                    continue
                turn_on = cmd.get("command") == "on"
                if not link.get("allow_control"):
                    error = (
                        "CAPABILITY-NOT-SUPPORTED",
                        "Breaker control was not enabled when this account was linked",
                    )
                    continue
                if turn_on and not dev.get("can_remote_on"):
                    error = (
                        "CAPABILITY-NOT-SUPPORTED",
                        "This breaker can't be turned on remotely; reset it at the panel",
                    )
                    continue
                if not dev.get("online"):
                    error = ("DEVICE-OFFLINE", "Panel is offline")
                    continue
                try:
                    ok, new_creds = leviton.set_breaker(
                        self._auth.get_creds(link), dev["breaker_id"], turn_on
                    )
                    self._auth.save_creds(link_id, new_creds)
                except (leviton.LDATAAuthError, leviton.TwoFactorRequired):
                    self._auth.mark_needs_reauth(link_id)
                    raise
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Breaker command failed for %s", ext_id)
                    ok = False
                _LOGGER.info(
                    "Breaker %s (%s) turned %s: %s",
                    dev["name"], ext_id, "on" if turn_on else "off", "ok" if ok else "FAILED",
                )
                if ok:
                    dev["on"] = turn_on
                    changed = True
                else:
                    error = ("DEVICE-UNAVAILABLE", "Leviton did not accept the command")

            states = build_states(dev)
            if error:
                device_state.append(self._device_error(ext_id, states, *error))
            else:
                device_state.append({"externalDeviceId": ext_id, "states": states})

        if changed:
            self._auth.save_snapshot(link_id, snapshot)
        return {"deviceState": device_state}

    @staticmethod
    def _device_error(ext_id, states, error_enum, detail) -> dict:
        entry = {"externalDeviceId": ext_id, "deviceError": [{"errorEnum": error_enum, "detail": detail}]}
        if states:
            entry["states"] = states
        return entry

    # ------------------------------------------------------------------
    # Callback access
    # ------------------------------------------------------------------

    def _grant_callback(self, body, link_id, link) -> dict:
        grant = body.get("callbackAuthentication", {})
        urls = body.get("callbackUrls", {})
        if grant.get("code") and urls.get("oauthToken"):
            try:
                cb = callbacks.exchange_code(grant, urls, self._cb_id, self._cb_secret)
                self._auth.set_callback(link_id, cb)
                _LOGGER.info("Callback access granted for link %s***", link_id[:6])
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Callback token exchange failed; live updates disabled")
        return {}

    # ------------------------------------------------------------------
    # Errors
    # ------------------------------------------------------------------

    def _global_error(self, response_type, request_id, error_enum, description) -> dict:
        _LOGGER.warning("Global error [%s]: %s – %s", response_type, error_enum, description)
        return {
            "headers": _headers(response_type, request_id),
            "globalError": {"errorEnum": error_enum, "detail": description},
        }
