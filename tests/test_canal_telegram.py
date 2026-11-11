"""Canal Telegram: convite por link, vínculo lead/chat, parar, envio pela mesma interface."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Conversation, Lead, TelegramLink
from app.routers import telegram as telegram_router
from app.services import telegram as tg
from app.services.conversation_engine import ensure_contact, ensure_open_conversation
from app.services.zapi_instances import service_for_conversation

SECRET = "segredo-telegram-de-teste-1234567890"
FAKE = SimpleNamespace(
    telegram_bot_token="123:ABC",
    telegram_bot_username="sulnet_teste_bot",
    telegram_webhook_secret=SECRET,
)


@pytest.fixture()
def bot(monkeypatch):
    sent: list[tuple[str, dict]] = []
    counter = {"n": 0}

    def fake_call(method, payload):
        sent.append((method, payload))
        counter["n"] += 1
        return {"message_id": 1000 + counter["n"]}

    jobs: list[int] = []
    monkeypatch.setattr(tg, "settings", FAKE)
    monkeypatch.setattr(telegram_router, "settings", FAKE)
    monkeypatch.setattr(tg, "_call", fake_call)
    monkeypatch.setattr(
        "app.services.ai_sdr.enqueue_ai_job",
        lambda db, *, conversation, message, takeover=False: jobs.append(message.id),
    )
    return SimpleNamespace(sent=sent, jobs=jobs)


def _lead(db) -> Lead:
    suffix = uuid4().int % 10**8
    lead = Lead(business_name="Cliente Telegram", phone=f"5555{suffix:08d}")
    db.add(lead)
    db.flush()
    return lead


def _update(chat_id, text, message_id=1, username="cliente"):
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "username": username},
            "text": text,
        },
    }


def _post(client, update, secret=SECRET):
    return client.post(f"/webhooks/telegram/{secret}", json=update)


def test_webhook_com_segredo_errado_retorna_404(bot):
    with TestClient(app) as client:
        assert _post(client, _update(1, "oi"), secret="errado").status_code == 404


def test_link_de_convite_vincula_chat_e_abre_conversa_telegram(bot):
    chat_id = 700000 + uuid4().int % 10**6
    with SessionLocal() as db:
        lead = _lead(db)
        url = tg.invite_url_for_lead(db, lead)
        db.commit()
        phone, lead_id = lead.phone, lead.id
    assert url.startswith("https://t.me/sulnet_teste_bot?start=")
    token = url.rsplit("=", 1)[1]

    with TestClient(app) as client:
        resp = _post(client, _update(chat_id, f"/start {token}"))
    assert resp.status_code == 200 and resp.json()["conversation_id"]

    with SessionLocal() as db:
        link = db.scalar(select(TelegramLink).where(TelegramLink.token == token))
        assert link.status == "ATIVO" and link.chat_id == str(chat_id) and link.lead_id == lead_id
        conversation = db.get(Conversation, resp.json()["conversation_id"])
        assert conversation.source == "TELEGRAM"
        # O telefone do contato continua sendo o real (CRM e IA dependem dele).
        assert conversation.contact.phone == phone
    assert bot.jobs, "a IA precisa ser acionada para responder o cliente"


def test_token_invalido_e_chat_de_outro_cadastro_nao_vinculam(bot):
    chat_id = 800000 + uuid4().int % 10**6
    with TestClient(app) as client:
        resp = _post(client, _update(chat_id, "/start tokeninexistente123"))
    assert resp.json()["ignored"] == "invalid_token"
    assert bot.sent[-1][1]["text"] == tg.INVALID_LINK
    assert not bot.jobs


def test_mensagem_de_quem_nao_tem_link_recebe_orientacao_sem_acionar_ia(bot):
    with TestClient(app) as client:
        resp = _post(client, _update(900000 + uuid4().int % 10**6, "quero contratar"))
    assert resp.json()["ignored"] == "unlinked_chat"
    assert not bot.jobs


def test_parar_encerra_o_canal_e_nao_gera_novo_convite(bot):
    chat_id = 600000 + uuid4().int % 10**6
    with SessionLocal() as db:
        lead = _lead(db)
        token = tg.invite_url_for_lead(db, lead).rsplit("=", 1)[1]
        db.commit()
        lead_id = lead.id
    with TestClient(app) as client:
        _post(client, _update(chat_id, f"/start {token}", message_id=1))
        resp = _post(client, _update(chat_id, "/parar", message_id=2))
    assert resp.json()["stopped"] is True
    with SessionLocal() as db:
        link = db.scalar(select(TelegramLink).where(TelegramLink.token == token))
        assert link.status == "PARADO"
        lead = db.get(Lead, lead_id)
        assert tg.invite_url_for_lead(db, lead) == ""
        assert tg.active_link_for_lead(db, lead_id) is None


def test_envio_da_conversa_telegram_usa_o_chat_do_cliente(bot):
    chat_id = 500000 + uuid4().int % 10**6
    with SessionLocal() as db:
        lead = _lead(db)
        token = tg.invite_url_for_lead(db, lead).rsplit("=", 1)[1]
        db.commit()
    with TestClient(app) as client:
        conversation_id = _post(client, _update(chat_id, f"/start {token}")).json()["conversation_id"]
    bot.sent.clear()
    with SessionLocal() as db:
        sender = service_for_conversation(db, conversation_id)
        result = sender.send_text(phone="ignorado", message="Olá, tudo bem?")
    method, payload = bot.sent[-1]
    assert method == "sendMessage" and payload["chat_id"] == str(chat_id)
    assert result["messageId"].startswith(f"tg:{chat_id}:")


def test_botoes_viram_teclado_inline(bot):
    tg.TelegramService("42").send_button_list(
        message="Quer seguir?", buttons=[{"label": "Sim"}, {"label": "Não"}]
    )
    markup = bot.sent[-1][1]["reply_markup"]["inline_keyboard"]
    assert [row[0]["text"] for row in markup] == ["Sim", "Não"]


def test_canais_nao_se_misturam_na_mesma_conversa(bot):
    with SessionLocal() as db:
        lead = _lead(db)
        contact = ensure_contact(db, lead.phone, lead=lead)
        whatsapp = ensure_open_conversation(db, contact, lead=lead, source="WHATSAPP")
        telegram = ensure_open_conversation(db, contact, lead=lead, source="TELEGRAM")
        again = ensure_open_conversation(db, contact, lead=lead, source="WHATSAPP")
        assert whatsapp.id != telegram.id
        assert again.id == whatsapp.id
        assert ensure_open_conversation(db, contact, lead=lead, source="TELEGRAM").id == telegram.id


def test_bloqueio_do_bot_pelo_cliente_marca_erro_bloqueado(monkeypatch):
    class Resp:
        status_code = 403

        def json(self):
            return {"ok": False, "description": "Forbidden: bot was blocked by the user"}

    class Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return Resp()

    monkeypatch.setattr(tg, "settings", FAKE)
    monkeypatch.setattr(tg.httpx, "Client", Client)
    with pytest.raises(tg.TelegramError) as info:
        tg.TelegramService("42").send_text(message="oi")
    assert info.value.blocked is True
