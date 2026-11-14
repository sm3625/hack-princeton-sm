"""The 02:14 -> close walkthrough on a real model. Opt-in only:

    make test-ollama   # needs `ollama serve` and the OLLAMA_MODEL pulled (free, local)
    make test-haiku    # calls the Claude API and costs money: only after you OK it

Real models are not scripted, so this checks the safety properties and the overall shape,
not exact wording.
"""

import pytest
from agent_fakes import Harness

from agent.llm import ROLES, get_chat_model
from common.contracts import SoundClass as S
from station.config import load_settings


def run_walkthrough(tmp_path, mode: str, monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODE", mode)
    settings = load_settings()
    h = Harness(tmp_path, models={r: get_chat_model(r, settings) for r in ROLES})
    h.hear("Creek Bend", 41, "02:14:05", S.CHAINSAW, 0.91)
    h.hear("Mossy Gap", 17, "02:14:09", S.CHAINSAW, 0.84)
    h.hear("Logging Road", 23, "02:14:12", S.CHAINSAW, 0.86)
    h.hear("Mossy Gap", 18, "02:14:10", S.CHAINSAW, 0.99, verified=False)  # must never be cited
    h.tick("02:14:20")
    (inc,) = h.commander.incidents.all()
    assert h.file(inc.id, "plan.md"), "commander should write plan.md"
    assert {"triage"} <= {m["agent"] for m in h.of_type("agent_step")}

    # whatever the model decided, nothing was dispatched without a person
    assert h.commander.incidents.get(inc.id).dispatches == []
    pending = h.pending()
    assert pending, "three agreeing nodes and a drone sighting should reach the approval step"
    h.sim.t += 8 * 60
    h.commander.decide(pending[0].id, "approve")
    assert len(h.commander.incidents.get(inc.id).dispatches) >= 1

    for t in ("02:52:00", "03:22:00", "03:52:00", "04:22:00"):
        h.tick(t)
        if h.file(inc.id, "case_file.md") is None and h.commander.incidents.get(inc.id).status == "dispatched":
            h.commander.message(inc.id, "Area is quiet. Close the incident with a case file.")
    if h.file(inc.id, "case_file.md") is not None:
        report = h.commander.case_file(inc.id)
        assert report["citations"]["unknown"] == [], report["citations"]
        assert "Mossy Gap #18" not in h.file(inc.id, "case_file.md")


@pytest.mark.ollama
def test_walkthrough_on_ollama(tmp_path, monkeypatch):
    run_walkthrough(tmp_path, "ollama", monkeypatch)


@pytest.mark.haiku
def test_walkthrough_on_haiku(tmp_path, monkeypatch):
    run_walkthrough(tmp_path, "haiku", monkeypatch)
