from __future__ import annotations

import re
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import (
    AuditLog,
    Contact,
    Conversation,
    ConversationZApiInstance,
    ServiceQueue,
    ServiceQueueZApiInstance,
    User,
    ZApiInstance,
)
from app.services.secret_store import decrypt_secret
from app.services.zapi import ZApiService
from app.services.zapi_instances import (
    bind_conversation_instance,
    bind_queue_instance,
    create_zapi_instance,
)


def _csrf(page: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert match
    return match.group(1)


def _login(client: TestClient) -> str:
    token = _csrf(client.get("/login").text)
    response = client.post(
        "/login",
        data={
            "email": "admin@sulnet.local",
            "password": "Sulnet@2026!",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return _csrf(client.get("/settings").text)


def _new_instance(db, *, source_type: str = "DB") -> ZApiInstance:
    suffix = uuid4().hex
    admin = db.scalar(select(User).where(User.email == "admin@sulnet.local"))
    instance = create_zapi_instance(
        db,
        name=f"WhatsApp Exclusao {suffix[:8]}",
        phone_label="+55 (55) 93619-3138",
        base_url="https://api.z-api.io",
        instance_id=f"delete-instance-{suffix}",
        instance_token=f"token-{suffix}",
        client_token=f"client-{suffix}",
        created_by_id=admin.id if admin else None,
        source_type=source_type,
    )
    db.commit()
    db.refresh(instance)
    return instance


def _fake_zapi(monkeypatch) -> None:
    monkeypatch.setattr(
        ZApiService,
        "status",
        lambda self: {
            "configured": True,
            "connected": True,
            "smartphone_connected": True,
            "error": None,
        },
    )
    monkeypatch.setattr(
        ZApiService,
        "device",
        lambda self: {"phone": "", "name": "", "is_business": False},
    )
    monkeypatch.setattr(
        ZApiService,
        "configure_webhooks",
        lambda self, url: {"ok": True},
    )
    monkeypatch.setattr(
        ZApiService,
        "queue_pending_summary",
        lambda self, *, max_pages=5: {
            "count": 0,
            "has_more": False,
            "capped": False,
            "samples": [],
        },
    )


def test_excluir_instancia_sem_vinculos_e_sem_historico(monkeypatch):
    _fake_zapi(monkeypatch)
    with TestClient(app) as client:
        token = _login(client)
        with SessionLocal() as db:
            instance = _new_instance(db)
            instance_db_id = instance.id
            name = instance.name
        page = client.get(f"/settings/zapi/instances/{instance_db_id}/delete")
        assert page.status_code == 200
        assert name in page.text
        assert "Confirmar exclusão" in page.text

        response = client.post(
            f"/settings/zapi/instances/{instance_db_id}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings/whatsapp"
        with SessionLocal() as db:
            assert db.get(ZApiInstance, instance_db_id) is None
            audit = db.scalar(
                select(AuditLog)
                .where(
                    AuditLog.action == "ZAPI_INSTANCE_DELETED",
                    AuditLog.entity_id == str(instance_db_id),
                )
                .order_by(AuditLog.id.desc())
            )
            assert audit is not None


def test_excluir_instancia_vinculada_a_fila_e_bloqueado(monkeypatch):
    _fake_zapi(monkeypatch)
    with TestClient(app) as client:
        token = _login(client)
        with SessionLocal() as db:
            instance = _new_instance(db)
            queue = ServiceQueue(
                name=f"Fila Protegida {uuid4().hex[:8]}",
                code=f"DEL-PROTECT-{uuid4().hex[:18]}",
                team="DIGITAL",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(queue)
            db.flush()
            bind_queue_instance(db, queue.id, instance.id)
            db.commit()
            instance_db_id = instance.id
            queue_id = queue.id
            queue_name = queue.name

        page = client.get(f"/settings/zapi/instances/{instance_db_id}/delete")
        assert page.status_code == 200
        assert queue_name in page.text
        assert f"/settings/queues/{queue_id}" in page.text
        assert 'name="replacement_instance_id"' in page.text
        assert 'type="submit">Confirmar exclusão e transferir filas' in page.text

        # Sem indicar um novo número, a exclusão continua bloqueada.
        response = client.post(
            f"/settings/zapi/instances/{instance_db_id}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        with SessionLocal() as db:
            assert db.get(ZApiInstance, instance_db_id).active is True
            association = db.scalar(
                select(ServiceQueueZApiInstance).where(
                    ServiceQueueZApiInstance.queue_id == queue_id
                )
            )
            assert association.zapi_instance_id == instance_db_id


def test_excluir_instancia_com_historico_posterior_preserva_conversas(monkeypatch):
    _fake_zapi(monkeypatch)
    with TestClient(app) as client:
        token = _login(client)
        with SessionLocal() as db:
            instance = _new_instance(db)
            contact = Contact(
                phone=f"5555{uuid4().int % 10**9:09d}",
                name="Contato Historico",
                business_name="Contato Historico",
            )
            db.add(contact)
            db.flush()
            conversation = Conversation(
                contact_id=contact.id,
                team="DIGITAL",
                status="EM_FILA",
            )
            db.add(conversation)
            db.flush()
            bind_conversation_instance(db, conversation.id, instance.id)
            db.commit()
            instance_db_id = instance.id
            conversation_id = conversation.id
            instance_key = instance.instance_id

        response = client.post(
            f"/settings/zapi/instances/{instance_db_id}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        with SessionLocal() as db:
            archived = db.get(ZApiInstance, instance_db_id)
            assert archived is not None
            assert archived.active is False
            history = db.scalar(
                select(ConversationZApiInstance).where(
                    ConversationZApiInstance.conversation_id == conversation_id
                )
            )
            assert history.zapi_instance_id == instance_db_id

        listing = client.get("/settings/whatsapp")
        assert listing.status_code == 200
        assert instance_key[-8:] not in listing.text


def test_instancia_gerenciada_pelo_railway_nao_pode_ser_excluida(monkeypatch):
    _fake_zapi(monkeypatch)
    with TestClient(app) as client:
        token = _login(client)
        with SessionLocal() as db:
            instance = _new_instance(db, source_type="ENV")
            instance_db_id = instance.id
        page = client.get(f"/settings/zapi/instances/{instance_db_id}/delete")
        assert "Exclusão indisponível" in page.text
        response = client.post(
            f"/settings/zapi/instances/{instance_db_id}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        with SessionLocal() as db:
            assert db.get(ZApiInstance, instance_db_id).active is True


def test_exclusao_exige_token_csrf(monkeypatch):
    _fake_zapi(monkeypatch)
    with TestClient(app) as client:
        _login(client)
        with SessionLocal() as db:
            instance = _new_instance(db)
            instance_db_id = instance.id
        response = client.post(
            f"/settings/zapi/instances/{instance_db_id}/delete",
            data={"csrf_token": "invalido"},
            follow_redirects=False,
        )
        assert response.status_code in {400, 403}
        with SessionLocal() as db:
            assert db.get(ZApiInstance, instance_db_id) is not None


def test_recadastro_reativa_instancia_arquivada_e_preserva_id(monkeypatch):
    _fake_zapi(monkeypatch)
    with TestClient(app) as client:
        token = _login(client)
        with SessionLocal() as db:
            instance = _new_instance(db)
            instance_db_id = instance.id
            instance_key = instance.instance_id
            instance.active = False
            db.commit()

        response = client.post(
            "/settings/zapi/instances",
            data={
                "csrf_token": token,
                "name": "WhatsApp Reativada",
                "phone_label": "",
                "base_url": "https://api.z-api.io",
                "instance_id": instance_key,
                "instance_token": "novo-token-test",
                "client_token": "novo-client-token-test",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        with SessionLocal() as db:
            instance = db.get(ZApiInstance, instance_db_id)
            assert instance.active is True
            assert instance.name == "WhatsApp Reativada"
            assert decrypt_secret(instance.instance_token_encrypted) == "novo-token-test"
            assert decrypt_secret(instance.client_token_encrypted) == "novo-client-token-test"
            assert db.scalar(
                select(AuditLog).where(
                    AuditLog.action == "ZAPI_INSTANCE_REACTIVATED",
                    AuditLog.entity_id == str(instance_db_id),
                )
            ) is not None


def test_exclusao_com_substituicao_migra_filas_e_preserva_historico(monkeypatch):
    _fake_zapi(monkeypatch)
    with TestClient(app) as client:
        token = _login(client)
        with SessionLocal() as db:
            old_instance = _new_instance(db)
            replacement = _new_instance(db)
            old_instance_id = old_instance.id
            replacement_id = replacement.id
            queue_ids = []
            for suffix in ("Individual", "Natine"):
                queue = ServiceQueue(
                    name=f"Fila {suffix} {uuid4().hex[:8]}",
                    code=f"DEL-MOVE-{uuid4().hex[:18]}",
                    team="DIGITAL",
                    routing_strategy="MANUAL",
                    active=True,
                )
                db.add(queue)
                db.flush()
                bind_queue_instance(db, queue.id, old_instance_id)
                queue_ids.append(queue.id)
            contact = Contact(
                phone=f"5555{uuid4().int % 10**9:09d}",
                name="Cliente historico da fila",
                business_name="Cliente historico da fila",
            )
            db.add(contact)
            db.flush()
            conversation = Conversation(contact_id=contact.id, team="DIGITAL", status="EM_FILA")
            db.add(conversation)
            db.flush()
            bind_conversation_instance(db, conversation.id, old_instance_id)
            historical_conversation_id = conversation.id
            db.commit()

        page = client.get(f"/settings/zapi/instances/{old_instance_id}/delete")
        assert page.status_code == 200
        assert 'name="replacement_instance_id"' in page.text
        assert f'value="{replacement_id}"' in page.text
        assert "Confirmar exclusão e transferir filas" in page.text

        response = client.post(
            f"/settings/zapi/instances/{old_instance_id}/delete",
            data={"csrf_token": token, "replacement_instance_id": str(replacement_id)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/settings/whatsapp"
        with SessionLocal() as db:
            old_record = db.get(ZApiInstance, old_instance_id)
            assert old_record is not None
            assert old_record.active is False
            for queue_id in queue_ids:
                link = db.scalar(
                    select(ServiceQueueZApiInstance).where(
                        ServiceQueueZApiInstance.queue_id == queue_id
                    )
                )
                assert link.zapi_instance_id == replacement_id
            history = db.scalar(
                select(ConversationZApiInstance).where(
                    ConversationZApiInstance.conversation_id == historical_conversation_id
                )
            )
            assert history.zapi_instance_id == old_instance_id
            audit = db.scalar(
                select(AuditLog)
                .where(
                    AuditLog.action == "ZAPI_INSTANCE_DELETED",
                    AuditLog.entity_id == str(old_instance_id),
                )
                .order_by(AuditLog.id.desc())
            )
            assert audit is not None


def test_substituto_desconectado_nao_altera_filiais(monkeypatch):
    _fake_zapi(monkeypatch)
    with TestClient(app) as client:
        token = _login(client)
        with SessionLocal() as db:
            old_instance = _new_instance(db)
            replacement = _new_instance(db)
            old_instance_id = old_instance.id
            replacement_id = replacement.id
            queue = ServiceQueue(
                name=f"Fila Retida {uuid4().hex[:8]}",
                code=f"DEL-STAY-{uuid4().hex[:18]}",
                team="DIGITAL",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(queue)
            db.flush()
            bind_queue_instance(db, queue.id, old_instance_id)
            queue_id = queue.id
            db.commit()

        previous_status = ZApiService.status

        def disconnected_replacement(self):
            status = previous_status(self)
            if self.instance_id.startswith("delete-instance-") and self.instance_id:
                # O primeiro e o segundo ID diferem, porem o que importa
                # aqui e recusar qualquer retorno desconectado.
                return {**status, "connected": False}
            return status

        monkeypatch.setattr(ZApiService, "status", disconnected_replacement)
        response = client.post(
            f"/settings/zapi/instances/{old_instance_id}/delete",
            data={"csrf_token": token, "replacement_instance_id": str(replacement_id)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        with SessionLocal() as db:
            assert db.get(ZApiInstance, old_instance_id).active is True
            link = db.scalar(
                select(ServiceQueueZApiInstance).where(
                    ServiceQueueZApiInstance.queue_id == queue_id
                )
            )
            assert link.zapi_instance_id == old_instance_id


def test_ignorar_substituicao_da_propria_instancia(monkeypatch):
    _fake_zapi(monkeypatch)
    with TestClient(app) as client:
        token = _login(client)
        with SessionLocal() as db:
            instance = _new_instance(db)
            queue = ServiceQueue(
                name=f"Fila Propria {uuid4().hex[:8]}",
                code=f"DEL-SELF-{uuid4().hex[:18]}",
                team="DIGITAL",
                routing_strategy="MANUAL",
                active=True,
            )
            db.add(queue)
            db.flush()
            bind_queue_instance(db, queue.id, instance.id)
            old_id = instance.id
            db.commit()

        response = client.post(
            f"/settings/zapi/instances/{old_id}/delete",
            data={"csrf_token": token, "replacement_instance_id": str(old_id)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        with SessionLocal() as db:
            assert db.get(ZApiInstance, old_id).active is True
