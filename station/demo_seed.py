"""One finished incident for the start of a demo run.

A scenario's `earlier:` block describes it. Its records are signed with the nodes' own keys and
stored like any other record, so the case file passes the same signature and citation checks as
one written during the run. No model runs here: the files are written by this code.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import time

from agent.incidents import Incident, fmt_time
from agent.mock import _case_file
from common.contracts import Detection, SoundClass
from node.keys import load_signing_key
from node.record import pack, unpack

SEED_SEQ = 900_000  # far above any seq a node runtime reaches, so a live record never collides
CAMERA_LIMIT = "One camera visit; limited time and coverage. Does not establish legality."


def _records(commander, earlier, t0: int, key_for) -> list[Detection]:
    """Each node hears it once, then the first node again seven minutes later."""
    world, model = commander.world, _model_version()
    plan = [(node_id, t0 + 11 * i) for i, node_id in enumerate(earlier.node_ids)]
    plan.append((earlier.node_ids[0], t0 + 7 * 60))
    out = []
    for i, (node_id, ts) in enumerate(plan):
        node = world.node(node_id)
        d = Detection(
            node_id=node_id, seq=SEED_SEQ + i, timestamp=ts, lat=node.lat, lon=node.lon,
            sound=SoundClass[earlier.sound.upper()], confidence=round(0.93 - 0.03 * (i % 3), 2),
            model_version=model,
            clip_hash=hashlib.sha256(f"earlier:{node_id}:{ts}".encode()).digest()[:16].hex(),
        )
        raw = pack(d, key_for(node_id))
        stored = unpack(raw, verified=commander.services.verify(node_id, d.seq, raw))
        if stored.verified and commander.store.add(stored, raw):
            out.append(stored)
    return out


def _model_version() -> int:
    from common.contracts import ROOT

    try:
        return int(json.loads((ROOT / "models" / "classifier.json").read_text())["model_version"])
    except (OSError, KeyError, ValueError):
        return 1


def _local(iso_utc: str) -> str:
    ts = calendar.timegm(time.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ"))
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def seed_closed_incident(commander, earlier, start: float, key_for=load_signing_key) -> Incident | None:
    """Create the incident, its evidence files and a case file, then close it. Returns None,
    writing no case file, if the records were not stored (already seeded) or did not group
    into one incident (nodes too far apart)."""
    t0 = int(start - earlier.minutes_before * 60)
    incidents, files = commander.incidents, commander.files
    # Quiet while it is being built: the dashboard should only ever see it finished.
    notify, incidents.on_change = incidents.on_change, None
    try:
        added = [incidents.add(d) for d in _records(commander, earlier, t0, key_for)]
    finally:
        incidents.on_change = notify
    ids = {inc.id for inc in added if inc is not None}
    if len(ids) != 1:
        return None
    inc = incidents.get(ids.pop())
    folder, sector = inc.folder, inc.sector
    names = sorted(commander.world.node(n).name for n in inc.node_ids())
    rows = [json.loads(line) for line in (files.read(folder + "detections.jsonl") or "").splitlines()]
    cites = " ".join(r["record"] for r in rows)

    files.write(
        folder + "triage.md",
        f"# Triage\n\nVerdict: real. {earlier.sound.capitalize()} heard by {len(names)} nodes in dry "
        f"weather ({', '.join(names)}), repeated after seven minutes. {cites}\n",
    )
    flown = bool(earlier.seen) and commander.deployment.has_drones
    observations = []
    if flown:
        seen_at = t0 + 9 * 60
        files.write(folder + "drone_log.md", f"## {fmt_time(seen_at)} flight to {sector}\n- camera: {earlier.seen}\n\n")
        observations.append({
            "source": "drone", "timestamp": seen_at, "sector": sector, "availability": "observed",
            "content": earlier.seen, "requires_ranger_review": False, "limitation": CAMERA_LIMIT,
        })

    sent = t0 + 14 * 60
    note = f"{earlier.sound.capitalize()} heard by {len(names)} nodes" + (f"; drone saw {earlier.seen}" if flown else "")
    files.append_jsonl(folder + "approvals.jsonl", {
        "time": fmt_time(sent), "decision": "approved", "by": "ranger", "via": "dashboard",
        "sector": sector, "note": note,
    })
    dispatch = {
        "time": fmt_time(sent), "timestamp": sent, "sector": sector, "note": note,
        "limitation": CAMERA_LIMIT if flown else "No recorded aerial verification.",
        "records": [{"node": commander.world.node(n).name, "seq": seq} for n, seq in inc.records],
    }
    files.write(
        folder + "plan.md",
        f"# {inc.title}\n\n- [x] Triage: real threat\n"
        + ("- [x] Drone check\n" if flown else "")
        + "- [x] Ranger approved dispatch\n- [x] Area quiet on follow-up\n- [x] Case file\n",
    )
    local = [{**r, "time": _local(r["time"])} for r in rows]  # station clock, like the rest of the page
    files.write(folder + "case_file.md", _case_file(
        {"incident": inc.id, "sector": sector}, local, {r["record"] for r in rows},
        files.read(folder + "triage.md") or "", files.read(folder + "drone_log.md") or "",
        [{"time": fmt_time(sent), "decision": "approved", "by": "ranger", "note": note}],
    ))
    closed = sent + commander.deployment.agent.check_interval_min * 60 + 60
    return incidents.update(
        inc.id, status="closed", summary="Area quiet; case file written", updated_at=closed,
        dispatches=[dispatch], observations=observations, next_check_at=None, earlier=True,
    )
