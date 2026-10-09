import json
from copy import deepcopy
from unittest.mock import Mock

import httpx
import pytest

from lockbox import Lockbox, PidControllerType, Server, SlopePreference


@pytest.fixture
def saved():
    data = {
        "id": "00000000-0000-0000-0000-000000000001",
        "arduinoId": 7,
        "modulationAmplitude": 41,
        "pidConfiguration": {
            "proportionalGain": 1.0,
            "integralGain": 2.0,
            "doubleIntegralGain": 3.0,
            "derivativeGain": 4.0,
            "derivativeFilterCutoffHz": 1000.0,
            "controllerType": 0,
        },
        "demodulationConfiguration": {
            "filterFrequencyHz": 100.0,
            "phaseRadians": 0.5,
            "amplitude": 5.0,
            "setPoint": 0.0,
        },
        "lockThresholdSettings": {
            "lockThreshold": 100,
            "outOfLockThreshold": 80,
            "skipDelock": 2,
            "msWithoutLock": 100,
            "delockSmoothingConstant": 0.1,
        },
        "pdhSettings": {"lowLimit": -100.0, "highLimit": 100.0, "slopePreference": 2},
        "coarseScanSettings": {"scanMin": -2000, "scanMax": 2000, "scanTimeMs": 1000},
        "fineScanSettings": {"scanMin": -1000, "scanMax": 1000, "scanTimeMs": 2000},
        "coarseScanRefinerSettings": {
            "refinementLevels": 2,
            "refinementInitialHalfWidth": 1000,
            "refinementMinHalfWidth": 200,
            "refinementTimeoutSweeps": 3,
        },
        "fineScanRefinerSettings": {
            "refinementLevels": 3,
            "refinementInitialHalfWidth": 2000,
            "refinementMinHalfWidth": 100,
            "refinementTimeoutSweeps": 4,
        },
        "notchFilters": [],
        "lockboxConfigurationNotchFilterConfigurations": [],
    }
    sections = [key for key, value in data.items() if isinstance(value, dict)]
    for index, key in enumerate(sections, 2):
        id = f"00000000-0000-0000-0000-{index:012}"
        data[key]["id"] = id
        data[key + "Id"] = id
    data["demodulationConfiguration"]["id"] = data["id"]
    del data["demodulationConfigurationId"]
    filter = {
        "id": "00000000-0000-0000-0000-000000000099",
        "samplingPeriod": 10.0,
        "filterFrequency": 60.0,
        "filterWidth": 5.0,
        "filterDepth": 0.8,
    }
    data["notchFilters"] = [filter]
    data["lockboxConfigurationNotchFilterConfigurations"] = [
        {
            "lockboxConfigurationId": data["id"],
            "notchFilterId": filter["id"],
            "enabled": True,
            "notchFilter": deepcopy(filter),
        }
    ]
    return data


@pytest.fixture
def device(saved):
    requests = []
    failures = {}

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(failures.get("get", 200), json=saved)
        number = sum(r.method != "GET" for r in requests)
        if number == failures.get("write"):
            return httpx.Response(400, text="server rejected this section\n")
        return httpx.Response(204)

    server = Server("http://test/prefix")
    server._run_hub = Mock()
    # Exercise the real Server HTTP path without opening hardware connections.
    with httpx.Client(
        base_url=server.url + "/", transport=httpx.MockTransport(respond)
    ) as http:
        server._http = http
        yield Lockbox(7, "Test", "", "", server), requests, failures


