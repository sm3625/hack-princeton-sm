import asyncio
import random

import pytest

from common.contracts import MAX_PACKET_BYTES
from transport import (
    InProcessLink,
    ManualScheduler,
    PacketTooLarge,
    UdpLink,
    immediate_scheduler,
    start_udp_listener,
)


def test_default_loss_is_about_5_percent_and_seeded():
    got: list[bytes] = []
    link = InProcessLink(got.append, schedule=immediate_scheduler, seed=42)
    results = [link.send(b"x") for _ in range(10_000)]
    assert link.loss == 0.05
    assert 400 < link.stats.dropped < 600
    assert link.stats.dropped + link.stats.sent == 10_000
    assert len(got) == link.stats.sent == sum(results)

    again = InProcessLink([].append, schedule=immediate_scheduler, seed=42)
    assert [again.send(b"x") for _ in range(10_000)] == results


def test_latency_is_within_range_and_delivers_in_time_order():
    sched = ManualScheduler()
    got: list[tuple[float, bytes]] = []
    link = InProcessLink(lambda p: got.append((sched.now, p)), schedule=sched, loss=0.0, seed=1)
    for i in range(200):
        assert link.send(bytes([i]))
    assert got == [] and sched.pending == 200  # nothing arrives before its latency
    sched.advance(0.099)
    assert got == []
    sched.advance(2.0)
    assert len(got) == 200 and sched.pending == 0
    times = [t for t, _ in got]
    assert all(0.1 <= t <= 2.0 for t in times)
    assert times == sorted(times)
    assert max(times) - min(times) > 1.0  # actually spread, not constant


def test_custom_loss_and_latency():
    sched = ManualScheduler()
    got: list[float] = []
    link = InProcessLink(
        lambda p: got.append(sched.now), schedule=sched, loss=0.5, latency_ms=(300, 300), seed=3
    )
    sent = sum(link.send(b"x") for _ in range(1000))
    sched.advance(1.0)
    assert 400 < sent < 600
    assert got == [pytest.approx(0.3)] * sent


def test_oversize_payload_is_rejected_not_sent():
    got: list[bytes] = []
    link = InProcessLink(got.append, schedule=immediate_scheduler, loss=0.0)
    assert link.send(b"a" * MAX_PACKET_BYTES)
    with pytest.raises(PacketTooLarge):
        link.send(b"a" * (MAX_PACKET_BYTES + 1))
    assert link.stats.rejected == 1
    assert len(got) == 1


def test_no_retries_dropped_packets_never_arrive():
    sched = ManualScheduler()
    got: list[bytes] = []
    link = InProcessLink(got.append, schedule=sched, loss=1.0)
    assert link.send(b"lost") is False
    sched.advance(60)
    assert got == [] and sched.pending == 0


def test_invalid_settings_rejected():
    with pytest.raises(ValueError):
        InProcessLink(print, loss=1.5)
    with pytest.raises(ValueError):
        InProcessLink(print, latency_ms=(500, 100))


def test_inprocess_link_delivers_on_the_running_loop():
    async def scenario():
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        got: list[tuple[bytes, bool]] = []

        def deliver(p: bytes) -> None:
            got.append((p, asyncio.get_running_loop() is loop))
            done.set()

        link = InProcessLink(deliver, loss=0.0, latency_ms=(1, 5))
        await asyncio.to_thread(link.send, b"from a worker thread")
        await asyncio.wait_for(done.wait(), 2)
        return got

    assert asyncio.run(scenario()) == [(b"from a worker thread", True)]


def test_udp_round_trip_over_localhost():
    async def scenario():
        got: list[bytes] = []
        arrived = asyncio.Event()

        def deliver(p: bytes) -> None:
            got.append(p)
            if len(got) == 3:
                arrived.set()

        transport = await start_udp_listener(deliver, "127.0.0.1", 0)
        port = transport.get_extra_info("sockname")[1]
        link = UdpLink("127.0.0.1", port, loss=0.0, latency_ms=(0, 30), rng=random.Random(5))
        try:
            for p in (b"one", b"two", b"three"):
                assert link.send(p)
            with pytest.raises(PacketTooLarge):
                link.send(b"z" * 300)
            await asyncio.wait_for(arrived.wait(), 3)
        finally:
            await asyncio.to_thread(link.close)
            transport.close()
        return got

    assert sorted(asyncio.run(scenario())) == [b"one", b"three", b"two"]


def test_udp_listener_hands_oversize_datagrams_to_the_receiver():
    """The station must see (and reject) oversize packets that bypass a well-behaved node."""

    async def scenario():
        got: list[bytes] = []
        arrived = asyncio.Event()

        def deliver(p: bytes) -> None:
            got.append(p)
            arrived.set()

        transport = await start_udp_listener(deliver, "127.0.0.1", 0)
        port = transport.get_extra_info("sockname")[1]
        link = UdpLink("127.0.0.1", port, loss=0.0, latency_ms=(0, 0), max_bytes=2000)
        link.send(b"q" * 500)
        await asyncio.wait_for(arrived.wait(), 3)
        link.close()
        transport.close()
        return got

    assert [len(p) for p in asyncio.run(scenario())] == [500]
