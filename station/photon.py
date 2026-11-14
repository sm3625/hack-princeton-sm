"""Opt-in Photon 2.0 ranger bridge. Default invocation only previews local approvals."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Photon's API is behind Cloudflare, which blocks Python's default "Python-urllib" agent
# (HTTP 403, error 1010) before the request reaches Photon. Generic name, no personal data.
USER_AGENT = "forest-guardian-bridge/0.1"


class BridgeError(RuntimeError):
    """Safe to print: never includes provider response bodies or credentials."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(method, url, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = Request(url, data=data, method=method,
                  headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **(headers or {})})
    try:
        with build_opener(NoRedirects).open(req, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        raise BridgeError(f"HTTP {exc.code}; stopped without retry") from None
    except (URLError, OSError, ValueError):
        raise BridgeError("Transport or JSON failure; stopped without retry") from None


def _explain(exc):
    """Photon's own error code/message for --discover. Keys are masked; nothing else is kept."""
    try:
        body = json.loads(exc.read(4096) or b"{}")
    except (OSError, ValueError):
        return ""
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    parts = [str(err.get(k)) for k in ("code", "message", "detail") if isinstance(err, dict) and err.get(k)]
    text = re.sub(r"pho_a?sk_[A-Za-z0-9_-]+", "pho_…(masked)", " ".join(parts))
    return text[:200]


def request_json_explained(method, url, body=None, headers=None):
    """Like request_json, but an HTTP error includes Photon's error code and message."""
    data = None if body is None else json.dumps(body).encode()
    req = Request(url, data=data, method=method,
                  headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **(headers or {})})
    try:
        with build_opener(NoRedirects).open(req, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        why = _explain(exc)
        raise BridgeError(f"HTTP {exc.code}{': ' + why if why else ''}; stopped without retry") from None
    except (URLError, OSError, ValueError):
        raise BridgeError("Transport or JSON failure; stopped without retry") from None


class Station:
    def __init__(self, url="http://127.0.0.1:8000", transport=request_json):
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise BridgeError("Station URL must be a loopback HTTP origin")
        self.url, self.transport = url.rstrip("/"), transport

    def call(self, method, path, body=None):
        return self.transport(method, self.url + path, body)

    def pending(self):
        for incident in self.call("GET", "/api/incidents")["incidents"]:
            view = self.call("GET", f'/api/incidents/{incident["id"]}')
            if approval := view.get("pending_approval"):
                yield approval


@dataclass(frozen=True)
class Target:
    project: str
    resource: str
    space: str
    agent: str
    ranger: str

    @classmethod
    def from_env(cls):
        values = {}
        for name, prefix in (("project", "prj"), ("resource", "res"), ("space", "spc"),
                             ("agent", "usr"), ("ranger", "usr")):
            value = os.getenv(f"PHOTON_{name.upper()}_ID", "").strip()
            if name == "resource" and value == "":
                values[name] = ""  # shared lines carry no resource ID; the space ID pins the chat
                continue
            if not re.fullmatch(rf"pho_{prefix}_[0-7][0-9a-hjkmnp-tv-z]{{25}}", value):
                raise BridgeError(f"Set a valid PHOTON_{name.upper()}_ID")
            values[name] = value
        if values["agent"] == values["ranger"]:
            raise BridgeError("Agent and ranger must be different users")
        return cls(**values)


class Photon:
    """Replaceable transport; no provisioning or account-wide operations."""
    def __init__(self, target, key, allow_sends=0, transport=request_json):
        if not key.startswith("pho_sk_") or any(c.isspace() for c in key):
            raise BridgeError("PHOTON_API_KEY must be a project key")
        self.target, self.remaining, self.transport = target, allow_sends, transport
        self._headers = {"Authorization": f"Bearer {key}"}
        self.url = f"https://api.photon.codes/v1/projects/{target.project}/messages"

    def send(self, text, identity):
        if self.remaining <= 0:
            raise BridgeError("Explicit send allowance exhausted")
        self.remaining -= 1  # Failures count too. Never retry automatically.
        return self.transport("POST", self.url, {
            "senderId": self.target.agent, "spaceId": self.target.space,
            "content": {"type": "text", "format": "plain", "text": text},
        }, {**self._headers, "idempotency-key": identity})

    def page(self, since, before, token=None):
        query = {"direction": "received", "platform": "imessage", "order": "asc",
                     "orderBy": "created_at", "createdSince": since, "createdBefore": before,
                     "pageSize": 100}
        # Photon rejects spaceId on this listing (422 VALIDATION_FAILED, seen 2026-11-14), so the
        # chat is pinned by authorized(): exact space, sender and two participants per message.
        if self.target.resource:
            query["resourceId"] = self.target.resource
        if token:
            query["pageToken"] = token
        return self.transport("GET", self.url + "?" + urlencode(query), None, self._headers)


def stamp(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def notification(approval, code):
    return (f'Forest Guardian, approval {code}\n'
            f'Sector: {approval["sector"]}\n{approval["question"][:500]}\n'
            f'Reply YES {code} or NO {code}. Expires in 15 minutes.')


class Bridge:
    def __init__(self, station, photon, db_path, clock=time.time):
        self.station, self.photon, self.clock = station, photon, clock
        self.db = sqlite3.connect(db_path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS approvals (
                code TEXT PRIMARY KEY, approval TEXT UNIQUE, incident TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, outcome TEXT);
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, text TEXT, code TEXT, state TEXT DEFAULT 'queued');
        """)
        binding = json.dumps(vars(photon.target), sort_keys=True)
        previous = self.meta("target")
        if previous is not None and previous != binding:
            self.db.close()
            raise BridgeError("Target changed; use a separate bridge database")
        self.set_meta("target", binding)
        if self.meta("since") is None:
            self.set_meta("since", stamp(clock()))

    def meta(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def queue(self, identity, text, code=None):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO outbox(id,text,code) VALUES (?,?,?)",
                            (identity, text, code))

    def discover(self):
        for approval in self.station.pending():
            row = self.db.execute("SELECT * FROM approvals WHERE approval=?",
                                  (approval["approval_id"],)).fetchone()
            if row is None:
                # Short per-bridge number (1, 2, 3...), never reused, so a stale reply can't
                # match a newer approval.
                number = int(self.meta("next_code") or 1)
                with self.db:
                    self.db.execute("INSERT INTO approvals VALUES (?,?,?,?)",
                                    (str(number), approval["approval_id"], approval["incident_id"],
                                     self.clock() + 900))
                    self.db.execute("INSERT OR REPLACE INTO meta VALUES ('next_code', ?)",
                                    (str(number + 1),))
                code = str(number)
            else:
                code = row["code"]
            self.queue("approval:" + approval["approval_id"], notification(approval, code), code)

    def current(self, row):
        view = self.station.call("GET", f'/api/incidents/{row["incident"]}')
        pending = view.get("pending_approval")
        return (self.clock() < row["expires"] and pending is not None
                and pending["approval_id"] == row["approval"])

    def flush(self):
        for row in self.db.execute("SELECT * FROM outbox WHERE state='queued' ORDER BY rowid"):
            if self.photon.remaining <= 0:
                break
            if row["code"]:
                approval = self.db.execute("SELECT * FROM approvals WHERE code=?",
                                           (row["code"],)).fetchone()
                if not self.current(approval):
                    with self.db:
                        self.db.execute("UPDATE outbox SET state='expired' WHERE id=?", (row["id"],))
                    continue
            # Commit before IO. A crash/timeout leaves 'attempting', never automatically resent.
            with self.db:
                claimed = self.db.execute(
                    "UPDATE outbox SET state='attempting' WHERE id=? AND state='queued'",
                    (row["id"],),
                ).rowcount
            if not claimed:
                continue
            self.photon.send(row["text"], row["id"])
            with self.db:
                self.db.execute("UPDATE outbox SET state='accepted' WHERE id=?", (row["id"],))

    def authorized(self, message):
        t = self.photon.target
        sender, space = message.get("sentBy") or {}, message.get("space") or {}
        receiver = message.get("receivedBy") or message.get("addressedTo")
        return (message.get("projectId") == t.project and message.get("platform") == "imessage"
                and message.get("status") == "received" and sender.get("id") == t.ranger
                and sender.get("kind") == "human" and space.get("id") == t.space
                and space.get("kind") == "direct"
                and set(space.get("participantIds", [])) == {t.agent, t.ranger}
                and (receiver is None or receiver.get("id") == t.agent)
                and (message.get("content") or {}).get("type") == "text"
                and isinstance(message.get("id"), str))

    def receive(self, message):
        if not self.authorized(message):
            return
        identity = message["id"]
        with self.db:
            inserted = self.db.execute("INSERT OR IGNORE INTO inbox VALUES (?, 'processing')",
                                       (identity,)).rowcount
        if not inserted:
            return
        # Claim first: failures/crashes are left for dashboard reconciliation, not replayed.
        text = message["content"].get("text", "")
        match = re.fullmatch(r"(YES|NO|APPROVE|REJECT)\s+(\d{1,6})(?:\s+(.{1,500}))?",
                             text.strip(), flags=re.IGNORECASE) if isinstance(text, str) else None
        result = "Reply YES <number> or NO <number> [reason]."
        word, number, note = (match[1].upper(), str(int(match[2])), match[3]) if match else (None, None, None)
        if match and not (word in ("YES", "APPROVE") and note):
            decision = "approve" if word in ("YES", "APPROVE") else "reject"
            row = self.db.execute("SELECT * FROM approvals WHERE code=?", (number,)).fetchone()
            if row and self.current(row):
                self.station.call("POST", f'/api/approvals/{row["approval"]}',
                                  {"decision": decision, "note": note or "", "via": "imessage"})
                result = f"{number}: recorded. Check dashboard."
            else:
                result = f"{number}: not open. Check dashboard."
        with self.db:
            self.db.execute("UPDATE inbox SET outcome=? WHERE id=?", (result, identity))
            self.db.execute("INSERT OR IGNORE INTO outbox(id,text) VALUES (?,?)",
                            ("reply:" + identity, result))

    def poll(self):
        since, before, token = self.meta("since"), stamp(self.clock()), None
        for _ in range(5):
            page = self.photon.page(since, before, token)
            for message in page["items"]:
                self.receive(message)
            token = page.get("nextPageToken")
            if not token:
                # Overlap protects boundary timestamps; inbox IDs prevent duplicate actions.
                checkpoint = max(self.clock_from_stamp(since), self.clock_from_stamp(before) - 30)
                self.set_meta("since", stamp(checkpoint))
                return
        raise BridgeError("Polling page limit reached; checkpoint retained, stopped")

    @staticmethod
    def clock_from_stamp(value):
        return datetime.fromisoformat(value).timestamp()


_ID = r"[0-7][0-9a-hjkmnp-tv-z]{25}"


def discover(project, key, transport=request_json_explained):
    """ONE read-only GET: the IDs in recent messages the line received. Sends nothing.

    Photon hands out agent/user/space IDs on incoming messages, so text the line first.
    """
    if not re.fullmatch(rf"pho_prj_{_ID}", project or ""):
        raise BridgeError("Set a valid PHOTON_PROJECT_ID first")
    if not key.startswith("pho_sk_") or any(c.isspace() for c in key):
        raise BridgeError("PHOTON_API_KEY must be a project key")
    query = urlencode({"direction": "received", "platform": "imessage", "order": "desc",
                       "orderBy": "created_at", "pageSize": 10})
    page = transport("GET", f"https://api.photon.codes/v1/projects/{project}/messages?{query}",
                     None, {"Authorization": f"Bearer {key}"})
    found = []
    for m in page.get("items", []):
        sender, space = m.get("sentBy") or {}, m.get("space") or {}
        receiver = m.get("receivedBy") or m.get("addressedTo") or {}
        if sender.get("kind") != "human" or space.get("kind") != "direct":
            continue
        others = [i for i in space.get("participantIds", []) if i != sender.get("id")]
        agent = receiver.get("id") or (others[0] if len(others) == 1 else "")
        details = receiver.get("details") or {}
        resource = details.get("resourceId") or ("" if details.get("type") == "shared" else "?")
        handle = str(sender.get("handle", ""))
        found.append({
            "from": ("…" + handle[-4:]) if len(handle) > 4 else "?",  # enough to recognise, no more
            "text": str((m.get("content") or {}).get("text", ""))[:40],
            "at": m.get("createdAt", ""),
            "PHOTON_RESOURCE_ID": resource,
            "PHOTON_SPACE_ID": space.get("id", ""),
            "PHOTON_AGENT_ID": agent,
            "PHOTON_RANGER_ID": sender.get("id", ""),
        })
    return found


MAX_CYCLES = 720


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Enable Photon reads and ranger decisions")
    parser.add_argument("--allow-sends", type=int, default=0,
                        help="Explicit maximum send attempts this invocation (may cost credits)")
    parser.add_argument("--seconds", type=int, default=600, help="Live duration, maximum 3600")
    parser.add_argument("--poll-seconds", type=int, default=10, help="Minimum 5 seconds")
    parser.add_argument("--db", default="photon.sqlite")
    parser.add_argument("--discover", action="store_true",
                        help="One read-only Photon call: print the IDs from messages you sent the line")
    args = parser.parse_args(argv)
    if args.allow_sends < 0 or (args.allow_sends and not args.live):
        parser.error("--allow-sends requires --live and a nonnegative limit")
    if not 1 <= args.seconds <= 3600 or args.poll_seconds < 5:
        parser.error("--seconds must be 1..3600; --poll-seconds must be at least 5")
    bridge = None
    try:
        from dotenv import load_dotenv
        load_dotenv()
        if args.discover:
            if args.live or args.allow_sends:
                parser.error("--discover cannot be combined with --live or --allow-sends")
            found = discover(os.getenv("PHOTON_PROJECT_ID", ""), os.getenv("PHOTON_API_KEY", ""))
            if not found:
                print("No direct iMessage from a person yet. Text the line (a new chat), then retry.")
                return 1
            print("Most recent first. Copy the block for YOUR message into .env:")
            for f in found:
                print(f"\n# from {f['from']} at {f['at']}: {f['text']!r}")
                for k in ("PHOTON_RESOURCE_ID", "PHOTON_SPACE_ID", "PHOTON_AGENT_ID", "PHOTON_RANGER_ID"):
                    v = f[k]
                    if k == "PHOTON_RESOURCE_ID" and v == "":
                        print(f"{k}=   # shared line: leave empty")
                    else:
                        print(f"{k}={v if v not in ('', '?') else '(not in message; ask)'}")
            return 0
        station = Station(os.getenv("PHOTON_STATION_URL") or "http://127.0.0.1:8000")
        if not args.live:
            print("PREVIEW ONLY — no Photon calls or station mutations")
            for approval in station.pending():
                print(notification(approval, "N"))  # real numbers are assigned by the live bridge
            return 0
        # Credentials never activate the bridge by themselves.
        target = Target.from_env()
        photon = Photon(target, os.getenv("PHOTON_API_KEY", ""), args.allow_sends)
        bridge = Bridge(station, photon, args.db)
        deadline = time.monotonic() + args.seconds
        # Enough cycles to cover the requested duration, never more than MAX_CYCLES
        # (an hour at 5 s; five pages per cycle bounds the GETs).
        cycles = min(MAX_CYCLES, -(-args.seconds // args.poll_seconds))
        for _ in range(cycles):
            if time.monotonic() >= deadline:
                break
            bridge.discover()
            bridge.flush()
            bridge.poll()
            bridge.flush()
            time.sleep(min(args.poll_seconds, max(0, deadline - time.monotonic())))
        print(f"Stopped. Unused send allowance: {photon.remaining}. Check dashboard for outcomes.")
        return 0
    except BridgeError as exc:
        print(f"Photon bridge stopped: {exc}")
        return 1
    finally:
        if bridge:
            bridge.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
