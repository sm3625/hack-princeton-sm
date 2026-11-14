import socket
import sys
import types

import numpy as np
import pytest

from common.contracts import SAMPLE_RATE
from node.keys import load_public_keys
from node.mic import find_node, main
from node.record import unpack, verify
from node.runtime import wav_bytes
from tests.pipeline_fakes import WORLD, LoudnessClassifier


@pytest.fixture
def fake_model(monkeypatch):
    """Stand-in for the classifier track's node/classifier.py."""
    module = types.ModuleType("node.classifier")

    class TFLiteClassifier(LoudnessClassifier):
        def __init__(self, model_path=None):
            self.model_path = model_path

    module.TFLiteClassifier = TFLiteClassifier
    monkeypatch.setitem(sys.modules, "node.classifier", module)


def test_find_node_by_name_case_insensitive():
    assert find_node(WORLD, "creek bend").id == 5
    with pytest.raises(SystemExit, match="Ridge North"):
        find_node(WORLD, "Nowhere")


def test_wav_mode_sends_a_signed_record_over_udp(tmp_path, fake_model, capsys, caplog):
    caplog.set_level("INFO")
    rng = np.random.default_rng(0)
    audio = np.concatenate(
        [np.zeros(SAMPLE_RATE * 2), rng.normal(0, 0.3, SAMPLE_RATE * 4), np.zeros(SAMPLE_RATE * 2)]
    ).astype(np.float32)
    wav = tmp_path / "chainsaw.wav"
    wav.write_bytes(wav_bytes(audio))

    station = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    station.bind(("127.0.0.1", 0))
    station.settimeout(5)
    try:
        main(
            [
                "--node", "Creek Bend",
                "--wav", str(wav),
                "--port", str(station.getsockname()[1]),
                "--loss", "0",
                "--min-latency-ms", "0",
                "--max-latency-ms", "0",
                "--keys-dir", str(tmp_path / "keys"),
                "--clip-dir", str(tmp_path / "node_data"),
            ]
        )  # fmt: skip
        packet, _ = station.recvfrom(1024)
    finally:
        station.close()

    assert len(packet) == 108
    assert verify(packet, load_public_keys(tmp_path / "keys")[5])
    d = unpack(packet)
    assert (d.node_id, d.seq, d.sound.name) == (5, 1, "CHAINSAW")
    assert (tmp_path / "node_data" / "5" / "1.wav").exists()
    assert "model 40000/100000 bytes" in caplog.text  # device limits are logged
    assert "Creek Bend: chainsaw 0.92 seq 1" in caplog.text
    assert "radio sent 1, lost 0" in capsys.readouterr().out
