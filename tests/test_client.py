import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from signalrcore.messages.completion_message import CompletionMessage

from lockbox import Lockbox, Server, SlopePreference, client
from lockbox._hub import Hub


@pytest.fixture
def environment(monkeypatch):
    requests, hubs = [], []
    inventory = [
        {
            "id": id,
            "name": f"Box {id}",
            "ipAddress": "10.0.0.1",
            "macAddress": "aa:bb:cc:dd:ee:ff",
        }
        for id in (1, 2)
    ]

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/Arduinos"):
            return httpx.Response(200, json=inventory)
        if "/Arduinos/" in request.url.path:
            return httpx.Response(200, json=inventory[int(request.url.path[-1]) - 1])
        return httpx.Response(200)

    http = httpx.Client(
        base_url="http://test/prefix/", transport=httpx.MockTransport(respond)
    )
    monkeypatch.setattr(client.httpx, "Client", lambda **kwargs: http)

    class FakeHub:
        def __init__(self, url, timeout):
            self.url = url
            self.closed = False
            self.calls = []
            self.handlers = {}
            hubs.append(self)

        def on(self, event, callback):
            self.handlers[event] = callback

        def start(self):
            pass

        def close(self):
            self.closed = True

        def invoke(self, method, arguments):
            self.calls.append((method, arguments))

    monkeypatch.setattr(client, "Hub", FakeHub)
    return SimpleNamespace(requests=requests, hubs=hubs, http=http, hub_type=FakeHub)


@pytest.mark.parametrize(
    "name,args,verb,route,body",
    [
        ("set_gain", (2,), "POST", "amplification", {"gain": 2}),
        (
            "set_demodulation_cutoff",
            (100,),
            "POST",
            "demodulation/frequency",
            {"filterFrequencyHz": 100},
        ),
        (
            "set_demodulation_phase",
            (0.5,),
            "POST",
            "demodulation/phase",
            {"phaseRadians": 0.5},
        ),
        ("set_modulation_amplitude", (41,), "POST", "modulation", {"amplitude": 41}),
        (
            "set_demodulation_amplitude",
            (5,),
            "POST",
            "demodulation/amplitude",
            {"amplitude": 5},
        ),
        ("set_pid", (1, 2, 3), "POST", "pid", {"p": 1, "i": 2, "d": 3}),
        ("reload_configuration", (), "GET", "configuration", None),
        ("lock", (), "POST", "lock", {"lockRequestType": "Normal"}),
        (
            "coarse_scan_only",
            (),
            "POST",
            "scan",
            {
                "dac": "CoarseDac",
                "lowerBound": -524287,
                "upperBound": 524287,
                "period": 0,
            },
        ),
        (
            "fine_scan_only",
            (),
            "POST",
            "scan",
            {
                "dac": "FineDac",
                "lowerBound": -524287,
                "upperBound": 524287,
                "period": 0,
            },
        ),
        (
            "coarse_scan_only",
            (-2000, 3000, 1500),
            "POST",
            "scan",
            {
                "dac": "CoarseDac",
                "lowerBound": -2000,
                "upperBound": 3000,
                "period": 1500,
            },
        ),
        (
            "fine_scan_only",
            (-500, 600, 2500),
            "POST",
            "scan",
            {"dac": "FineDac", "lowerBound": -500, "upperBound": 600, "period": 2500},
        ),
        (
            "fine_scan_only",
            (-500, 600),
            "POST",
            "scan",
            {"dac": "FineDac", "lowerBound": -500, "upperBound": 600, "period": 0},
        ),
        (
            "unlock",
            (),
            "POST",
            "unlock",
            {"scanAfter": False, "lockRequestType": "Normal"},
        ),
        ("freeze", (), "GET", "freeze", None),
        ("unfreeze", (), "GET", "freeze", None),
        (
            "set_fine_output_code",
            (-100,),
            "POST",
            "voltage",
            {"dac": "FineDac", "voltage": -100},
        ),
        (
            "set_coarse_output_code",
            (100,),
            "POST",
            "voltage",
            {"dac": "CoarseDac", "voltage": 100},
        ),
    ],
)
def test_http_commands(environment, name, args, verb, route, body):
    with Server("http://test/prefix/") as server:
        box = server.get_lockbox(1)
        callback = Mock()
        box.on_lockbox_frozen(callback)
        assert getattr(box, name)(*args) is None
        request = environment.requests[-1]
        assert request.method == verb
        assert request.url.path == f"/prefix/api/Run/{route}"
        assert dict(request.url.params) == {"teensyId": "1"}
        assert (json.loads(request.content) if request.content else None) == body
        callback.assert_not_called()


