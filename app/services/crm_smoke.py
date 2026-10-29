from __future__ import annotations

import hashlib
import logging
import os

from app.services.crm_sulnet import CRMIntegrationError, SulnetCRMClient


LOG = logging.getLogger("sulnet_crm_smoke")


def _enabled() -> bool:
    return os.getenv("CRM_SMOKE_TEST_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def run_crm_smoke_test_once() -> bool:
    if not _enabled():
        return False

    name = os.getenv("CRM_SMOKE_TEST_NAME", "").strip()
    phone = "".join(ch for ch in os.getenv("CRM_SMOKE_TEST_PHONE", "") if ch.isdigit())
    cpf = "".join(ch for ch in os.getenv("CRM_SMOKE_TEST_CPF", "") if ch.isdigit())
    if not name or len(phone) < 10 or len(cpf) not in {11, 14}:
        LOG.error("crm_smoke_invalid_input")
        return False

    external_id = "crm-smoke-" + hashlib.sha256(
        f"{name}|{phone}".encode("utf-8")
    ).hexdigest()[:20]

    client = SulnetCRMClient.from_settings()
    try:
        created = client.create_opportunity(
            {
                "externalId": external_id,
                "clientName": name,
                "clientPhone": phone,
                "runConsult": False,
            }
        )
        opportunity_id = int(created.get("opportunityId") or 0)
        protocol = str(created.get("protocol") or "")
        if not opportunity_id:
            LOG.error("crm_smoke_create_missing_id keys=%s", sorted(created.keys()))
            return False

        LOG.info(
            "crm_smoke_lead_created opportunity_id=%s protocol=%s created=%s",
            opportunity_id,
            protocol or "-",
            created.get("created"),
        )

        credit = client.consult_credit(opportunity_id, cpf)
        LOG.info(
            "crm_smoke_credit_result opportunity_id=%s analysis_id=%s status=%s approved=%s rejected=%s manual_review=%s",
            opportunity_id,
            credit.get("analysisId"),
            str(credit.get("status") or "-")[:120],
            bool(credit.get("approved")),
            bool(credit.get("rejected")),
            bool(credit.get("manualReview")),
        )
        return True
    except CRMIntegrationError as exc:
        LOG.error("crm_smoke_failed error=%s", str(exc)[:600])
        return False
