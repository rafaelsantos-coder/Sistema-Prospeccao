from __future__ import annotations

from app.services.ai_autocorrector import _is_safe_retriable_error
from app.services.ai_engineer import EngineerReplacement, _replacement_safe
from app.services.ai_learning import _stable_variant


def test_phase_c_only_retries_known_safe_operational_errors():
    assert _is_safe_retriable_error("Guardião bloqueou a decisão antes do envio")
    assert _is_safe_retriable_error("timeout ao chamar CRM")
    assert _is_safe_retriable_error("HTTP 503 temporariamente indisponível")
    assert not _is_safe_retriable_error("CPF reprovado no crédito")
    assert not _is_safe_retriable_error("Contrato recusado pelo cliente")


def test_phase_d_assignment_is_stable_for_same_conversation():
    first = _stable_variant(11, 999)
    second = _stable_variant(11, 999)
    assert first == second
    assert first in {"CONTROL", "VARIANT"}


def test_phase_e_allows_only_whitelisted_non_sensitive_patch():
    safe = EngineerReplacement(
        path="app/services/ai_sdr.py",
        old_text="if repetitive:\n    return True",
        new_text="if repetitive:\n    return False",
        reason="Teste de zona segura.",
    )
    assert _replacement_safe(safe) is True

    forbidden_path = EngineerReplacement(
        path="app/config.py",
        old_text="x = 1",
        new_text="x = 2",
    )
    assert _replacement_safe(forbidden_path) is False

    sensitive = EngineerReplacement(
        path="app/services/ai_sdr.py",
        old_text="secret_key = value",
        new_text="secret_key = other",
    )
    assert _replacement_safe(sensitive) is False


def test_phase_e_blocks_price_like_patch_even_in_allowed_file():
    change = EngineerReplacement(
        path="app/services/ai_sdr.py",
        old_text="mensagem antiga",
        new_text="novo valor R$ 99,90",
    )
    assert _replacement_safe(change) is False
