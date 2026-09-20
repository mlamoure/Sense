"""Sense Home Energy Indigo plugin: thin adapter between Indigo and the sense_energy client.

API access lives in ``sense/`` (Indigo-free). This module owns device lifecycle, the prefs
ConfigUI, the poll loop and pushing state to the Indigo server.
"""

try:
    import indigo
except ImportError:  # unit tests inject a stub
    pass

import json
import logging

from sense import registry
from sense.csvlog import CsvLog
from sense.client import (
    DEFAULT_TIMEOUT,
    SenseAuthError,
    SenseClient,
    SenseTransientError,
    TrendSnapshot,
)
from sense.poller import (
    LOGIN,
    LOGIN_RETRY_AUTH,
    LOGIN_RETRY_TRANSIENT,
    MIN_REALTIME_INTERVAL,
    REALTIME,
    REALTIME_INTERVAL,
    TRENDS,
    Poller,
)
from sense.registry import CORE_ID, IndigoView

DEVICE_TYPE = "sensedevice"
AUTH_PREF = "senseAuth"  # JSON blob of the tokens returned by SenseClient.connect()
MIN_TIMEOUT, MAX_TIMEOUT = 5, 120

# Tests replace this with a fake Senseable class; None means the real library.
SENSEABLE_FACTORY = None


