"""Pytest configuration: stub the `indigo` module before any plugin import.

The plugin runs inside Indigo's embedded Python where `indigo` is injected by the host.
Tests stub just the surface the plugin touches (same approach as the Dometic CFX plugin).
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import types

indigo_stub = types.SimpleNamespace()


class Devices(dict):
    """`indigo.devices`: id -> Device, plus the `folders` collection the plugin validates."""

    def __init__(self):
        super().__init__()
        self.folders = Folders()

    def __iter__(self):
        return iter(self.values())

    def iter(self, _filter=""):
        return iter(list(self.values()))


class Folders(dict):
    def __contains__(self, key):
        return dict.__contains__(self, int(key))


class Device:
    _next_id = 100

    def __init__(
        self, dev_id=None, name="", deviceTypeId="sensedevice", folder=0, pluginProps=None
    ):
        if dev_id is None:
            Device._next_id += 1
            dev_id = Device._next_id
        self.id = dev_id
        self.name = name or f"device-{dev_id}"
        self.deviceTypeId = deviceTypeId
        self.folderId = folder
        self.pluginProps = pluginProps or {}
        self.address = ""
        self.enabled = True
        self.states = {}
        self.errorState = None
        self.state_updates = []
        self.image_updates = []
        self.state_list_changed = 0

    def updateStatesOnServer(self, state_list):
        self.state_updates.append(state_list)
        for item in state_list:
            self.states[item["key"]] = item["value"]

    def updateStateOnServer(self, key, value, uiValue=None, decimalPlaces=None):
        self.updateStatesOnServer([{"key": key, "value": value, "uiValue": uiValue}])

    def updateStateImageOnServer(self, image):
        self.image_updates.append(image)

    def setErrorStateOnServer(self, message):
        self.errorState = message

    def stateListOrDisplayStateIdChanged(self):
        self.state_list_changed += 1

    def replaceOnServer(self):
        for other in indigo_stub.devices.values():
            if other is not self and other.name == self.name:
                raise ValueError("NameNotUniqueError")

    def replacePluginPropsOnServer(self, props):
        self.pluginProps = props


class IndigoDict(dict):
    pass


class _DeviceApi:
    """`indigo.device.*` module-level functions."""

    @staticmethod
    def create(protocol, name, description, deviceTypeId="", folder=0, props=None):
        for other in indigo_stub.devices.values():
            if other.name == name:
                raise ValueError("NameNotUniqueError")
        dev = Device(name=name, deviceTypeId=deviceTypeId, folder=folder, pluginProps=props)
        indigo_stub.devices[dev.id] = dev
        return dev

    @staticmethod
    def delete(dev_or_id):
        dev_id = getattr(dev_or_id, "id", dev_or_id)
        indigo_stub.devices.pop(dev_id, None)

    @staticmethod
    def enable(dev_or_id, value=True):
        dev_id = getattr(dev_or_id, "id", dev_or_id)
        indigo_stub.devices[dev_id].enabled = value


class _DummyHandler(logging.Handler):
    def __init__(self, baseFilename="/tmp/Logs/plugin.log"):
        super().__init__()
        self.baseFilename = baseFilename

    def emit(self, record):
        pass


class _StopThread(Exception):
    pass


class PluginBase:
    StopThread = _StopThread

    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs):
        self.pluginId = plugin_id
        self.pluginDisplayName = plugin_display_name
        self.pluginVersion = plugin_version
        self.pluginPrefs = plugin_prefs
        self.logger = logging.getLogger("Plugin")
        self.indigo_log_handler = _DummyHandler()
        self.plugin_file_handler = _DummyHandler()
        self.debug = False

    # Legacy wrappers still present on the real PluginBase.
    def debugLog(self, msg):
        self.logger.debug(msg)

    def errorLog(self, msg):
        self.logger.error(msg)

    def sleep(self, seconds):
        raise _StopThread()

    def savePluginPrefs(self):
        pass


class _Server:
    def __init__(self):
        self.install_folder = tempfile.mkdtemp(prefix="indigo-stub-")
        # The real install folder always has Preferences/Plugins; the plugin mkdirs its own subfolder.
        os.makedirs(os.path.join(self.install_folder, "Preferences", "Plugins"))
        self.log_lines = []

    def getInstallFolderPath(self):
        return self.install_folder

    def log(self, msg, type="Plugin"):
        self.log_lines.append(msg)


indigo_stub.devices = Devices()
indigo_stub.device = _DeviceApi()
indigo_stub.Device = Device
indigo_stub.Dict = IndigoDict
indigo_stub.PluginBase = PluginBase
indigo_stub.server = _Server()
indigo_stub.kProtocol = types.SimpleNamespace(Plugin="Plugin")
indigo_stub.kStateImageSel = types.SimpleNamespace(PowerOn="PowerOn", PowerOff="PowerOff")

sys.modules["indigo"] = indigo_stub

SERVER_PLUGIN = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), os.pardir, "Sense.indigoPlugin", "Contents", "Server Plugin"
    )
)
sys.path.insert(0, SERVER_PLUGIN)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fake_indigo():
    indigo_stub.devices.clear()
    indigo_stub.devices.folders.clear()
    indigo_stub.server = _Server()
    yield indigo_stub
