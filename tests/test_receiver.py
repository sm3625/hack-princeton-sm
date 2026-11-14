import asyncio
import threading

import numpy as np

from common.contracts import SAMPLE_RATE
from node.record import OFFSET_CONFIDENCE, pack
from node.runtime import NodeRuntime
from station.bus import bus
from station.receiver import Receiver
from station.store import SqliteEventStore
from tests.pipeline_fakes import (
    BG,
    CREEK_BEND,
    SAW,
    WORLD,
    Clock,
    ScriptedClassifier,
    detection,
    keys_for,
)
from transport import InProcessLink, ManualScheduler

KEYS = keys_for(*(n.id for n in WORLD.nodes), 99)
PUBLIC = {n.id: KEYS[n.id].verify_key for n in WORLD.nodes}  # node 99 is not provisioned


def rig(publish=None):
    messages: list[dict] = []
    store = SqliteEventStore()
    rx = Receiver(store, PUBLIC, WORLD, publish=publish or messages.append)
    return rx, store, messages


def record(node_id=5, seq=1, key_id=None, **kw):
    return pack(detection(node_id=node_id, seq=seq, **kw), KEYS[key_id or node_id])


def test_verified_record_is_stored_published_and_hooked():
    rx, store, messages = rig()
    hooked = []
    rx.on_verified.append(hooked.append)
    out = rx.handle(record())
    assert out.accepted and out.reason is None and out.detection.verified
    assert store.query() == [out.detection]
    assert messages == [
        {"type": "detection", "detection": out.detection.to_json(), "node": "Arroyo Recodo"}
    ]
    assert messages[0]["detection"]["sound"] == "chainsaw"
    assert hooked == [out.detection]


def test_bad_signature_is_stored_unverified_and_never_hooked():
    rx, store, messages = rig()
    hooked = []
    rx.on_verified.append(hooked.append)
    raw = bytearray(record())
    raw[OFFSET_CONFIDENCE] ^= 0xFF
    out = rx.handle(bytes(raw))
    assert (out.accepted, out.reason) == (False, "bad_signature")
    assert messages == [
        {"type": "rejected", "node": "Arroyo Recodo", "reason": "bad_signature", "seq": 1}
    ]
    assert store.query(verified_only=True) == []
    assert [d.verified for d in store.query(verified_only=False)] == [False]
    assert store.raw(5, 1) == bytes(raw)
    assert hooked == []


def test_record_signed_with_another_nodes_key_is_rejected():
    rx, store, _ = rig()
    out = rx.handle(record(node_id=5, key_id=6))
    assert out.reason == "bad_signature"
    assert store.query() == []


def test_replay_is_rejected():
    rx, store, messages = rig()
    raw = record()
    assert rx.handle(raw).accepted
    out = rx.handle(raw)
    assert (out.accepted, out.reason) == (False, "replay")
    assert messages[-1] == {"type": "rejected", "node": "Arroyo Recodo", "reason": "replay", "seq": 1}
    assert len(store.query(verified_only=False)) == 1


def test_forged_future_seq_does_not_block_the_real_record():
    rx, store, _ = rig()
    forged = bytearray(record(seq=2))
    forged[30] ^= 0x01
    assert rx.handle(bytes(forged)).reason == "bad_signature"
    assert rx.handle(record(seq=2)).accepted
    assert [d.seq for d in store.query()] == [2]


def test_oversize_and_malformed_packets():
    rx, store, messages = rig()
    assert rx.handle(b"\x01" * 223).reason == "oversize"
    assert rx.handle(b"\x01" * 50).reason == "malformed"
    assert messages == [
        {"type": "rejected", "node": "Unknown node", "reason": "oversize", "seq": None},
        {"type": "rejected", "node": "Unknown node", "reason": "malformed", "seq": None},
    ]
    assert store.query(verified_only=False) == []


def test_unknown_node_is_rejected_and_kept_as_evidence():
    rx, store, messages = rig()
    out = rx.handle(record(node_id=99))
    assert out.reason == "unknown_node"
    assert messages[0]["node"] == "Unknown node"  # never a raw id
    assert store.query() == [] and len(store.query(verified_only=False)) == 1


def test_tampered_version_byte_is_rejected_as_bad_signature():
    rx, store, messages = rig()
    raw = bytearray(record())
    raw[0] = 0x7F
    assert rx.handle(bytes(raw)).reason == "bad_signature"
    assert messages == [
        {"type": "rejected", "node": "Arroyo Recodo", "reason": "bad_signature", "seq": None}
    ]
    assert store.query(verified_only=False) == []  # unparseable, so not storable


def test_a_failing_hook_does_not_stop_others():
    rx, _, _ = rig()
    seen = []

    def broken(_):
        raise RuntimeError("agent down")

    rx.on_verified.extend([broken, seen.append])
    assert rx.handle(record()).accepted
    assert len(seen) == 1


def test_publishes_to_the_real_station_bus():
    async def scenario():
        queue = bus.subscribe()
        try:
            rx = Receiver(SqliteEventStore(), PUBLIC, WORLD)
            rx.handle(record(seq=7))
            return queue.get_nowait()
        finally:
            bus.unsubscribe(queue)

    msg = asyncio.run(scenario())
    assert msg["type"] == "detection" and msg["detection"]["seq"] == 7
    assert msg["node"] == "Arroyo Recodo"


def test_handle_from_a_thread_publishes_on_the_bound_loop():
    async def scenario():
        loop = asyncio.get_running_loop()
        seen: list[bool] = []
        done = asyncio.Event()

        def publish(msg):
            seen.append(asyncio.get_running_loop() is loop)
            done.set()

        rx = Receiver(SqliteEventStore(), PUBLIC, WORLD, publish=publish)
        rx.bind_loop(loop)
        worker = threading.Thread(target=rx.handle, args=(record(),))
        worker.start()
        await asyncio.wait_for(done.wait(), 2)
        worker.join()
        return seen

    assert asyncio.run(scenario()) == [True]


def test_tamper_flips_a_stored_byte_and_reverifies():
    rx, store, messages = rig()
    rx.handle(record())
    result = rx.tamper(5, 1)
    assert result == {
        "node": "Arroyo Recodo",
        "seq": 1,
        "byte": OFFSET_CONFIDENCE,
        "verified_before": True,
        "verified": False,
    }
    assert store.query() == []  # no longer evidence
    assert messages[-1]["reason"] == "bad_signature"
    assert rx.tamper(5, 1)["verified"] is True  # flipping back restores it
    assert rx.tamper(5, 42) is None


def test_node_to_station_end_to_end_with_loss_shows_seq_gaps(tmp_path):
    """NodeRuntime -> lossy InProcessLink -> Receiver: what arrives verifies; losses are gaps."""
    rx, store, messages = rig()
    sched = ManualScheduler()
    link = InProcessLink(rx.handle, schedule=sched, loss=0.3, seed=11)
    clock = Clock()
    bursts = ([SAW] * 4 + [BG] * 62) * 10  # 10 bursts, 33 s apart
    node = NodeRuntime(CREEK_BEND, ScriptedClassifier(bursts), KEYS[5], tmp_path, link.send, clock)
    for _ in range(10 * 66 + 1):
        clock.t += 0.5
        node.feed(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))
        sched.advance(0.5)
    sched.advance(5)

    assert node.seq == 10
    got = [d.seq for d in store.query(node_ids=[5])]
    assert len(got) == link.stats.sent and 0 < len(got) < 10  # some lost, no retries
    assert all(d.verified for d in store.query())
    assert [m["type"] for m in messages] == ["detection"] * len(got)
