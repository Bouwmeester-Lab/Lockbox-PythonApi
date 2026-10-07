from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import ExitStack
from typing import Self

import httpx

from ._hub import Hub
from .configuration import LockboxConfiguration, SlopePreference

Callback = Callable[[], None]


class Lockbox:
    def __init__(self, id: int, name: str, ip: str, mac: str, server: Server = None):
        self.id = id
        self.name = name
        self.ip = ip
        self.mac = mac
        self.server = server
        self._callbacks: dict[str, Callback] = {}

    def _server(self) -> Server:
        if self.server is None:
            raise RuntimeError("Lockbox must be registered with a Server")
        return self.server

    def _command(self, route: str, payload=None, *, method: str = "POST") -> None:
        self._server()._request(
            method, f"api/Run/{route}", params={"teensyId": self.id}, json=payload
        )

    ### configurations
    def get_configuration(self) -> LockboxConfiguration:
        """Fetch a fresh snapshot of saved settings, not live device settings."""
        server = self._server()
        response = server._request(
            "GET", f"api/LockboxConfigurations/arduino/{self.id}"
        )
        return LockboxConfiguration._from_payload(response.json(), server.url)

    def save_configuration(self, config: LockboxConfiguration) -> None:
        """Persist supported settings without applying them. Writes are not atomic."""
        server = self._server()
        for method, path, payload in config._save_requests(self.id, server.url):
            server._request(method, path, json=payload)

    def set_gain(self, gain):
        self._command("amplification", {"gain": gain})

    #### demodulation/modulation configurations
    def set_demodulation_cutoff(self, frequency):
        """Set cutoff frequency in Hz."""
        self._command("demodulation/frequency", {"filterFrequencyHz": frequency})

    def set_demodulation_phase(self, phase):
        """Set phase in radians."""
        self._command("demodulation/phase", {"phaseRadians": phase})

    def set_modulation_amplitude(self, amplitude):
        """Set modulation amplitude in DAC codes (0 through 524287)."""
        self._command("modulation", {"amplitude": amplitude})

    def set_demodulation_amplitude(self, amplitude):
        self._command("demodulation/amplitude", {"amplitude": amplitude})

    #### PID + Setpoint + Error configurations
    def set_setpoint(self, setpoint):
        self._server()._invoke("UpdateSetpoint", [self.id, setpoint])

    def set_pid(self, p, i, d):
        self._command("pid", {"p": p, "i": i, "d": d})

    def set_threshold(self, threshold):
        self._server()._invoke(
            "UpdateThresholds",
            [self.id, {"signalInputType": 0, "lockingThreshold": threshold}],
        )

    def set_low_high_threshold(
        self, low, high, slope_preference: SlopePreference = SlopePreference.NONE
    ):
        self._server()._invoke(
            "UpdateThresholds",
            [
                self.id,
                {
                    "signalInputType": 1,
                    "lowThreshold": low,
                    "highThreshold": high,
                    "slopePreference": int(SlopePreference(slope_preference)),
                },
            ],
        )

    ### actions
    def reload_configuration(self):
        """Reapply the server's saved configuration to the device."""
        self._command("configuration", method="GET")

    def lock(self):
        self._command("lock", {"lockRequestType": "Normal"})

    def unlock(self):
        self._command("unlock", {"scanAfter": False, "lockRequestType": "Normal"})

    def freeze(self):
        """Toggle freeze; enabling before ready is rejected by the server."""
        self._command("freeze", method="GET")

    def unfreeze(self):
        """Toggle freeze using the same endpoint as freeze()."""
        self.freeze()

    ### dac controls
    def set_fine_output_code(self, voltage_code):
        self._command("voltage", {"dac": "FineDac", "voltage": voltage_code})

    def set_coarse_output_code(self, voltage_code):
        self._command("voltage", {"dac": "CoarseDac", "voltage": voltage_code})

    ### Callbacks
    def on_coarse_resonance_found(self, callback: Callback):
        self._callbacks["CoarseResonanceFound"] = callback

    def on_fine_resonance_found(self, callback: Callback):
        self._callbacks["FineResonanceFound"] = callback

    def on_lock_acquired(self, callback: Callback):
        raise NotImplementedError("No dedicated lock-acquired status exists")

    def on_lock_lost(self, callback: Callback):
        self._callbacks["Unlocked"] = callback

    def on_lockbox_frozen(self, callback: Callback):
        self._callbacks["FrozenEnabled"] = callback

    def on_lockbox_unfrozen(self, callback: Callback):
        self._callbacks["FrozenDisabled"] = callback

    def on_lockbox_recentering(self, callback: Callback):
        raise NotImplementedError("No recentering status exists")

    def on_lockbox_freeze_ready(self, callback: Callback):
        self._callbacks["DriftEstimateReady"] = callback


