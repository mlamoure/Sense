# Sense Home Energy — Indigo plugin

Brings your [Sense](https://sense.com) home energy monitor into [Indigo](https://www.indigodomo.com):
one Indigo device per appliance Sense has learned, plus an "Active Total" device for the whole
house. Each device's `power` state is the live wattage Sense reports, so any Indigo trigger,
control page or script can react to an appliance switching on or off.

## Requirements

- Indigo 2023.2 or later (Python 3; tested on Indigo 2025.2 with Python 3.13).
- A Sense account (email + password, optionally with two-factor authentication). The plugin
  talks to Sense's cloud API; there is no local connection to the monitor.
- Internet access from the Indigo Mac to pypi.org the first time the plugin starts after an
  install or update: Indigo reads the bundle's `requirements.txt` and installs the
  [sense_energy](https://github.com/scottbonline/sense) client into the bundle's
  `Contents/Packages` folder. If that download fails the plugin stops with an error in the
  Event Log; reload it once the network is back.

## Installation

1. Double-click `Sense.indigoPlugin` (or drop it into Indigo's Plugins folder) and choose
   **Install and Enable**.
2. **Plugins → Sense Home Energy → Configure**: enter your Sense login, the poll interval and the
   Indigo folder that Sense devices should be created in.
3. Within a minute the plugin creates one device per Sense-detected appliance in that folder,
   plus **Active Total**.

## Plugin configuration

| Setting | Default | Meaning |
|---|---|---|
| Username / Password | — | Your Sense account login. Stored in Indigo's plugin preferences. After the first login the plugin keeps Sense's session tokens (also in the preferences) and renews them; the password is only used again if that renewal fails or you change the settings. |
| Multi-factor code | — | Only for accounts with two-factor authentication: the current code from your authenticator app. It is used once for the login and then cleared. |
| Poll interval | 60 s | How often live power is read (30 s minimum). Today's kWh and the appliance list refresh every 5 minutes regardless. |
| API timeout | 30 s | How long one Sense request may take (5–120). |
| Solar enabled | off | Also create/track the Sense "solar" pseudo-device. |
| Log power to CSV | off | One row per poll of whole-house watts, one file per day (`activeLog-YYYY-MM-DD.csv`), 30 days kept. |
| Folder for Sense device creation | — | The **ID** of the Indigo device folder to create devices in (right-click the folder → Copy ID). |
| Enable debugging | off | Debug detail in the Indigo event log. The plugin's own file log always keeps debug detail. |

## Devices and states

Every device is of type **Sense Device** with two states:

| State | Meaning |
|---|---|
| `id` | Sense's identifier for the appliance (`core` for Active Total). |
| `power` | Live wattage. `0` when Sense does not currently see the appliance running. |

Sense-side renames are mirrored to Indigo. Appliances Sense has revoked or you have deleted in
the Sense app are disabled in Indigo, not deleted; merged appliances are deleted.

When Sense is slow or unreachable the plugin keeps the last values, logs at debug level, and
retries with a growing wait (at most 5 minutes); after three failures in a row it warns in the
Event Log. A lost session is renewed automatically. Nothing needs a scheduled plugin restart.

Live data comes from Sense's `realtime_update` endpoint on monitors running firmware 1.64 or
newer (older firmware falls back to the realtime websocket); the plugin log says which path is
in use. Daily kWh comes from the current usage-history endpoints.

## Logs

The plugin's own log (debug detail, rotated daily) is at
`/Library/Application Support/Perceptive Automation/Indigo <version>/Logs/com.howartp.sense/plugin.log`.
With "Log power to CSV" on, daily CSV files land in
`/Library/Application Support/Perceptive Automation/Indigo <version>/Preferences/Plugins/com.howartp.sense/`.
Older versions wrote a single ever-growing `activeLog.csv` there; it is left alone and can be
deleted by hand.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate.fish && pip install -r requirements-dev.txt
black . && ruff check --select E9,F . && python -m pytest
```

Tests stub the `indigo` module (see `tests/conftest.py`) so they run anywhere; nothing needs
an Indigo server. To try a change on a server, copy the `Sense.indigoPlugin` bundle over the
installed one (without `Contents/Packages`) and reload the plugin from Indigo's Plugins menu.

## Sense API

The Sense API is unofficial and undocumented; this plugin uses the community
[sense_energy](https://github.com/scottbonline/sense) library to talk to it.
