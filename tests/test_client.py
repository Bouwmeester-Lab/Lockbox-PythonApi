import json
import math
from concurrent.futures import Future
from contextlib import contextmanager
from threading import Event, Thread, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from signalrcore.messages.completion_message import CompletionMessage

from lockbox import Lockbox, LockboxHttpError, Server, SlopePreference, client
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

        def on_close(self, callback):
            self.close_callback = callback

        def close(self):
            self.closed = True
            self.close_callback()

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
        ("stop", (), "POST", "stop", None),
        ("go", (), "POST", "start", None),
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


@pytest.mark.parametrize(
    "degrees,expected", [(0, 0), (90, math.pi / 2), (-180, -math.pi), (22.5, math.pi / 8)]
)
def test_demodulation_phase_degrees(environment, degrees, expected):
    with Server("http://test/prefix") as server:
        box = server.get_lockbox(1)
        assert box.set_demodulation_phase_deg(degrees) is None
        request = environment.requests[-1]
        assert request.method == "POST"
        assert request.url.path == "/prefix/api/Run/demodulation/phase"
        assert dict(request.url.params) == {"teensyId": "1"}
        assert json.loads(request.content) == {"phaseRadians": pytest.approx(expected)}


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
    with pytest.raises(NotImplementedError):
        box.on_lockbox_recentering(lambda: None)


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
        with pytest.raises(LockboxHttpError) as caught:
            Lockbox(1, "", "", "", server).freeze()
        assert caught.value.body == "device error\n"
        assert caught.value.status_code == status
        assert f"HTTP {status}" in str(caught.value)
        assert "device error\n" in str(caught.value)


@pytest.mark.parametrize("action,route", [("stop", "stop"), ("go", "start")])
@pytest.mark.parametrize("body", ["", "Lockbox already enabled", "Lockbox already disabled"])
def test_stop_go_conflict_details(environment, monkeypatch, action, route, body):
    response = httpx.Response(409, text=body)
    monkeypatch.setattr(environment.http, "request", lambda *a, **kw: response)
    with Server("http://test") as server:
        with pytest.raises(LockboxHttpError) as caught:
            getattr(Lockbox(1, "", "", "", server), action)()
        error = caught.value
        assert error.status_code == 409
        assert error.response is response
        assert error.body == body
        assert f"POST api/Run/{route}: HTTP 409 Conflict" in str(error)
        assert body in str(error)
        assert not server._stopped.is_set()


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


@contextmanager
def running_wait(server, monkeypatch):
    """Synchronize with a real blocking wait and never leave a test thread alive."""
    entered, finished = Event(), Event()
    errors = []
    original_wait = server._stopped.wait

    def wait(timeout):
        assert timeout == 0.1
        entered.set()
        return original_wait(timeout)

    def run():
        try:
            server.run_forever()
        except ConnectionError as error:
            errors.append(error)
        finally:
            finished.set()

    monkeypatch.setattr(server._stopped, "wait", wait)
    thread = Thread(target=run, daemon=True)
    thread.start()
    try:
        assert entered.wait(2)
        assert not finished.is_set()
        yield finished, errors
    finally:
        server.stop()
        thread.join(2)
        assert not thread.is_alive()


@pytest.mark.parametrize(
    "registration,status",
    [("on_lockbox_freeze_ready", "DriftEstimateReady"), ("on_lock_acquired", "Locked")],
)
def test_callback_stops_wait_without_closing_connections(
    environment, monkeypatch, registration, status
):
    with Server("http://test") as server:
        box = server.get_lockbox(1)
        callback_threads = []

        def stop():
            callback_threads.append(get_ident())
            server.stop()

        getattr(box, registration)(stop)
        with running_wait(server, monkeypatch) as (finished, errors):
            environment.hubs[0].handlers["ReceiveStatus"](
                [{"arduinoId": 1, "status": status}]
            )
            assert finished.wait(2)
            assert errors == []
        assert callback_threads == [get_ident()]
        assert not environment.http.is_closed
        assert not any(h.closed for h in environment.hubs)
        assert server._disconnect_error is None
        assert len(environment.requests) == 1  # Only the inventory request.
        assert not any(h.calls for h in environment.hubs)


