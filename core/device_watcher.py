"""Monitor Windows audio device changes via IMMNotificationClient (COM).

Emits a Qt signal whenever devices are added, removed, or the default changes.
"""

from __future__ import annotations

import comtypes
from _ctypes import COMError
from comtypes import COMMETHOD, GUID, HRESULT, COMObject
from ctypes import POINTER, Structure, c_int, c_uint, c_ushort, c_wchar_p
from ctypes.wintypes import DWORD, LPCWSTR

from PyQt6.QtCore import QObject, pyqtSignal

from core.log import logger

_TAG = "[DeviceWatcher]"

# GetDefaultAudioEndpoint returns this when no device is assigned (normal during replug / empty set).
_HRESULT_NOTFOUND = -2147023728  # 0x80070490 E_NOTFOUND

# ── Struct / COM interface definitions ─────────────────────────────────


class PROPERTYKEY(Structure):
    _fields_ = [("fmtid", GUID), ("pid", DWORD)]


class IMMNotificationClient(comtypes.IUnknown):
    _iid_ = GUID("{7991EEC9-7E89-4D85-8390-6C703CEC60C0}")
    _methods_ = [
        COMMETHOD([], HRESULT, "OnDeviceStateChanged",
                  (["in"], LPCWSTR, "pwstrDeviceId"),
                  (["in"], DWORD, "dwNewState")),
        COMMETHOD([], HRESULT, "OnDeviceAdded",
                  (["in"], LPCWSTR, "pwstrDeviceId")),
        COMMETHOD([], HRESULT, "OnDeviceRemoved",
                  (["in"], LPCWSTR, "pwstrDeviceId")),
        COMMETHOD([], HRESULT, "OnDefaultDeviceChanged",
                  (["in"], c_int, "flow"),
                  (["in"], c_int, "role"),
                  (["in"], LPCWSTR, "pwstrDefaultDeviceId")),
        COMMETHOD([], HRESULT, "OnPropertyValueChanged",
                  (["in"], LPCWSTR, "pwstrDeviceId"),
                  (["in"], PROPERTYKEY, "key")),
    ]


_CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")

_STGM_READ = 0x00000000
_DEVICE_STATE_ACTIVE = 0x00000001
_eCapture = 1  # EDataFlow.eCapture

# ── COM interfaces for property store ──────────────────────────────────

class _PROPVARIANT(Structure):
    _fields_ = [
        ("vt", c_ushort),
        ("wReserved1", c_ushort),
        ("wReserved2", c_ushort),
        ("wReserved3", c_ushort),
        ("pwszVal", c_wchar_p),
        ("_pad", c_uint),
    ]


_PKEY_Device_FriendlyName = PROPERTYKEY()
_PKEY_Device_FriendlyName.fmtid = GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}")
_PKEY_Device_FriendlyName.pid = 14


class IPropertyStore(comtypes.IUnknown):
    _iid_ = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount",
                  (["out"], POINTER(DWORD), "cProps")),
        COMMETHOD([], HRESULT, "GetAt",
                  (["in"], DWORD, "iProp"),
                  (["out"], POINTER(PROPERTYKEY), "pkey")),
        COMMETHOD([], HRESULT, "GetValue",
                  (["in"], POINTER(PROPERTYKEY), "key"),
                  (["out"], POINTER(_PROPVARIANT), "pv")),
    ]


class IMMDevice(comtypes.IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD([], HRESULT, "Activate",
                  (["in"], POINTER(GUID), "iid"),
                  (["in"], DWORD, "dwClsCtx"),
                  (["in"], POINTER(_PROPVARIANT), "pActivationParams"),
                  (["out"], POINTER(POINTER(comtypes.IUnknown)), "ppInterface")),
        COMMETHOD([], HRESULT, "OpenPropertyStore",
                  (["in"], DWORD, "stgmAccess"),
                  (["out"], POINTER(POINTER(IPropertyStore)), "ppProperties")),
        COMMETHOD([], HRESULT, "GetId",
                  (["out"], POINTER(LPCWSTR), "ppstrId")),
    ]


