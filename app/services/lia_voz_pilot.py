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
PROBE_PENDING = "LIA_PROBE_PENDENTE"
PROBE_RUNNING = "LIA_PROBE_EXECUTANDO"
ACCOUNT_PENDING = "LIA_ACC_PENDENTE"
ACCOUNT_RUNNING = "LIA_ACC_EXECUTANDO"
ACCOUNT_PROBE_PENDING = "LIA_ACC_PROBE_PENDENTE"
ACCOUNT_PROBE_RUNNING = "LIA_ACC_PROBE_EXECUTANDO"
CLAIM_STATES = {
    PENDING: RUNNING,
    PROBE_PENDING: PROBE_RUNNING,
    ACCOUNT_PENDING: ACCOUNT_RUNNING,
    ACCOUNT_PROBE_PENDING: ACCOUNT_PROBE_RUNNING,
}
PENDING_STATES = set(CLAIM_STATES)
RUNNING_STATES = set(CLAIM_STATES.values())
PROBE_STATES = {PROBE_RUNNING, ACCOUNT_PROBE_RUNNING}
ACCOUNT_STATES = {ACCOUNT_RUNNING, ACCOUNT_PROBE_RUNNING}
ACTIVE = PENDING_STATES | RUNNING_STATES


def requested_state(mode: str, route: str = "native") -> str:
    # Persist the explicitly selected route as part of the queued state, not
    # mutable message text or environment. Never fallback/retry automatically.
    states = {
        ("ia", "native"): PENDING,
        ("sip", "native"): PROBE_PENDING,
        ("ia", "account"): ACCOUNT_PENDING,
        ("sip", "account"): ACCOUNT_PROBE_PENDING,
    }
    if (mode, route) not in states:
        raise PilotDenied("Modo ou rota de homologacao invalida.")
    return states[mode, route]

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

    def authorize(self, phone: str, username: str, *, route: str = "native") -> str:
        if not self.enabled or not self.exclusive:
            raise PilotDenied("Homologacao Lia Voz bloqueada: exclusividade externa do ramal SIP nao confirmada.")
        if route not in {"account", "native"} or not (username or "").strip():
            raise PilotDenied("Conta SIP ou rota de homologacao invalida.")
        # The administrator selects an active registered account. Its record
        # is checked at submission and execution; Native prefixes are not portable.
        if route == "native" and username != NATIVE_ACCOUNT_USERNAME:
            raise PilotDenied("A rota Native por techprefix exige a conta SIP 1804.")
        try:
            expected = canonical_phone(validate_test_phone(self.allowed_phone))
            actual = canonical_phone(validate_test_phone(phone))
            if route == "native":
                native_dial_digits(actual, self.prefix)
        except ValueError:
            raise PilotDenied("Numero de teste ou rota de discagem invalida.") from None
        if actual != expected:
            raise PilotDenied("Destino nao autorizado para homologacao.")
        return actual

    @property
    def configured(self) -> bool:
        try:
            self.authorize(self.allowed_phone, NATIVE_ACCOUNT_USERNAME, route="account")
            return True
        except PilotDenied:
            return False

    @property
    def native_configured(self) -> bool:
        try:
            self.authorize(self.allowed_phone, NATIVE_ACCOUNT_USERNAME, route="native")
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
