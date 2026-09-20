# Sense Home Energy — Indigo plugin

Brings your [Sense](https://sense.com) home energy monitor into [Indigo](https://www.indigodomo.com):
one Indigo device per appliance Sense has learned, plus an "Active Total" device for the whole
house. Each device's `power` state is the live wattage Sense reports, so any Indigo trigger,
control page or script can react to an appliance switching on or off.

## Requirements

- Indigo 2023.2 or later (Python 3; tested on Indigo 2025.2 with Python 3.13).
- A Sense account (email + password). The plugin talks to Sense's cloud API; there is no
  local connection to the monitor.

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
| Username / Password | — | Your Sense account login. Stored in Indigo's plugin preferences. |
| Max rate | 30 s | Seconds between polls of the Sense API. |
| Solar enabled | off | Also create/track the Sense "solar" pseudo-device. |
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

## Logs

The plugin's own log (debug detail, rotated daily) is at
`/Library/Application Support/Perceptive Automation/Indigo <version>/Logs/com.howartp.sense/plugin.log`.
It also appends one line per poll to
`/Library/Application Support/Perceptive Automation/Indigo <version>/Preferences/Plugins/com.howartp.sense/activeLog.csv`.

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
