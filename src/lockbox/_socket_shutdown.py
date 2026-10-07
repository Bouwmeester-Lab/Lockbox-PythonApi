"""Per-socket shutdown compatibility for signalrcore 1.0.2."""

import os
import socket
import struct
from threading import current_thread

from signalrcore.transport.sockets.errors import SocketClosedError


def configure_shutdown(client) -> None:
    """Unblock the receiver on close without hiding errors on live connections."""
    receive = client._recv_frame

    def receive_frame():
        try:
            return receive()
        except (OSError, SocketClosedError):
            if not client.is_closing:
                raise
            return None

    def dispose():
        sock = client.sock
        if sock is not None:
            try:
                if client.is_closing:
                    # Send the normal WebSocket close code before shutting down TCP.
                    try:
                        # client.close() has already cleared running, so its
                        # send() would reject this final frame. Clients must mask.
                        mask = os.urandom(4)
                        payload = struct.pack(">H", 1000)
                        masked = bytes(
                            value ^ mask[i] for i, value in enumerate(payload)
                        )
                        sock.sendall(b"\x88\x82" + mask + masked)
                    except OSError:
                        pass  # The peer may already have closed its socket.
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # Already disconnected.
            finally:
                sock.close()
        thread = client.recv_thread
        if thread is not None and thread is not current_thread():
            thread.join()
            client.recv_thread = None

    client._recv_frame = receive_frame
    client.dispose = dispose
