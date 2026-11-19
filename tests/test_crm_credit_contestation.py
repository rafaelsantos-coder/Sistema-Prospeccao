"""Contestação comercial: testes offline, sem CRM real/Serasa."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.services import crm_credit_contestation as contest
from app.services.crm_manual_registration import CRMManualError
from app.services.crm_sulnet import SulnetCRMClient


CTX = contest.ContestedOpportunity(
    opportunity_id=16976,
    analysis_id=3501,
    seller_username="fabiano.braga",
    seller_user_id=36,
    funnel_id=17,
)
REASONS = [
    {"key": "geral", "label": "Outro motivo", "active": True,
     "requiresJustification": True, "requiresDocConfirmation": False,
     "minAttachments": 0, "maxAttachments": 0, "attachmentDocType": ""},
    {"key": "provedor_atual", "label": "Cliente já possui provedor", "active": True,
     "requiresJustification": False, "requiresDocConfirmation": True,
     "minAttachments": 1, "maxAttachments": 1, "attachmentDocType": "contestacao_fatura"},
    {"key": "liberado_comprovante_provedor", "label": "Liberado comprovante provedor", "active": True,
     "requiresJustification": False, "requiresDocConfirmation": False,
     "minAttachments": 0, "maxAttachments": 3, "attachmentDocType": "contestacao_anexo"},
    {"key": "cliente_da_base_paga_em_dia", "label": "Cliente da base paga em dia", "active": True,
     "requiresJustification": True, "requiresDocConfirmation": False,
     "minAttachments": 0, "maxAttachments": 0, "attachmentDocType": ""},
]


def _identity(**extras):
    return {
        "opportunityId": 16976, "analysisId": 3501,
        "sellerUsername": "fabiano.braga", "assignedUserId": 36,
        "funnelId": 17, **extras,
    }


def _setup(monkeypatch):
    monkeypatch.setattr(
        contest, "settings",
        replace(contest.settings, sulnet_crm_contestation_enabled=True),
    )
    monkeypatch.setattr(contest, "_context", lambda db, conv: CTX)
    monkeypatch.setattr(contest, "_guard_contact_lock", lambda db, contact: None)
    conv = SimpleNamespace(id=8, contact_id=44, assigned_user_id=6)
    actor = SimpleNamespace(id=6, role="CONSULTOR")
    return conv, actor


def test_contest_reasons_dynamic_metadata_and_reject_incomplete():
    actual = contest._reason_list({"reasons": REASONS})
    assert [r["key"] for r in actual] == [
        "geral", "provedor_atual",
        "liberado_comprovante_provedor", "cliente_da_base_paga_em_dia",
    ]
    assert actual[1]["minAttachments"] == 1
    assert actual[2]["maxAttachments"] == 3
    with pytest.raises(CRMManualError):
        contest._reason_list({"reasons": [{"key": "geral", "label": "Outro motivo"}]})


def test_eligibility_only_from_crm_flag_and_matching_identity():
    assert contest._safe_status(CTX, _identity(
        status="Reprovado", result="Reprovado", canContest=False,
    ))["canContest"] is False
    assert contest._safe_status(CTX, _identity(
        status="Liberado com taxa", result="Aprovado com taxa", canContest=True,
    ))["canContest"] is True
    assert contest._safe_status(CTX, _identity(
        status="Contestação enviada", canContest=True,
    ))["canContest"] is False
    with pytest.raises(CRMManualError):
        contest._safe_status(CTX, _identity(
            assignedUserId=24, status="Reprovado", canContest=True,
        ))


def test_panel_reasons_and_readonly_for_admin(monkeypatch):
    conv, actor = _setup(monkeypatch)
    calls = []

    class FakeCRM:
        def seller_contestation_status(self, **kwargs):
            calls.append(("status", kwargs))
            return _identity(
                status="Reprovado", result="Reprovado",
                canContest=True, managementDecision=None,
            )

        def seller_contestation_reasons(self, **kwargs):
            calls.append(("reasons", kwargs))
            return _identity(reasons=REASONS, billMonthExpectedLabel="10/2026")

    monkeypatch.setattr(
        contest.SulnetCRMClient, "from_settings",
        classmethod(lambda cls: FakeCRM()),
    )
    monkeypatch.setattr(contest, "_state", lambda db, contact: None)
    panel = contest.get_contestation_panel(None, conversation=conv, actor=actor)
    assert panel["canContest"] is True
    assert panel["billMonthExpected"] == "10/2026"
    assert len(panel["reasons"]) == 4
    assert [c[0] for c in calls] == ["status", "reasons"]
    admin = SimpleNamespace(id=9, role="ADMIN")
    panel = contest.get_contestation_panel(None, conversation=conv, actor=admin)
    assert panel["readOnly"] is True
    assert panel["canContest"] is False


def test_send_fatura_uses_same_analysis_without_serasa_or_local_document(monkeypatch):
    conv, actor = _setup(monkeypatch)
    calls = []
    audits = []

    class FakeCRM:
        def upload_seller_contestation_file(self, **kwargs):
            calls.append(("upload", kwargs))
            return _identity(attachmentId=888)

        def submit_seller_contestation(self, **kwargs):
            calls.append(("contest", kwargs))
            return _identity(status="Contestação enviada", canContest=False)

    monkeypatch.setattr(
        contest.SulnetCRMClient, "from_settings",
        classmethod(lambda cls: FakeCRM()),
    )
    monkeypatch.setattr(contest, "_state", lambda db, contact: None)
    monkeypatch.setattr(
        contest, "get_contestation_panel",
        lambda db, conversation, actor: {
            "canContest": True, "reasons": contest._reason_list({"reasons": REASONS}),
        },
    )
    monkeypatch.setattr(
        contest, "_write_state",
        lambda db, **kwargs: audits.append(kwargs),
    )

    document_bytes = b"%PDF-1.5\n1 0 obj\n<<>>\nendobj\n"
    result = contest.send_contestation(
        None, conversation=conv, actor=actor, reason_type="provedor_atual",
        justification="", doc_confirmed=True,
        documents=[("cpf_do_cliente.pdf", "application/pdf", document_bytes)],
    )
    assert result["status"] == "Contestação enviada"
    assert len(calls) == 2
    uploaded = calls[0][1]
    assert uploaded["analysis_id"] == 3501
    assert uploaded["opportunity_id"] == 16976
    assert uploaded["document_type"] == "contestacao_fatura"
    assert uploaded["filename"].startswith("contestacao-")
    assert "cpf_do_cliente" not in uploaded["filename"]
    assert calls[1][1]["attachment_ids"] == [888]
    assert calls[1][1]["seller_username"] == "fabiano.braga"
    assert audits[0]["action"] == contest.CONTEST_SENT
    assert "justification" not in audits[0]["values"]
    assert "cpf" not in str(audits[0]["values"]).lower()


@pytest.mark.parametrize(
    ("reason_type", "justification", "confirmed", "documents"),
    [
        ("geral", "", False, []),
        ("cliente_da_base_paga_em_dia", "", False, []),
        ("provedor_atual", "", True, []),
        ("provedor_atual", "Tenho fatura", False,
         [("doc.pdf", "application/pdf", b"%PDF-1.5\n")]),
        ("liberado_comprovante_provedor", "", False,
         [("e.exe", "application/octet-stream", b"MZ")]),
    ],
)
def test_invalid_contestation_fails_without_external_submission(
    monkeypatch, reason_type, justification, confirmed, documents,
):
    conv, actor = _setup(monkeypatch)
    monkeypatch.setattr(contest, "_state", lambda db, contact: None)
    monkeypatch.setattr(
        contest, "get_contestation_panel",
        lambda db, conversation, actor: {
            "canContest": True, "reasons": contest._reason_list({"reasons": REASONS}),
        },
    )

    class NeverCRM:
        def submit_seller_contestation(self, **kwargs):
            raise AssertionError("Falha local deve impedir contestação no CRM")

        def upload_seller_contestation_file(self, **kwargs):
            raise AssertionError("Falha local deve impedir upload no CRM")

    monkeypatch.setattr(
        contest.SulnetCRMClient, "from_settings",
        classmethod(lambda cls: NeverCRM()),
    )
    with pytest.raises(CRMManualError):
        contest.send_contestation(
            None, conversation=conv, actor=actor,
            reason_type=reason_type, justification=justification,
            doc_confirmed=confirmed, documents=documents,
        )


def test_duplicate_submission_blocked_until_manager_decision(monkeypatch):
    conv, actor = _setup(monkeypatch)
    monkeypatch.setattr(
        contest, "_state", lambda db, contact: {"state": contest.CONTEST_SENT}
    )
    monkeypatch.setattr(
        contest, "get_contestation_panel",
        lambda db, conversation, actor: {
            "canContest": True,
            "managementDecision": "",
            "reasons": contest._reason_list({"reasons": REASONS}),
        },
    )
    with pytest.raises(CRMManualError, match="já enviada"):
        contest.send_contestation(
            None, conversation=conv, actor=actor, reason_type="geral",
            justification="Cliente quitou os débitos", doc_confirmed=False, documents=[],
        )


def test_s2s_webhooks_use_commercial_key_and_no_bearer(monkeypatch):
    seen = []

    def fake_request(self, method, path, **kwargs):
        seen.append((method, path, kwargs))
        return _identity(status="Contestação enviada")

    monkeypatch.setattr(SulnetCRMClient, "_request", fake_request)
    client = SulnetCRMClient(
        base_url="https://crm.example",
        bearer_token="NEVER_USE",
        integration_key="CURRENT_COMMERCIAL_KEY",
    )
    client.seller_contestation_status(
        opportunity_id=16976, analysis_id=3501, seller_username="fabiano.braga"
    )
    client.seller_contestation_reasons(
        opportunity_id=16976, analysis_id=3501, seller_username="fabiano.braga"
    )
    client.upload_seller_contestation_file(
        opportunity_id=16976, analysis_id=3501, seller_username="fabiano.braga",
        document_type="contestacao_fatura", filename="doc.pdf",
        mime_type="application/pdf", data=b"%PDF-1.5",
    )
    client.submit_seller_contestation(
        opportunity_id=16976, analysis_id=3501, seller_username="fabiano.braga",
        reason_type="provedor_atual", justification="", doc_confirmed=True,
        attachment_ids=[888],
    )
    assert [x[1] for x in seen] == [
        "/webhook/leads/credit-status",
        "/webhook/leads/contest-reasons",
        "/webhook/leads/contest-attachment",
        "/webhook/leads/contest",
    ]
    for method, _path, kwargs in seen:
        assert method == "POST"
        assert kwargs["auth_mode"] == "integration"
        assert kwargs.get("extra_headers") is None
        assert "NEVER_USE" not in str(kwargs)
    assert seen[2][2]["files"]["file"][0] == "doc.pdf"
    assert seen[3][2]["json_body"]["analysisId"] == 3501
