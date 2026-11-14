"""SQLite event store (SPEC 3.5). Implements contracts.EventStore.

Every row keeps the raw 108 bytes, the parsed fields, the verified flag and received_at.
Replay rule: a verified (node_id, seq) is accepted once, ever. The seq is reserved in its own
table (seen_seqs), so it stays taken even if the stored row is later marked unverified (tamper
demo). Unverified records are kept as evidence but never occupy a seq, so forged packets cannot
block the real record from arriving; only the newest MAX_UNVERIFIED of them are kept, so a
sender flooding the port cannot grow the database without limit. Rows that were verified on
arrival (pinned) are never pruned.
All SQL is parameterized. One connection shared across threads behind a lock (sync FastAPI routes
and agent tools run in worker threads).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from common.contracts import Detection, SoundClass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id       INTEGER NOT NULL,
    seq           INTEGER NOT NULL,
    timestamp     INTEGER NOT NULL,
    lat           REAL    NOT NULL,
    lon           REAL    NOT NULL,
    sound         INTEGER NOT NULL,
    confidence    REAL    NOT NULL,
    model_version INTEGER NOT NULL,
    clip_hash     TEXT    NOT NULL,
    signature     TEXT    NOT NULL,
    verified      INTEGER NOT NULL CHECK (verified IN (0, 1)),
    raw           BLOB    NOT NULL,
    received_at   REAL    NOT NULL,
    pinned        INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen_seqs (
    node_id INTEGER NOT NULL,
    seq     INTEGER NOT NULL,
    PRIMARY KEY (node_id, seq)
) WITHOUT ROWID;
CREATE UNIQUE INDEX IF NOT EXISTS events_verified_once ON events (node_id, seq) WHERE verified = 1;
CREATE INDEX IF NOT EXISTS events_by_time ON events (timestamp);
"""

MAX_UNVERIFIED = 500  # forged / unknown-node rows kept as evidence (newest win)

_COLUMNS = (
    "id, node_id, seq, timestamp, lat, lon, sound, confidence, model_version, clip_hash, "
    "signature, verified, raw, received_at"
)


@dataclass(frozen=True)
class StoredEvent:
    row_id: int
    detection: Detection
    raw: bytes
    received_at: float


def _detection(row: sqlite3.Row) -> Detection:
    return Detection(
        node_id=row["node_id"],
        seq=row["seq"],
        timestamp=row["timestamp"],
        lat=row["lat"],
        lon=row["lon"],
        sound=SoundClass(row["sound"]),
        confidence=row["confidence"],
        model_version=row["model_version"],
        clip_hash=row["clip_hash"],
        signature=row["signature"],
        verified=bool(row["verified"]),
    )


def _stored(row: sqlite3.Row) -> StoredEvent:
    return StoredEvent(row["id"], _detection(row), bytes(row["raw"]), row["received_at"])


class SqliteEventStore:
    def __init__(
        self,
        path: str | Path = ":memory:",
        clock: Callable[[], float] = time.time,
        max_unverified: int = MAX_UNVERIFIED,
    ):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.max_unverified = max(0, int(max_unverified))
        self.pruned = 0  # unverified rows dropped by the cap (this process)
        self._unverified_adds = 0
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            # databases from before the pinned column / seen_seqs table
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(events)")}
            if "pinned" not in cols:
                self._conn.execute("ALTER TABLE events ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
                self._conn.execute("UPDATE events SET pinned = 1 WHERE verified = 1")
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_seqs (node_id, seq) "
                "SELECT node_id, seq FROM events WHERE verified = 1 OR pinned = 1"
            )

    # ---------- contracts.EventStore ----------

    def add(self, detection: Detection, raw: bytes) -> bool:
        d = detection
        try:
            with self._lock, self._conn:
                if d.verified:  # reserve the seq first; a second claim raises IntegrityError
                    self._conn.execute(
                        "INSERT INTO seen_seqs (node_id, seq) VALUES (?, ?)", (d.node_id, d.seq)
                    )
                self._conn.execute(
                    "INSERT INTO events (node_id, seq, timestamp, lat, lon, sound, confidence, "
                    "model_version, clip_hash, signature, verified, raw, received_at, pinned) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        d.node_id,
                        d.seq,
                        d.timestamp,
                        d.lat,
                        d.lon,
                        int(d.sound),
                        d.confidence,
                        d.model_version,
                        d.clip_hash,
                        d.signature,
                        int(d.verified),
                        bytes(raw),
                        self._clock(),
                        int(d.verified),
                    ),
                )
        except sqlite3.IntegrityError:
            return False  # verified (node_id, seq) already seen: replay
        if not d.verified:
            self._unverified_adds += 1
            if self._unverified_adds % 32 == 1:  # amortised: the cap may overshoot by 31 rows
                self._prune_unverified()
        return True

    def _prune_unverified(self) -> None:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM events WHERE verified = 0 AND pinned = 0 AND id <= COALESCE(("
                "SELECT id FROM events WHERE verified = 0 AND pinned = 0 "
                "ORDER BY id DESC LIMIT 1 OFFSET ?), -1)",
                (self.max_unverified,),
            )
            self.pruned += cur.rowcount

    def unverified_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM events WHERE verified = 0").fetchone()[0]

    def query(
        self,
        node_ids: list[int] | None = None,
        since: int | None = None,
        until: int | None = None,
        verified_only: bool = True,
    ) -> list[Detection]:
        """Detections ordered by timestamp. since/until are inclusive unix seconds."""
        where: list[str] = []
        params: list[object] = []
        if verified_only:
            where.append("verified = 1")
        if node_ids is not None:
            if not node_ids:
                return []
            # Only "?" placeholders are formatted in; the ids themselves are bound parameters.
            where.append(f"node_id IN ({', '.join('?' for _ in node_ids)})")
            params.extend(int(n) for n in node_ids)
        if since is not None:
            where.append("timestamp >= ?")
            params.append(int(since))
        if until is not None:
            where.append("timestamp <= ?")
            params.append(int(until))
        sql = f"SELECT {_COLUMNS} FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY timestamp, node_id, seq, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_detection(r) for r in rows]

    def raw(self, node_id: int, seq: int) -> bytes | None:
        event = self.find(node_id, seq)
        return event.raw if event else None

    # ---------- extras for the station ----------

    def find(self, node_id: int, seq: int) -> StoredEvent | None:
        """The record for (node_id, seq): the verified one if any, else the first received."""
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM events WHERE node_id = ? AND seq = ? "
                "ORDER BY verified DESC, pinned DESC, id LIMIT 1",
                (node_id, seq),
            ).fetchone()
        return _stored(row) if row else None

    def recent(self, limit: int = 50, verified_only: bool = False) -> list[StoredEvent]:
        """Most recently received first."""
        sql = f"SELECT {_COLUMNS} FROM events"
        if verified_only:
            sql += " WHERE verified = 1"
        sql += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (int(limit),)).fetchall()
        return [_stored(r) for r in rows]

    def update_raw(self, row_id: int, raw: bytes, verified: bool) -> bool:
        """Replace a row's raw bytes and verified flag (tamper demo). False if that would make
        a second verified copy of the same (node_id, seq)."""
        try:
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "UPDATE events SET raw = ?, verified = ? WHERE id = ?",
                    (bytes(raw), int(verified), row_id),
                )
        except sqlite3.IntegrityError:
            return False
        return cur.rowcount == 1

    def clear(self) -> None:
        """Delete every event (e.g. before replaying a scenario from seq 1)."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM events")
            self._conn.execute("DELETE FROM seen_seqs")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
