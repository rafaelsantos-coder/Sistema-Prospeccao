"""Regression tests for one-number, admin-only SIP diagnostics without live outbound calls."""
from __future__ import annotations

import re
import socket
import threading
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import User, VoiceSipAccount, VoiceTestAgent, VoiceTestCall
from app.security import hash_password
from app.services.secret_store import encrypt_secret
from app.services.voice_sip_probe import SipProbe, _digest
from app.services.voice_test import now_utc, validate_test_phone


def _csrf(body: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', body)
    assert m
    return m.group(1)


def _login(client: TestClient, email: str = "admin@sulnet.local",
           password: str = "Sulnet@2026!") -> str:
    csrf = _csrf(client.get("/login").text)
    r = client.post("/login", data={"csrf_token": csrf, "email": email,
                                    "password": password}, follow_redirects=False)
    assert r.status_code == 303
    return _csrf(client.get("/dashboard").text)


def _account() -> int:
    with SessionLocal() as db:
        acc = VoiceSipAccount(
            name="Teste SIP " + uuid4().hex[:8],
            domain="sip.example.com", username="88200",
            password_encrypted=encrypt_secret("dummytest"), transport="UDP", active=True,
        )
        db.add(acc)
        db.commit()
        return acc.id


def _worker_heartbeat() -> None:
    with SessionLocal() as db:
        agent = db.get(VoiceTestAgent, 1)
        if not agent:
            agent = VoiceTestAgent(id=1, worker_id="pytest-agent")
            db.add(agent)
        agent.heartbeat_at = now_utc()
        db.commit()


def test_phone_validation_for_authorized_numbers_only():
    assert validate_test_phone("+55 (55) 99999-1000") == "5555999991000"
    assert validate_test_phone("(55) 99999-1000") == "55999991000"
    for bad in ("190", "192", "0800123456", "abc", "", "+351 91 000 1000", "558001234567"):
        with pytest.raises(ValueError):
            validate_test_phone(bad)


def test_md5_digest_supports_proxy_and_registrar_challenges():
    hdr = _digest('Digest realm="sip.test", nonce="ABCD", qop="auth", algorithm=MD5',
                  "REGISTER", "sip:sip.test", "200", "not-a-real-password")
    assert 'username="200"' in hdr
    assert "response=" in hdr and "qop=auth" in hdr
    with pytest.raises(Exception):
        _digest('Bearer realm="sip.test"', "REGISTER", "sip:sip.test", "200", "abc")


def test_admin_can_request_one_call_when_agent_is_online():
    acc_id = _account()
    with TestClient(app) as client:
        token = _login(client)
        page = client.get(f"/voice/accounts/{acc_id}/test")
        assert page.status_code == 200
        assert "Agente SIP offline" in page.text

        offline = client.post(f"/voice/accounts/{acc_id}/test", data={
            "csrf_token": token, "phone": "55 55 99999-1001", "authorized": "yes",
        }, follow_redirects=False)
        assert offline.status_code == 303
        with SessionLocal() as db:
            assert db.scalar(select(VoiceTestCall).where(VoiceTestCall.account_id == acc_id)) is None

        _worker_heartbeat()
        page = client.get(f"/voice/accounts/{acc_id}/test")
        assert "Agente SIP disponível" in page.text

        declined = client.post(f"/voice/accounts/{acc_id}/test", data={
            "csrf_token": token, "phone": "55 55 99999-1001",
        }, follow_redirects=False)
        assert declined.status_code == 303
        with SessionLocal() as db:
            assert db.scalar(select(VoiceTestCall).where(VoiceTestCall.account_id == acc_id)) is None

        created = client.post(f"/voice/accounts/{acc_id}/test", data={
            "csrf_token": token, "phone": "55 55 99999-1001", "authorized": "yes",
        }, follow_redirects=False)
        assert created.status_code == 303
        assert created.headers["location"] == f"/voice/accounts/{acc_id}/test"
        with SessionLocal() as db:
            call = db.scalar(select(VoiceTestCall).where(VoiceTestCall.account_id == acc_id))
            assert call is not None
            assert call.status == "PENDENTE"
            assert call.target_phone == "5555999991001"
            assert call.sip_register_code is None
        overview = client.get(f"/voice/accounts/{acc_id}/test")
        assert "5555999991001" in overview.text
        assert "Aguardando agente" in overview.text


def test_non_admin_cannot_trigger_sip_test():
    acc_id = _account()
    email = f"voice-test-user-{uuid4().hex[:8]}@sulnet.local"
    with TestClient(app) as client:
        with SessionLocal() as db:
            db.add(User(name="Sem permissao SIP", email=email,
                        password_hash=hash_password("UserTest@2026!"), active=True,
                        must_change_password=False, role="GESTOR", team="DIGITAL"))
            db.commit()
        token = _login(client, email=email, password="UserTest@2026!")
        response = client.get(f"/voice/accounts/{acc_id}/test", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"
        blocked = client.post(f"/voice/accounts/{acc_id}/test", data={
            "csrf_token": token, "phone": "5555999991001", "authorized": "yes",
        }, follow_redirects=False)
        assert blocked.status_code == 303
        assert blocked.headers["location"] == "/dashboard"


def test_real_udp_probe_with_local_fake_pbx(monkeypatch):
    # This test *does* originate SIP signaling, but only to localhost inside CI.
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(4)
    server_port = server.getsockname()[1]
    observed: list[bytes] = []

    def answer() -> None:
        try:
            for _ in range(6):
                packet, peer = server.recvfrom(65535)
                observed.append(packet)
                content = packet.decode("utf-8", "replace")
                first = content.split("\r\n", 1)[0]
                method = first.split()[0]
                callid = re.search(r"^Call-ID:\s*(.+)$", content, re.M).group(1).strip()
                cseq = re.search(r"^CSeq:\s*(.+)$", content, re.M).group(1).strip()
                from_line = re.search(r"^From:\s*(.+)$", content, re.M).group(1).strip()
                to_line = re.search(r"^To:\s*(.+)$", content, re.M).group(1).strip()
                has_auth = "Authorization:" in content
                if method == "REGISTER":
                    if not has_auth:
                        code, description, extra = 401, "Unauthorized", (
                            'WWW-Authenticate: Digest realm="sip.example.com", nonce="1234abcd", qop="auth"\r\n'
                        )
                    else:
                        code, description, extra = 200, "OK", ""
                elif method == "INVITE":
                    code, description, extra = 200, "OK", ""
                    to_line += ";tag=test-pbx"
                else:
                    code, description, extra = 200, "OK", ""
                response = (
                    f"SIP/2.0 {code} {description}\r\n"
                    f"From: {from_line}\r\n"
                    f"To: {to_line}\r\n"
                    f"Call-ID: {callid}\r\n"
                    f"CSeq: {cseq}\r\n"
                    f"{extra}Content-Length: 0\r\n\r\n"
                ).encode()
                server.sendto(response, peer)
                if method == "BYE":
                    break
        except socket.timeout:
            pass
        finally:
            server.close()

    original_getaddrinfo = socket.getaddrinfo
    def local_address(host, port, family=0, type=0, proto=0, flags=0):
        if host == "sip.example.com" and port == 5060:
            return original_getaddrinfo("127.0.0.1", server_port, family, type, proto, flags)
        return original_getaddrinfo(host, port, family, type, proto, flags)

    monkeypatch.setattr(socket, "getaddrinfo", local_address)
    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    state = []
    result = SipProbe(
        host="sip.example.com", username="200", password="not-a-real-password",
        phone="5555999991001", notify=lambda *args: state.append(args),
    ).run()
    thread.join(timeout=2)
    assert result.register_code == 200
    assert result.invite_code == 200
    assert result.status == "ATENDIDA"
    assert any(b"Authorization: Digest" in p for p in observed)
    assert any(p.startswith(b"INVITE") for p in observed)
    assert any(p.startswith(b"BYE") for p in observed)
