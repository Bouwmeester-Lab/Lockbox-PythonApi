"""Editable saved settings with immutable identities and filter information."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from enum import IntEnum
from typing import ClassVar


class SlopePreference(IntEnum):
    NONE = 0
    NEGATIVE = 1
    POSITIVE = 2


class PidControllerType(IntEnum):
    SIMPLE_PI = 0
    FILTERED_PID = 1


class _Settings:
    _editable: ClassVar[frozenset[str]] = frozenset()

    def __setattr__(self, name, value):
        if name not in {f.name for f in fields(self)}:
            raise AttributeError(f"Unknown configuration field: {name}")
        if name in self.__dict__ and name not in self._editable:
            raise AttributeError(f"{name} is read-only")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise AttributeError("Configuration fields cannot be deleted")


@dataclass
class PidConfiguration(_Settings):
    id: str
    proportional_gain: float
    integral_gain: float
    double_integral_gain: float
    derivative_gain: float
    derivative_filter_cutoff_hz: float
    controller_type: PidControllerType
    _editable = frozenset(
        {
            "proportional_gain",
            "integral_gain",
            "double_integral_gain",
            "derivative_gain",
            "derivative_filter_cutoff_hz",
            "controller_type",
        }
    )


@dataclass
class DemodulationConfiguration(_Settings):
    id: str
    filter_frequency_hz: float
    phase_radians: float
    amplitude: float
    set_point: float
    _editable = frozenset(
        {"filter_frequency_hz", "phase_radians", "amplitude", "set_point"}
    )


@dataclass
class LockThresholdSettings(_Settings):
    id: str
    lock_threshold: int
    out_of_lock_threshold: int
    skip_delock: int
    ms_without_lock: int
    delock_smoothing_constant: float
    _editable = frozenset(
        {
            "lock_threshold",
            "out_of_lock_threshold",
            "skip_delock",
            "ms_without_lock",
            "delock_smoothing_constant",
        }
    )


@dataclass
class PdhSettings(_Settings):
    id: str
    high_limit: float
    low_limit: float
    slope_preference: SlopePreference
    _editable = frozenset({"high_limit", "low_limit", "slope_preference"})


@dataclass
class ScanSettings(_Settings):
    id: str
    scan_max: int
    scan_min: int
    scan_time_ms: int
    _editable = frozenset({"scan_max", "scan_min", "scan_time_ms"})


@dataclass
class ScanRefinerSettings(_Settings):
    id: str
    refinement_levels: int
    refinement_initial_half_width: int
    refinement_min_half_width: int
    refinement_timeout_sweeps: int
    _editable = frozenset(
        {
            "refinement_levels",
            "refinement_initial_half_width",
            "refinement_min_half_width",
            "refinement_timeout_sweeps",
        }
    )


@dataclass
class NotchFilter(_Settings):
    id: str
    sampling_period: float
    filter_frequency: float
    filter_width: float
    filter_depth: float


@dataclass
class NotchFilterSelection(_Settings):
    lockbox_configuration_id: str
    notch_filter_id: str
    enabled: bool
    notch_filter: NotchFilter | None


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _enum(enum_type, value):
    if isinstance(value, str) and not value.lstrip("-").isdigit():
        normalized = value.replace("_", "").lower()
        return next(
            item
            for item in enum_type
            if item.name.replace("_", "").lower() == normalized
        )
    return enum_type(int(value))


def _section(section_type, payload):
    if payload is None:
        return None
    values = {f.name: payload[_camel(f.name)] for f in fields(section_type)}
    if section_type is PidConfiguration:
        values["controller_type"] = _enum(PidControllerType, values["controller_type"])
    elif section_type is PdhSettings:
        values["slope_preference"] = _enum(SlopePreference, values["slope_preference"])
    return section_type(**values)


def _payload(section):
    return {_camel(f.name): getattr(section, f.name) for f in fields(section)}


@dataclass
class LockboxConfiguration(_Settings):
    id: str
    arduino_id: int
    pid: PidConfiguration | None
    demodulation: DemodulationConfiguration | None
    lock_thresholds: LockThresholdSettings | None
    pdh: PdhSettings | None
    coarse_scan: ScanSettings | None
    fine_scan: ScanSettings | None
    coarse_refinement: ScanRefinerSettings | None
    fine_refinement: ScanRefinerSettings | None
    modulation_amplitude: int
    notch_filters: tuple[NotchFilter, ...]
    filter_selections: tuple[NotchFilterSelection, ...]
    _server_url: str = field(repr=False)
    _record_ids: tuple[tuple[str, str | None], ...] = field(repr=False)
    _editable = frozenset({"modulation_amplitude"})

    @classmethod
    def _from_payload(cls, payload: dict, server_url: str) -> LockboxConfiguration:
        sections = {
            "pid": (PidConfiguration, "pidConfiguration"),
            "demodulation": (DemodulationConfiguration, "demodulationConfiguration"),
            "lock_thresholds": (LockThresholdSettings, "lockThresholdSettings"),
            "pdh": (PdhSettings, "pdhSettings"),
            "coarse_scan": (ScanSettings, "coarseScanSettings"),
            "fine_scan": (ScanSettings, "fineScanSettings"),
            "coarse_refinement": (ScanRefinerSettings, "coarseScanRefinerSettings"),
            "fine_refinement": (ScanRefinerSettings, "fineScanRefinerSettings"),
        }
        values = {
            name: _section(kind, payload.get(key))
            for name, (kind, key) in sections.items()
        }
        selections = tuple(
            NotchFilterSelection(
                item["lockboxConfigurationId"],
                item["notchFilterId"],
                item["enabled"],
                _section(NotchFilter, item.get("notchFilter")),
            )
            for item in payload.get("lockboxConfigurationNotchFilterConfigurations", [])
        )
        return cls(
            id=payload["id"],
            arduino_id=payload["arduinoId"],
            **values,
            modulation_amplitude=payload["modulationAmplitude"],
            notch_filters=tuple(
                _section(NotchFilter, item) for item in payload.get("notchFilters", [])
            ),
            filter_selections=selections,
            _server_url=server_url,
            # Demodulation uses the root configuration ID as its shared primary key.
            _record_ids=tuple(
                (
                    name,
                    payload["id"]
                    if name == "demodulation"
                    else payload.get(key + "Id"),
                )
                for name, (_, key) in sections.items()
            ),
        )

    def _save_requests(
        self, arduino_id: int, server_url: str
    ) -> list[tuple[str, str, dict]]:
        """Build and validate every payload before the first HTTP write."""
        if self.arduino_id != arduino_id or self._server_url != server_url:
            raise ValueError("Configuration belongs to a different lockbox or server")
        if not self.id:
            raise ValueError("Configuration has no saved ID")
        required = (
            "pid",
            "demodulation",
            "lock_thresholds",
            "coarse_scan",
            "fine_scan",
            "coarse_refinement",
            "fine_refinement",
        )
        for name, expected_id in self._record_ids:
            section = getattr(self, name)
            if section is None:
                if name in required or (name == "pdh" and expected_id):
                    raise ValueError(f"Configuration is missing its {name} record")
            elif not section.id or section.id != expected_id:
                raise ValueError(f"Configuration has inconsistent {name} record IDs")

        pid = _payload(self.pid)
        pid["controllerType"] = int(PidControllerType(self.pid.controller_type))
        refinement = {}
        for prefix, section in (
            ("coarse", self.coarse_refinement),
            ("fine", self.fine_refinement),
        ):
            refinement.update(
                {
                    prefix + key[0].upper() + key[1:]: value
                    for key, value in _payload(section).items()
                    if key != "id"
                }
            )
        path = f"api/PidConfigurations/{self.pid.id}"
        requests = [
            ("PUT", path + "/persist", pid),
            (
                "PUT",
                f"api/DemodulationConfigurations/{self.demodulation.id}/persist",
                _payload(self.demodulation),
            ),
            ("PUT", path + "/modulation", {"amplitude": self.modulation_amplitude}),
            (
                "PUT",
                f"api/LockThresholdSettings/{self.lock_thresholds.id}",
                _payload(self.lock_thresholds),
            ),
        ]
        if self.pdh is not None:
            pdh = _payload(self.pdh)
            pdh["slopePreference"] = int(SlopePreference(self.pdh.slope_preference))
            requests.append(("PUT", f"api/PdhSettings/{self.pdh.id}", pdh))
        requests.extend(
            [
                (
                    "PUT",
                    f"api/ScanSettings/{self.coarse_scan.id}",
                    _payload(self.coarse_scan),
                ),
                (
                    "PUT",
                    f"api/ScanSettings/{self.fine_scan.id}",
                    _payload(self.fine_scan),
                ),
                ("PUT", path + "/refinement", refinement),
            ]
        )
        # Detect unserializable edits before any endpoint can persist a section.
        json.dumps(requests, allow_nan=False)
        return requests
