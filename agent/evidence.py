"""Evidence checks in plain code: signatures, clip hashes, case-file citations, safe rendering.

The LLM writes case files; code decides whether they hold up.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import markdown
import nh3

from common.contracts import RECORD_BYTES, ROOT, EventStore, World

# SPEC 3.3: 44 signed bytes, then a 64-byte Ed25519 signature.
_SIGNED = struct.Struct("<BHIIiiBBH16s5s")
SIGNED_BYTES = _SIGNED.size  # 44

# verify(node_id, seq, raw) -> True only if raw is that node's record and its signature checks.
Verifier = Callable[[int, int, bytes], bool]

CITATION = re.compile(r"\[([^\[\]#\n]+?) #(\d+)\]")


def load_public_keys(path: Path = ROOT / "station" / "node_keys.json") -> dict[int, bytes]:
    """Accepts {"<node_id>": "<hex>"} or [{"id"|"node_id": n, "public_key": "<hex>"}]."""
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    if isinstance(data, dict) and "nodes" in data:
        data = data["nodes"]
    if isinstance(data, dict):
        return {int(k): bytes.fromhex(v) for k, v in data.items()}
    out = {}
    for row in data:
        node_id = row.get("id", row.get("node_id"))
        out[int(node_id)] = bytes.fromhex(row.get("public_key") or row["pubkey"])
    return out


class Ed25519Verifier:
    """Re-checks a stored 108-byte record against the node's public key."""

    def __init__(self, public_keys: dict[int, bytes] | None = None) -> None:
        self.keys = public_keys if public_keys is not None else load_public_keys()

    def __call__(self, node_id: int, seq: int, raw: bytes) -> bool:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey

        if raw is None or len(raw) != RECORD_BYTES or node_id not in self.keys:
            return False
        fields = _SIGNED.unpack(raw[:SIGNED_BYTES])
        if fields[1] != node_id or fields[2] != seq:
            return False
        try:
            VerifyKey(self.keys[node_id]).verify(raw[:SIGNED_BYTES], raw[SIGNED_BYTES:RECORD_BYTES])
        except (BadSignatureError, ValueError):
            return False
        return True


def clip_status(node_id: int, clip_hash: str, clips_root: Path = ROOT / "node_data") -> str:
    """'match' if a stored clip has this hash, 'missing' if clips exist but none match,
    'not collected' if the node's clips were never collected."""
    clip_dir = clips_root / str(node_id) / "clips"
    if not clip_dir.is_dir():
        return "not collected"
    for path in clip_dir.iterdir():
        if path.is_file() and hashlib.sha256(path.read_bytes()).digest()[:16].hex() == clip_hash:
            return "match"
    return "missing"


@dataclass
class RecordCheck:
    tag: str
    valid: bool
    clip: str


@dataclass
class Integrity:
    records: list[RecordCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.valid and r.clip != "missing" for r in self.records)

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "checked": len(self.records),
            "valid": sum(r.valid for r in self.records),
            "invalid": [r.tag for r in self.records if not r.valid],
            "clips_missing": [r.tag for r in self.records if r.clip == "missing"],
            "clips_matched": sum(r.clip == "match" for r in self.records),
        }


def check_records(
    world: World,
    store: EventStore,
    verify: Verifier,
    records: list[list[int]],
    clips_root: Path = ROOT / "node_data",
) -> Integrity:
    """Re-verify every record of an incident from its raw bytes (and its clip, if collected)."""
    out = Integrity()
    by_key = {(d.node_id, d.seq): d for d in store.query(verified_only=False)}
    for node_id, seq in records:
        tag = f"{world.node(node_id).name} #{seq}"
        raw = store.raw(node_id, seq)
        valid = bool(raw) and verify(node_id, seq, raw)
        det = by_key.get((node_id, seq))
        clip = clip_status(node_id, det.clip_hash, clips_root) if det else "missing"
        out.records.append(RecordCheck(tag=tag, valid=valid, clip=clip))
    return out


def citations(text: str) -> list[tuple[str, int]]:
    return [(m.group(1).strip(), int(m.group(2))) for m in CITATION.finditer(text)]


def check_citations(text: str, known: set[tuple[str, int]]) -> dict:
    """Every [Node Name #seq] in a case file must be a verified record of this incident."""
    cited = citations(text)
    unknown = sorted({f"{n} #{s}" for n, s in cited if (n, s) not in known})
    return {"ok": bool(cited) and not unknown, "cited": len(set(cited)), "unknown": unknown}


_TAGS = {
    "p", "h1", "h2", "h3", "h4", "ul", "ol", "li", "strong", "em", "code", "pre", "blockquote",
    "table", "thead", "tbody", "tr", "th", "td", "hr", "br", "a",
}  # fmt: skip


def render_markdown(text: str) -> str:
    """Markdown -> HTML, sanitized. Case files and plans are LLM output: treat as untrusted."""
    html = markdown.markdown(text or "", extensions=["tables", "fenced_code"])
    return nh3.clean(
        html,
        tags=_TAGS,
        attributes={"a": {"href"}},
        url_schemes={"https"},
        clean_content_tags={"script", "style"},
    )
