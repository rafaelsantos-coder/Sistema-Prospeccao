"""Explicit one-number Lia Voz pilot policy; not a campaign or CRM operation."""
from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy import select

from app.models import VoiceCampaign, VoiceRecipient, VoiceTestCall, VoiceTransferTest
from app.services.global_blacklist import canonical_phone
from app.services.voice_native_routing import NATIVE_ACCOUNT_USERNAME, native_dial_digits
from app.services.voice_test import validate_test_phone

PENDING = "LIA_PENDENTE"
RUNNING = "LIA_EXECUTANDO"
ACTIVE = {PENDING, RUNNING}
OTHER_TESTS = {
    "PENDENTE", "REGISTRANDO", "REGISTRADO", "PENDENTE_PJSIP",
    "REGISTRANDO_PJSIP", "CHAMANDO", "TOCANDO", "ATENDIDA",
}
CALLS_IN_FLIGHT = {
    "INICIANDO", "CHAMANDO", "TOCANDO", "ATENDIDA",
    "TRANSFERINDO", "REPRODUZINDO_AUDIO", "CONFIRMADO_DTMF",
}


class PilotDenied(ValueError):
    pass


@dataclass(frozen=True)
class PilotConfig:
    enabled: bool
    exclusive: bool
    allowed_phone: str
    prefix: str
    shared: bool = False

    @classmethod
    def from_env(cls):
        return cls(
            enabled=os.getenv("LIA_VOZ_TEST_ENABLED", "").lower() == "true",
            exclusive=os.getenv("LIA_VOZ_TEST_EXCLUSIVE_SIP", "").lower() == "true",
            allowed_phone=os.getenv("LIA_VOZ_TEST_PHONE", ""),
            prefix=os.getenv("LIA_VOZ_TEST_NATIVE_PREFIX", ""),
            shared=os.getenv("LIA_VOZ_TEST_SHARED_ENGINE", "").lower() == "true",
        )

    @property
    def request_seconds(self) -> int:
        return 600 if self.shared else 90

    def authorize(self, phone: str, username: str) -> str:
        if not self.enabled or not self.exclusive:
            raise PilotDenied("Homologacao Lia Voz bloqueada: exclusividade externa do ramal SIP nao confirmada.")
        if username != NATIVE_ACCOUNT_USERNAME:
            raise PilotDenied("O piloto exige a conta Native 1804 autorizada.")
        try:
            expected = canonical_phone(validate_test_phone(self.allowed_phone))
            actual = canonical_phone(validate_test_phone(phone))
            native_dial_digits(actual, self.prefix)
        except ValueError:
            raise PilotDenied("Numero de teste ou rota Native invalida.") from None
        if actual != expected:
            raise PilotDenied("Destino nao autorizado para homologacao.")
        return actual

    @property
    def configured(self) -> bool:
        try:
            self.authorize(self.allowed_phone, NATIVE_ACCOUNT_USERNAME)
            return True
        except PilotDenied:
            return False


def ensure_idle(db, *, own_test_id: int | None = None, shared: bool = False,
                admission: bool = False) -> None:
    """Fail closed if any other voice work could use the isolated account.

    This checks persisted operations; it does NOT prove exclusive registration
    across Railway projects or external devices. Explicit staging isolation
    remains a prerequisite before enabling the pilot.
    """
    # Submission can wait behind existing calls; only duplicate Lia tests block it.
    # Execution happens inside the global SIP slot, and rejects stale active rows.
    other = OTHER_TESTS
    if shared:
        other = set() if admission else OTHER_TESTS - {"PENDENTE", "PENDENTE_PJSIP"}
    tests = select(VoiceTestCall.id).where(
        VoiceTestCall.finished_at.is_(None),
        VoiceTestCall.status.in_(ACTIVE | other),
    )
    if own_test_id is not None:
        tests = tests.where(VoiceTestCall.id != own_test_id)
    if db.scalar(tests.limit(1)) is not None:
        raise PilotDenied("Existe outro teste de telefonia pendente ou em andamento.")
    if shared and admission:
        return
    if not shared and db.scalar(select(VoiceCampaign.id).where(
        VoiceCampaign.status == "EM_EXECUCAO"
    ).limit(1)) is not None:
        raise PilotDenied("Pause as campanhas de voz antes da homologacao isolada.")
    if db.scalar(select(VoiceRecipient.id).where(
        VoiceRecipient.status.in_(CALLS_IN_FLIGHT)
    ).limit(1)) is not None:
        raise PilotDenied("Existe chamada de campanha em andamento.")
    transfer_states = {"REGISTRANDO", "CHAMANDO", "TOCANDO", "ATENDIDA", "TRANSFERINDO"}
    if not shared:
        transfer_states.add("PENDENTE")
    if db.scalar(select(VoiceTransferTest.id).where(
        VoiceTransferTest.finished_at.is_(None),
        VoiceTransferTest.result.in_(transfer_states),
    ).limit(1)) is not None:
        raise PilotDenied("Existe teste de transferencia em andamento.")
