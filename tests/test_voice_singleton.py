"""Provas do singleton SIP com PostgreSQL 18 descartável, sem ligações."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap

import pytest
from sqlalchemy import create_engine

from app.workers import voice_singleton
from app.workers.voice_singleton import VoiceLeadershipLost


PG_TEST_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/sulnet_ci"


def test_sip_leadership_fails_closed_without_postgres():
    # O ambiente normal dos testes é SQLite. Não tentar simular um lock inseguro.
    with pytest.raises(VoiceLeadershipLost, match="PostgreSQL"):
        with voice_singleton.voice_engine_leadership():
            pass


@pytest.mark.skipif(
    os.getenv("GITHUB_ACTIONS") != "true",
    reason="Exige o PostgreSQL 18 temporário do GitHub Actions",
)
def test_postgresql_sip_lock_allows_exactly_one_leader(monkeypatch):
    pg_engine = create_engine(PG_TEST_URL)
    monkeypatch.setattr(voice_singleton, "engine", pg_engine)
    try:
        with voice_singleton.voice_engine_leadership() as leader:
            assert leader is not None
            leader.assert_held(force=True)
            with voice_singleton.voice_engine_leadership() as contender:
                assert contender is None
            leader.assert_held(force=True)
        with voice_singleton.voice_engine_leadership() as successor:
            assert successor is not None
            successor.assert_held(force=True)
    finally:
        pg_engine.dispose()


@pytest.mark.skipif(
    os.getenv("GITHUB_ACTIONS") != "true",
    reason="Exige o PostgreSQL 18 temporário do GitHub Actions",
)
def test_sip_lock_disconnect_fails_closed(monkeypatch):
    pg_engine = create_engine(PG_TEST_URL)
    monkeypatch.setattr(voice_singleton, "engine", pg_engine)
    try:
        with voice_singleton.voice_engine_leadership() as leader:
            assert leader is not None
            leader._connection.invalidate()  # simula perda súbita da sessão
            assert leader.healthy() is False
            with pytest.raises(VoiceLeadershipLost):
                leader.assert_held(force=True)
        with voice_singleton.voice_engine_leadership() as successor:
            assert successor is not None
    finally:
        pg_engine.dispose()


@pytest.mark.skipif(
    os.getenv("GITHUB_ACTIONS") != "true" or os.name != "posix",
    reason="SIGKILL e PostgreSQL 18 descartável são exigidos",
)
def test_sigkill_releases_exclusive_sip_leadership(monkeypatch):
    """Duas instâncias reais disputam o lock; não há chamadas externas."""
    pg_engine = create_engine(PG_TEST_URL)
    monkeypatch.setattr(voice_singleton, "engine", pg_engine)
    script = textwrap.dedent(
        """
        import time
        from app.workers.voice_singleton import voice_engine_leadership

        with voice_engine_leadership() as leader:
            print("LEADER" if leader else "STANDBY", flush=True)
            if leader:
                time.sleep(60)
        """
    )
    env = dict(os.environ, DATABASE_URL=PG_TEST_URL)
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env,
    )
    try:
        assert child.stdout is not None
        ready = child.stdout.readline().strip()
        assert ready == "LEADER", f"Lock não adquirido; saída={ready!r}"
        with voice_singleton.voice_engine_leadership() as contender:
            assert contender is None
        child.send_signal(signal.SIGKILL)
        child.communicate(timeout=10)
        assert child.returncode == -signal.SIGKILL
        with voice_singleton.voice_engine_leadership() as successor:
            assert successor is not None
            successor.assert_held(force=True)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)
        pg_engine.dispose()
