from types import SimpleNamespace

from app.models import Campaign, Lead
from app.services.campaign_queue import (
    campaign_media_attachments,
    campaign_message_template,
    campaign_quick_replies,
    campaign_sequence_messages,
    list_quick_chat_templates,
    personalize_message,
    personalize_quick_chat_template,
    resolve_quick_chat_template,
)


def test_campaign_personalization():
    lead = Lead(
        business_name="Clínica Teste",
        contact_name="Mariana",
        phone="5555999999999",
        segment="Clínica odontológica",
        city="Santa Rosa",
    )
    template = "Olá, {{nome}}! A {{empresa}} de {{cidade}} atua como {{segmento}}."
    rendered = personalize_message(template, lead)
    assert rendered == "Olá, Mariana! A Clínica Teste de Santa Rosa atua como Clínica odontológica."


def test_campaign_reads_first_message_from_sequence():
    campaign = Campaign(
        name="Teste",
        channel="WHATSAPP",
        sequence_json='[{"day":1,"message":"Mensagem inicial"},{"day":3,"label":"Follow-up"}]',
    )
    assert campaign_message_template(campaign) == "Mensagem inicial"


def test_campaign_sequence_messages_keeps_follow_ups():
    campaign = Campaign(
        name="Régua",
        channel="WHATSAPP",
        sequence_json=(
            '[{"day":1,"message":"Abordagem"},'
            '{"day":3,"message":"Follow-up dia 3"},'
            '{"day":5,"label":"sem texto"}]'
        ),
    )
    steps = campaign_sequence_messages(campaign)
    assert [item["message"] for item in steps] == ["Abordagem", "Follow-up dia 3"]
    assert steps[1]["day"] == 3


def test_quick_chat_templates_include_telefonia_fixa_alternate_keys():
    """Campanhas como Telefonia Fixa - Empresas entram mesmo com chave alternativa."""
    from fastapi.testclient import TestClient

    from app.database import SessionLocal
    from app.main import app

    with TestClient(app):
        with SessionLocal() as db:
            db.add(
                Campaign(
                    name="Telefonia Fixa - Empresas",
                    channel="EMAIL",
                    status="ATIVA",
                    sequence_json='[{"day":1,"text":"Olá! Oferta de telefonia fixa para empresas."}]',
                )
            )
            db.add(
                Campaign(
                    name="Legado steps",
                    channel="whatsapp",
                    status="ATIVA",
                    sequence_json='{"steps":[{"day":1,"body":"Mensagem em steps aninhados"}]}',
                )
            )
            db.commit()
            user = SimpleNamespace(role="ADMIN", branch_id=None)
            templates = list_quick_chat_templates(db, user)
            labels = [item["label"] for item in templates]
            assert any(label.startswith("Telefonia Fixa - Empresas") for label in labels)
            assert any("Legado steps" in label for label in labels)
            telefonia = next(
                item for item in templates
                if item["label"].startswith("Telefonia Fixa - Empresas")
                and item["group"] == "Campanhas"
            )
            assert "telefonia fixa" in telefonia["message"].lower()
            assert "[EMAIL]" in telefonia["label"]


def test_quick_chat_templates_list_campaigns_and_standardized_steps():
    from fastapi.testclient import TestClient

    from app.database import SessionLocal
    from app.main import app

    with TestClient(app):
        with SessionLocal() as db:
            db.add(
                Campaign(
                    name="650Mb 3 meses - Maria",
                    channel="WHATSAPP",
                    status="ATIVA",
                    sequence_json=(
                        '[{"day":1,"message":"Oi {{nome}}, oferta 650Mb"},'
                        '{"day":3,"message":"Ainda posso te ajudar com a oferta?"}]'
                    ),
                )
            )
            db.add(
                Campaign(
                    name="Rascunho ignorado",
                    channel="WHATSAPP",
                    status="RASCUNHO",
                    sequence_json='[{"day":1,"message":"Não deve aparecer"}]',
                )
            )
            db.commit()
            user = SimpleNamespace(role="ADMIN", branch_id=None)
            templates = list_quick_chat_templates(db, user)
            labels = [item["label"] for item in templates]
            groups = {item["label"]: item["group"] for item in templates}
            assert "650Mb 3 meses - Maria" in labels
            assert any(label.startswith("650Mb 3 meses - Maria · dia 3") for label in labels)
            assert any(label.startswith("650Mb 3 meses - Maria · abordagem") for label in labels)
            assert groups["650Mb 3 meses - Maria"] == "Campanhas"
            assert "Rascunho ignorado [RASCUNHO]" in labels
            assert resolve_quick_chat_template(db, user, "") == ""
            assert resolve_quick_chat_template(db, user, "NENHUM") == ""
            campaign_id = next(
                item["id"] for item in templates if item["label"] == "650Mb 3 meses - Maria"
            )
            assert "oferta 650Mb" in resolve_quick_chat_template(db, user, campaign_id)
            assert personalize_quick_chat_template(
                "Oi {{nome}}", contact_name="Ana"
            ) == "Oi Ana"



def test_campaign_reads_media_from_initial_step():
    campaign = Campaign(
        name="Teste mídia",
        channel="WHATSAPP",
        sequence_json='[{"day":1,"message":"Olá","media":[{"type":"IMAGE","storage_uri":"s3://bucket/campaigns/1/foto.jpg","file_name":"foto.jpg","mime_type":"image/jpeg"}]}]',
    )
    media = campaign_media_attachments(campaign)
    assert len(media) == 1
    assert media[0]["type"] == "IMAGE"
    assert media[0]["file_name"] == "foto.jpg"



def test_campaign_reads_quick_replies():
    campaign = Campaign(
        name="Teste botões",
        channel="WHATSAPP",
        sequence_json='[{"day":1,"message":"Olá","quick_replies":[{"id":"yes","label":"Tenho interesse"},{"id":"more","label":"Quero saber mais"},{"id":"no","label":"Agora não"}]}]',
    )
    replies = campaign_quick_replies(campaign)
    assert [item["label"] for item in replies] == [
        "Tenho interesse",
        "Quero saber mais",
        "Agora não",
    ]


def test_campaign_template_page_renders():
    import re
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        login = client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
        client.post(
            "/login",
            data={"email": "admin@sulnet.local", "password": "Sulnet@2026!", "csrf_token": token},
        )
        page = client.get("/campaigns")
        assert page.status_code == 200
        assert "Mensagem inicial" in page.text
        assert "{{nome}}" in page.text
        assert 'name="media"' in page.text
        assert 'accept="image/*,video/*,audio/*"' in page.text
        assert 'name="button_1"' in page.text
        assert "Tenho interesse" in page.text
