"""Station receiver (SPEC 3.5): packet -> size check -> parse -> verify -> replay check -> store
-> bus message -> on_verified hooks.

Bus messages (common/contracts.py), plus two additive keys:
  {"type": "detection", "detection": Detection.to_json(), "node": <name>}
  {"type": "rejected", "node": <name>, "reason": <reason>, "seq": <int | None>}
Reasons: bad_signature | replay | oversize (contract) and malformed | unknown_node (extra).

Agent track: get_receiver().on_verified.append(fn). fn(detection) is called only for records
whose signature verified and whose seq is new. It runs on the station's event loop thread for
UDP and loop-scheduled in-process delivery, so keep it quick (enqueue and return).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from nacl.signing import VerifyKey

from common.contracts import MAX_PACKET_BYTES, RECORD_BYTES, ROOT, Detection, World, load_world
from node.keys import ensure_keys
from node.record import OFFSET_CONFIDENCE, RecordError, peek_node_id, unpack, verify
from station.bus import bus
from station.store import SqliteEventStore
from transport.udp import DEFAULT_HOST, DEFAULT_PORT, start_udp_listener

log = logging.getLogger(__name__)

OVERSIZE = "oversize"
MALFORMED = "malformed"
UNKNOWN_NODE = "unknown_node"
BAD_SIGNATURE = "bad_signature"
REPLAY = "replay"
UNKNOWN_NODE_NAME = "Unknown node"


@dataclass(frozen=True)
class Outcome:
    accepted: bool
    reason: str | None = None  # None when accepted
    detection: Detection | None = None


class Receiver:
    def __init__(
        self,
        store: SqliteEventStore,
        public_keys: Mapping[int, VerifyKey],
        world: World | None = None,
        publish: Callable[[dict], None] | None = None,
    ) -> None:
        self.store = store
        self.public_keys = dict(public_keys)
        self.names = {n.id: n.name for n in world.nodes} if world else {}
        self.on_verified: list[Callable[[Detection], None]] = []
        self.counts: Counter[str] = Counter()
        self._publish = publish or bus.publish
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_world(self, world: World) -> None:
        """Another map: its node names, and keys for any node id not provisioned yet."""
        from node.keys import ensure_keys

        self.public_keys = dict(ensure_keys(world))
        self.names = {n.id: n.name for n in world.nodes}

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bus messages from other threads are handed to this loop (asyncio queues are not
        thread-safe)."""
        self._loop = loop

    def node_name(self, node_id: int) -> str:
        return self.names.get(node_id, UNKNOWN_NODE_NAME)

    def handle(self, packet: bytes) -> Outcome:
        packet = bytes(packet)
        if len(packet) > MAX_PACKET_BYTES:
            return self._reject(OVERSIZE, None)
        if len(packet) != RECORD_BYTES:
            return self._reject(MALFORMED, None)

        node_id = peek_node_id(packet)
        key = self.public_keys.get(node_id)
        signature_ok = key is not None and verify(packet, key)
        try:
            detection = unpack(packet, verified=signature_ok)
        except RecordError:
            # Unparseable: if the signature is bad it was tampered with; otherwise it is a
            # signed record this station does not understand. Not storable either way.
            return self._reject(MALFORMED if signature_ok else BAD_SIGNATURE, None, node_id)

        if not signature_ok:
            self.store.add(detection, packet)  # kept as evidence, verified=False
            return self._reject(BAD_SIGNATURE if key else UNKNOWN_NODE, detection.seq, node_id)
        if not self.store.add(detection, packet):
            return self._reject(REPLAY, detection.seq, node_id)

        self.counts["verified"] += 1
        self._emit(
            {"type": "detection", "detection": detection.to_json(), "node": self.node_name(node_id)}
        )
        for hook in list(self.on_verified):
            try:
                hook(detection)
            except Exception:
                log.exception("on_verified hook %r failed", hook)
        return Outcome(True, None, detection)

    def reverify(self, raw: bytes) -> bool:
        if len(raw) != RECORD_BYTES:
            return False
        key = self.public_keys.get(peek_node_id(raw))
        return key is not None and verify(raw, key)

    def tamper(self, node_id: int, seq: int, index: int = OFFSET_CONFIDENCE) -> dict | None:
        """Demo: flip one byte of the stored record, re-verify, store the result.
        Returns None if there is no such record."""
        if not 0 <= index < RECORD_BYTES:
            raise ValueError(f"byte index must be 0..{RECORD_BYTES - 1}")
        event = self.store.find(node_id, seq)
        if event is None:
            return None
        raw = bytearray(event.raw)
        raw[index] ^= 0xFF
        ok = self.reverify(bytes(raw))
        self.store.update_raw(event.row_id, bytes(raw), ok)
        if not ok:
            self._reject(BAD_SIGNATURE, seq, node_id)
        return {
            "node": self.node_name(node_id),
            "seq": seq,
            "byte": index,
            "verified_before": event.detection.verified,
            "verified": ok,
        }

    def _reject(self, reason: str, seq: int | None, node_id: int | None = None) -> Outcome:
        self.counts[reason] += 1
        name = self.node_name(node_id) if node_id is not None else UNKNOWN_NODE_NAME
        log.warning("rejected record from %s (seq %s): %s", name, seq, reason)
        self._emit({"type": "rejected", "node": name, "reason": reason, "seq": seq})
        return Outcome(False, reason, None)

    def _emit(self, message: dict) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                current = asyncio.get_running_loop()
            except RuntimeError:
                current = None
            if current is not loop:
                loop.call_soon_threadsafe(self._publish, message)
                return
        self._publish(message)


# ---------- the station's default instance ----------

_default: Receiver | None = None
_default_lock = threading.Lock()


def events_db_path() -> Path:
    return Path(os.getenv("EVENTS_DB") or ROOT / "station.sqlite")


def get_receiver() -> Receiver:
    """The station's receiver (created on first use: opens the DB, loads or provisions keys)."""
    global _default
    with _default_lock:
        if _default is None:
            world = load_world()
            _default = Receiver(SqliteEventStore(events_db_path()), ensure_keys(world), world)
        return _default


def get_store() -> SqliteEventStore:
    return get_receiver().store


def udp_settings() -> tuple[str, int] | None:
    """LORA_UDP_HOST / LORA_UDP_PORT from the environment; LORA_UDP_PORT=off disables UDP."""
    port = os.getenv("LORA_UDP_PORT", str(DEFAULT_PORT)).strip().lower()
    if port in ("off", "none", ""):
        return None
    return os.getenv("LORA_UDP_HOST", DEFAULT_HOST), int(port)


async def serve_udp(receiver: Receiver) -> asyncio.DatagramTransport | None:
    """Bind the receiver to the current loop and start the UDP listener. A busy port is logged,
    not fatal, so the web UI still starts (e.g. tests run while `make dev` is up)."""
    receiver.bind_loop(asyncio.get_running_loop())
    settings = udp_settings()
    if settings is None:
        return None
    host, port = settings
    try:
        transport = await start_udp_listener(receiver.handle, host, port)
    except OSError as e:
        log.warning("LoRa UDP listener not started on %s:%d: %s", host, port, e)
        return None
    log.info("LoRa UDP listener on %s:%d", *transport.get_extra_info("sockname")[:2])
    return transport
