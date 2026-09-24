# Leviton Smart Panel – SmartThings Integration

Bring **Leviton LDATA and LWHEM smart breaker panels** into **Samsung SmartThings**.
Every smart breaker, panel and CT clamp shows up in the SmartThings app with live power,
voltage, current and energy. Breakers can also be switched on and off remotely, but only
if you allow it.

Hosted at **https://smart-panel.bizgeni.com**.

> **This is a fork.** It started as [rwoldberg/ldata-ha](https://github.com/rwoldberg/ldata-ha),
> the Leviton LDATA / LWHEM integration for **Home Assistant** by
> [RWoldberg](https://github.com/rwoldberg), [MrToast99](https://github.com/MrToast99) and
> contributors. This fork adds a **SmartThings** connector on top of their work. Their
> Home Assistant integration is still here, unchanged. See
> [What changed from the original](#what-changed-from-the-original).

---

## What shows up in SmartThings

| Device | One per | What you see |
|---|---|---|
| **Panel** | LDATA / LWHEM hub | Total power and energy, plus Leg A / Leg B power, current and voltage |
| **CT clamp** | CT with a usage type (Grid, Solar…) | Power and energy, plus Leg A / Leg B power, current and voltage |
| **Breaker** | smart breaker | On/off, power, energy, voltage, current |

- Panels and CT clamps follow the layout of SmartThings' own whole-home energy meters.
- Solar CTs and breakers count generation. Everything else counts consumption.
- **Breaker control is opt-in.** A checkbox on the login page, off by default, decides
  whether SmartThings can switch breakers. Gen 1 breakers can be turned off remotely but must
  be reset by hand at the panel.
- **Data stays fresh.** A background worker pushes new readings to SmartThings every
  15 minutes. Opening a device in the app also fetches fresh data.
- **Energy (kWh) is calculated by the connector** as power × time between readings. Leviton's
  2.x panel firmware has no running energy total.

Full details, setup and deployment steps are in **[SMARTTHINGS.md](SMARTTHINGS.md)**.

---

## How it works

```
SmartThings app ──webhook──►  app.py (Flask + gunicorn)  ──►  Leviton cloud ──► your panel
       ▲                            │
       └──── state push ──────── worker.py (every 15 min)
```

This is a **SmartThings Schema Connector** (cloud-to-cloud integration), built the same way as
the [Philips Home Access connector](https://github.com/JoAnna-Cohen/philips_home_access_smartthings):

1. In the SmartThings app you add the connector and sign in with your my.leviton.com account.
   Accounts with two-factor authentication are supported.
2. Your Leviton credentials are stored **encrypted** on the server, so the connector can sign in
   again by itself when Leviton's session expires.
3. SmartThings discovers your panels, breakers and CT clamps as devices.

---

## What changed from the original

### Added in this fork

Everything below is new. None of it existed in upstream `ldata-ha`.

| Path | What it is |
|---|---|
| `app.py` | Web app: Leviton login page (with 2FA), OAuth token endpoint, SmartThings webhook, landing page |
| `worker.py` | Background service that pushes fresh readings to SmartThings every 15 minutes |
| `smartpanel_smartthings/` | The connector: SmartThings interactions, account linking, encrypted credential storage, energy calculation, callback tokens |
| `smartthings/profiles/` | SmartThings device profiles for breakers, panels, CT clamps and solar CTs |
| `templates/` | Login page and the smart-panel.bizgeni.com landing page |
| `tests/test_smartthings_connector.py` | Tests for the connector (they don't need Home Assistant) |
| `SMARTTHINGS.md` | Setup, SmartThings Developer Center steps and Hestia deployment guide |
| `requirements.txt`, `gunicorn.conf.py`, `.env.example` | Server configuration |

### Changed

- **README.md** is now this page. The original Home Assistant documentation moved, unchanged, to
  **[HOME_ASSISTANT.md](HOME_ASSISTANT.md)**.
- **.gitignore** also ignores the server's `.env`, virtual environment and `data/` folder.

### Not changed

- **The Home Assistant integration** (`custom_components/ldata/`), its tests, blueprints, HACS
  files and GitHub workflows are **exactly as they were upstream** (version 2.0.12). They
  still work in Home Assistant. To install it there, use the
  [upstream repository](https://github.com/rwoldberg/ldata-ha), as described in
  [HOME_ASSISTANT.md](HOME_ASSISTANT.md).
- **CHANGELOG.md** keeps upstream's history, with a section for this fork added at the top.

### How the connector reuses the original code

The SmartThings connector doesn't reimplement Leviton's API. It loads the original
`custom_components/ldata/ldata_service.py` directly, **without Home Assistant**, and uses
upstream's own code for login, 2FA, panel wake-up and breaker/CT parsing. So:

- Fixes to the Leviton client in upstream `ldata-ha` also fix the SmartThings connector once
  merged into this fork.
- The connector's energy calculation follows the same approach as the Home Assistant
  integration's fallback: power × time, skipping long gaps.

### Keeping in sync with upstream

```bash
git remote add upstream https://github.com/rwoldberg/ldata-ha.git   # once
git fetch upstream
git merge upstream/main
pytest tests/test_smartthings_connector.py   # check the connector still works
```

Because `custom_components/` is untouched here, merges from upstream shouldn't conflict. If
upstream changes the shape of the data `ldata_service.py` returns, the connector tests will
catch it. The mapping lives in `smartpanel_smartthings/leviton.py`.

---

## Quick start

```bash
git clone https://github.com/JoAnna-Cohen/smart-panel-st
cd smart-panel-st
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in the values, see SMARTTHINGS.md
gunicorn app:app -c gunicorn.conf.py    # web app on 127.0.0.1:5001
python worker.py                        # 15-minute push (run as a second service)
```

Before it's useful you also need a Schema App and device profiles in the SmartThings Developer
Center, plus an HTTPS domain pointing at the server. [SMARTTHINGS.md](SMARTTHINGS.md) walks
through all of it, including the systemd services and Hestia proxy setup.

---

## Credits

- **[rwoldberg/ldata-ha](https://github.com/rwoldberg/ldata-ha)** by RWoldberg, MrToast99 and
  contributors. They did the Leviton API research and wrote the Home Assistant integration
  and Leviton client this project is built on. If you find it useful, consider supporting them:
  - RWoldberg: [Buy Me A Coffee](https://www.buymeacoffee.com/RWoldberg)
  - MrToast99: [Buy Me A Coffee](https://www.buymeacoffee.com/mrtoast99)
- **SmartThings connector** by [BizGeni](https://bizgeni.com).

This is an independent project. It isn't affiliated with or endorsed by Leviton or Samsung.
