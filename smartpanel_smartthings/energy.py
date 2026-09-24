"""Running energy totals for SmartThings Energy (power × time).

Leviton's 2.x panel firmware has no lifetime energy counter (it resets on
every panel wake-up), so, like the Home Assistant integration's fallback,
we integrate power over time between readings, using the trapezoid rule:
    Wh += (previous W + current W) / 2 × hours elapsed
Every fresh Leviton fetch (the worker's 15-minute push, plus any state
refresh from the app) adds a reading.

Gaps longer than ENERGY_MAX_GAP (panel offline, server down) are skipped
rather than guessed, which matches the HA integration's default "skip"
gap handling.

The total is shown in kWh via energyMeter. It is also sent as a
powerConsumptionReport (running Wh plus the change since the last report),
which is what SmartThings Energy reads; SmartThings only lists devices from
certified integrations there. SmartThings allows at most one report per 15
minutes per device, so reports are only emitted when that much time has
passed.
"""

import os
from datetime import datetime, timezone

MIN_REPORT_INTERVAL = 15 * 60
MAX_GAP = int(os.environ.get("ENERGY_MAX_GAP", 35 * 60))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _metered_watts(dev: dict) -> float | None:
    power = dev.get("power")
    if power is None:
        return None
    if dev.get("role") == "solar":
        # Solar CTs/breakers can read negative depending on clamp direction.
        return abs(power)
    # Negative on a consumption meter means export; it isn't consumption.
    return max(power, 0.0)


def apply(state: dict | None, snapshot: dict, now: float) -> dict:
    """Advance the per-device totals with this snapshot's readings.

    Mutates snapshot devices in place (adds "energy" in kWh and, when due,
    "consumption_report") and returns the new state to persist.
    """
    state = dict(state or {})
    devices = snapshot.get("devices", {})

    for ext_id, dev in devices.items():
        watts = _metered_watts(dev)
        st = state.get(ext_id)
        if st is None:
            st = {"wh": 0.0, "w": watts, "ts": now, "rep_wh": 0.0, "rep_ts": now}
        elif now <= st["ts"]:
            # An older fetch finishing after a newer one (worker and app
            # refresh racing): its reading is already superseded.
            dev["energy"] = round(st["wh"] / 1000, 3)
            continue
        else:
            st = dict(st)
            dt = now - st["ts"]
            if (
                watts is not None
                and st.get("w") is not None
                and dev.get("online")
                and 0 < dt <= MAX_GAP
            ):
                st["wh"] += (st["w"] + watts) / 2 * dt / 3600
            if watts is not None or dt > MAX_GAP:
                st["w"], st["ts"] = watts, now

        dev["energy"] = round(st["wh"] / 1000, 3)
        if now - st["rep_ts"] >= MIN_REPORT_INTERVAL:
            dev["consumption_report"] = {
                "start": _iso(st["rep_ts"]),
                "end": _iso(now),
                "energy": round(st["wh"], 1),
                "deltaEnergy": round(max(st["wh"] - st["rep_wh"], 0.0), 1),
            }
            st["rep_wh"], st["rep_ts"] = st["wh"], now
        state[ext_id] = st

    # Forget devices that disappeared from the account.
    for ext_id in [k for k in state if k not in devices]:
        del state[ext_id]
    return state