class IMMDeviceCollection(comtypes.IUnknown):
    _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
    _methods_ = [
        COMMETHOD([], HRESULT, "GetCount",
                  (["out"], POINTER(c_uint), "pcDevices")),
        COMMETHOD([], HRESULT, "Item",
                  (["in"], c_uint, "nDevice"),
                  (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
    ]


class IMMDeviceEnumerator(comtypes.IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        COMMETHOD([], HRESULT, "EnumAudioEndpoints",
                  (["in"], DWORD, "dataFlow"),
                  (["in"], DWORD, "dwStateMask"),
                  (["out"], POINTER(POINTER(IMMDeviceCollection)), "ppDevices")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
                  (["in"], DWORD, "dataFlow"),
                  (["in"], DWORD, "role"),
                  (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
        COMMETHOD([], HRESULT, "GetDevice",
                  (["in"], LPCWSTR, "pwstrId"),
                  (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
        COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback",
                  (["in"], POINTER(IMMNotificationClient), "pClient")),
        COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback",
                  (["in"], POINTER(IMMNotificationClient), "pClient")),
    ]


def get_full_device_names() -> dict[str, str]:
    """Get full (non-truncated) friendly names for all active capture devices.

    Returns a dict mapping the first 31 chars (PyAudio truncation) to the
    full name.  Runs in ~5ms via COM, no subprocess needed.
    """
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass
    try:
        enum = comtypes.CoCreateInstance(_CLSID_MMDeviceEnumerator, IMMDeviceEnumerator)
        collection = enum.EnumAudioEndpoints(_eCapture, _DEVICE_STATE_ACTIVE)
        count = collection.GetCount()
        result: dict[str, str] = {}
        for i in range(count):
            device = collection.Item(i)
            store = device.OpenPropertyStore(_STGM_READ)
            pv = store.GetValue(_PKEY_Device_FriendlyName)
            if pv.pwszVal:
                full = pv.pwszVal
                trunc = full[:31]
                result[trunc] = full
        return result
    except Exception:
        logger.opt(exception=True).warning(f"{_TAG} Failed to get full device names")
        return {}


def get_default_capture_device_name() -> str | None:
    """Get the active default capture device friendly name via Windows COM."""
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass
    try:
        enum = comtypes.CoCreateInstance(_CLSID_MMDeviceEnumerator, IMMDeviceEnumerator)
        device = enum.GetDefaultAudioEndpoint(_eCapture, 0)
        store = device.OpenPropertyStore(_STGM_READ)
        pv = store.GetValue(_PKEY_Device_FriendlyName)
        return pv.pwszVal if pv.pwszVal else None
    except COMError as e:
        hr = e.args[0] if e.args else 0
        if hr == _HRESULT_NOTFOUND or (hr & 0xFFFFFFFF) == 0x80070490:
            logger.debug(f"{_TAG} No default capture endpoint (transient or none configured)")
            return None
        logger.opt(exception=True).warning(f"{_TAG} COM error getting default capture device")
        return None
    except Exception:
        logger.opt(exception=True).warning(f"{_TAG} Failed to get default capture device name")
        return None


# ── Qt signal carrier ─────────────────────────────────────────────────

class DeviceChangeSignal(QObject):
    """Thin QObject wrapper that carries a signal for device changes."""
    changed = pyqtSignal()


# ── Callback implementation ───────────────────────────────────────────

class _NotificationClient(COMObject):
    """Receives device-change callbacks from Windows and fires a Qt signal."""

    _com_interfaces_ = [IMMNotificationClient]

    def __init__(self, emitter: DeviceChangeSignal):
        super().__init__()
        self._emitter = emitter

    def OnDeviceStateChanged(self, pwstrDeviceId, dwNewState):
        logger.info(f"{_TAG} OnDeviceStateChanged state={dwNewState}")
        self._emitter.changed.emit()
        return 0

    def OnDeviceAdded(self, pwstrDeviceId):
        logger.info(f"{_TAG} OnDeviceAdded")
        self._emitter.changed.emit()
        return 0

    def OnDeviceRemoved(self, pwstrDeviceId):
        logger.info(f"{_TAG} OnDeviceRemoved")
        self._emitter.changed.emit()
        return 0

    def OnDefaultDeviceChanged(self, flow, role, pwstrDefaultDeviceId):
        logger.info(f"{_TAG} OnDefaultDeviceChanged flow={flow} role={role}")
        self._emitter.changed.emit()
        return 0

    def OnPropertyValueChanged(self, pwstrDeviceId, key):
        return 0


# ── Public API ─────────────────────────────────────────────────────────

class AudioDeviceWatcher:
    """Register / unregister Windows audio device change notifications.

    Usage::

        watcher = AudioDeviceWatcher()
        watcher.signals.changed.connect(my_refresh_slot)
        watcher.start()   # begins listening
        ...
        watcher.stop()     # cleanup
    """

    def __init__(self):
        self.signals = DeviceChangeSignal()
        self._enumerator = None
        self._client = None

    def start(self):
        if self._enumerator is not None:
            return
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        except OSError:
            pass  # already initialized in this thread
        try:
            self._enumerator = comtypes.CoCreateInstance(
                _CLSID_MMDeviceEnumerator,
                IMMDeviceEnumerator,
            )
            self._client = _NotificationClient(self.signals)
            self._enumerator.RegisterEndpointNotificationCallback(self._client)
            logger.info(f"{_TAG} Listening for audio device changes")
        except Exception:
            logger.opt(exception=True).error(
                f"{_TAG} Failed to register device notifications")
            self._enumerator = None
            self._client = None

    def stop(self):
        if self._enumerator is None:
            return
        try:
            self._enumerator.UnregisterEndpointNotificationCallback(self._client)
        except Exception:
            pass
        self._enumerator = None
        self._client = None
        logger.info(f"{_TAG} Stopped listening for audio device changes")