def test_early_stop_and_repeated_wait(environment):
    with Server("http://test") as server:
        server.stop()
        server.stop()
        assert server.run_forever() is None
        assert server.run_forever() is None


@pytest.mark.parametrize("status", ["Locked", 26])
def test_lock_acquired_is_a_dedicated_device_event(environment, status):
    with Server("http://test") as server:
        first, second = server.list_lockboxes()
        old, acquired, other = Mock(), Mock(), Mock()
        first.on_lock_acquired(old)
        first.on_lock_acquired(acquired)
        second.on_lock_acquired(other)
        first.lock()
        receive = environment.hubs[0].handlers["ReceiveStatus"]
        for unrelated in ("CoarseResonanceFound", "FineResonanceFound", "LockAttempted", 1):
            receive([{"arduinoId": 1, "status": unrelated}])
        receive([{"arduinoId": 99, "status": status}])
        acquired.assert_not_called()
        receive([{"arduinoId": 1, "status": status}])
        acquired.assert_called_once_with()
        other.assert_not_called()
        old.assert_not_called()
        receive([{"arduinoId": 2, "status": status}])
        other.assert_called_once_with()


@pytest.mark.parametrize("hub_index,name", [(0, "Status"), (1, "Run")])
@pytest.mark.parametrize("before_wait", [False, True])
def test_disconnect_releases_wait(environment, monkeypatch, hub_index, name, before_wait):
    with Server("http://test") as server:
        if before_wait:
            server.stop()
            environment.hubs[hub_index].close()
            with pytest.raises(ConnectionError, match=name):
                server.run_forever()
        else:
            with running_wait(server, monkeypatch) as (finished, errors):
                environment.hubs[hub_index].close()
                assert finished.wait(2)
                assert len(errors) == 1
                assert isinstance(errors[0], ConnectionError)
                assert name in str(errors[0])


def test_context_exit_releases_wait_without_disconnect_error(environment, monkeypatch):
    server = Server("http://test")
    with server, running_wait(server, monkeypatch) as (finished, errors):
        server.__exit__(None, None, None)
        assert finished.wait(2)
        assert errors == []
        assert server._disconnect_error is None
    assert environment.http.is_closed
    assert all(h.closed for h in environment.hubs)


def test_reenter_resets_stop_and_disconnect(environment, monkeypatch):
    server = Server("http://test")
    with server:
        environment.hubs[0].close()
        with pytest.raises(ConnectionError):
            server.run_forever()
    # Each context uses a fresh HTTP client, as the real factory does.
    monkeypatch.setattr(client.httpx, "Client", lambda **kw: type(environment.http)(**kw))
    with server:
        assert server._disconnect_error is None
        with running_wait(server, monkeypatch) as (finished, errors):
            server.stop()
            assert finished.wait(2)
            assert errors == []


def test_wait_requires_context(environment):
    server = Server("http://test")
    with pytest.raises(RuntimeError, match="with block"):
        server.run_forever()
    with server:
        server.stop()
    with pytest.raises(RuntimeError, match="with block"):
        server.run_forever()


def test_keyboard_interrupt_cleans_up(environment, monkeypatch):
    server = Server("http://test")
    monkeypatch.setattr(server._stopped, "wait", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt), server:
        server.run_forever()
    assert environment.http.is_closed
    assert all(h.closed for h in environment.hubs)
    assert server._disconnect_error is None


def test_hub_close_notifies_and_fails_pending_invocation():
    hub = Hub("http://test/Hubs/Run", 0.1)
    callback = Mock()
    hub.on_close(callback)
    pending = Future()
    hub._pending["command"] = pending
    hub._closed()
    callback.assert_called_once_with()
    with pytest.raises(ConnectionError, match="connection closed"):
        pending.result()
    assert not hub._pending