class Plugin(indigo.PluginBase):
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.version = pluginVersion
        self._apply_log_level(pluginPrefs.get("showDebugInfo", False))

        self.rateLimit = self._int_pref(pluginPrefs, "rateLimit", REALTIME_INTERVAL)
        self.apiTimeout = self._int_pref(pluginPrefs, "apiTimeout", DEFAULT_TIMEOUT)
        self.doSolar = bool(pluginPrefs.get("solarEnabled", False))
        self.folderID = pluginPrefs.get("folderID", None)
        self.poller = Poller(realtime_interval=self.rateLimit)
        self._discovered = []  # last device list from Sense (refreshed on the trend cadence)
        self._trends = TrendSnapshot(0.0, 0.0)

        self._rename_warned = set()
        self._duplicate_warned = set()
        self.dontStart = True

        self.client = None

        install = indigo.server.getInstallFolderPath()
        self.csvPath = f"{install}/Preferences/Plugins/{self.pluginId}"
        self.csvEnabled = bool(pluginPrefs.get("csvEnabled", False))
        self._csv = CsvLog(self.csvPath, keep_days=30)

    ########################################
    # Prefs / ConfigUI
    ########################################

    @staticmethod
    def _int_pref(prefs, key, default):
        try:
            return int(prefs.get(key, default))
        except (TypeError, ValueError):
            return default

    def _apply_log_level(self, debug_enabled):
        self.debug = bool(debug_enabled)
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        self.plugin_file_handler.setLevel(logging.DEBUG)

    def validatePrefsConfigUi(self, valuesDict):
        errorDict = indigo.Dict()
        try:
            fid = int(valuesDict["folderID"])
        except (KeyError, TypeError, ValueError):
            fid = -1
        if fid not in indigo.devices.folders:
            errorDict["folderID"] = "This field should contain a folder ID"
            errorDict["showAlertText"] = (
                f"Folder not found with ID: {fid} \n\nEnsure you have used the ID, not the name "
                "of the folder.\n\nRight-click the folder you want to use and use 'Copy ID' to "
                "obtain the correct ID."
            )
        rate = self._int_pref(valuesDict, "rateLimit", -1)
        if rate < MIN_REALTIME_INTERVAL:
            errorDict["rateLimit"] = f"Poll every {MIN_REALTIME_INTERVAL} seconds or more"
        timeout = self._int_pref(valuesDict, "apiTimeout", -1)
        if not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
            errorDict["apiTimeout"] = f"Timeout must be {MIN_TIMEOUT}-{MAX_TIMEOUT} seconds"
        if errorDict:
            return (False, valuesDict, errorDict)
        return True

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if userCancelled:
            return
        self._apply_log_level(valuesDict.get("showDebugInfo", False))
        self.logger.info("Debug logging " + ("enabled" if self.debug else "disabled"))
        self.rateLimit = self._int_pref(valuesDict, "rateLimit", REALTIME_INTERVAL)
        self.apiTimeout = self._int_pref(valuesDict, "apiTimeout", DEFAULT_TIMEOUT)
        self.doSolar = bool(valuesDict.get("solarEnabled", False))
        self.csvEnabled = bool(valuesDict.get("csvEnabled", False))
        self.folderID = valuesDict.get("folderID", "")
        self.poller = Poller(realtime_interval=self.rateLimit)

        # Credentials may have changed: forget the saved session and log in afresh.
        self.pluginPrefs.pop(AUTH_PREF, None)
        self._connect(
            str(valuesDict.get("username", "")),
            str(valuesDict.get("password", "")),
            str(valuesDict.get("mfaCode", "")),
        )
        self.createCore()
        if not self.dontStart:
            self.getDevices()

    ########################################
    # Sense session
    ########################################

    def _saved_auth(self):
        raw = self.pluginPrefs.get(AUTH_PREF, "")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            self.logger.debug("Ignoring unreadable saved Sense session")
            return None

    def _connect(self, username=None, password=None, mfa_code=None):
        """Build the client and log in. Returns True when polling can proceed."""
        username = self.pluginPrefs.get("username", "") if username is None else username
        password = self.pluginPrefs.get("password", "") if password is None else password
        mfa_code = self.pluginPrefs.get("mfaCode", "") if mfa_code is None else mfa_code
        self.client = None
        if not username or not password:
            self.logger.error("Sense username/password not configured (Plugins > Configure).")
            self.poller.failed(LOGIN, wait=LOGIN_RETRY_AUTH)
            return False
        kwargs = {"senseable_factory": SENSEABLE_FACTORY} if SENSEABLE_FACTORY else {}
        client = SenseClient(
            str(username),
            str(password),
            timeout=self.apiTimeout,
            saved_auth=self._saved_auth(),
            mfa_code=str(mfa_code or ""),
            **kwargs,
        )
        try:
            auth = client.connect()
        except SenseAuthError as err:
            wait = self.poller.failed(LOGIN, wait=LOGIN_RETRY_AUTH)
            self.logger.error(f"Sense login failed: {err} (next attempt in {int(wait)} s)")
            return False
        except SenseTransientError as err:
            wait = self.poller.failed(LOGIN, wait=LOGIN_RETRY_TRANSIENT)
            self.logger.warning(
                f"Sense login could not be completed: {err} (retry in {int(wait)} s)"
            )
            return False
        self.client = client
        self.poller.succeeded(LOGIN)
        self.poller.reset(REALTIME)
        self.poller.reset(TRENDS)
        self.pluginPrefs[AUTH_PREF] = json.dumps(auth)
        # A multi-factor code is single-use; never keep it around.
        if self.pluginPrefs.get("mfaCode"):
            self.pluginPrefs["mfaCode"] = ""
        self.savePluginPrefs()
        self.logger.info("Connected to Sense (monitor %s)" % auth.get("monitor_id"))
        return True

    def _ensure_client(self):
        if self.client is not None:
            return True
        if not self.poller.due(LOGIN):
            return False
        return self._connect()

    def _drop_session(self, err):
        self.logger.error(f"Sense session lost, will log in again: {err}")
        self.client = None
        self.poller.failed(LOGIN, wait=LOGIN_RETRY_TRANSIENT)

    ########################################
    # Lifecycle
    ########################################

    def startup(self):
        self.logger.debug(f"startup (plugin {self.version})")
        self._connect()

    def shutdown(self):
        self.logger.debug("shutdown")

    def deviceStartComm(self, dev):
        dev.stateListOrDisplayStateIdChanged()
        if dev.deviceTypeId == DEVICE_TYPE:
            self._ensure_identity(dev)

    def _ensure_identity(self, dev):
        """Make sure the Sense id is in the address, the senseId prop and the `id` state.

        Older versions kept it only in the `id` state; a device whose creation was interrupted
        may have it only in the props. `address` is a base-class prop a plugin can change only
        through its pluginProps.
        """
        sense_id = self._sense_id_of(dev)
        if not sense_id:
            return
        props = dev.pluginProps
        if not dev.address or props.get("senseId") != sense_id:
            props["senseId"] = sense_id
            props["address"] = sense_id
            dev.replacePluginPropsOnServer(props)
            self.logger.debug(f"Stored Sense id {sense_id} on {dev.name}")
        if str(dev.states.get("id", "") or "") != sense_id:
            dev.updateStateOnServer(key="id", value=sense_id)

    def deviceStopComm(self, dev):
        pass

    @staticmethod
    def _sense_id_of(dev):
        return str(
            dev.address or dev.pluginProps.get("senseId", "") or dev.states.get("id", "") or ""
        )

    def _existing(self):
        """Every plugin device, enabled or not, keyed by Sense id. The core device included.

        Two devices with the same Sense id (a create interrupted half-way, then retried) are
        reported once: the enabled one with the lowest id wins and the others are named in a
        warning so they can be deleted by hand; the plugin never deletes user-visible devices.
        """
        views = {}
        duplicates = {}
        for dev in sorted(indigo.devices.iter("self"), key=lambda d: (not d.enabled, d.id)):
            if dev.deviceTypeId != DEVICE_TYPE:
                continue
            sense_id = self._sense_id_of(dev)
            if not sense_id:
                continue
            if not dev.address:
                self._ensure_identity(dev)
            if sense_id in views:
                duplicates.setdefault(sense_id, []).append(dev.name)
                continue
            try:
                power = float(dev.states.get("power", 0) or 0)
            except (TypeError, ValueError):
                power = 0.0
            is_on = dev.states.get("isOn")
            views[sense_id] = IndigoView(
                dev.id,
                sense_id,
                dev.name,
                bool(dev.enabled),
                power,
                None if is_on is None else bool(is_on),
            )
        for sense_id, names in duplicates.items():
            if sense_id not in self._duplicate_warned:
                self._duplicate_warned.add(sense_id)
                self.logger.warning(
                    f"More than one Indigo device has Sense id {sense_id}; using "
                    f"{views[sense_id].name!r} and ignoring {names} - delete the extras"
                )
        return views

    def _folder(self):
        try:
            fid = int(self.folderID)
        except (TypeError, ValueError):
            fid = 0
        if fid and fid not in indigo.devices.folders:
            self.logger.warning(
                f"Device folder {fid} no longer exists; creating Sense devices at the top level"
            )
            fid = 0
        return fid

    def _create_device(self, sense_id, name):
        """Create an Indigo device for a Sense id; fall back to a unique name on a clash."""
        for candidate in (name, f"{name} ({sense_id})"):
            try:
                dev = indigo.device.create(
                    indigo.kProtocol.Plugin,
                    candidate,
                    candidate,
                    deviceTypeId=DEVICE_TYPE,
                    folder=self._folder(),
                    props={"senseId": sense_id, "address": sense_id},
                )
                break
            except ValueError as err:
                if str(err) != "NameNotUniqueError":
                    self.logger.error(f"Could not create device {name}: {err}")
                    return None
                self.logger.warning(
                    f"Another Indigo device is already called {candidate!r}; "
                    f"naming the new Sense device {name!r} ({sense_id}) instead"
                )
        else:
            return None
        dev.updateStatesOnServer(
            [
                {"key": "id", "value": sense_id},
                {"key": "power", "value": 0, "uiValue": "0 w"},
                {"key": "isOn", "value": False},
            ]
        )
        dev.updateStateImageOnServer(indigo.kStateImageSel.PowerOff)
        dev.stateListOrDisplayStateIdChanged()
        self.logger.info(f"Created Sense device {dev.name!r} ({sense_id})")
        return dev

    def _rename(self, dev, name):
        old = dev.name
        dev.name = name
        try:
            dev.replaceOnServer()
        except ValueError as err:
            dev.name = old
            if str(err) == "NameNotUniqueError":
                if dev.id not in self._rename_warned:
                    self._rename_warned.add(dev.id)
                    self.logger.warning(
                        f"Cannot rename {old!r} to {name!r}: another device already has that name"
                    )
            else:
                self.logger.error(f"Cannot rename {old!r}: {err}")
            return False
        self._rename_warned.discard(dev.id)
        return True

    def _push_power(self, dev, watts):
        watts = int(round(watts))
        dev.updateStatesOnServer(
            [
                {"key": "power", "value": watts, "uiValue": f"{watts} w"},
                {"key": "isOn", "value": watts > 0},
            ]
        )
        dev.updateStateImageOnServer(
            indigo.kStateImageSel.PowerOn if watts > 0 else indigo.kStateImageSel.PowerOff
        )

    def createCore(self):
        if CORE_ID in self._existing():
            return
        self._create_device(CORE_ID, "Active Total")

    def _update_core(self, realtime, trends):
        existing = self._existing()
        if CORE_ID not in existing:
            self.logger.debug("No Active Total device found - creating it")
            self.createCore()
            existing = self._existing()
            if CORE_ID not in existing:
                return
        dev = indigo.devices[existing[CORE_ID].dev_id]
        watts = int(round(realtime.total_w))
        wanted = {
            "power": watts,
            "isOn": watts > 0,
            "dailyKwh": round(trends.daily_kwh, 3),
            "solarW": int(round(realtime.solar_w)),
            "voltage": "/".join(f"{v:.1f}" for v in realtime.voltage),
            "hz": round(realtime.hz, 2),
        }
        ui = {
            "power": f"{watts} w",
            "dailyKwh": f"{trends.daily_kwh:.2f} kWh",
            "solarW": f"{wanted['solarW']} w",
            "hz": f"{wanted['hz']} Hz",
        }
        changed = [
            {"key": k, "value": v, **({"uiValue": ui[k]} if k in ui else {})}
            for k, v in wanted.items()
            if dev.states.get(k) != v
        ]
        if changed:
            dev.updateStatesOnServer(changed)
        dev.updateStateImageOnServer(indigo.kStateImageSel.PowerOn)

    ########################################
    # Polling
    ########################################

    def getDevices(self):
        """One pass of the poll loop: fetch whatever is due and push it to Indigo."""
        if not self._ensure_client():
            return
        if self.poller.due(TRENDS):
            self._poll_trends()
        if self.client is not None and self.poller.due(REALTIME):
            self._poll_realtime()

    def _poll_trends(self):
        try:
            self._trends = self.client.refresh_trends()
            self._discovered = self.client.discovered_devices()
        except SenseTransientError as err:
            wait = self.poller.failed(TRENDS)
            self.logger.warning(
                f"Sense trend/device list fetch failed: {err} (retry in {int(wait)} s)"
            )
            return
        except SenseAuthError as err:
            self._drop_session(err)
            return
        self.poller.succeeded(TRENDS)
        self.logger.debug(
            f"Trends: {self._trends.daily_kwh} kWh today; {len(self._discovered)} Sense devices"
        )

    def _poll_realtime(self):
        try:
            realtime = self.client.refresh_realtime()
        except SenseTransientError as err:
            wait = self.poller.failed(REALTIME)
            level = self.logger.warning if self.poller.failures(REALTIME) > 2 else self.logger.debug
            level(f"Sense realtime fetch failed: {err} (retry in {int(wait)} s)")
            return
        except SenseAuthError as err:
            self._drop_session(err)
            return
        self.poller.succeeded(REALTIME)

        active = int(round(realtime.total_w))
        self.logger.debug(f"Active: {active} w (via {self.client.realtime_path})")
        self._update_core(realtime, self._trends)

        if self.csvEnabled:
            try:
                self._csv.append(active)
            except OSError as err:
                self.logger.warning(f"Could not write the power CSV log: {err}")

        self._apply(
            registry.plan(
                self._discovered, realtime.devices, self._existing(), include_solar=self.doSolar
            )
        )

    def _apply(self, actions):
        for action in actions:
            if isinstance(action, registry.Create):
                self._create_device(action.sense_id, action.name)
                continue
            try:
                dev = indigo.devices[action.dev_id]
            except KeyError:
                continue
            if isinstance(action, registry.UpdatePower):
                self._push_power(dev, action.watts)
            elif isinstance(action, registry.Rename):
                if self._rename(dev, action.name):
                    self.logger.info(f"Renamed Sense device to {action.name!r} (Sense renamed it)")
            elif isinstance(action, registry.Retire):
                self.logger.info(
                    f"Sense no longer reports {dev.name!r} ({self._sense_id_of(dev)}); "
                    "disabling it and freeing its name"
                )
                self._rename(dev, action.name)
                indigo.device.enable(dev.id, value=False)
            elif isinstance(action, registry.Revive):
                self.logger.info(f"Sense reports {action.name!r} again; re-enabling its device")
                self._rename(dev, action.name)
                indigo.device.enable(dev.id, value=True)
            elif isinstance(action, registry.Delete):
                self.logger.info(f"Deleting {dev.name!r}: Sense merged it into another appliance")
                indigo.device.delete(dev.id)

    def runConcurrentThread(self):
        try:
            self.sleep(5)  # let device comms start before the first poll
            self.dontStart = False
            while True:
                try:
                    self.getDevices()
                except Exception:  # noqa: BLE001 - the poll loop must survive anything
                    self.logger.exception("Unexpected error while polling Sense")
                self.sleep(1)
        except self.StopThread:
            pass
