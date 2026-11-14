"""Signature re-checks, clip hashes, citation checks, and sanitized rendering."""

import hashlib
import json

from agent_fakes import Nodes, at

from agent.baseline import baseline_alerts
from agent.evidence import (
    Ed25519Verifier,
    check_citations,
    citations,
    clip_status,
    load_public_keys,
    render_markdown,
)
from common.contracts import SoundClass as S
from common.contracts import load_world


def test_verifier_accepts_only_untouched_records():
    world = load_world()
    nodes = Nodes(world)
    verify = Ed25519Verifier(nodes.public)
    _det, raw = nodes.record("Arroyo Recodo", 41, at("02:14:05"), S.CHAINSAW, 0.91)
    assert verify(5, 41, raw)
    assert not verify(5, 41, raw[:30] + bytes([raw[30] ^ 1]) + raw[31:])  # one flipped byte
    assert not verify(5, 40, raw)  # wrong seq
    assert not verify(8, 41, raw)  # someone else's record
    assert not verify(5, 41, raw[:-1])
    assert not Ed25519Verifier({})(5, 41, raw)


def test_load_public_keys_formats(tmp_path):
    p = tmp_path / "keys.json"
    p.write_text(json.dumps({"5": "ab" * 32}))
    assert load_public_keys(p) == {5: bytes.fromhex("ab" * 32)}
    p.write_text(json.dumps([{"node_id": 7, "public_key": "cd" * 32}]))
    assert load_public_keys(p) == {7: bytes.fromhex("cd" * 32)}
    assert load_public_keys(tmp_path / "missing.json") == {}


def test_clip_status(tmp_path):
    clip = b"audio bytes"
    h = hashlib.sha256(clip).digest()[:16].hex()
    assert clip_status(5, h, tmp_path) == "not collected"
    (tmp_path / "5" / "clips").mkdir(parents=True)
    (tmp_path / "5" / "clips" / "a.wav").write_bytes(clip)
    assert clip_status(5, h, tmp_path) == "match"
    (tmp_path / "5" / "clips" / "a.wav").write_bytes(clip + b"!")
    assert clip_status(5, h, tmp_path) == "missing"


def test_citations():
    text = "Heard [Arroyo Recodo #41] and [Paso Musgo #17]; see [Arroyo Recodo #41]."
    assert citations(text) == [("Arroyo Recodo", 41), ("Paso Musgo", 17), ("Arroyo Recodo", 41)]
    known = {("Arroyo Recodo", 41), ("Paso Musgo", 17)}
    assert check_citations(text, known) == {"ok": True, "cited": 2, "unknown": []}
    assert check_citations(text + " [Cedro Viejo #3]", known)["unknown"] == ["Cedro Viejo #3"]
    assert check_citations("no citations at all", known)["ok"] is False


def test_render_strips_scripts_and_handlers():
    md = (
        "# Case\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(2)>\n\n"
        "[click](javascript:alert(3)) <a href='https://example.org' onclick='x()'>ok</a>\n\n"
        "<iframe src='https://evil'></iframe><style>body{}</style>\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    html = render_markdown(md)
    for bad in ("<script", "alert(1)", "onerror", "javascript:", "onclick", "<iframe", "<style", "<img"):
        assert bad not in html
    assert "<h1>Case</h1>" in html and "<table>" in html
    assert 'href="https://example.org"' in html


def test_baseline_alerts_every_verified_detection_over_threshold():
    world = load_world()
    nodes = Nodes(world)
    dets = [
        nodes.record("El Bajo", 1, at("03:00:00"), S.CHAINSAW, 0.82)[0],
        nodes.record("El Bajo", 2, at("03:05:00"), S.CHAINSAW, 0.79)[0],
        nodes.record("El Bajo", 3, at("03:06:00"), S.BACKGROUND, 0.99)[0],
        nodes.record("Arroyo Recodo", 1, at("02:14:00"), S.CHAINSAW, 0.95, verified=False)[0],
        nodes.record("Arroyo Recodo", 2, at("02:14:05"), S.VEHICLE, 0.80)[0],
    ]
    alerts = baseline_alerts(dets, world)
    assert [(a.node, a.seq) for a in alerts] == [("Arroyo Recodo", 2), ("El Bajo", 1)]
