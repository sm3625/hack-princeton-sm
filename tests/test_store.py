import threading
from dataclasses import replace

import pytest

from common.contracts import EventStore
from node.record import pack
from station.store import SqliteEventStore
from tests.pipeline_fakes import T0, detection, keys_for

KEYS = keys_for(1, 5, 6)


def signed(node_id=5, seq=1, verified=True, **kw):
    d = detection(node_id=node_id, seq=seq, **kw)
    raw = pack(d, KEYS[node_id])
    return replace(d, signature=raw[44:].hex(), verified=verified), raw


def test_implements_the_contract():
    assert isinstance(SqliteEventStore(), EventStore)


def test_add_query_and_raw_round_trip():
    store = SqliteEventStore()
    d, raw = signed()
    assert store.add(d, raw)
    assert store.query() == [d]
    assert store.raw(5, 1) == raw
    assert store.raw(5, 2) is None


def test_replay_of_same_node_and_seq_is_refused():
    store = SqliteEventStore()
    d, raw = signed()
    assert store.add(d, raw)
    assert not store.add(d, raw)
    assert len(store.query(verified_only=False)) == 1
    other_node, raw2 = signed(node_id=6, seq=1)
    assert store.add(other_node, raw2)  # same seq, different node: fine


def test_unverified_records_are_kept_but_never_returned_as_verified():
    store = SqliteEventStore()
    forged, raw = signed(seq=9, verified=False)
    assert store.add(forged, raw)
    assert store.add(forged, raw)  # evidence is always appended
    assert store.query() == []
    assert store.query(verified_only=True) == []
    assert [d.verified for d in store.query(verified_only=False)] == [False, False]


def test_forged_record_cannot_squat_on_a_future_seq():
    store = SqliteEventStore()
    forged, raw = signed(seq=3, verified=False)
    store.add(forged, raw)
    real, real_raw = signed(seq=3)
    assert store.add(real, real_raw)
    assert store.query() == [real]
    assert store.raw(5, 3) == real_raw  # the verified copy wins


def test_query_filters_by_node_and_time():
    store = SqliteEventStore()
    for node_id, seq, ts in [(1, 1, T0), (5, 1, T0 + 10), (5, 2, T0 + 20), (6, 1, T0 + 30)]:
        store.add(*signed(node_id=node_id, seq=seq, timestamp=ts))
    key = [(d.node_id, d.seq) for d in store.query(node_ids=[5])]
    assert key == [(5, 1), (5, 2)]
    assert [d.timestamp for d in store.query(since=T0 + 10, until=T0 + 20)] == [T0 + 10, T0 + 20]
    assert [d.node_id for d in store.query(node_ids=[1, 6], since=T0 + 1)] == [6]
    assert store.query(node_ids=[]) == []
    assert [d.timestamp for d in store.query()] == [T0, T0 + 10, T0 + 20, T0 + 30]


def test_query_refuses_non_integer_filters():
    store = SqliteEventStore()
    store.add(*signed())
    with pytest.raises(ValueError):
        store.query(node_ids=["5) OR 1=1 --"])
    with pytest.raises(ValueError):
        store.query(since="0 OR 1=1")


def test_recent_is_newest_first_with_received_at():
    ticks = iter([100.0, 200.0, 300.0])
    store = SqliteEventStore(clock=lambda: next(ticks))
    store.add(*signed(seq=1))
    store.add(*signed(seq=2, verified=False))
    store.add(*signed(seq=3))
    recent = store.recent(limit=2)
    assert [(e.detection.seq, e.received_at) for e in recent] == [(3, 300.0), (2, 200.0)]
    assert [e.detection.seq for e in store.recent(verified_only=True)] == [3, 1]


def test_update_raw_and_clear():
    store = SqliteEventStore()
    d, raw = signed()
    store.add(d, raw)
    event = store.find(5, 1)
    assert store.update_raw(event.row_id, b"\x00" * 108, verified=False)
    assert store.raw(5, 1) == b"\x00" * 108
    assert store.query() == []
    store.clear()
    assert store.query(verified_only=False) == []


def test_persists_to_disk(tmp_path):
    path = tmp_path / "db" / "events.sqlite"
    store = SqliteEventStore(path)
    d, raw = signed()
    store.add(d, raw)
    store.close()
    reopened = SqliteEventStore(path)
    assert reopened.query() == [d]
    assert not reopened.add(d, raw)  # replay protection survives restarts


def test_concurrent_writers_from_threads():
    store = SqliteEventStore()
    results: list[bool] = []

    def worker(start: int) -> None:
        for seq in range(start, start + 50):
            results.append(store.add(*signed(seq=seq % 100 + 1)))

    threads = [threading.Thread(target=worker, args=(i * 25,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.query()) == sum(results) == 100


def test_old_database_without_seen_seqs_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    store = SqliteEventStore(path)
    d, raw = signed()
    assert store.add(d, raw)
    store.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE seen_seqs")
    conn.execute("ALTER TABLE events DROP COLUMN pinned")
    conn.commit()
    conn.close()
    reopened = SqliteEventStore(path)
    assert not reopened.add(d, raw)
    assert reopened.query() == [d]