class Server:
    """Own connections for a with block; url is the server root, not /api."""

    def __init__(self, url: str, *, timeout: float = 10.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._lockboxes: dict[int, Lockbox] = {}
        self._resources: ExitStack | None = None
        self._http: httpx.Client | None = None
        self._status_hub: Hub | None = None
        self._run_hub: Hub | None = None

    def __enter__(self) -> Self:
        if self._resources is not None:
            raise RuntimeError("Server is already open")
        self._resources = ExitStack()
        try:
            self._http = self._resources.enter_context(
                httpx.Client(base_url=self.url + "/", timeout=self.timeout)
            )
            self._status_hub = Hub(self.url + "/Hubs/Status", self.timeout)
            self._resources.callback(self._status_hub.close)
            self._status_hub.on("ReceiveStatus", self._receive_status)
            self._status_hub.start()
            self._run_hub = Hub(self.url + "/Hubs/Run", self.timeout)
            self._resources.callback(self._run_hub.close)
            self._run_hub.start()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        resources, self._resources = self._resources, None
        try:
            if resources is not None:
                resources.__exit__(exc_type, exc_value, traceback)
        finally:
            self._http = self._status_hub = self._run_hub = None

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self._http is None:
            raise RuntimeError("Use Server inside a with block")
        response = self._http.request(method, path, **kwargs)
        if not response.is_success:
            raise Exception(response.text)  # noqa: TRY002 -- V1 exposes the response body.
        return response

    def _invoke(self, method: str, arguments: list) -> None:
        if self._run_hub is None:
            raise RuntimeError("Use Server inside a with block")
        self._run_hub.invoke(method, arguments)

    def _lockbox_from_payload(self, payload: dict) -> Lockbox:
        id = int(payload["id"])
        box = self._lockboxes.get(id)
        if box is None:
            box = Lockbox(id, "", "", "", self)
            self.connect_to_status_hub(box)
        box.name = payload.get("name") or ""
        box.ip = payload.get("ipAddress") or ""
        box.mac = payload.get("macAddress") or ""
        return box

    def list_lockboxes(self) -> list[Lockbox]:
        return [
            self._lockbox_from_payload(p)
            for p in self._request("GET", "api/Arduinos").json()
        ]

    def get_lockbox(self, id: int) -> Lockbox:
        return self._lockbox_from_payload(
            self._request("GET", f"api/Arduinos/{id}").json()
        )

    def connect_to_status_hub(self, lockbox: Lockbox) -> None:
        """Register an object for routing on the shared status connection."""
        if lockbox.server is not None and lockbox.server is not self:
            raise ValueError("Lockbox belongs to another Server")
        existing = self._lockboxes.get(lockbox.id)
        if existing is not None and existing is not lockbox:
            raise ValueError("A different Lockbox with this ID is already registered")
        lockbox.server = self
        self._lockboxes[lockbox.id] = lockbox

    def _receive_status(self, arguments: list) -> None:
        payload = arguments[0]
        box = self._lockboxes.get(payload.get("arduinoId", payload.get("ArduinoId")))
        if box is None:
            return
        status = payload.get("status", payload.get("Status"))
        # JSON normally carries enum names; accept numeric enum values too.
        if isinstance(status, int):
            status = {
                0: "CoarseResonanceFound",
                1: "FineResonanceFound",
                5: "Unlocked",
                6: "FrozenDisabled",
                7: "FrozenEnabled",
                8: "DriftEstimateReady",
            }.get(status)
        callback = box._callbacks.get(status)
        if callback is not None:
            callback()
