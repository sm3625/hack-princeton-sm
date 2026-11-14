"""The 108-byte signed detection record (SPEC 3.3).

Layout, little-endian, no padding:

    offset size field
    0      1    version (0x01)
    1      2    node_id      u16
    3      4    seq          u32
    7      4    timestamp    u32 unix seconds
    11     4    lat          i32 degrees x 1e7
    15     4    lon          i32 degrees x 1e7
    19     1    class        u8 (SoundClass)
    20     1    confidence   u8 0..255
    21     2    model_ver    u16
    23     16   clip_hash    first 16 bytes of SHA-256 of the stored clip file
    39     5    reserved     zero
    44     64   signature    Ed25519 over bytes 0..43
"""

from __future__ import annotations

import hashlib
import struct

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from common.contracts import RECORD_BYTES, Detection, SoundClass

VERSION = 1
# "<" = standard sizes, no alignment padding. Without it struct pads to native alignment.
_HEADER = struct.Struct("<BHIIiiBBH16s5x")
SIGNED_BYTES = _HEADER.size  # 44
SIGNATURE_BYTES = 64
CLIP_HASH_BYTES = 16
COORD_SCALE = 10_000_000

# Byte offsets, used by the tamper demo and tests.
OFFSET_NODE_ID = 1
OFFSET_CONFIDENCE = 20

assert SIGNED_BYTES + SIGNATURE_BYTES == RECORD_BYTES


class RecordError(ValueError):
    """Bytes are not a well-formed v1 record."""


def clip_hash(clip_bytes: bytes) -> bytes:
    """First 16 bytes of SHA-256: what goes in the record for a stored clip."""
    return hashlib.sha256(clip_bytes).digest()[:CLIP_HASH_BYTES]


def _confidence_byte(confidence: float) -> int:
    return max(0, min(255, round(confidence * 255)))


def _coord(deg: float) -> int:
    return round(deg * COORD_SCALE)


def pack_body(d: Detection) -> bytes:
    """The 44 signed bytes for a detection (signature and verified are ignored)."""
    digest = bytes.fromhex(d.clip_hash)
    if len(digest) != CLIP_HASH_BYTES:
        raise RecordError(f"clip_hash must be {CLIP_HASH_BYTES} bytes, got {len(digest)}")
    try:
        return _HEADER.pack(
            VERSION,
            d.node_id,
            d.seq,
            d.timestamp,
            _coord(d.lat),
            _coord(d.lon),
            int(d.sound),
            _confidence_byte(d.confidence),
            d.model_version,
            digest,
        )
    except struct.error as e:
        raise RecordError(f"field out of range: {e}") from e


def pack(d: Detection, signing_key: SigningKey) -> bytes:
    """Sign and serialise a detection into exactly RECORD_BYTES bytes."""
    body = pack_body(d)
    signature = signing_key.sign(body).signature
    return body + signature


def peek_node_id(raw: bytes) -> int:
    """node_id from a record without checking anything else (to pick the public key)."""
    if len(raw) < OFFSET_NODE_ID + 2:
        raise RecordError("too short for a node id")
    return int.from_bytes(raw[OFFSET_NODE_ID : OFFSET_NODE_ID + 2], "little")


def unpack(raw: bytes, verified: bool = False) -> Detection:
    """Parse a record. Does not check the signature; use verify() or unpack_verified()."""
    if len(raw) != RECORD_BYTES:
        raise RecordError(f"record must be {RECORD_BYTES} bytes, got {len(raw)}")
    (version, node_id, seq, ts, lat, lon, cls, conf, model_ver, digest) = _HEADER.unpack(
        raw[:SIGNED_BYTES]
    )
    if version != VERSION:
        raise RecordError(f"unsupported record version {version}")
    try:
        sound = SoundClass(cls)
    except ValueError as e:
        raise RecordError(f"unknown sound class {cls}") from e
    return Detection(
        node_id=node_id,
        seq=seq,
        timestamp=ts,
        lat=lat / COORD_SCALE,
        lon=lon / COORD_SCALE,
        sound=sound,
        confidence=conf / 255,
        model_version=model_ver,
        clip_hash=digest.hex(),
        signature=raw[SIGNED_BYTES:].hex(),
        verified=verified,
    )


def verify(raw: bytes, verify_key: VerifyKey) -> bool:
    """True only if raw is a full record whose signature matches its first 44 bytes."""
    if len(raw) != RECORD_BYTES:
        return False
    try:
        verify_key.verify(raw[:SIGNED_BYTES], raw[SIGNED_BYTES:])
    except BadSignatureError:
        return False
    return True


def unpack_verified(raw: bytes, verify_key: VerifyKey) -> Detection:
    """Parse and set verified from the signature check."""
    return unpack(raw, verified=verify(raw, verify_key))
