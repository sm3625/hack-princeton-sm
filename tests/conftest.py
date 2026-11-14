import pytest


@pytest.fixture(autouse=True)
def _isolated_node_data(monkeypatch, tmp_path_factory):
    """Station lifespans start node runtimes; keep their clip folders out of the repo."""
    monkeypatch.setenv("NODE_DATA_DIR", str(tmp_path_factory.mktemp("node_data")))
