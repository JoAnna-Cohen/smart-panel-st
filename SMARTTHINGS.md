# Leviton Smart Panel – SmartThings Integration

Bring **Leviton LDATA / LWHEM smart breaker panels** into **Samsung SmartThings**.

This is a **SmartThings Schema Connector** (cloud-to-cloud integration), built
the same way as the
[Philips Home Access connector](https://github.com/JoAnna-Cohen/philips_home_access_smartthings).
It reuses the Leviton cloud client from the Home Assistant integration in
[`custom_components/ldata`](custom_components/ldata) (`ldata_service.py`), so
login, 2FA, panel wake-up and breaker parsing stay identical between the two.

Hosted at **https://smart-panel.bizgeni.com**.

---

## What shows up in SmartThings

The layout follows SmartThings' own whole-home energy meters (the Aeotec Home
Energy Meter and SmartThings' 2-phase power meter). Panels and CT clamps are one
device each, with the totals on top and a **Leg A / Leg B** section showing each
leg's power, current and voltage. They use SmartThings' `CurbPowerMeter` category,
the same one its official power meters use.

| Device | One per | Main (top of the device page) | Leg A / Leg B |
|---|---|---|---|
| Panel | LDATA / LWHEM hub | Total power (sum of breakers), energy, leg-to-leg voltage | Power, current, voltage |
| CT clamp | CT with a usage type (Grid, Solar…) | Power, energy | Power, current, voltage |
| Breaker | smart breaker | On/off, power, energy, voltage, current | – |

A CT whose usage type is Solar (or a breaker whose circuit type is Solar) is a
**production** meter: its energy counts generation. Everything else counts consumption.

### SmartThings Energy

Every device reports `powerConsumptionReport`, which is the capability that makes a
device show up in **SmartThings Energy** (the Energy section of the app). SmartThings
accepts at most one report per device every 15 minutes, which is why the push
interval is 15 minutes.

Leviton's 2.x panel firmware doesn't keep a running energy total. Its counter
resets every time the panel is woken up. So the connector works out energy itself,
the same way the Home Assistant integration does:

- Between two readings it adds **average power × time elapsed** (the trapezoid
  rule). Readings come from the 15-minute push, plus any refresh from the app.
- Energy totals start at 0 when you link the account.
- If there's no reading for more than 35 minutes (`ENERGY_MAX_GAP`), for example
  because the panel or the server was offline, that gap is **skipped** rather than
  guessed. Energy used while the panel is offline isn't counted.
- For consumption, negative power (export) counts as 0. For solar, the size of the
  power counts, so a clamp mounted backwards still reads as generation.
- Because it samples every 15 minutes, a load that switches on and off between
  readings (a microwave, say) is estimated, not measured. Over a day, whole-panel
  and CT totals come out much closer than short, spiky individual circuits.

In the SmartThings app's Energy settings, pick the **Grid CT** (or the panel, if you
have no CT) as your whole-home meter and the **Solar CT** as your solar production
meter.

Every device also reports online/offline (`healthCheck`) from the panel's
connection state. Non-smart ("dumb") breakers and Decora Smart Wi-Fi devices are
not included. Leviton already has an official SmartThings integration for Decora.

### Breaker control is opt-in

The login page has an **"Allow SmartThings to turn breakers off and on"**
checkbox, unchecked by default. Without it, breakers show their on/off state
but SmartThings commands are refused. With it:

- **Off** remotely trips the breaker.
- **On** works only for breakers Leviton reports as `canRemoteOn`. Gen 1
  breakers can be tripped remotely but must be reset by hand at the panel.

To change the setting, remove the integration in SmartThings and link again.

### How fresh is the data?

- **When you open a device in the app** (stateRefresh), the connector fetches
  from Leviton. It reuses data up to 60 s old (`SNAPSHOT_MAX_AGE`) so it doesn't
  wake the panel on every request.
- **Every 15 minutes** (`PUSH_INTERVAL`), `worker.py` fetches every linked
  account and pushes the values and energy reports to SmartThings, so
  automations and SmartThings Energy stay current even when nobody has the
  app open.

---

## How it works

```
SmartThings ──webhook──►  app.py (gunicorn, 127.0.0.1:5001)  ──►  Leviton cloud ──► panel
     ▲                         │  links.json / snapshots.json / energy.json
     └──── stateCallback ──── worker.py (every 15 min)
```

1. In the SmartThings app, you add the connector and land on the login page
   (`/oauth/authorize`).
