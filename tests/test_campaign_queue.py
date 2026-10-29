from app.models import Campaign, Lead
from app.services.campaign_queue import (
    campaign_media_attachments,
    campaign_message_template,
    campaign_quick_replies,
    personalize_message,
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
