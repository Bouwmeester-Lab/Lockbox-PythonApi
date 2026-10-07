import socket
from threading import Event, Thread
from unittest.mock import Mock

import pytest
from signalrcore.transport.websockets.websocket_client import WebSocketClient

from lockbox._socket_shutdown import configure_shutdown


def make_client():
    return WebSocketClient(
        "ws://localhost/",
        on_open=Mock(),
        on_close=Mock(),
        on_error=Mock(),
        on_message=Mock(),
    )


@pytest.mark.parametrize("closing", [False, True])
def test_windows_abort_only_suppressed_during_local_close(closing):
    client = make_client()

    def aborted_receive():
        if closing:
            client.running = False  # close() clears this before waking recv().
        raise ConnectionAbortedError(10053, "aborted")

    client._recv_frame = aborted_receive
    configure_shutdown(client)
    client.is_closing = closing
    client.running = True
    client.logger = Mock()
    client.run()
    if closing:
        client.on_error.assert_not_called()
        client.logger.error.assert_not_called()
    else:
        client.on_error.assert_called_once()
        client.logger.error.assert_called_once()


def test_close_unblocks_receiver_sends_close_frame_and_joins(caplog):
    client = make_client()
    client.sock, peer = socket.socketpair()
    peer.settimeout(2)
    entered = Event()
    original_receive = client._recv_frame

    def receive():
        entered.set()
        return original_receive()

    client._recv_frame = receive
    configure_shutdown(client)
    client.running = True
    receiver = Thread(target=client.run, name=client.thread_name, daemon=True)
    client.recv_thread = receiver
    receiver.start()
    try:
        assert entered.wait(2)
        closer = Thread(target=client.close, daemon=True)
        closer.start()
        closer.join(2)
        assert not closer.is_alive()
        assert not receiver.is_alive()
        assert client.recv_thread is None
        assert client.sock.fileno() == -1
        frame = b""
        while len(frame) < 8:
            frame += peer.recv(8 - len(frame))
        assert frame[:2] == b"\x88\x82"
        assert bytes(frame[6 + i] ^ frame[2 + i] for i in range(2)) == b"\x03\xe8"
        client.close()  # Repeated shutdown is harmless.
        client.on_close.assert_called_once()
        client.on_error.assert_not_called()
        assert not [record for record in caplog.records if record.levelno >= 40]
    finally:
        peer.close()
        client.sock.close()
        receiver.join(2)
