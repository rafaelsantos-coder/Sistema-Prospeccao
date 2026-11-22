"""Um aviso por caso, com dados reais, sem inventar motivo de handoff."""
from sqlalchemy.orm import Session

from app.models import Contact, Conversation, ConversationQueueLink, Message
from app.services import manual_human_review_notices as notices
from test_ai_all_queues_coverage import _database, _setup


def test_two_distinct_notifications_contain_contact_status_and_reason(monkeypatch):
    engine = _database()
    try:
        with Session(engine) as db:
            _, general_id, _, _ = _setup(db)
            for conversation_id, name, phone, body in (
                (4802, "Contato Exemplo A", "5555999004802",
                 "Agradecemos pelo contato. Responderemos em breve."),
                (4887, "Contato Exemplo B", "5555999004887",
                 "Mensagem automática: estamos fora do nosso horário de atendimento."),
            ):
                contact = Contact(name=name, phone=phone)
                db.add(contact)
                db.flush()
                conversation = Conversation(
                    id=conversation_id, contact_id=contact.id, source="WHATSAPP",
                    team="DIGITAL", status="EM_FILA",
                )
                db.add(conversation)
                db.flush()
                db.add(ConversationQueueLink(
                    conversation_id=conversation.id, queue_id=general_id,
                    origin="INBOUND",
                ))
                db.add(Message(
                    conversation_id=conversation.id, contact_id=contact.id,
                    direction="INBOUND", message_type="TEXT", status="RECEIVED",
                    body=body,
                ))
            db.commit()

            captured = []

            def fake_send_ops_alert(_db, *, key, text, recipient_phone):
                captured.append((key, text, recipient_phone))
                return True

            monkeypatch.setattr(notices, "send_ops_alert", fake_send_ops_alert)
            outcome = notices.notify_two_human_review_cases(db)
            assert outcome == {"sent": 2, "not_sent_or_already_sent": 0}
            assert len(captured) == 2
            assert all(phone == "55999002508" for _, _, phone in captured)
            assert len(set(key for key, _, _ in captured)) == 2
            assert "Aviso 1/2" in captured[0][1]
            assert "Aviso 2/2" in captured[1][1]
            assert "Contato Exemplo A" in captured[0][1]
            assert "Contato Exemplo B" in captured[1][1]
            assert "*Status:* EM_FILA" in captured[0][1]
            assert "classificada pelo filtro como *resposta automática*" in captured[0][1]
            assert "Responderemos em breve" in captured[0][1]
            assert "Mensagem automática" in captured[1][1]
            assert "Revisar o conteúdo" in captured[1][1]
    finally:
        engine.dispose()


def test_notice_masks_cpf_and_secrets():
    assert "[CPF ocultado]" in notices._safe_excerpt("Meu CPF: 123.456.789-09")
    assert "abc123" not in notices._safe_excerpt("token=abc123 e senha: abc123")
