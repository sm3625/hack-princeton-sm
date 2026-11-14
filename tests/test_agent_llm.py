"""LLM_MODE switch. Builds model objects only; nothing here talks to a model server."""

import pytest
from langchain_core.messages import HumanMessage

from agent.llm import HAIKU_MODEL, ROLES, get_chat_model
from agent.mock import MockChatModel
from station.config import Settings


def settings(mode: str) -> Settings:
    return Settings(llm_mode=mode, ollama_model="qwen3:8b", host="127.0.0.1", port=8000)


def test_mock_mode_gives_a_scripted_model_per_role():
    for role in ROLES:
        model = get_chat_model(role, settings("mock"))
        assert isinstance(model, MockChatModel) and model.role == role
    with pytest.raises(ValueError):
        get_chat_model("general-purpose", settings("mock"))


def test_ollama_mode_builds_chat_ollama(monkeypatch):
    from langchain_ollama import ChatOllama

    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
    model = get_chat_model("triage", settings("ollama"))
    assert isinstance(model, ChatOllama)
    assert model.model == "qwen3:8b" and model.num_ctx == 16384 and model.temperature == 0


def test_haiku_mode_builds_chat_anthropic_without_calling_it(monkeypatch):
    from langchain_anthropic import ChatAnthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder-not-a-key")
    model = get_chat_model("commander", settings("haiku"))
    assert isinstance(model, ChatAnthropic) and model.model == HAIKU_MODEL


def test_mock_refuses_tools_it_was_not_given():
    model = get_chat_model("triage", settings("mock")).bind_tools([])
    model = model.model_copy(update={"bound_tools": ["read_file"]})
    with pytest.raises(ValueError, match="unbound tool"):
        model.invoke([HumanMessage("Incident: x\nSector: E5\nFolder: /incidents/x/")])


def test_gemini_uses_vertex_adc_and_preserves_tool_support(monkeypatch):
    import google.auth
    from google.auth.credentials import AnonymousCredentials
    from langchain_google_genai import ChatGoogleGenerativeAI

    credential = AnonymousCredentials()
    monkeypatch.setattr(google.auth, 'default', lambda **kw: (credential, 'test-project'))
    # An unrelated API key must not switch this integration away from ADC.
    monkeypatch.setenv('GOOGLE_API_KEY', 'unused-placeholder')
    model = get_chat_model('commander', settings('gemini'))
    assert isinstance(model, ChatGoogleGenerativeAI)
    assert model.vertexai is True and model.credentials is credential
    assert model.project == 'test-project' and model.location == 'global'
    assert model.model == 'gemini-3.5-flash'
    assert model.bind_tools([{'name': 'read_weather', 'description': 'Get rain conditions',
                             'parameters': {'type': 'object', 'properties': {}}}])


def test_gemini_missing_adc_has_actionable_error(monkeypatch):
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    from station.config import ConfigError

    def missing(**kw):
        raise DefaultCredentialsError('No credentials')

    monkeypatch.setattr(google.auth, 'default', missing)
    with pytest.raises(ConfigError, match='application-default login'):
        get_chat_model(settings=settings('gemini'))


def test_gemini_requires_project(monkeypatch):
    import google.auth
    from google.auth.credentials import AnonymousCredentials

    from station.config import ConfigError

    monkeypatch.setattr(google.auth, 'default', lambda **kw: (AnonymousCredentials(), None))
    with pytest.raises(ConfigError, match='GOOGLE_CLOUD_PROJECT'):
        get_chat_model(settings=settings('gemini'))
