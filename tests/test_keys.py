import json
import stat

from node.keys import (
    PUBLIC_FILE,
    ensure_keys,
    load_public_keys,
    load_signing_key,
    provision,
)
from tests.pipeline_fakes import WORLD


def test_provision_writes_private_and_public_keys(tmp_path):
    public = provision(WORLD, tmp_path)
    assert set(public) == {n.id for n in WORLD.nodes}
    for node in WORLD.nodes:
        key_file = tmp_path / f"{node.id}.key"
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
        assert load_signing_key(node.id, tmp_path).verify_key.encode().hex() == public[node.id]
    data = json.loads((tmp_path / PUBLIC_FILE).read_text())
    assert data["5"] == {"name": "Creek Bend", "public_key": public[5]}
    assert "private" not in (tmp_path / PUBLIC_FILE).read_text().lower()


def test_provision_is_idempotent(tmp_path):
    first = provision(WORLD, tmp_path)
    before = (tmp_path / "5.key").read_text()
    assert provision(WORLD, tmp_path) == first
    assert (tmp_path / "5.key").read_text() == before


def test_station_side_keys_verify_node_signatures(tmp_path):
    keys = ensure_keys(WORLD, tmp_path)
    signed = load_signing_key(5, tmp_path).sign(b"hello")
    assert keys[5].verify(signed) == b"hello"
    assert load_public_keys(tmp_path).keys() == keys.keys()


def test_ensure_keys_fills_in_a_missing_node(tmp_path):
    provision(WORLD, tmp_path)
    (tmp_path / "3.key").unlink()
    keys = ensure_keys(WORLD, tmp_path)
    assert (tmp_path / "3.key").exists()
    assert keys[3].encode() == load_signing_key(3, tmp_path).verify_key.encode()
