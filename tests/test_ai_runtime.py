"""Garante que cada thread do worker usa um cliente OpenAI próprio.

Regressão: com o provedor padrão do openai-agents, todas as threads
compartilhavam um único cliente HTTP global, e chamadas da SulIA falhavam com
``RuntimeError: ... is bound to a different event loop``.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

from app.services import ai_runtime


class _FakeAsyncOpenAI:
    def __init__(self, api_key=None):
        self.api_key = api_key


class _FakeMultiProvider:
    def __init__(self, openai_client=None):
        self.openai_client = openai_client


class _FakeRunConfig:
    def __init__(self, model_provider=None):
        self.model_provider = model_provider


class _FakeRunner:
    calls: list[tuple] = []

    @classmethod
    def run_sync(cls, agent, prompt, run_config=None):
        cls.calls.append((agent, prompt, run_config))
        return types.SimpleNamespace(final_output="ok")


@pytest.fixture
def fake_sdk(monkeypatch):
    agents = types.ModuleType("agents")
    agents.MultiProvider = _FakeMultiProvider
    agents.RunConfig = _FakeRunConfig
    agents.Runner = _FakeRunner
    openai = types.ModuleType("openai")
    openai.AsyncOpenAI = _FakeAsyncOpenAI
    monkeypatch.setitem(sys.modules, "agents", agents)
    monkeypatch.setitem(sys.modules, "openai", openai)
    monkeypatch.setattr(ai_runtime, "_thread_state", threading.local())
    _FakeRunner.calls = []
    return agents


def _provider_in_new_thread():
    holder = {}

    def target():
        holder["first"] = ai_runtime.thread_model_provider()
        holder["second"] = ai_runtime.thread_model_provider()

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    return holder


def test_same_thread_reuses_provider_and_client(fake_sdk):
    first = ai_runtime.thread_model_provider()
    second = ai_runtime.thread_model_provider()
    assert first is second
    assert first.openai_client is second.openai_client


def test_each_thread_gets_its_own_openai_client(fake_sdk):
    main_provider = ai_runtime.thread_model_provider()
    other = _provider_in_new_thread()
    another = _provider_in_new_thread()

    assert other["first"] is other["second"]
    clients = {
        id(main_provider.openai_client),
        id(other["first"].openai_client),
        id(another["first"].openai_client),
    }
    assert len(clients) == 3


def test_run_agent_sync_uses_thread_provider(fake_sdk):
    result = ai_runtime.run_agent_sync("agente", "prompt")

    assert result.final_output == "ok"
    assert len(_FakeRunner.calls) == 1
    agent, prompt, run_config = _FakeRunner.calls[0]
    assert (agent, prompt) == ("agente", "prompt")
    assert run_config.model_provider is ai_runtime.thread_model_provider()


def test_services_no_longer_call_runner_directly():
    from pathlib import Path

    services = Path(ai_runtime.__file__).parent
    offenders = [
        path.name
        for path in services.glob("ai_*.py")
        if path.name != "ai_runtime.py" and "Runner.run_sync(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
