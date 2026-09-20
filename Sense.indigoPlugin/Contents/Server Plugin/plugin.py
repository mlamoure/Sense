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
import os
import time
from datetime import datetime

from sense.client import DEFAULT_TIMEOUT, SenseAuthError, SenseClient, SenseTransientError

DEVICE_TYPE = "sensedevice"
CORE_ID = "core"
AUTH_PREF = "senseAuth"  # JSON blob of the tokens returned by SenseClient.connect()
RECONNECT_INTERVAL = 300  # seconds between login retries after an auth failure

# Tests replace this with a fake Senseable class; None means the real library.
SENSEABLE_FACTORY = None


class Plugin(indigo.PluginBase):
    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)
        self.version = pluginVersion
        self._apply_log_level(pluginPrefs.get("showDebugInfo", False))

        self.rateLimit = pluginPrefs.get("rateLimit", 30)
        self.doSolar = bool(pluginPrefs.get("solarEnabled", False))
        self.folderID = pluginPrefs.get("folderID", None)

        self.devIDs = list()
        self.sidFromDev = dict()
        self.devFromSid = dict()
        self.rt = dict()  # Sense id -> watts, from the last realtime snapshot
        self.dontStart = True

        self.client = None
        self._next_connect_attempt = 0.0

        install = indigo.server.getInstallFolderPath()
        self.csvPath = f"{install}/Preferences/Plugins/{self.pluginId}"
        self.csvActive = f"{self.csvPath}/activeLog.csv"
        if not os.path.exists(self.csvPath):
            os.mkdir(self.csvPath)
            with open(self.csvActive, "w+") as csv_file:
                csv_file.write("Timestamp,power\n")

    ########################################
    # Prefs / ConfigUI
    ########################################

    def _apply_log_level(self, debug_enabled):
        self.debug = bool(debug_enabled)
        self.indigo_log_handler.setLevel(logging.DEBUG if self.debug else logging.INFO)
        self.plugin_file_handler.setLevel(logging.DEBUG)

    def validatePrefsConfigUi(self, valuesDict):
        fid = int(valuesDict["folderID"])
        if fid in indigo.devices.folders:
            return True
        errorDict = indigo.Dict()
        errorDict["folderID"] = "This field should contain a folder ID"
        errorDict["showAlertText"] = (
            f"Folder not found with ID: {fid} \n\nEnsure you have used the ID, not the name of "
            "the folder.\n\nRight-click the folder you want to use and use 'Copy ID' to obtain "
            "the correct ID."
        )
        return (False, valuesDict, errorDict)

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if userCancelled:
            return
        self._apply_log_level(valuesDict.get("showDebugInfo", False))
        self.logger.info("Debug logging " + ("enabled" if self.debug else "disabled"))
        self.rateLimit = int(valuesDict.get("rateLimit", 30))
        self.doSolar = bool(valuesDict.get("solarEnabled", False))
        self.folderID = valuesDict.get("folderID", "")

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
        self._next_connect_attempt = time.time() + RECONNECT_INTERVAL
        if not username or not password:
            self.logger.error("Sense username/password not configured (Plugins > Configure).")
            return False
        kwargs = {"senseable_factory": SENSEABLE_FACTORY} if SENSEABLE_FACTORY else {}
        client = SenseClient(
            str(username),
            str(password),
            timeout=DEFAULT_TIMEOUT,
            saved_auth=self._saved_auth(),
            mfa_code=str(mfa_code or ""),
            **kwargs,
        )
        try:
            auth = client.connect()
        except SenseAuthError as err:
            self.logger.error(f"Sense login failed: {err}")
            return False
        except SenseTransientError as err:
            self.logger.warning(f"Sense login could not be completed, will retry: {err}")
            self._next_connect_attempt = time.time() + int(self.rateLimit)
            return False
        self.client = client
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
        if time.time() < self._next_connect_attempt:
            return False
        return self._connect()

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
            sID = dev.states["id"]
            if sID != "":  # state is empty on a device that was only just created
                self.devIDs.append(sID)
                self.sidFromDev[int(dev.id)] = sID
                self.devFromSid[sID] = dev.id

    def deviceStopComm(self, dev):
        if dev.deviceTypeId == DEVICE_TYPE:
            sID = dev.states["id"]
            try:
                self.devIDs.remove(sID)
            except ValueError:
                pass
            self.sidFromDev.pop(int(dev.id), None)
            self.devFromSid.pop(sID, None)

    def createCore(self):
        try:
            self.logger.debug("Creating the Active Total device")
            dev = indigo.device.create(
                indigo.kProtocol.Plugin,
                "Active Total",
                "Active Total",
                deviceTypeId=DEVICE_TYPE,
                folder=int(self.folderID),
            )
            dev.updateStatesOnServer(
                [
                    {"key": "id", "value": CORE_ID},
                    {"key": "power", "value": "0", "uiValue": "0 w"},
                ]
            )
            dev.updateStateImageOnServer(indigo.kStateImageSel.PowerOff)
            dev.stateListOrDisplayStateIdChanged()
            self.devIDs.append(CORE_ID)
            self.sidFromDev[int(dev.id)] = CORE_ID
            self.devFromSid[CORE_ID] = dev.id
        except ValueError:
            self.logger.error("Could not create the Active Total device.")

    ########################################
    # Polling
    ########################################

    def getDevices(self):
        if not self._ensure_client():
            return
        try:
            realtime = self.client.refresh_realtime()
            trends = self.client.refresh_trends()
            discovered = self.client.discovered_devices()
        except SenseTransientError as err:
            self.logger.warning(f"Sense poll skipped: {err}")
            return
        except SenseAuthError as err:
            self.logger.error(f"Sense session lost, will log in again: {err}")
            self.client = None
            self._next_connect_attempt = time.time() + int(self.rateLimit)
            return

        self.rt = {sid: int(watts) for sid, (_name, watts) in realtime.devices.items()}
        active = int(realtime.total_w)
        self.logger.debug(
            f"Active: {active} w, daily: {trends.daily_kwh} kWh (via {self.client.realtime_path})"
        )
        try:
            core = indigo.devices[self.devFromSid[CORE_ID]]
            core.updateStateOnServer(key="power", value=str(active), uiValue=f"{active} w")
            core.updateStateImageOnServer(indigo.kStateImageSel.PowerOn)
        except KeyError:
            self.logger.debug("No Active Total device found - creating it")
            self.createCore()

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        with open(self.csvActive, "a+") as csv_file:
            csv_file.write(f"{stamp},{active}\n")

        if self.doSolar:
            self.logger.debug(
                f"Active solar: {int(realtime.solar_w)} w, daily solar: {trends.daily_solar_kwh} kWh"
            )

        for d in discovered:
            sID = d.id
            if not self.doSolar and sID == "solar":
                continue
            dName = d.name

            for md in d.merged_ids:
                if md in self.devIDs:
                    self.logger.debug(
                        f"Deleting merged device: {indigo.devices[self.devFromSid[md]].name}"
                    )
                    indigo.device.delete(self.devFromSid[md])

            if d.revoked:
                if sID in self.devIDs:
                    indigo.device.enable(self.devFromSid[sID], value=False)
                continue

            if sID in self.devIDs:
                dev = indigo.devices[self.devFromSid[sID]]
                devOldName = dev.name
                if dev.name != dName:
                    dev.name = dName
                    try:
                        dev.replaceOnServer()
                    except ValueError as e:
                        if str(e) == "NameNotUniqueError":
                            self.logger.debug(
                                f"Cannot rename {devOldName} to {dName}: another device already has that name"
                            )
                        else:
                            self.logger.error(e)
                if sID in self.rt:  # currently running per Sense
                    dev.updateStateOnServer(
                        key="power", value=str(self.rt[sID]), uiValue=f"{self.rt[sID]} w"
                    )
                    dev.updateStateImageOnServer(indigo.kStateImageSel.PowerOn)
                else:
                    dev.updateStateOnServer(key="power", value="0", uiValue="0 w")
                    dev.updateStateImageOnServer(indigo.kStateImageSel.PowerOff)
            else:
                self.logger.debug(f"Creating: {dName} ({sID})")
                try:
                    dev = indigo.device.create(
                        indigo.kProtocol.Plugin,
                        dName,
                        dName,
                        deviceTypeId=DEVICE_TYPE,
                        folder=int(self.folderID),
                    )
                    dev.updateStatesOnServer(
                        [
                            {"key": "id", "value": str(sID)},
                            {"key": "power", "value": "0", "uiValue": "0 w"},
                        ]
                    )
                    dev.updateStateImageOnServer(indigo.kStateImageSel.PowerOff)
                    dev.stateListOrDisplayStateIdChanged()
                    self.devIDs.append(sID)
                    self.sidFromDev[int(dev.id)] = sID
                    self.devFromSid[sID] = dev.id
                except ValueError as e:
                    if str(e) == "NameNotUniqueError":
                        self.logger.debug(
                            f"Cannot create {dName}: another device already has that name"
                        )
                    else:
                        self.logger.error(e)
        self.rt = dict()

    def runConcurrentThread(self):
        try:
            while True:
                if self.dontStart:
                    self.sleep(10)  # let device comms start before the first poll
                    self.dontStart = False
                try:
                    self.getDevices()
                except Exception:  # noqa: BLE001 - the poll loop must survive anything
                    self.logger.exception("Unexpected error while polling Sense")
                self.sleep(int(self.rateLimit))
        except self.StopThread:
            pass
