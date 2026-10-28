from app.models import Campaign, Lead
from app.services.campaign_queue import campaign_message_template, personalize_message


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
