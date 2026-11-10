"""Execução síncrona dos agentes OpenAI com isolamento por thread.

O ``Runner.run_sync`` da biblioteca ``openai-agents`` mantém um event loop
fixo por thread, mas o provedor padrão reutiliza um único cliente HTTP global
(``shared_http_client``) para todas as threads. O worker da SulIA executa SDR,
Vendedor, Supervisor/Guardião, autocorretor, aprendizado e engenheiro em
threads diferentes; quando uma thread reaproveita uma conexão aberta no loop de
outra, a chamada falha com ``RuntimeError: ... is bound to a different event
loop``.

Este módulo mantém um provedor (e, portanto, um ``AsyncOpenAI`` com pool de
conexões próprio) por thread. Dentro da mesma thread as conexões continuam
sendo reaproveitadas; entre threads nada é compartilhado.
"""

from __future__ import annotations

import threading
from typing import Any

from app.config import settings

_thread_state = threading.local()


def _build_model_provider() -> Any:
    from agents import MultiProvider
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key or None)
    return MultiProvider(openai_client=client)


def thread_model_provider() -> Any:
    """Retorna o provedor de modelos exclusivo da thread atual."""
    provider = getattr(_thread_state, "model_provider", None)
    if provider is None:
        provider = _build_model_provider()
        _thread_state.model_provider = provider
    return provider


def run_agent_sync(agent: Any, prompt: Any) -> Any:
    """Equivalente a ``Runner.run_sync(agent, prompt)`` sem compartilhar
    conexões HTTP entre threads."""
    from agents import RunConfig, Runner

    return Runner.run_sync(
        agent,
        prompt,
        run_config=RunConfig(model_provider=thread_model_provider()),
    )
