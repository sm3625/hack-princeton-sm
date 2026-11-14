import struct

import pytest

from common.contracts import MAX_PACKET_BYTES, RECORD_BYTES, SoundClass
from node.record import (
    SIGNED_BYTES,
    RecordError,
    clip_hash,
    pack,
    peek_node_id,
    unpack,
    unpack_verified,
    verify,
)
from tests.pipeline_fakes import detection, keys_for

KEYS = keys_for(5, 6)


def test_record_is_exactly_108_bytes_and_fits_lora():
    raw = pack(detection(), KEYS[5])
    assert len(raw) == RECORD_BYTES == 108
    assert SIGNED_BYTES == 44
    assert len(raw) <= MAX_PACKET_BYTES


def test_round_trip():
    d = detection(seq=4_000_000_000, timestamp=4_000_000_001, confidence=0.5, model_version=65535)
    out = unpack(pack(d, KEYS[5]))
    assert (out.node_id, out.seq, out.timestamp) == (5, 4_000_000_000, 4_000_000_001)
    assert out.sound is SoundClass.CHAINSAW
    assert out.model_version == 65535
    assert out.clip_hash == d.clip_hash
    assert abs(out.lat - d.lat) < 1e-7 and abs(out.lon - d.lon) < 1e-7
    assert abs(out.confidence - 0.5) <= 0.5 / 255
    assert len(bytes.fromhex(out.signature)) == 64
    assert out.verified is False  # unpack alone never claims verification


def test_wire_layout_is_little_endian_spec_order():
    raw = pack(detection(node_id=0x0102, seq=0x03040506), keys_for(0x0102)[0x0102])
    assert raw[0] == 1  # version
    assert raw[1:3] == b"\x02\x01"  # node_id u16 LE
    assert raw[3:7] == b"\x06\x05\x04\x03"  # seq u32 LE
    assert struct.unpack("<i", raw[11:15])[0] == round(-3.4443 * 1e7)
    assert raw[19] == SoundClass.CHAINSAW
    assert raw[20] == round(0.9 * 255)
    assert raw[39:44] == b"\x00" * 5  # reserved
    assert peek_node_id(raw) == 0x0102


def test_confidence_is_clamped_to_a_byte():
    assert unpack(pack(detection(confidence=1.7), KEYS[5])).confidence == 1.0
    assert unpack(pack(detection(confidence=-0.2), KEYS[5])).confidence == 0.0


def test_signature_verifies_with_the_right_key_only():
    raw = pack(detection(), KEYS[5])
    assert verify(raw, KEYS[5].verify_key)
    assert not verify(raw, KEYS[6].verify_key)
    assert unpack_verified(raw, KEYS[5].verify_key).verified is True


@pytest.mark.parametrize("index", range(RECORD_BYTES))
def test_flipping_any_single_byte_fails_verification(index):
    raw = bytearray(pack(detection(), KEYS[5]))
    for mask in (0x01, 0xFF):
        tampered = raw.copy()
        tampered[index] ^= mask
        assert not verify(bytes(tampered), KEYS[5].verify_key)


def test_wrong_length_never_verifies_or_parses():
    raw = pack(detection(), KEYS[5])
    assert not verify(raw[:-1], KEYS[5].verify_key)
    assert not verify(raw + b"\x00", KEYS[5].verify_key)
    with pytest.raises(RecordError):
        unpack(raw[:-1])


def test_unknown_version_or_class_is_rejected_by_parser():
    raw = bytearray(pack(detection(), KEYS[5]))
    raw[0] = 2
    with pytest.raises(RecordError, match="version"):
        unpack(bytes(raw))
    raw[0] = 1
    raw[19] = 200
    with pytest.raises(RecordError, match="class"):
        unpack(bytes(raw))


def test_out_of_range_fields_raise():
    with pytest.raises(RecordError):
        pack(detection(node_id=70_000), KEYS[5])
    with pytest.raises(RecordError):
        pack(detection(clip_hash="ab"), KEYS[5])


def test_clip_hash_is_first_16_bytes_of_sha256():
    import hashlib

    assert clip_hash(b"audio") == hashlib.sha256(b"audio").digest()[:16]
