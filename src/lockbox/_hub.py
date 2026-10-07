"""Synchronous SignalR transport adapter, without automatic reconnection."""

from concurrent.futures import Future
from threading import Event
from uuid import uuid4

from signalrcore.hub_connection_builder import HubConnectionBuilder
from signalrcore.protocol.json_hub_protocol import JsonHubProtocol


class Hub:
    def __init__(self, url: str, timeout: float):
        self.timeout = timeout
        self._ready = Event()
        self._connected = False
        self._pending: dict[str, Future] = {}
        self._connection = (
            HubConnectionBuilder()
            .with_url(url)
            .with_hub_protocol(JsonHubProtocol())
            .build()
        )
        self._connection.on_open(self._opened)
        self._connection.on_close(self._closed)
        self._connection.on_error(self._completed)

    def on(self, event, callback) -> None:
        self._connection.on(event, callback)

    def _opened(self) -> None:
        # signalrcore 1.0.2 only starts keepalive automatically with reconnect
        # enabled. Start its ping worker independently; never reconnect.
        checker = self._connection.transport.connection_checker
        if not checker.running:
            checker.start()
        self._connected = True
        self._ready.set()

    def _closed(self) -> None:
        self._connected = False
        self._ready.set()
        for id in list(self._pending):
            future = self._pending.pop(id, None)
            if future is not None:
                future.set_exception(ConnectionError("SignalR connection closed"))

    def start(self) -> None:
        self._connection.start()
        if not self._ready.wait(self.timeout):
            raise TimeoutError("SignalR handshake did not complete")
        if not self._connected:
            raise ConnectionError("SignalR connection closed during startup")

    def _completed(self, message) -> None:
        future = self._pending.pop(message.invocation_id, None)
        if future is not None:
            if message.error:
                future.set_exception(Exception(message.error))
            else:
                future.set_result(None)

    def invoke(self, method: str, arguments: list) -> None:
        if not self._connected:
            raise ConnectionError("SignalR connection is not open")
        id = str(uuid4())
        future = self._pending[id] = Future()
        try:
            self._connection.invoke(
                method, arguments, on_invocation=self._completed, invocation_id=id
            )
            future.result(timeout=self.timeout)
        finally:
            self._pending.pop(id, None)

    def close(self) -> None:
        try:
            self._connection.stop()
        finally:
            # stop() is a no-op after a remote disconnect; stop keepalive too.
            if self._connection.transport is not None:
                self._connection.transport.connection_checker.stop()
            self._closed()