2. You sign in with your my.leviton.com account. If the account uses 2FA,
   a second screen asks for the code.
3. The connector stores your Leviton email, password and session token
   **encrypted** (Fernet, key in `CREDENTIAL_KEY`). When Leviton's session
   expires it signs in again by itself, as the Home Assistant integration does.
   - If the account has 2FA, it can't enter a new code for you. When that
     happens, SmartThings asks you to re-link.
4. SmartThings discovers one device per panel, breaker and CT.

---

## 1. SmartThings Developer Center

This needs its **own** Schema App, separate from the Philips one.

1. Sign in at <https://developer.smartthings.com/> and create a new project.
2. **Create the device profiles.** Ready-made definitions are in
   [`smartthings/profiles/`](smartthings/profiles). The easiest way is the
   [SmartThings CLI](https://github.com/SmartThingsCommunity/smartthings-cli):

   ```bash
   smartthings login
   cd smartthings/profiles
   smartthings deviceprofiles:create -i leviton-breaker.yml
   smartthings deviceprofiles:create -i leviton-panel.yml
   smartthings deviceprofiles:create -i leviton-ct.yml
   smartthings deviceprofiles:create -i leviton-solar.yml
   ```

   Each command prints the new profile's id. Put them in `.env`:

   | File | Put its id in | What it has |
   |---|---|---|
   | `leviton-breaker.yml` | `ST_PROFILE_BREAKER` | Switch, power, energy, energy report, voltage, current |
   | `leviton-panel.yml` | `ST_PROFILE_PANEL` | Main: power, energy, energy report, voltage · Leg A/B: power, current, voltage |
   | `leviton-ct.yml` | `ST_PROFILE_CT` | Main: power, energy, energy report · Leg A/B: power, current, voltage |
   | `leviton-solar.yml` | `ST_PROFILE_SOLAR` | Same as CT, labelled Production (optional; uses the CT profile if blank) |

   You can also build them by hand in the Developer Center. Match the component ids
   (`main`, `legA`, `legB`) exactly, because the connector sends states to those names.
   If `ST_PROFILE_BREAKER` is blank, breakers use SmartThings' built-in
   `c2c-switch-power-energy` handler, which has no voltage/current tiles and doesn't
   show up in SmartThings Energy. Panels and CTs are skipped until their profile id is set.
3. Add a **Schema App (Cloud Connector)**:

   | Field | Value |
   |---|---|
   | Hosting | Webhook |
   | Webhook URL | `https://smart-panel.bizgeni.com/webhook` |
   | Authorization URI | `https://smart-panel.bizgeni.com/oauth/authorize` |
   | Token URI | `https://smart-panel.bizgeni.com/oauth/token` |
   | Client ID / Secret | Any strings you make up → `ST_CLIENT_ID` / `ST_CLIENT_SECRET` |
   | Scopes | *(blank)* |

4. After saving, SmartThings shows **its own** Client ID and Client Secret
   for the connector. Put those in `ST_CALLBACK_CLIENT_ID` /
   `ST_CALLBACK_CLIENT_SECRET`. Without them, the 15-minute push won't work.

### Test in the app

1. Turn on Developer Mode: SmartThings app → Menu → Settings → long-press
   **About SmartThings** for 10 s, then restart the app.
2. Go to **Add device → Partner devices → My Testing Devices**, pick the
   connector and sign in with Leviton.

---

## 2. Deploy on the Hestia server

It runs next to the Philips connector (which uses port 5000). This one uses
**127.0.0.1:5001**.

```bash
# As the Hestia web user (adjust user/paths to match the Philips setup)
cd /home/<user>/apps
git clone -b main https://github.com/JoAnna-Cohen/smart-panel-st
cd smart-panel-st
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
# Fill in .env. Generate the two secrets with:
python -c "import secrets; print(secrets.token_hex(32))"                          # SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # CREDENTIAL_KEY
mkdir -p data && chmod 700 data
```

Keep `DATA_DIR` **outside** `public_html`. It holds the encrypted credentials.
Back up `CREDENTIAL_KEY`: if it's lost, every account has to be re-linked.

### systemd: two services

`/etc/systemd/system/smart-panel-web.service`

```ini
[Unit]
Description=Leviton Smart Panel SmartThings Connector (web)
After=network.target

[Service]
User=<user>
WorkingDirectory=/home/<user>/apps/smart-panel-st
EnvironmentFile=/home/<user>/apps/smart-panel-st/.env
ExecStart=/home/<user>/apps/smart-panel-st/.venv/bin/gunicorn app:app -c gunicorn.conf.py
Restart=always

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/smart-panel-worker.service`

```ini
[Unit]
Description=Leviton Smart Panel SmartThings Connector (15-minute push)
After=network.target smart-panel-web.service

[Service]
User=<user>
WorkingDirectory=/home/<user>/apps/smart-panel-st
EnvironmentFile=/home/<user>/apps/smart-panel-st/.env
ExecStart=/home/<user>/apps/smart-panel-st/.venv/bin/python worker.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now smart-panel-web smart-panel-worker
curl -s http://127.0.0.1:5001/health    # {"status":"ok"}
journalctl -u smart-panel-worker -f      # "Pushed N device(s)…" every 15 min
```

### Hestia web domain + proxy

1. In Hestia add the web domain **smart-panel.bizgeni.com** (DNS A record to
   the server) and enable **SSL → Let's Encrypt** and **Force SSL**.
2. Proxy to port 5001 the same way as `samsung.bizgeni.com`. If you used a
   custom nginx proxy template, copy it with the port changed:

   ```bash
   cd /usr/local/hestia/data/templates/web/nginx
   sudo cp <philips-template>.tpl  smart-panel.tpl
   sudo cp <philips-template>.stpl smart-panel.stpl
   sudo sed -i 's/127.0.0.1:5000/127.0.0.1:5001/' smart-panel.tpl smart-panel.stpl
   ```

   The `location /` block in both files should be:

   ```nginx
   location / {
       proxy_pass http://127.0.0.1:5001;
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
       proxy_read_timeout 120s;
   }
   ```

3. In Hestia → Edit web domain → Advanced → **Proxy template** pick
   `smart-panel`, then save.
4. Check that `https://smart-panel.bizgeni.com/` shows the landing page and
   `/health` returns ok.

### Updating

```bash
cd /home/<user>/apps/smart-panel-st && git pull
source .venv/bin/activate && pip install -r requirements.txt
sudo systemctl restart smart-panel-web smart-panel-worker
```

---

## Configuration reference

| Variable | Required | Description |
|---|---|---|
| `ST_CLIENT_ID` / `ST_CLIENT_SECRET` | Yes | OAuth credentials you entered in the Schema App |
| `ST_CALLBACK_CLIENT_ID` / `ST_CALLBACK_CLIENT_SECRET` | Yes | Credentials SmartThings issued (for the 15-minute push) |
| `ST_PROFILE_BREAKER` / `_PANEL` / `_CT` / `_SOLAR` | Recommended | Device profile ids (from `smartthings/profiles/`) |
| `SECRET_KEY` | Yes | Flask session signing |
| `CREDENTIAL_KEY` | Yes | Fernet key encrypting stored Leviton credentials |
| `DATA_DIR` | No | Where JSON state lives (default `.`; use `./data`) |
| `PUSH_INTERVAL` | No | Seconds between pushes (default `900`; SmartThings' energy-report minimum) |
| `ENERGY_MAX_GAP` | No | Longest gap between readings (seconds) that still counts toward energy (default `2100`) |
| `SNAPSHOT_MAX_AGE` | No | Reuse fetched data for this many seconds (default `60`) |
| `ALLOWED_REDIRECT_HOSTS` | No | OAuth redirect host suffixes (default `.smartthings.com`) |
| `DEBUG` / `DEBUG_LEVITON` | No | Verbose logs / raw Leviton responses |

---

## Project layout (connector only)

```
app.py                        Flask app: OAuth login + token, webhook, landing page
worker.py                     15-minute push to SmartThings (separate service)
gunicorn.conf.py              127.0.0.1:5001, 2 workers, 120 s timeout
smartpanel_smartthings/
  leviton.py                  Loads custom_components/ldata/ldata_service.py without
                              Home Assistant; converts its data to SmartThings devices
  connector.py                st-schema interactions
  energy.py                   Energy totals (power × time) + SmartThings Energy reports
  auth.py                     OAuth codes/tokens, account links, snapshot cache
  callbacks.py                SmartThings callback tokens + stateCallback
  crypto.py                   Credential encryption
  storage.py                  JSON store with a cross-process file lock
smartthings/profiles/         Device profiles to create with the SmartThings CLI
templates/                    login.html (incl. 2FA step), index.html
tests/test_smartthings_connector.py
```

Run the connector tests (no Home Assistant needed):

```bash
pip install -r requirements.txt pytest
pytest tests/test_smartthings_connector.py
```