def test_run_commands_and_slope_preferences(environment):
    with Server("http://test/prefix") as server:
        box = server.get_lockbox(1)
        box.set_setpoint(0.25)
        box.set_threshold(123)
        box.set_low_high_threshold(-10, 10)
        for slope in SlopePreference:
            box.set_low_high_threshold(-10, 10, slope_preference=slope)
        calls = environment.hubs[1].calls
        assert calls[:2] == [
            ("UpdateSetpoint", [1, 0.25]),
            ("UpdateThresholds", [1, {"signalInputType": 0, "lockingThreshold": 123}]),
        ]
        assert [args[1]["slopePreference"] for _, args in calls[2:]] == [0, 0, 1, 2]
        assert calls[-1][1][1] == {
            "signalInputType": 1,
            "lowThreshold": -10,
            "highThreshold": 10,
            "slopePreference": 2,
        }


def test_routing_identity_and_callback_replacement(environment):
    with Server("http://test/prefix") as server:
        first, second = server.list_lockboxes()
        old, coarse, fine, other = Mock(), Mock(), Mock(), Mock()
        first.on_coarse_resonance_found(old)
        first.on_coarse_resonance_found(coarse)
        first.on_fine_resonance_found(fine)
        second.on_coarse_resonance_found(other)
        assert server.get_lockbox(1) is first
        assert server.list_lockboxes()[0] is first
        server.connect_to_status_hub(first)
        receive = environment.hubs[0].handlers["ReceiveStatus"]
        for id, status in [
            (1, "CoarseResonanceFound"),
            (1, "FineResonanceFound"),
            (2, 0),
            (99, 0),
            (1, "LockAttempted"),
        ]:
            receive([{"arduinoId": id, "status": status}])
        old.assert_not_called()
        for callback in (coarse, fine, other):
            callback.assert_called_once_with()
        assert len(environment.hubs) == 2
        assert [h.url for h in environment.hubs] == [
            "http://test/prefix/Hubs/Status",
            "http://test/prefix/Hubs/Run",
        ]


@pytest.mark.parametrize(
    "method,status",
    [
        ("on_lock_lost", "Unlocked"),
        ("on_lockbox_frozen", "FrozenEnabled"),
        ("on_lockbox_unfrozen", "FrozenDisabled"),
        ("on_lockbox_freeze_ready", "DriftEstimateReady"),
    ],
)
def test_remaining_status_callbacks(environment, method, status):
    with Server("http://test") as server:
        callback = Mock()
        getattr(server.get_lockbox(1), method)(callback)
        server._receive_status([{"ArduinoId": 1, "Status": status}])
        callback.assert_called_once_with()


def test_unsupported_callbacks():
    box = Lockbox(1, "", "", "")
    for method in (box.on_lock_acquired, box.on_lockbox_recentering):
        with pytest.raises(NotImplementedError):
            method(lambda: None)


@pytest.mark.parametrize("fail", [False, True])
def test_context_cleanup(environment, fail):
    try:
        with Server("http://test"):
            if fail:
                raise ValueError("experiment failed")
    except ValueError as error:
        assert str(error) == "experiment failed"
    assert environment.http.is_closed
    assert all(h.closed for h in environment.hubs)


def test_partial_startup_cleanup(environment, monkeypatch):
    def start(hub):
        if hub.url.endswith("/Run"):
            raise ConnectionError("unavailable")

    monkeypatch.setattr(environment.hub_type, "start", start)
    with pytest.raises(ConnectionError, match="unavailable"), Server("http://test"):
        pytest.fail("must not enter before connected")
    assert environment.http.is_closed
    assert all(h.closed for h in environment.hubs)


@pytest.mark.parametrize("status", [400, 409, 500])
def test_http_error_body(environment, monkeypatch, status):
    monkeypatch.setattr(
        environment.http,
        "request",
        lambda *a, **kw: httpx.Response(status, text="device error\n"),
    )
    with Server("http://test") as server:
        with pytest.raises(Exception) as caught:
            Lockbox(1, "", "", "", server).freeze()
        assert str(caught.value) == "device error\n"


@pytest.mark.parametrize("error", [None, "threshold rejected"])
def test_real_signalr_completion_dispatch(monkeypatch, error):
    hub = Hub("http://test/Hubs/Run", 0.1)
    transport = Mock()
    transport.connection_checker.running = False
    hub._connection.transport = transport
    monkeypatch.setattr(hub._connection, "start", hub._opened)
    hub.start()

    def send(message):
        # Use the actual library dispatch: failed completions call on_error,
        # rather than the normal invocation-completion callback.
        hub._connection.on_message(
            [CompletionMessage(message.invocation_id, None, error)]
        )

    transport.send.side_effect = send
    try:
        if error:
            with pytest.raises(Exception, match=error):
                hub.invoke("UpdateSetpoint", [1, 0.25])
        else:
            assert hub.invoke("UpdateSetpoint", [1, 0.25]) is None
        assert not hub._pending
        transport.connection_checker.start.assert_called_once()
    finally:
        hub.close()
    transport.stop.assert_called_once()


def test_signalr_startup_timeout_and_no_reconnect(monkeypatch):
    hub = Hub("http://test/Hubs/Status", 0.001)
    assert hub._connection.kwargs["reconnection_handler"] is None
    monkeypatch.setattr(hub._connection, "start", lambda: True)
    with pytest.raises(TimeoutError):
        hub.start()
    hub.close()