def test_fresh_typed_snapshot_and_read_only_fields(device, saved):
    box, requests, _ = device
    config = box.get_configuration()
    assert requests[0].url.path == "/prefix/api/LockboxConfigurations/arduino/7"
    assert config.id == saved["id"] and config.arduino_id == 7
    assert config.pid.controller_type is PidControllerType.SIMPLE_PI
    assert config.pdh.slope_preference is SlopePreference.POSITIVE
    assert config.demodulation.id == config.id
    assert config.coarse_scan.scan_time_ms == 1000
    assert config.fine_scan.scan_min == -1000
    assert config.filter_selections[0].notch_filter.filter_frequency == 60.0
    assert config.notch_filters[0].id == config.filter_selections[0].notch_filter_id
    for target, field in [
        (config, "id"),
        (config, "arduino_id"),
        (config, "pid"),
        (config.pid, "id"),
        (config.coarse_scan, "id"),
        (config.fine_scan, "id"),
        (config.lock_thresholds, "id"),
        (config, "coarse_scan"),
        (config, "fine_scan"),
        (config, "lock_thresholds"),
        (config, "filter_selections"),
        (config.filter_selections[0], "enabled"),
        (config.notch_filters[0], "filter_width"),
        (config.filter_selections[0].notch_filter, "filter_frequency"),
    ]:
        with pytest.raises(AttributeError, match="read-only"):
            setattr(target, field, None)
    with pytest.raises(AttributeError):
        config.pid.misspelled_gain = 3
    config.pid.proportional_gain = 12
    second = box.get_configuration()
    assert second.pid is not config.pid
    assert second.pid.proportional_gain == 1
    box.set_pid(9, 8, 7)
    assert config.pid.proportional_gain == 12
    assert second.pid.proportional_gain == 1


def test_all_eight_save_requests_and_payloads(device, saved):
    box, requests, _ = device
    config = box.get_configuration()
    config.pid.controller_type = PidControllerType.FILTERED_PID
    config.pid.proportional_gain = 2.5
    config.demodulation.set_point = 0.1
    config.modulation_amplitude = 50
    config.lock_thresholds.lock_threshold = 120
    config.lock_thresholds.out_of_lock_threshold = 90
    config.lock_thresholds.skip_delock = 4
    config.lock_thresholds.ms_without_lock = 250
    config.lock_thresholds.delock_smoothing_constant = 0.25
    config.coarse_scan.scan_min = -3000
    config.coarse_scan.scan_max = 3000
    config.coarse_scan.scan_time_ms = 1500
    config.fine_scan.scan_min = -500
    config.fine_scan.scan_max = 500
    config.fine_scan.scan_time_ms = 2500
    config.pdh.slope_preference = SlopePreference.NEGATIVE
    config.coarse_refinement.refinement_levels = 5
    assert box.save_configuration(config) is None
    pid_path = f"/prefix/api/PidConfigurations/{config.pid.id}"
    assert [(r.method, r.url.path) for r in requests[1:]] == [
        ("PUT", pid_path + "/persist"),
        (
            "PUT",
            f"/prefix/api/DemodulationConfigurations/{config.demodulation.id}/persist",
        ),
        ("PUT", pid_path + "/modulation"),
        ("PUT", f"/prefix/api/LockThresholdSettings/{config.lock_thresholds.id}"),
        ("PUT", f"/prefix/api/PdhSettings/{config.pdh.id}"),
        ("PUT", f"/prefix/api/ScanSettings/{config.coarse_scan.id}"),
        ("PUT", f"/prefix/api/ScanSettings/{config.fine_scan.id}"),
        ("PUT", pid_path + "/refinement"),
    ]
    bodies = [json.loads(r.content) for r in requests[1:]]
    assert bodies == [
        dict(saved["pidConfiguration"], proportionalGain=2.5, controllerType=1),
        dict(saved["demodulationConfiguration"], setPoint=0.1),
        {"amplitude": 50},
        dict(
            saved["lockThresholdSettings"],
            lockThreshold=120,
            outOfLockThreshold=90,
            skipDelock=4,
            msWithoutLock=250,
            delockSmoothingConstant=0.25,
        ),
        dict(saved["pdhSettings"], slopePreference=1),
        dict(saved["coarseScanSettings"], scanMin=-3000, scanMax=3000, scanTimeMs=1500),
        dict(saved["fineScanSettings"], scanMin=-500, scanMax=500, scanTimeMs=2500),
        {
            "coarseRefinementLevels": 5,
            "coarseRefinementInitialHalfWidth": 1000,
            "coarseRefinementMinHalfWidth": 200,
            "coarseRefinementTimeoutSweeps": 3,
            "fineRefinementLevels": 3,
            "fineRefinementInitialHalfWidth": 2000,
            "fineRefinementMinHalfWidth": 100,
            "fineRefinementTimeoutSweeps": 4,
        },
    ]
    box.server._run_hub.invoke.assert_not_called()


