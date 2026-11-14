"""UDP LoRa link: node processes send datagrams to the station on localhost.

Loss and latency are applied on the sending side (UdpLink); the station side (start_udp_listener)
hands every datagram, whatever its size, to the receiver, which does its own size check.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
from collections.abc import Callable

from transport.base import LoRaLink

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9871


class UdpLink(LoRaLink):
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, **link_options) -> None:
        super().__init__(**link_options)
        self.address = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._timers: set[threading.Timer] = set()
        self._timers_lock = threading.Lock()

    def _transmit(self, payload: bytes, delay_s: float) -> None:
        if delay_s <= 0:
            self._sendto(payload)
            return

        def fire() -> None:
            with self._timers_lock:
                self._timers.discard(timer)
            self._sendto(payload)

        timer = threading.Timer(delay_s, fire)
        timer.daemon = True
        with self._timers_lock:
            self._timers.add(timer)
        timer.start()

    def _sendto(self, payload: bytes) -> None:
        try:
            self._sock.sendto(payload, self.address)
        except OSError:
            log.exception("udp send to %s:%d failed", *self.address)

    def close(self, wait: bool = True) -> None:
        """Close the socket. wait=True lets packets already in flight arrive first."""
        with self._timers_lock:
            timers = list(self._timers)
        for t in timers:
            if wait:
                t.join(timeout=self.latency_ms[1] / 1000.0 + 1.0)
            else:
                t.cancel()
        self._sock.close()


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, deliver: Callable[[bytes], object]) -> None:
        self._deliver = deliver

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            self._deliver(data)
        except Exception:
            log.exception("receiver failed on a datagram from %s", addr)


async def start_udp_listener(
    deliver: Callable[[bytes], object], host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> asyncio.DatagramTransport:
    """Listen on host:port; deliver(datagram) runs on the current event loop.
    port=0 picks a free port: read it back with transport.get_extra_info("sockname")."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _Protocol(deliver), local_addr=(host, port)
    )
    return transport