def test_absent_optional_pdh(device, saved):
    saved["pdhSettings"] = saved["pdhSettingsId"] = None
    box, requests, _ = device
    config = box.get_configuration()
    assert config.pdh is None
    box.save_configuration(config)
    assert len(requests) == 8  # One GET and seven PUTs.
    assert json.loads(requests[4].content) == saved["lockThresholdSettings"]
    assert all("PdhSettings" not in request.url.path for request in requests)
    assert [request.url.path for request in requests[5:7]] == [
        f"/prefix/api/ScanSettings/{config.coarse_scan.id}",
        f"/prefix/api/ScanSettings/{config.fine_scan.id}",
    ]


def test_fractional_pdh_transmitted_unchanged(device):
    box, requests, _ = device
    config = box.get_configuration()
    config.pdh.low_limit = -100.5
    config.pdh.high_limit = 100.25
    box.save_configuration(config)
    body = json.loads(requests[5].content)
    assert body["lowLimit"] == -100.5
    assert body["highLimit"] == 100.25
    assert body["id"] == config.pdh.id


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), object()])
def test_invalid_serialization_rejected_before_writes(device, value):
    box, requests, _ = device
    config = box.get_configuration()
    config.pdh.low_limit = value
    with pytest.raises((ValueError, TypeError)):
        box.save_configuration(config)
    assert len(requests) == 1


def test_other_device_or_server_rejected_before_writes(device):
    box, requests, _ = device
    config = box.get_configuration()
    for other in (
        Lockbox(8, "", "", "", box.server),
        Lockbox(7, "", "", "", Server("http://different")),
    ):
        with pytest.raises(ValueError, match="different lockbox or server"):
            other.save_configuration(config)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "section",
    [
        "pidConfiguration",
        "demodulationConfiguration",
        "lockThresholdSettings",
        "coarseScanSettings",
        "fineScanSettings",
        "coarseScanRefinerSettings",
        "fineScanRefinerSettings",
        "pdhSettings",
    ],
)
def test_missing_linked_record_rejected_before_writes(device, saved, section):
    saved[section] = None
    box, requests, _ = device
    config = box.get_configuration()
    with pytest.raises(ValueError, match="missing"):
        box.save_configuration(config)
    assert len(requests) == 1


def test_mismatched_nested_id_rejected_before_writes(device, saved):
    saved["pidConfigurationId"] = "different"
    box, requests, _ = device
    with pytest.raises(ValueError, match="inconsistent pid"):
        box.save_configuration(box.get_configuration())
    assert len(requests) == 1


@pytest.mark.parametrize("write", range(1, 9))
def test_failure_stops_saves_and_preserves_response_error(device, write):
    box, requests, failures = device
    failures["write"] = write
    with pytest.raises(Exception) as caught:
        box.save_configuration(box.get_configuration())
    assert caught.value.body == "server rejected this section\n"
    assert "server rejected this section\n" in str(caught.value)
    assert len(requests) == 1 + write
    assert all(request.method == "PUT" for request in requests[1:])
    assert all(
        "/Run/" not in r.url.path and "Filter" not in r.url.path for r in requests
    )
    box.server._run_hub.invoke.assert_not_called()


def test_missing_configuration_uses_http_error(device):
    box, requests, failures = device
    failures["get"] = 404
    with pytest.raises(Exception) as caught:
        box.get_configuration()
    assert caught.value.status_code == 404
    assert json.loads(caught.value.body)["arduinoId"] == 7
    assert len(requests) == 1


def test_string_enums(device, saved):
    saved["pidConfiguration"]["controllerType"] = "FilteredPid"
    saved["pdhSettings"]["slopePreference"] = "Negative"
    config = device[0].get_configuration()
    assert config.pid.controller_type is PidControllerType.FILTERED_PID
    assert config.pdh.slope_preference is SlopePreference.NEGATIVE
